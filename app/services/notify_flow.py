# app/services/notify_flow.py
#
# Shared notify / schedule logic.
#
# Each delivery carries its own encrypted_payload, produced on the phone:
#   key = derived from THAT card's raw token (both halves share it)
#   payload = encrypt(STI type) with that key
# Different cards have different tokens, so ciphertext cannot be reused
# across deliveries. The server never has the raw token and never decrypts.

from datetime import datetime, timezone, timedelta

from fastapi import HTTPException
import asyncpg

from app.config import settings
from app.services.devices import verify_device
from app.services.push import send_bundle
from app.services.rate_limit import assert_campaign_allowed, record_campaign_success

MAX_SCHEDULE_DAYS = 60


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
    needs_record = await assert_campaign_allowed(conn, sender_push_id_hash, campaign_id)

    already_rows = await conn.fetch(
        "SELECT et_hash FROM campaign_contacts WHERE campaign_id = $1",
        campaign_id,
    )
    already = {row["et_hash"].strip() for row in already_rows}

    unique_requested = list(dict.fromkeys(item.et_hash for item in deliveries))
    new_contacts = [et for et in unique_requested if et not in already]
    if len(already) + len(new_contacts) > settings.notify_max_contacts_per_campaign:
        raise HTTPException(
            status_code=400,
            detail=f"A campaign can notify at most {settings.notify_max_contacts_per_campaign} contacts",
        )

    now = datetime.now(timezone.utc)

    # Step 1: validate EVERY item before sending anything.
    # A bad item rejects the whole request and nothing goes out.
    todo = []
    for item in deliveries:
        if item.et_hash in already:
            continue  # handled by an earlier call with this campaign_id

        if not await sender_is_subscribed(conn, item.et_hash, sender_push_id_hash):
            raise HTTPException(status_code=403, detail="Not a subscriber of this connection")

        if item.scheduled_at is not None:
            if item.scheduled_at.tzinfo is None:
                raise HTTPException(status_code=400, detail="scheduled_at must include a timezone (UTC)")
            if item.scheduled_at <= now:
                raise HTTPException(status_code=400, detail="scheduled_at must be in the future")
            if item.scheduled_at > now + timedelta(days=MAX_SCHEDULE_DAYS):
                raise HTTPException(
                    status_code=400,
                    detail=f"scheduled_at must be within {MAX_SCHEDULE_DAYS} days from now",
                )
        todo.append(item)

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

    # Immediate deliveries grouped by recipient device, in request order.
    per_device: dict[str, dict] = {}
    for i, item in enumerate(todo):
        if item.scheduled_at is not None:
            continue
        for r in recipients_of[i]:
            entry = per_device.setdefault(r["push_id_hash"], {"recipient": dict(r), "payloads": []})
            entry["payloads"].append(item.encrypted_payload)

    # Step 3: deliver. One broken contact must not stop the others.
    # Each contact is recorded as soon as it is handled, so a retry with the
    # same campaign_id skips it (no double sends).
    pushed = 0
    scheduled = 0
    handled = 0
    failed: list[str] = []
    recorded = not needs_record

    device_ok: dict[str, bool] = {}
    for device, entry in per_device.items():
        count = await send_bundle(entry["recipient"], entry["payloads"])
        device_ok[device] = count >= 0
        pushed += max(count, 0)

    for i, item in enumerate(todo):
        if item.scheduled_at is not None:
            await _record_campaign_once(conn, sender_push_id_hash, campaign_id, recorded)
            recorded = True
            await conn.execute(
                """
                INSERT INTO pending_notifications
                    (et_hash, sender_push_id_hash, encrypted_payload, scheduled_at)
                VALUES ($1, $2, $3, $4)
                """,
                item.et_hash,
                sender_push_id_hash,
                item.encrypted_payload,
                item.scheduled_at,
            )
            scheduled += 1
        else:
            if not recipients_of[i]:
                continue  # nobody else on this code yet
            if not all(device_ok[r["push_id_hash"]] for r in recipients_of[i]):
                failed.append(item.et_hash)  # not recorded, so a retry can try again
                continue
            # Only a successful push uses up the rate limit.
            await _record_campaign_once(conn, sender_push_id_hash, campaign_id, recorded)
            recorded = True

        await conn.execute(
            """
            INSERT INTO campaign_contacts (campaign_id, et_hash)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            """,
            campaign_id,
            item.et_hash,
        )
        handled += 1

    return {
        "status": "partial" if failed else "ok",
        "pushed": pushed,
        "scheduled": scheduled,
        "failed": len(failed),
        "failed_contacts": failed,
        "contacts": len(already_rows) + handled,
    }


async def _record_campaign_once(conn, push_id_hash, campaign_id, already_recorded: bool) -> None:
    if not already_recorded:
        await record_campaign_success(conn, push_id_hash, campaign_id)
