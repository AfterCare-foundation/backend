# app/database.py
#
# Manages the connection pool to PostgreSQL.
#
# What is a connection pool?
#   Opening a database connection takes time. A pool keeps a set of connections
#   open and ready, so each request can borrow one instantly and return it when done.
#   asyncpg handles this automatically — we just set it up once on startup.
#
# What is async?
#   Our server handles many requests at once. While one request is waiting for the
#   database to respond, async lets the server handle other requests in the meantime.
#   "await" means "pause here and let others run while we wait for the result".

import asyncpg
from app.config import settings

# The pool is created on startup and shared across all requests.
# It starts as None and gets assigned in init_db().
pool: asyncpg.Pool | None = None


async def init_db():
    """Create the connection pool. Called once when the server starts."""
    global pool
    pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=2,   # Keep at least 2 connections open
        max_size=10,  # Never open more than 10 at once
        server_settings={
            # Force UTC so CURRENT_DATE is always UTC, regardless of
            # the server's local timezone setting.
            "TimeZone": "UTC"
        }
    )


async def close_db():
    """Close the connection pool. Called once when the server shuts down."""
    global pool
    if pool:
        await pool.close()


async def get_db() -> asyncpg.Connection:
    """
    Borrow a connection from the pool for one request.

    Usage in a route:
        async with get_db() as conn:
            result = await conn.fetch("SELECT ...")
    """
    async with pool.acquire() as conn:
        yield conn
