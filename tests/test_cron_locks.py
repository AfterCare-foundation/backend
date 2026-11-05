"""The background jobs hold a transaction-level advisory lock that always ends with the job."""

import asyncio

import asyncpg
import pytest

from app import cron
from app.services.push import PUSH_DEAD_TOKEN, PUSH_FAILED, PUSH_OK
from tests.conftest import TEST_DATABASE_URL, notify, push_token_for, sha256_hex, sql, subscribe


def _install(monkeypatch, behave):
    calls = []

    async def fake(push_token, platform, lang="en"):
        calls.append(push_token)
        return await behave(push_token)

    monkeypatch.setattr("app.services.notify_flow.send_push", fake)
    monkeypatch.setattr("app.cron.send_push", fake)
    return calls


async def _ok(_token):
    return PUSH_OK


async def _fail(_token):
    return PUSH_FAILED


def _pending_pushes(client, monkeypatch, devices=("bob",)):
    """Alice notifies each device (one card each); every push fails, so all stay pending."""
    _install(monkeypatch, _fail)
    for d in devices:
        subscribe(client, device="alice", card=f"card-{d}")
        subscribe(client, device=d, card=f"card-{d}")
        response, _ = notify(client, device="alice", card=f"card-{d}")
        assert response.status_code == 200


async def _advisory_locks(key: int) -> int:
    conn = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        return await conn.fetchval(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = $1", key
        )
    finally:
        await conn.close()


async def _scalar(query: str):
    conn = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        return await conn.fetchval(query)
    finally:
        await conn.close()


async def _held_elsewhere(key: int, job, probe):
    """Run `job` while another connection holds the lock, probe, release it, run `job` again."""
    other = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        tr = other.transaction()
        await tr.start()
        await other.execute("SELECT pg_advisory_xact_lock($1)", key)
        await job()
        seen = await probe()
        await tr.commit()
    finally:
        await other.close()
    await job()
    return seen


def test_dispatcher_does_nothing_while_the_lock_is_held_elsewhere(client, monkeypatch):
    _pending_pushes(client, monkeypatch)
    calls = _install(monkeypatch, _ok)

    async def probe():
        return len(calls)

    seen = client.portal.call(_held_elsewhere, cron.LOCK_DISPATCH, cron.dispatch_wakeups, probe)
    assert seen == 0
    assert calls == [push_token_for("bob")]


def test_cleanup_waits_while_the_lock_is_held_elsewhere(client):
    subscribe(client, device="alice", card="pink")
    sql("UPDATE token_subscriptions SET created_date = CURRENT_DATE - 400")

    async def probe():
        return await _scalar("SELECT count(*) FROM token_subscriptions")

    seen = client.portal.call(_held_elsewhere, cron.LOCK_CLEANUP, cron.cleanup_expired_subscriptions, probe)
    assert seen == 1  # untouched while locked
    assert sql("SELECT count(*) FROM token_subscriptions") == "0"  # gone after release


def test_two_concurrent_dispatcher_runs_push_each_device_once(client, monkeypatch):
    devices = ("bob", "cara", "dan")
    _pending_pushes(client, monkeypatch, devices)

    async def slow(_token):
        await asyncio.sleep(0.2)
        return PUSH_OK

    calls = _install(monkeypatch, slow)

    async def both():
        await asyncio.gather(cron.dispatch_wakeups(), cron.dispatch_wakeups())

    client.portal.call(both)
    assert sorted(calls) == sorted(push_token_for(d) for d in devices)


def test_two_concurrent_cleanup_runs_do_the_work_once(client, monkeypatch):
    subscribe(client, device="alice", card="pink")
    sql("UPDATE token_subscriptions SET created_date = CURRENT_DATE - 400")
    snapshots = []
    real = cron.stats.record_snapshot

    async def counting(conn):
        snapshots.append(1)
        await asyncio.sleep(0.2)
        await real(conn)

    monkeypatch.setattr(cron.stats, "record_snapshot", counting)

    async def both():
        await asyncio.gather(cron.cleanup_expired_subscriptions(), cron.cleanup_expired_subscriptions())

    client.portal.call(both)
    assert snapshots == [1]


def test_a_dispatcher_that_fails_midway_leaves_no_lock_behind(client, monkeypatch):
    _pending_pushes(client, monkeypatch)

    async def dead(_token):
        return PUSH_DEAD_TOKEN

    calls = _install(monkeypatch, dead)

    async def broken(*_a, **_k):
        raise RuntimeError("boom")

    with monkeypatch.context() as m:
        m.setattr("app.cron.mark_dead", broken)
        with pytest.raises(RuntimeError):
            client.portal.call(cron.dispatch_wakeups)

    assert client.portal.call(_advisory_locks, cron.LOCK_DISPATCH) == 0
    client.portal.call(cron.dispatch_wakeups)  # the next run gets the lock and works
    assert len(calls) == 2
    bob = sha256_hex("push:bob")
    assert sql(f"SELECT dead_since IS NOT NULL FROM token_subscriptions WHERE push_id_hash = '{bob}'") == "t"


def test_a_cleanup_that_fails_midway_leaves_no_lock_behind(client, monkeypatch):
    subscribe(client, device="alice", card="pink")
    sql("UPDATE token_subscriptions SET created_date = CURRENT_DATE - 400")

    async def broken(_conn):
        raise RuntimeError("boom")

    with monkeypatch.context() as m:
        m.setattr(cron.stats, "record_snapshot", broken)
        with pytest.raises(RuntimeError):
            client.portal.call(cron.cleanup_expired_subscriptions)

    assert sql("SELECT count(*) FROM token_subscriptions") == "1"  # rolled back as a whole
    assert client.portal.call(_advisory_locks, cron.LOCK_CLEANUP) == 0
    client.portal.call(cron.cleanup_expired_subscriptions)
    assert sql("SELECT count(*) FROM token_subscriptions") == "0"
