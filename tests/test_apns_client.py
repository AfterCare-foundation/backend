"""The shared APNs client and the cached provider token. No real network."""

import asyncio
import time
import types

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import push
from tests.conftest import push_token_for


@pytest.fixture(autouse=True)
def _clean_apns_state():
    asyncio.run(push.close_apns_client())
    yield
    asyncio.run(push.close_apns_client())


@pytest.fixture
def apns(monkeypatch):
    """Real signing with a throwaway key, MockTransport instead of Apple."""
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(settings, "push_stub_mode", False)
    monkeypatch.setattr(settings, "apns_key", pem)
    monkeypatch.setattr(settings, "apns_key_id", "KEYID12345")
    monkeypatch.setattr(settings, "apns_team_id", "TEAMID1234")

    state = types.SimpleNamespace(signs=0, clients=[], requests=[], status=200, body={})

    real_encode = jwt.encode

    def counting_encode(*a, **kw):
        state.signs += 1
        return real_encode(*a, **kw)

    monkeypatch.setattr("jwt.encode", counting_encode)

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        return httpx.Response(state.status, json=state.body)

    real_client = httpx.AsyncClient

    def factory(**kw):
        client = real_client(transport=httpx.MockTransport(handler), **kw)
        state.clients.append(client)
        return client

    monkeypatch.setattr(push.httpx, "AsyncClient", factory)
    return state


def test_five_pushes_share_one_client_and_one_token(apns):
    async def go():
        return await asyncio.gather(
            *[push.send_push(push_token_for(f"d{i}"), "ios") for i in range(5)]
        )

    assert asyncio.run(go()) == [push.PUSH_OK] * 5
    assert apns.signs == 1
    assert len(apns.clients) == 1
    auth = {r.headers["authorization"] for r in apns.requests}
    assert len(apns.requests) == 5 and len(auth) == 1


def test_token_is_signed_again_after_50_minutes(apns, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(
        push, "time", types.SimpleNamespace(time=time.time, monotonic=lambda: now[0])
    )

    async def go():
        await push.send_push(push_token_for("a"), "ios")
        now[0] += 49 * 60
        await push.send_push(push_token_for("a"), "ios")
        assert apns.signs == 1
        now[0] += 2 * 60
        await push.send_push(push_token_for("a"), "ios")

    asyncio.run(go())
    assert apns.signs == 2


def test_expired_provider_token_drops_the_cache(apns):
    async def go():
        assert await push.send_push(push_token_for("a"), "ios") == push.PUSH_OK
        apns.status, apns.body = 403, {"reason": "ExpiredProviderToken"}
        assert await push.send_push(push_token_for("a"), "ios") == push.PUSH_FAILED
        apns.status, apns.body = 200, {}
        assert await push.send_push(push_token_for("a"), "ios") == push.PUSH_OK

    asyncio.run(go())
    assert apns.signs == 2


def test_two_asyncio_runs_each_get_a_working_client(apns):
    for _ in range(2):
        assert asyncio.run(push.send_push(push_token_for("a"), "ios")) == push.PUSH_OK
    assert len(apns.clients) == 2


def test_lifespan_shutdown_closes_the_client(apns):
    with TestClient(app) as c:
        assert c.portal.call(push.send_push, push_token_for("a"), "ios") == push.PUSH_OK
        assert len(apns.clients) == 1 and not apns.clients[0].is_closed
    assert apns.clients[0].is_closed
