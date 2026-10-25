# app/services/notify_flow.py
#
# Notify logic: every notification is stored at once in the recipient's mailbox
# and the recipient's phone is woken with a push (no ciphertext in the push).
#
# Each delivery carries its own encrypted_payload, produced on the phone:
#   key = derived from THAT card's raw token (both halves share it)
#   payload = encrypt(STI type) with that key
# Different cards have different tokens, so ciphertext cannot be reused
# across deliveries. The server never has the raw token and never decrypts.

from fastapi import HTTPException
import asyncpg

from app.config import settings
from app.services import stats
from app.services.devices import verify_device
from app.services.push import PUSH_DEAD_TOKEN, PUSH_OK, send_push
from app.services.rate_limit import claim_campaign
from app.services.tokens import mark_dead

async def sender_is_subscribed(conn: asyncpg.Connection, et_hash: str, sender_push_id_hash: str) -> bool:
    row = await conn.fetchval(
        """
        SELECT 1 FROM token_subscriptions
        WHERE et_hash = $1 AND push_id_hash = $2
        """,
        et_hash,
        sender_push_id_hash,
    )
    return row is not None


async def run_deliveries(
    conn: asyncpg.Connection,
    *,
    device_credential: str,
    sender_push_id_hash: str,
    campaign_id,
    deliveries: list,
) -> dict:
    await verify_device(conn, sender_push_id_hash, device_credential)

    unique_requested = {item.et_hash for item in deliveries}
    if len(unique_requested) > settings.notify_max_contacts_per_campaign:
        raise HTTPException(
            status_code=400,
            detail=f"A campaign can notify at most {settings.notify_max_contacts_per_campaign} contacts",
        )

    # Step 1: keep the deliveries for codes the sender is on. A code the sender
    # is not (or no longer) on is skipped and reported, so one stale code never
    # blocks the others. Only the sender's own codes are mentioned.
    todo = []
    skipped = []
    for item in deliveries:
        if await sender_is_subscribed(conn, item.et_hash, sender_push_id_hash):
            todo.append(item)
        else:
            skipped.append({"et_hash": item.et_hash, "reason": "not_subscribed"})

    # Every attempt uses the campaign budget, even one that reaches nobody, so
    # repeated empty attempts hit the same limits (3 per day, 6 per 30 days).
    # A campaign_id also works once: this stops a resent request from pushing
    # to the same contacts twice.
    await claim_campaign(conn, sender_push_id_hash, campaign_id)

    # Step 2: look up who receives each contact.
    # A device can be on several of the sender's contacts (same person, two
    # codes), possibly with a different STI each time. The server cannot see the
    # STI, so it never drops a delivery: every ciphertext goes into the mailbox
    # and the device is woken ONCE.
    reached = []        # deliveries somebody else is on
    recipients_of = []  # their recipients, in the same order
    for item in todo:
        recipients = await conn.fetch(
            """
            SELECT s.push_id_hash, s.push_token, s.platform, s.dead_since, d.lang
            FROM token_subscriptions s
            JOIN devices d ON d.push_id_hash = s.push_id_hash
            WHERE s.et_hash = $1
              AND s.push_id_hash != $2
            """,
            item.et_hash,
            sender_push_id_hash,
        )
        if recipients:
            reached.append(item)
            recipients_of.append(recipients)
        else:
            skipped.append({"et_hash": item.et_hash, "reason": "no_recipient"})
            await stats.bump(conn, stats.DELIVERIES_NO_RECIPIENT)
    todo = reached

    if not todo:
        # Nothing was sent. The campaign stays used up (see above).
        return {"status": "ok", "pushed": 0, "retrying": 0, "contacts": 0, "skipped": skipped}

    # Step 3: store every message in the recipient's mailbox (all or nothing).
    # From here on nothing can be lost, whatever happens to the push.
    devices: dict[str, dict] = {}
    async with conn.transaction():
        for i, item in enumerate(todo):
            for r in recipients_of[i]:
                devices.setdefault(r["push_id_hash"], dict(r))
                await conn.execute(
                    "INSERT INTO mailbox (push_id_hash, encrypted_payload) VALUES ($1, $2)",
                    r["push_id_hash"],
                    item.encrypted_payload,
                )

    await stats.bump(conn, stats.MESSAGES_SENT, sum(len(r) for r in recipients_of))

    # Step 4: wake each device once. One broken contact must not stop the
    # others. A wake-up that fails stays marked `wake_pending` and the
    # 15-minute dispatcher retries it. The app never has to retry.
    woken: dict[str, bool] = {}
    for device, recipient in devices.items():
        if recipient["dead_since"] is not None:
            woken[device] = False  # the provider already refused this token
            continue
        result = await send_push(recipient["push_token"], recipient["platform"], lang=recipient["lang"])
        await stats.bump(
            conn,
            stats.PUSHES_OK if result == PUSH_OK
            else stats.PUSHES_DEAD if result == PUSH_DEAD_TOKEN
            else stats.PUSHES_FAILED,
        )
        if result == PUSH_DEAD_TOKEN:
            await mark_dead(conn, device, recipient["push_token"])
        if result == PUSH_OK:
            await conn.execute("UPDATE mailbox SET wake_pending = FALSE WHERE push_id_hash = $1", device)
        woken[device] = result == PUSH_OK

    pushed = sum(woken.values())
    retrying = sum(
        1 for recipients in recipients_of
        if not all(woken[r["push_id_hash"]] for r in recipients)
    )
    handled = len(todo)
    await stats.bump(conn, stats.NOTIFY_TAPS)

    return {
        "status": "ok",
        "pushed": pushed,      # devices whose lock-screen push was accepted
        "retrying": retrying,  # contacts whose push failed; the message is safe in the mailbox
        "contacts": handled,
        # Codes nothing was sent for: {"et_hash", "reason": "not_subscribed" | "no_recipient"}
        "skipped": skipped,
    }
