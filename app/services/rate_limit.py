# app/services/rate_limit.py
#
# Two caps on new campaigns (same campaign_id does not count twice):
#   - at least 1 day between campaigns (no two on the same day)
#   - at most 4 campaigns per rolling 30 days

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

    last = await conn.fetchval(
        """
        SELECT MAX(created_at) FROM notification_campaigns
        WHERE push_id_hash = $1
        """,
        push_id_hash,
    )
    if last is not None:
        min_gap = timedelta(days=settings.notify_min_days_between_campaigns)
        if datetime.now(timezone.utc) - last < min_gap:
            raise HTTPException(
                status_code=429,
                detail=f"At most one notification campaign every {settings.notify_min_days_between_campaigns} days",
            )

    window_start = datetime.now(timezone.utc) - timedelta(days=settings.notify_rate_limit_days)
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
                f"At most {settings.notify_max_campaigns} notification campaigns "
                f"are allowed every {settings.notify_rate_limit_days} days"
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
