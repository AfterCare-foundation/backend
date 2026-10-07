# app/services/rate_limit.py
#
# Two caps on new campaigns:
#   - at most 3 campaigns per rolling 24 hours (one run per infection, a few per sitting)
#   - at most 4 campaigns per rolling 30 days (the total limit)
# The server never learns which infection a campaign is about.

# A campaign_id can be used ONCE. A second request with the same id is
# rejected (409), so the server needs no list of contacts per campaign.
# The row holds only: campaign_id, sender device, time.

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException
import asyncpg

from app.config import settings


async def claim_campaign(conn: asyncpg.Connection, push_id_hash: str, campaign_id: UUID) -> None:
    """
    Check the limits and reserve this campaign_id. Raises 409 if the id was
    already used, 429 if a limit is reached. Call release_campaign() if
    nothing ends up being sent, so a failed attempt does not use up a slot.
    """
    if await conn.fetchval(
        "SELECT 1 FROM notification_campaigns WHERE campaign_id = $1", campaign_id
    ):
        raise HTTPException(status_code=409, detail="campaign_already_used")

    now = datetime.now(timezone.utc)

    today = await conn.fetchval(
        "SELECT COUNT(*) FROM notification_campaigns WHERE push_id_hash = $1 AND created_at >= $2",
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
        "SELECT COUNT(*) FROM notification_campaigns WHERE push_id_hash = $1 AND created_at >= $2",
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

    # The primary key makes this atomic: of two simultaneous requests with the
    # same campaign_id, only one gets the row.
    inserted = await conn.fetchval(
        """
        INSERT INTO notification_campaigns (campaign_id, push_id_hash)
        VALUES ($1, $2)
        ON CONFLICT (campaign_id) DO NOTHING
        RETURNING campaign_id
        """,
        campaign_id,
        push_id_hash,
    )
    if inserted is None:
        raise HTTPException(status_code=409, detail="campaign_already_used")


async def release_campaign(conn: asyncpg.Connection, campaign_id: UUID) -> None:
    """Nothing was sent or queued: give the slot (and the id) back."""
    await conn.execute("DELETE FROM notification_campaigns WHERE campaign_id = $1", campaign_id)
