# app/cron.py
#
# Background jobs. PostgreSQL advisory locks mean two app processes
# cannot dispatch the same pending row twice.

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import app.database as database
from app.config import settings
from app.services.push import send_bundle

logger = logging.getLogger(__name__)

DISPATCH_INTERVAL_SECONDS = 15 * 60
CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60

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
                SELECT id, et_hash, sender_push_id_hash, encrypted_payload, scheduled_at
                FROM pending_notifications
                WHERE scheduled_at <= NOW()
                ORDER BY scheduled_at
                """
            )
            if not rows:
                return

            logger.info("Dispatching %s pending notification(s)", len(rows))

            # Group by recipient device: if several due rows reach the same
            # device (e.g. two codes with the same person, one STI scheduled
            # for both), send ONE push carrying all ciphertexts.
            recipients_of = {}
            per_device = {}
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
                recipients_of[row["id"]] = [r["push_id_hash"] for r in recipients]
                for r in recipients:
                    entry = per_device.setdefault(
                        r["push_id_hash"], {"recipient": dict(r), "payloads": []}
                    )
                    entry["payloads"].append(row["encrypted_payload"])

            device_ok = {}
            for device, entry in per_device.items():
                device_ok[device] = await send_bundle(entry["recipient"], entry["payloads"]) >= 0

            for row in rows:
                # Failed? Keep the row and retry on the next run,
                # but give up after a day so dead tokens do not pile up.
                too_old = row["scheduled_at"] < datetime.now(timezone.utc) - timedelta(days=1)
                all_ok = all(device_ok[d] for d in recipients_of[row["id"]])
                if not all_ok and not too_old:
                    continue
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
            cutoff = datetime.now(timezone.utc).date() - timedelta(days=settings.subscription_ttl_days)
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
