# app/routes/subscribe.py

from fastapi import APIRouter, Depends, HTTPException
import asyncpg

from app.config import settings
from app.database import get_db
from app.models import SubscribeRequest, DeleteSubscriptionRequest
from app.services import stats
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
    with 409 "code_in_use". A device can add at most MAX_SUBSCRIPTIONS_PER_DAY new
    codes per (UTC) day, which stops someone registering a whole bowl of
    photographed cards (429).
    """
    await register_or_verify_device(conn, body.push_id_hash, body.device_credential)
    if body.lang is not None:
        await conn.execute(
            "UPDATE devices SET lang = $1 WHERE push_id_hash = $2",
            body.lang, body.push_id_hash,
        )

    async with conn.transaction():
        # Serialise scans of the same code, so two simultaneous scans
        # cannot both pass the check below.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            body.et_hash,
        )

        # A code closed by someone's "delete all my data" takes nobody new.
        # Phones already on it can still re-scan it. The answer is the same
        # as for a full code, so it does not tell an outsider which is which.
        burned = await conn.fetchval("SELECT 1 FROM burned_codes WHERE et_hash = $1", body.et_hash)
        if burned and not await conn.fetchval(
            "SELECT 1 FROM token_subscriptions WHERE et_hash = $1 AND push_id_hash = $2",
            body.et_hash,
            body.push_id_hash,
        ):
            raise HTTPException(status_code=409, detail="code_in_use")

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

        already_on_code = await conn.fetchval(
            "SELECT 1 FROM token_subscriptions WHERE et_hash = $1 AND push_id_hash = $2",
            body.et_hash,
            body.push_id_hash,
        )
        if not already_on_code:
            # created_date is a date, so the limit counts per UTC day. Nothing
            # extra is stored for it.
            added_today = await conn.fetchval(
                """
                SELECT COUNT(*) FROM token_subscriptions
                WHERE push_id_hash = $1 AND created_date = CURRENT_DATE
                """,
                body.push_id_hash,
            )
            if added_today >= settings.max_subscriptions_per_day:
                raise HTTPException(
                    status_code=429,
                    detail=f"At most {settings.max_subscriptions_per_day} new connections per day",
                )

        await conn.execute(
            """
            INSERT INTO token_subscriptions (et_hash, push_id_hash, push_token, platform, created_date)
            VALUES ($1, $2, $3, $4, CURRENT_DATE)
            ON CONFLICT (et_hash, push_id_hash)
            DO UPDATE SET
                push_token = EXCLUDED.push_token,
                platform   = EXCLUDED.platform,
                dead_since = NULL
            """,
            body.et_hash,
            body.push_id_hash,
            body.push_token,
            body.platform,
        )

        if not already_on_code:
            await stats.bump(conn, stats.CODES_JOINED)
            if others == 1:  # this device is the code's second one
                await stats.bump(conn, stats.PAIRINGS_COMPLETED)

    return {"status": "ok"}


@router.delete("/subscribe")
async def delete_subscription(
    body: DeleteSubscriptionRequest,
    conn: asyncpg.Connection = Depends(get_db),
):
    """GDPR Art. 17 — delete everything for this device."""
    await verify_device(conn, body.push_id_hash, body.device_credential)

    async with conn.transaction():
        # Close every code this device was on, so the freed slot cannot be
        # taken by someone who holds a photo of the card.
        await conn.execute(
            """
            INSERT INTO burned_codes (et_hash)
            SELECT et_hash FROM token_subscriptions WHERE push_id_hash = $1
            ON CONFLICT (et_hash) DO NOTHING
            """,
            body.push_id_hash,
        )
        await conn.execute(
            "DELETE FROM token_subscriptions WHERE push_id_hash = $1",
            body.push_id_hash,
        )
        await conn.execute(
            "DELETE FROM mailbox WHERE push_id_hash = $1",
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
        await stats.bump(conn, stats.DELETE_REQUESTS)

    return {"status": "ok"}
