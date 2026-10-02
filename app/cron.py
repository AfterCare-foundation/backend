# app/cron.py
#
# Background jobs. PostgreSQL advisory locks mean two app processes
# cannot dispatch the same pending row twice.

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import app.database as database
from app.services.push import send_push_to_all

logger = logging.getLogger(__name__)

DISPATCH_INTERVAL_SECONDS = 15 * 60
CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
SUBSCRIPTION_TTL_DAYS = 60
CAMPAIGN_TTL_DAYS = 30

LOCK_DISPATCH = 87001
LOCK_CLEANUP = 87002


async def dispatch_pending_notifications():
    async with database.pool.acquire() as conn:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_DISPATCH)
        if not locked:
            return
        try:
            rows = await conn.fetch(
                """
                SELECT id, et_hash, sender_push_id_hash, encrypted_payload
                FROM pending_notifications
                WHERE scheduled_at <= NOW()
                """
            )
            if not rows:
                return

            logger.info("Dispatching %s pending notification(s)", len(rows))

            for row in rows:
                recipients = await conn.fetch(
                    """
                    SELECT push_id_hash, push_token, platform
                    FROM token_subscriptions
                    WHERE et_hash = $1
                      AND push_id_hash != $2
                    """,
                    row["et_hash"],
                    row["sender_push_id_hash"],
                )
                if recipients:
                    await send_push_to_all(
                        recipients=[dict(r) for r in recipients],
                        encrypted_payload=row["encrypted_payload"],
                    )
                await conn.execute(
                    "DELETE FROM pending_notifications WHERE id = $1",
                    row["id"],
                )
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_DISPATCH)


async def cleanup_expired_subscriptions():
    async with database.pool.acquire() as conn:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_CLEANUP)
        if not locked:
            return
        try:
            cutoff = datetime.now(timezone.utc).date() - timedelta(days=SUBSCRIPTION_TTL_DAYS)
            result = await conn.execute(
                "DELETE FROM token_subscriptions WHERE created_date < $1",
                cutoff,
            )
            deleted = result.split()[-1]
            if int(deleted) > 0:
                logger.info("TTL cleanup: deleted %s expired subscription(s)", deleted)

            await conn.execute(
                """
                DELETE FROM pending_notifications
                WHERE et_hash NOT IN (
                    SELECT DISTINCT et_hash FROM token_subscriptions
                )
                """
            )

            campaign_cutoff = datetime.now(timezone.utc) - timedelta(days=CAMPAIGN_TTL_DAYS)
            await conn.execute(
                "DELETE FROM notification_campaigns WHERE created_at < $1",
                campaign_cutoff,
            )

            await conn.execute(
                """
                DELETE FROM devices
                WHERE push_id_hash NOT IN (
                    SELECT DISTINCT push_id_hash FROM token_subscriptions
                )
                AND created_date < $1
                """,
                cutoff,
            )
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_CLEANUP)


async def run_dispatcher():
    while True:
        try:
            await dispatch_pending_notifications()
        except Exception:
            logger.exception("Error in dispatch_pending_notifications")
        await asyncio.sleep(DISPATCH_INTERVAL_SECONDS)


async def run_cleanup():
    while True:
        try:
            await cleanup_expired_subscriptions()
        except Exception:
            logger.exception("Error in cleanup_expired_subscriptions")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
