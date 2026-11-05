"""Wake-up pushes run in parallel, with a time limit, and results reach the right device."""

import asyncio
import logging
import time
from uuid import uuid4

import pytest

from app import cron
from app.services import push
from app.services.push import PUSH_DEAD_TOKEN, PUSH_FAILED, PUSH_OK, wake_all
from tests.conftest import push_token_for, sha256_hex, sql, subscribe

MARKER = "SECRET-MARKER-9137"


def _install(monkeypatch, behave):
    """Replace the push with `behave(push_token)`, an async function returning the result."""
    async def fake(push_token, platform, lang="en"):
        return await behave(push_token)

    monkeypatch.setattr("app.services.notify_flow.send_push", fake)
    monkeypatch.setattr("app.cron.send_push", fake)


def _cards_with_alice(client, n):
    """alice shares one card with each of n other devices (a card holds two devices)."""
    others = [f"d{i}" for i in range(n)]
    for i, other in enumerate(others):
        subscribe(client, device="alice", card=f"c{i}")
        subscribe(client, device=other, card=f"c{i}")
    return others


def _notify_all(client, n):
    return client.post(
        "/notify",
        json={
            "sender_push_id_hash": sha256_hex("push:alice"),
            "device_credential": sha256_hex("secret:alice"),
            "campaign_id": str(uuid4()),
            "deliveries": [
                {"et_hash": sha256_hex(f"card:c{i}"), "encrypted_payload": f"ct-{i}"}
                for i in range(n)
            ],
        },
    )


def _pending(device):
    return sql(f"SELECT bool_and(wake_pending) FROM mailbox WHERE push_id_hash = '{sha256_hex(f'push:{device}')}'")


def _dead(device):
    return sql(f"SELECT dead_since IS NOT NULL FROM token_subscriptions WHERE push_id_hash = '{sha256_hex(f'push:{device}')}'")


async def _ok(_token):
    return PUSH_OK


async def _fail(_token):
    return PUSH_FAILED


def _run(client, monkeypatch, via, n, behave):
    """Run one wake-up round through /notify or through the retry job."""
    others = _cards_with_alice(client, n)
    if via == "notify":
        _install(monkeypatch, behave)
        response = _notify_all(client, n)
        assert response.status_code == 200, response.text
        return others, response.json()
    _install(monkeypatch, _fail)
    assert _notify_all(client, n).json()["pushed"] == 0
    assert all(_pending(d) == "t" for d in others)
    _install(monkeypatch, behave)
    client.portal.call(cron.dispatch_wakeups)
    return others, None


def test_six_recipients_are_woken_in_parallel(client, monkeypatch):
    async def slow(_token):
        await asyncio.sleep(0.5)
        return PUSH_OK

    others = _cards_with_alice(client, 6)
    _install(monkeypatch, slow)
    started = time.monotonic()
    response = _notify_all(client, 6)
    assert time.monotonic() - started < 1.5
    assert response.json()["pushed"] == 6
    assert all(_pending(d) == "f" for d in others)


def test_pushes_waiting_for_a_slot_are_not_timed_out(monkeypatch):
    monkeypatch.setattr(push, "WAKE_TIMEOUT_SECONDS", 0.3)

    async def send(token, platform, lang="en"):
        await asyncio.sleep(0.2)
        return PUSH_OK

    targets = [(f"t{i}", "ios", "en") for i in range(15)]  # more than the 10 slots
    assert asyncio.run(wake_all(targets, send=send)) == [PUSH_OK] * 15


@pytest.mark.parametrize("via", ["notify", "dispatch"])
def test_a_hanging_push_fails_and_the_others_are_woken(client, monkeypatch, via):
    monkeypatch.setattr(push, "WAKE_TIMEOUT_SECONDS", 0.3)
    hung = push_token_for("d2")

    async def behave(token):
        if token == hung:
            await asyncio.sleep(5)
        return PUSH_OK

    others, body = _run(client, monkeypatch, via, 6, behave)
    if body:
        assert body["pushed"] == 5 and body["retrying"] == 1
    assert [_pending(d) for d in others] == ["f", "f", "t", "f", "f", "f"]


@pytest.mark.parametrize("via", ["notify", "dispatch"])
def test_a_push_that_raises_counts_as_failed_and_logs_no_message(client, monkeypatch, caplog, via):
    bad = push_token_for("d1")

    async def behave(token):
        if token == bad:
            raise RuntimeError(MARKER)
        return PUSH_OK

    with caplog.at_level(logging.DEBUG):
        others, body = _run(client, monkeypatch, via, 4, behave)
    if body:
        assert body["pushed"] == 3 and body["retrying"] == 1
    assert [_pending(d) for d in others] == ["f", "t", "f", "f"]
    assert MARKER not in caplog.text
    assert "Push failed: RuntimeError" in caplog.text


@pytest.mark.parametrize("via", ["notify", "dispatch"])
def test_results_reach_the_right_device(client, monkeypatch, via):
    dead = push_token_for("d2")
    delays = {push_token_for(f"d{i}"): (5 - i) * 0.1 for i in range(5)}  # last one finishes first

    async def behave(token):
        await asyncio.sleep(delays[token])
        return PUSH_DEAD_TOKEN if token == dead else PUSH_OK

    others, _ = _run(client, monkeypatch, via, 5, behave)
    assert [_dead(d) for d in others] == ["f", "f", "t", "f", "f"]
    assert [_pending(d) for d in others] == ["f", "f", "t", "f", "f"]
