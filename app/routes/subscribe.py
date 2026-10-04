# app/routes/subscribe.py

from fastapi import APIRouter, Depends, HTTPException
import asyncpg

from app.database import get_db
from app.models import SubscribeRequest, DeleteSubscriptionRequest
from app.services.devices import register_or_verify_device, verify_device

router = APIRouter()

# A code connects exactly two phones, once. This is a promise to users,
# so it is a constant, not a setting.
MAX_DEVICES_PER_CODE = 2


@router.post("/subscribe")
async def subscribe(body: SubscribeRequest, conn: asyncpg.Connection = Depends(get_db)):
    """
    Register this device on a card token.
    Re-scanning the same card updates the push token but does not extend TTL.

    One code = one connection between two phones. A third device is rejected
    with 409 "code_in_use".
    """
    await register_or_verify_device(conn, body.push_id_hash, body.device_credential)

    async with conn.transaction():
        # Serialise scans of the same code, so two simultaneous scans
        # cannot both pass the check below.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            body.et_hash,
        )

        # Other devices already on this code (the caller does not count,
        # so re-scanning your own code always works).
        others = await conn.fetchval(
            """
            SELECT COUNT(*) FROM token_subscriptions
            WHERE et_hash = $1 AND push_id_hash != $2
            """,
            body.et_hash,
            body.push_id_hash,
        )
        if others >= MAX_DEVICES_PER_CODE:
            raise HTTPException(status_code=409, detail="code_in_use")

        await conn.execute(
            """
            INSERT INTO token_subscriptions (et_hash, push_id_hash, push_token, platform, created_date)
            VALUES ($1, $2, $3, $4, CURRENT_DATE)
            ON CONFLICT (et_hash, push_id_hash)
            DO UPDATE SET
                push_token = EXCLUDED.push_token,
                platform   = EXCLUDED.platform
            """,
            body.et_hash,
            body.push_id_hash,
            body.push_token,
            body.platform,
        )

    return {"status": "ok"}


@router.delete("/subscribe")
async def delete_subscription(
    body: DeleteSubscriptionRequest,
    conn: asyncpg.Connection = Depends(get_db),
):
    """GDPR Art. 17 — delete everything for this device."""
    await verify_device(conn, body.push_id_hash, body.device_credential)

    await conn.execute(
        "DELETE FROM token_subscriptions WHERE push_id_hash = $1",
        body.push_id_hash,
    )
    await conn.execute(
        "DELETE FROM pending_notifications WHERE sender_push_id_hash = $1",
        body.push_id_hash,
    )
    await conn.execute(
        "DELETE FROM notification_campaigns WHERE push_id_hash = $1",
        body.push_id_hash,
    )
    await conn.execute(
        "DELETE FROM devices WHERE push_id_hash = $1",
        body.push_id_hash,
    )

    return {"status": "ok"}
