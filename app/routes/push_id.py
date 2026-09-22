# app/routes/push_id.py

from fastapi import APIRouter, Depends
import asyncpg

from app.database import get_db
from app.models import UpdatePushIdRequest
from app.services.devices import verify_device

router = APIRouter()


@router.post("/update-push-id")
async def update_push_id(body: UpdatePushIdRequest, conn: asyncpg.Connection = Depends(get_db)):
    """
    Push tokens change after reinstall or permission reset.
    Credential must match the old device. Colliding rows (new hash already
    present for the same token) keep the existing new row and drop the old one.
    """
    await verify_device(conn, body.old_push_id_hash, body.device_credential)

    async with conn.transaction():
        # Subscriptions: drop old rows that would collide with the new hash
        await conn.execute(
            """
            DELETE FROM token_subscriptions AS old
            WHERE old.push_id_hash = $1
              AND EXISTS (
                  SELECT 1 FROM token_subscriptions AS existing
                  WHERE existing.et_hash = old.et_hash
                    AND existing.push_id_hash = $2
              )
            """,
            body.old_push_id_hash,
            body.new_push_id_hash,
        )
        await conn.execute(
            """
            UPDATE token_subscriptions
            SET push_id_hash = $1,
                push_token   = $2,
                platform     = $3
            WHERE push_id_hash = $4
            """,
            body.new_push_id_hash,
            body.new_push_token,
            body.new_platform,
            body.old_push_id_hash,
        )

        await conn.execute(
            """
            UPDATE pending_notifications
            SET sender_push_id_hash = $1
            WHERE sender_push_id_hash = $2
            """,
            body.new_push_id_hash,
            body.old_push_id_hash,
        )

        await conn.execute(
            """
            UPDATE notification_campaigns
            SET push_id_hash = $1
            WHERE push_id_hash = $2
            """,
            body.new_push_id_hash,
            body.old_push_id_hash,
        )

        await conn.execute(
            """
            DELETE FROM devices
            WHERE push_id_hash = $1
              AND $1 != $2
              AND EXISTS (SELECT 1 FROM devices WHERE push_id_hash = $2)
            """,
            body.old_push_id_hash,
            body.new_push_id_hash,
        )
        await conn.execute(
            """
            UPDATE devices
            SET push_id_hash = $1
            WHERE push_id_hash = $2
            """,
            body.new_push_id_hash,
            body.old_push_id_hash,
        )

    return {"status": "ok"}
