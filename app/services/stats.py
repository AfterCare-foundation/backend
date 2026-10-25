# app/services/stats.py
#
# Anonymous usage counts for reporting (partners, community, investors).
#
# One row per day and metric in `daily_stats`: a name and a number, nothing
# else. No device, card hash, STI type or location is ever passed in here,
# so the table can be kept after the 180-day cleanup.
#
# Counting must never break the app: a failure is logged (error type only)
# and the request carries on.

import logging

import asyncpg

logger = logging.getLogger(__name__)

# Events (a number added each time it happens)
DEVICES_NEW = "devices_new"                      # first time a device registers
CODES_JOINED = "codes_joined"                    # a device joins a code
PAIRINGS_COMPLETED = "pairings_completed"        # a code reaches its second device
NOTIFY_TAPS = "notify_taps"                      # Notify taps that reached someone
MESSAGES_SENT = "messages_sent"                  # messages put in mailboxes
MESSAGES_COLLECTED = "messages_collected"        # messages the app confirmed
MESSAGES_EXPIRED = "messages_expired"            # messages nobody collected in time
PUSHES_OK = "pushes_ok"
PUSHES_FAILED = "pushes_failed"
PUSHES_DEAD = "pushes_dead"                      # provider said the token is gone
DELETE_REQUESTS = "delete_requests"              # "delete my data"
SUBSCRIPTIONS_EXPIRED = "subscriptions_expired"  # reached the 180-day limit
TOKENS_WIPED = "tokens_wiped"                    # dead push tokens erased (subscription kept)

# Snapshots (the day's value is set, not added to)
ACTIVE_CODES = "active_codes"
ACTIVE_DEVICES = "active_devices"
WAITING_MESSAGES = "waiting_messages"


async def bump(conn: asyncpg.Connection, metric: str, n: int = 1) -> None:
    """Add n to today's count for this metric."""
    if n <= 0:
        return
    try:
        # A nested transaction is a savepoint: a failure here cannot abort
        # the transaction of the request that called us.
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO daily_stats (day, metric, value)
                VALUES (CURRENT_DATE, $1, $2)
                ON CONFLICT (day, metric) DO UPDATE SET value = daily_stats.value + EXCLUDED.value
                """,
                metric,
                n,
            )
    except Exception as exc:
        logger.error("Stats failed: %s", type(exc).__name__)


async def record_snapshot(conn: asyncpg.Connection) -> None:
    """Today's totals right now. Safe to run more than once a day."""
    try:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO daily_stats (day, metric, value)
                SELECT CURRENT_DATE, m.metric, m.value FROM (
                    SELECT $1::text AS metric, COUNT(DISTINCT et_hash)::bigint AS value FROM token_subscriptions
                    UNION ALL SELECT $2, COUNT(*) FROM devices
                    UNION ALL SELECT $3, COUNT(*) FROM mailbox
                ) m
                ON CONFLICT (day, metric) DO UPDATE SET value = EXCLUDED.value
                """,
                ACTIVE_CODES,
                ACTIVE_DEVICES,
                WAITING_MESSAGES,
            )
    except Exception as exc:
        logger.error("Stats snapshot failed: %s", type(exc).__name__)
