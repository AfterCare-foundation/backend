# app/routes/dev_inbox.py
#
# POST /dev/inbox — pull notifications that stub mode held in memory.
# Exists so a second simulator can decrypt a push without APNs or FCM.
# Returns 404 unless ENVIRONMENT=development and PUSH_STUB_MODE=true.

from fastapi import APIRouter, Depends, HTTPException
import asyncpg

from app.config import settings
from app.database import get_db
from app.models import DevInboxRequest
from app.services import dev_inbox
from app.services.devices import verify_device

router = APIRouter()


def _inbox_enabled() -> bool:
    return settings.environment == "development" and settings.push_stub_mode


@router.post("/dev/inbox")
async def pull_dev_inbox(body: DevInboxRequest, conn: asyncpg.Connection = Depends(get_db)):
    if not _inbox_enabled():
        raise HTTPException(status_code=404, detail="Not found")

    await verify_device(conn, body.push_id_hash, body.device_credential)
    return {"notifications": dev_inbox.take(body.push_id_hash)}
