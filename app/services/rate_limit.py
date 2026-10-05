# app/services/rate_limit.py
#
# Two caps on new campaigns (same campaign_id does not count twice):
#   - at most 3 campaigns per rolling 24 hours (one run per infection, a few per sitting)
#   - at most 4 campaigns per rolling 30 days (the total limit)
# The server never learns which infection a campaign is about.

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException
import asyncpg

from app.config import settings


async def assert_campaign_allowed(
    conn: asyncpg.Connection,
    push_id_hash: str,
    campaign_id: UUID,
) -> bool:
    """
    Returns True if this is a new campaign (count it after a successful send).
    Returns False if this campaign_id was already recorded.
    """
    row = await conn.fetchrow(
        "SELECT push_id_hash FROM notification_campaigns WHERE campaign_id = $1",
        campaign_id,
    )
    if row is not None:
        if row["push_id_hash"] != push_id_hash:
            raise HTTPException(status_code=403, detail="Campaign does not belong to this device")
        return False

    now = datetime.now(timezone.utc)

    today = await conn.fetchval(
        """
        SELECT COUNT(*) FROM notification_campaigns
        WHERE push_id_hash = $1
          AND created_at >= $2
        """,
        push_id_hash,
        now - timedelta(hours=24),
    )
    if today >= settings.notify_max_campaigns_per_day:
        raise HTTPException(
            status_code=429,
            detail=f"At most {settings.notify_max_campaigns_per_day} campaigns per day",
        )

    window_start = now - timedelta(days=settings.notify_rate_limit_days)
    used = await conn.fetchval(
        """
        SELECT COUNT(*) FROM notification_campaigns
        WHERE push_id_hash = $1
          AND created_at >= $2
        """,
        push_id_hash,
        window_start,
    )
    if used >= settings.notify_max_campaigns:
        raise HTTPException(
            status_code=429,
            detail=(
                f"At most {settings.notify_max_campaigns} campaigns "
                f"per {settings.notify_rate_limit_days} days"
            ),
        )
    return True


async def record_campaign_success(
    conn: asyncpg.Connection,
    push_id_hash: str,
    campaign_id: UUID,
) -> None:
    """Call only after at least one push was sent or one row was scheduled."""
    await conn.execute(
        """
        INSERT INTO notification_campaigns (campaign_id, push_id_hash)
        VALUES ($1, $2)
        ON CONFLICT (campaign_id) DO NOTHING
        """,
        campaign_id,
        push_id_hash,
    )
