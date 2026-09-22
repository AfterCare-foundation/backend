# app/routes/subscribe.py

from fastapi import APIRouter, Depends
import asyncpg

from app.database import get_db
from app.models import SubscribeRequest, DeleteSubscriptionRequest
from app.services.devices import register_or_verify_device, verify_device

router = APIRouter()


@router.post("/subscribe")
async def subscribe(body: SubscribeRequest, conn: asyncpg.Connection = Depends(get_db)):
    """
    Register this device on a card token.
    Re-scanning the same card updates the push token but does not extend TTL.
    """
    await register_or_verify_device(conn, body.push_id_hash, body.device_credential)

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
