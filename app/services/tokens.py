# app/services/tokens.py
#
# Dead push tokens. When APNs/FCM says a token will never work again (app
# uninstalled, token replaced), we do not delete at once: the app fixes the
# token on its next launch via /update-push-id or /subscribe, which clears the
# mark. Cleanup removes subscriptions that stay dead past the grace period.

import asyncpg


async def mark_dead(conn: asyncpg.Connection, push_id_hash: str, push_token: str) -> None:
    await conn.execute(
        """
        UPDATE token_subscriptions
        SET dead_since = COALESCE(dead_since, CURRENT_DATE)
        WHERE push_id_hash = $1 AND push_token = $2
        """,
        push_id_hash,
        push_token,
    )
