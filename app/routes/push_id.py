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

    old, new = body.old_push_id_hash, body.new_push_id_hash

    async with conn.transaction():
        if old == new:
            await conn.execute(
                "UPDATE token_subscriptions SET push_token = $1, platform = $2 WHERE push_id_hash = $3",
                body.new_push_token, body.new_platform, old,
            )
            return {"status": "ok"}

        # Subscriptions point at devices(push_id_hash), so lift them out first,
        # rename the device, then put them back under the new ID.
        # created_date is kept, so the retention clock does not restart.
        subs = await conn.fetch(
            "SELECT et_hash, created_date FROM token_subscriptions WHERE push_id_hash = $1",
            old,
        )
        await conn.execute("DELETE FROM token_subscriptions WHERE push_id_hash = $1", old)

        new_device_exists = await conn.fetchval(
            "SELECT 1 FROM devices WHERE push_id_hash = $1", new
        )
        if new_device_exists:
            # The new ID already registered itself: just drop the old device.
            await conn.execute("DELETE FROM devices WHERE push_id_hash = $1", old)
        else:
            await conn.execute(
                "UPDATE devices SET push_id_hash = $1 WHERE push_id_hash = $2", new, old
            )

        for sub in subs:
            await conn.execute(
                """
                INSERT INTO token_subscriptions
                    (et_hash, push_id_hash, push_token, platform, created_date)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (et_hash, push_id_hash) DO NOTHING
                """,
                sub["et_hash"], new, body.new_push_token, body.new_platform, sub["created_date"],
            )

        await conn.execute(
            "UPDATE pending_notifications SET sender_push_id_hash = $1 WHERE sender_push_id_hash = $2",
            new, old,
        )
        await conn.execute(
            "UPDATE notification_campaigns SET push_id_hash = $1 WHERE push_id_hash = $2",
            new, old,
        )

    return {"status": "ok"}
