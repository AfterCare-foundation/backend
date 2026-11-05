"""/health asks the database, and reports only that it is unavailable."""

import asyncio
import logging
import time

import app.database as database
from app import main

MARKER = "SECRET-MARKER-5521"


class _FakePool:
    def __init__(self, enter):
        self._enter = enter

    def acquire(self):
        return self

    async def __aenter__(self):
        return await self._enter()

    async def __aexit__(self, *exc):
        return False


def test_healthy_database_gives_200(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_a_failing_pool_gives_503_without_the_message(client, monkeypatch, caplog):
    async def boom():
        raise RuntimeError(MARKER)

    with monkeypatch.context() as m, caplog.at_level(logging.DEBUG):
        m.setattr(database, "pool", _FakePool(boom))
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert MARKER not in response.text
    assert MARKER not in caplog.text
    lines = [r.getMessage() for r in caplog.records if "Health check failed" in r.getMessage()]
    assert lines == ["Health check failed: RuntimeError"]


def test_a_hanging_acquire_gives_503_within_the_timeout(client, monkeypatch):
    async def hang():
        await asyncio.sleep(3600)

    with monkeypatch.context() as m:
        m.setattr(database, "pool", _FakePool(hang))
        m.setattr(main, "HEALTH_TIMEOUT_SECONDS", 0.2)
        started = time.monotonic()
        response = client.get("/health")
        elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert elapsed < 1
