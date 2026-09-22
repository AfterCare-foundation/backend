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
from app.services.push import send_push_to_all
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
    pushed = 0
    scheduled = 0
    recorded = not needs_record
    succeeded: list[str] = []

    for item in deliveries:
        if item.et_hash in already:
            continue

        if not await sender_is_subscribed(conn, item.et_hash, sender_push_id_hash):
            raise HTTPException(status_code=403, detail="Not a subscriber of this connection")

        if item.scheduled_at is not None:
            scheduled_at = item.scheduled_at
            if scheduled_at.tzinfo is None:
                raise HTTPException(status_code=400, detail="scheduled_at must include a timezone (UTC)")
            if scheduled_at <= now:
                raise HTTPException(status_code=400, detail="scheduled_at must be in the future")
            if scheduled_at > now + timedelta(days=MAX_SCHEDULE_DAYS):
                raise HTTPException(
                    status_code=400,
                    detail=f"scheduled_at must be within {MAX_SCHEDULE_DAYS} days from now",
                )
            if not recorded:
                await record_campaign_success(conn, sender_push_id_hash, campaign_id)
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
                scheduled_at,
            )
            scheduled += 1
            succeeded.append(item.et_hash)
            already.add(item.et_hash)
            continue

        recipients = await conn.fetch(
            """
            SELECT push_token, platform
            FROM token_subscriptions
            WHERE et_hash = $1
              AND push_id_hash != $2
            """,
            item.et_hash,
            sender_push_id_hash,
        )
        if not recipients:
            continue

        if not recorded:
            await record_campaign_success(conn, sender_push_id_hash, campaign_id)
            recorded = True
        await send_push_to_all(
            recipients=[dict(r) for r in recipients],
            encrypted_payload=item.encrypted_payload,
        )
        pushed += len(recipients)
        succeeded.append(item.et_hash)
        already.add(item.et_hash)

    for et_hash in succeeded:
        await conn.execute(
            """
            INSERT INTO campaign_contacts (campaign_id, et_hash)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            """,
            campaign_id,
            et_hash,
        )

    return {
        "status": "ok",
        "pushed": pushed,
        "scheduled": scheduled,
        "contacts": len(already_rows) + len(succeeded),
    }
