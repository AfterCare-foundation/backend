# app/cron.py
#
# Background jobs. PostgreSQL advisory locks mean two app processes
# cannot dispatch the same pending row twice.

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import app.database as database
from app.config import settings
from app.services.push import PUSH_DEAD_TOKEN, PUSH_OK, send_push
from app.services.tokens import mark_dead

logger = logging.getLogger(__name__)

DISPATCH_INTERVAL_SECONDS = 15 * 60
CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60

LOCK_DISPATCH = 87001
LOCK_CLEANUP = 87002


async def dispatch_wakeups():
    """
    Retry lock-screen pushes that failed. The messages themselves are already
    safe in the mailbox; this only buzzes the phone. Tries for about a day
    (today and yesterday), and never calls a token the provider called dead.
    """
    async with database.pool.acquire() as conn:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_DISPATCH)
        if not locked:
            return
        try:
            devices = await conn.fetch(
                """
                SELECT DISTINCT ON (m.push_id_hash)
                       m.push_id_hash, s.push_token, s.platform
                FROM mailbox m
                JOIN token_subscriptions s ON s.push_id_hash = m.push_id_hash
                WHERE m.wake_pending
                  AND m.created_date >= CURRENT_DATE - 1
                  AND s.dead_since IS NULL
                ORDER BY m.push_id_hash
                """
            )
            if not devices:
                return

            logger.info("Retrying %s wake-up push(es)", len(devices))
            for d in devices:
                result = await send_push(d["push_token"], d["platform"])
                if result == PUSH_DEAD_TOKEN:
                    await mark_dead(conn, d["push_id_hash"], d["push_token"])
                elif result == PUSH_OK:
                    await conn.execute(
                        "UPDATE mailbox SET wake_pending = FALSE WHERE push_id_hash = $1",
                        d["push_id_hash"],
                    )
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_DISPATCH)


async def cleanup_expired_subscriptions():
    async with database.pool.acquire() as conn:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_CLEANUP)
        if not locked:
            return
        try:
            cutoff = datetime.now(timezone.utc).date() - timedelta(days=settings.subscription_ttl_days)
            result = await conn.execute(
                "DELETE FROM token_subscriptions WHERE created_date < $1",
                cutoff,
            )
            deleted = result.split()[-1]
            if int(deleted) > 0:
                logger.info("TTL cleanup: deleted %s expired subscription(s)", deleted)

            # Tokens the provider reported dead and the app never fixed.
            dead_cutoff = datetime.now(timezone.utc).date() - timedelta(days=settings.dead_token_grace_days)
            result = await conn.execute(
                "DELETE FROM token_subscriptions WHERE dead_since IS NOT NULL AND dead_since < $1",
                dead_cutoff,
            )
            dead_deleted = result.split()[-1]
            if int(dead_deleted) > 0:
                logger.info("Dead-token cleanup: deleted %s subscription(s)", dead_deleted)

            # Messages nobody fetched in time.
            await conn.execute(
                "DELETE FROM mailbox WHERE created_date < CURRENT_DATE - $1::int",
                settings.mailbox_ttl_days,
            )

            campaign_cutoff = datetime.now(timezone.utc) - timedelta(days=settings.notify_rate_limit_days)  # campaigns only matter inside the rate-limit window
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
            await dispatch_wakeups()
        except Exception:
            logger.exception("Error in dispatch_wakeups")
        await asyncio.sleep(DISPATCH_INTERVAL_SECONDS)


async def run_cleanup():
    while True:
        try:
            await cleanup_expired_subscriptions()
        except Exception:
            logger.exception("Error in cleanup_expired_subscriptions")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
