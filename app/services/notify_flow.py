# app/services/notify_flow.py
#
# Notify logic: every notification is sent immediately.
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
from app.services.push import send_bundle
from app.services.rate_limit import claim_campaign, release_campaign

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
    # STI, so it never drops a delivery: one device gets ONE push that carries
    # all its ciphertexts (`enc` + `more`), and the app shows each STI once.
    recipients_of = []
    for item in todo:
        recipients_of.append(await conn.fetch(
            """
            SELECT push_id_hash, push_token, platform
            FROM token_subscriptions
            WHERE et_hash = $1
              AND push_id_hash != $2
            """,
            item.et_hash,
            sender_push_id_hash,
        ))

    # Deliveries grouped by recipient device, in request order.
    per_device: dict[str, dict] = {}
    for i, item in enumerate(todo):
        for r in recipients_of[i]:
            entry = per_device.setdefault(r["push_id_hash"], {"recipient": dict(r), "payloads": []})
            entry["payloads"].append(item.encrypted_payload)

    # Step 3: deliver. One broken contact must not stop the others.
    # A push that fails is queued as a "due now" pending notification: the
    # 15-minute dispatcher retries it and gives up after a day. The app never
    # has to retry, and the campaign is used up exactly once.
    pushed = 0
    handled = 0
    retrying = 0

    device_ok: dict[str, bool] = {}
    for device, entry in per_device.items():
        count = await send_bundle(entry["recipient"], entry["payloads"])
        device_ok[device] = count >= 0
        pushed += max(count, 0)

    for i, item in enumerate(todo):
        if not recipients_of[i]:
            continue  # nobody else on this code yet
        if not all(device_ok[r["push_id_hash"]] for r in recipients_of[i]):
            await conn.execute(
                """
                INSERT INTO pending_notifications
                    (et_hash, sender_push_id_hash, encrypted_payload, scheduled_at)
                VALUES ($1, $2, $3, NOW())
                """,
                item.et_hash,
                sender_push_id_hash,
                item.encrypted_payload,
            )
            retrying += 1
        handled += 1

    if pushed == 0 and retrying == 0:
        # Nothing went out (nobody on the codes yet):
        # do not use up a slot, and let the app retry with the same id.
        await release_campaign(conn, campaign_id)

    return {
        "status": "ok",
        "pushed": pushed,
        "retrying": retrying,  # pushes that failed and the server will retry itself
        "contacts": handled,
    }
