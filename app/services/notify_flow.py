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
from app.services.devices import verify_device
from app.services.push import PUSH_DEAD_TOKEN, PUSH_OK, send_push
from app.services.rate_limit import claim_campaign, release_campaign
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

    # Step 1: validate EVERY item before sending anything.
    # A bad item rejects the whole request and nothing goes out.
    todo = []
    for item in deliveries:
        if not await sender_is_subscribed(conn, item.et_hash, sender_push_id_hash):
            raise HTTPException(status_code=403, detail="Not a subscriber of this connection")
        todo.append(item)

    # A campaign_id works once: this also stops a resent request from
    # pushing to the same contacts twice. Released below if nothing is sent.
    await claim_campaign(conn, sender_push_id_hash, campaign_id)

    # Step 2: look up who receives each contact.
    # A device can be on several of the sender's contacts (same person, two
    # codes), possibly with a different STI each time. The server cannot see the
    # STI, so it never drops a delivery: every ciphertext goes into the mailbox
    # and the device is woken ONCE.
    recipients_of = []
    for item in todo:
        recipients_of.append(await conn.fetch(
            """
            SELECT push_id_hash, push_token, platform, dead_since
            FROM token_subscriptions
            WHERE et_hash = $1
              AND push_id_hash != $2
            """,
            item.et_hash,
            sender_push_id_hash,
        ))

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

    # Step 4: wake each device once. One broken contact must not stop the
    # others. A wake-up that fails stays marked `wake_pending` and the
    # 15-minute dispatcher retries it. The app never has to retry.
    woken: dict[str, bool] = {}
    for device, recipient in devices.items():
        if recipient["dead_since"] is not None:
            woken[device] = False  # the provider already refused this token
            continue
        result = await send_push(recipient["push_token"], recipient["platform"])
        if result == PUSH_DEAD_TOKEN:
            await mark_dead(conn, device, recipient["push_token"])
        if result == PUSH_OK:
            await conn.execute("UPDATE mailbox SET wake_pending = FALSE WHERE push_id_hash = $1", device)
        woken[device] = result == PUSH_OK

    pushed = sum(woken.values())
    retrying = 0
    handled = 0
    for i, item in enumerate(todo):
        if not recipients_of[i]:
            continue  # nobody else on this code yet
        if not all(woken[r["push_id_hash"]] for r in recipients_of[i]):
            retrying += 1
        handled += 1

    if handled == 0:
        # Nobody on the codes yet: do not use up a slot,
        # and let the app retry with the same id.
        await release_campaign(conn, campaign_id)

    return {
        "status": "ok",
        "pushed": pushed,      # devices whose lock-screen push was accepted
        "retrying": retrying,  # contacts whose push failed; the message is safe in the mailbox
        "contacts": handled,
    }
