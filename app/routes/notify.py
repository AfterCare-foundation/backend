# app/routes/notify.py

from fastapi import APIRouter, Depends
import asyncpg

from app.database import get_db
from app.models import NotifyRequest
from app.services.notify_flow import run_deliveries

router = APIRouter()


@router.post("/notify")
async def notify(body: NotifyRequest, conn: asyncpg.Connection = Depends(get_db)):
    """
    Send notifications for one or more tokens, immediately.

    The sender must already be subscribed to each token.
    Rate limit applies per campaign_id, not per HTTP call.
    Each delivery's encrypted_payload is forwarded as-is; nothing plaintext is stored.
    """
    return await run_deliveries(
        conn,
        device_credential=body.device_credential,
        sender_push_id_hash=body.sender_push_id_hash,
        campaign_id=body.campaign_id,
        deliveries=body.deliveries,
    )
