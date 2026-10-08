# app/cron.py
#
# Background jobs. PostgreSQL advisory locks mean two app processes
# cannot dispatch the same pending row twice.

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import app.database as database
from app.config import settings
from app.services.push import BUNDLE_DEAD_TOKEN, send_bundle
from app.services.tokens import mark_dead

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
            # device (e.g. two codes with the same person, two failed pushes
            # for both), send ONE push carrying all ciphertexts.
            recipients_of = {}
            has_dead = {}
            per_device = {}
            for row in rows:
                recipients = await conn.fetch(
                    """
                    SELECT push_id_hash, push_token, platform, dead_since
                    FROM token_subscriptions
                    WHERE et_hash = $1
                      AND push_id_hash != $2
                    """,
                    row["et_hash"],
                    row["sender_push_id_hash"],
                )
                recipients_of[row["id"]] = [r["push_id_hash"] for r in recipients]
                has_dead[row["id"]] = any(r["dead_since"] is not None for r in recipients)
                for r in recipients:
                    if r["dead_since"] is not None:
                        # The provider already said this token is dead: do not
                        # call it again. Wait for the app to send a new token.
                        continue
                    entry = per_device.setdefault(
                        r["push_id_hash"], {"recipient": dict(r), "payloads": []}
                    )
                    entry["payloads"].append(row["encrypted_payload"])

            device_ok = {}
            for device, entry in per_device.items():
                count = await send_bundle(entry["recipient"], entry["payloads"])
                if count == BUNDLE_DEAD_TOKEN:
                    await mark_dead(conn, device, entry["recipient"]["push_token"])
                device_ok[device] = count >= 0

            for row in rows:
                # Two kinds of waiting:
                # - ordinary failure: retry every run, give up after a day;
                # - dead token: no calls, but keep the message for the dead-token
                #   grace period, so it is delivered if the app sends a new token.
                keep_days = settings.dead_token_grace_days if has_dead[row["id"]] else 1
                too_old = row["scheduled_at"] < datetime.now(timezone.utc) - timedelta(days=keep_days)
                # device_ok has no entry for skipped (dead) devices: they count as not delivered.
                all_ok = all(device_ok.get(d, False) for d in recipients_of[row["id"]])
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

            # Tokens the provider reported dead and the app never fixed.
            dead_cutoff = datetime.now(timezone.utc).date() - timedelta(days=settings.dead_token_grace_days)
            result = await conn.execute(
                "DELETE FROM token_subscriptions WHERE dead_since IS NOT NULL AND dead_since < $1",
                dead_cutoff,
            )
            dead_deleted = result.split()[-1]
            if int(dead_deleted) > 0:
                logger.info("Dead-token cleanup: deleted %s subscription(s)", dead_deleted)

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
