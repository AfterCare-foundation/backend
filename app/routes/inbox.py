# app/routes/inbox.py
#
# The mailbox. Notifications wait here, encrypted, until the app collects them.
#   POST /inbox          list the messages waiting for this device
#   POST /inbox/confirm  delete the ones the app has safely stored
#
# POST (not GET) so the device credential travels in the body, never in a URL.
# Fetching does not delete anything: a message is only deleted when the app
# confirms it, so a crash between "fetch" and "store" loses nothing.

from fastapi import APIRouter, Depends
import asyncpg

from app.database import get_db
from app.models import InboxConfirmRequest, InboxRequest
from app.services.devices import verify_device

router = APIRouter()

MAX_MESSAGES_PER_FETCH = 200


@router.post("/inbox")
async def fetch_inbox(body: InboxRequest, conn: asyncpg.Connection = Depends(get_db)):
    await verify_device(conn, body.push_id_hash, body.device_credential)

    rows = await conn.fetch(
        """
        SELECT id, encrypted_payload FROM mailbox
        WHERE push_id_hash = $1
        ORDER BY seq
        LIMIT $2
        """,
        body.push_id_hash,
        MAX_MESSAGES_PER_FETCH,
    )
    return {"notifications": [{"id": str(r["id"]), "enc": r["encrypted_payload"]} for r in rows]}


@router.post("/inbox/confirm")
async def confirm_inbox(body: InboxConfirmRequest, conn: asyncpg.Connection = Depends(get_db)):
    await verify_device(conn, body.push_id_hash, body.device_credential)

    # Only this device's own messages can be deleted.
    result = await conn.execute(
        "DELETE FROM mailbox WHERE push_id_hash = $1 AND id = ANY($2::uuid[])",
        body.push_id_hash,
        body.ids,
    )
    return {"status": "ok", "deleted": int(result.split()[-1])}
