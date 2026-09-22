# app/routes/schedule.py
#
# Convenience wrapper around POST /notify for a single future delivery.

from fastapi import APIRouter, Depends
import asyncpg

from app.database import get_db
from app.models import Delivery, ScheduleRequest
from app.services.notify_flow import run_deliveries

router = APIRouter()


@router.post("/schedule")
async def schedule(body: ScheduleRequest, conn: asyncpg.Connection = Depends(get_db)):
    return await run_deliveries(
        conn,
        device_credential=body.device_credential,
        sender_push_id_hash=body.sender_push_id_hash,
        campaign_id=body.campaign_id,
        deliveries=[
            Delivery(
                et_hash=body.et_hash,
                encrypted_payload=body.encrypted_payload,
                scheduled_at=body.scheduled_at,
            )
        ],
    )
