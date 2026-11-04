# Push token format is checked at the HTTP boundary and again before any send.
# A bad token must give a fixed 422 message, never be echoed or logged, never
# be stored, and a bad token already stored must never reach APNs/FCM.

import asyncio
import logging

import pytest

from app.config import settings
from app.services import push
from tests.conftest import push_token_for, sha256_hex, sql, subscribe

HEX_MARKER = "deadbeef" * 8  # 64 hex chars, valid iOS token on its own

# (platform, token, marker that must appear nowhere in the response or the logs)
BAD_SUBSCRIBE_TOKENS = [
    pytest.param("ios", "deadbeef" * 12_000, "deadbeef", id="ios-huge"),
    pytest.param("ios", "../3/device/x?y=1", "device/x", id="ios-path-injection"),
    pytest.param("ios", HEX_MARKER + "\n", "deadbeef", id="ios-trailing-newline"),
    pytest.param("ios", "MARKERxyz:APA91b" + "a" * 40, "MARKERxyz", id="ios-fcm-shaped"),
    pytest.param("android", "MARKERxyz" + "a" * 30 + "/" + "b" * 10, "MARKERxyz", id="android-slash"),
]


def _subscribe_body(platform, token):
    return {
        "et_hash": sha256_hex("card:x"),
        "push_id_hash": sha256_hex("push:x"),
        "push_token": token,
        "platform": platform,
        "device_credential": sha256_hex("secret:x"),
    }


@pytest.mark.parametrize("platform,token,marker", BAD_SUBSCRIBE_TOKENS)
def test_subscribe_refuses_a_malformed_token(client, caplog, platform, token, marker):
    caplog.set_level(logging.DEBUG)
    response = client.post("/subscribe", json=_subscribe_body(platform, token))

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert len(detail) == 1
    assert detail[0]["type"] == "value_error"
    assert "push_token" in detail[0]["msg"]
    assert marker not in response.text
    assert token not in response.text
    assert all(marker not in record.getMessage() for record in caplog.records)
    assert sql("SELECT count(*) FROM token_subscriptions") == "0"
    assert sql("SELECT count(*) FROM devices") == "0"


def test_update_push_id_refuses_a_malformed_token_and_keeps_the_stored_one(client, caplog):
    body = subscribe(client, device="bob", card="pink")
    stored = sql("SELECT push_token FROM token_subscriptions")
    assert stored == push_token_for("bob")

    caplog.set_level(logging.DEBUG)
    bad = "MARKERxyz" + "a" * 30 + "/" + "b" * 10
    response = client.post("/update-push-id", json={
        "old_push_id_hash": body["push_id_hash"],
        "new_push_id_hash": sha256_hex("push:bob2"),
        "new_push_token": bad,
        "new_platform": "android",
        "device_credential": body["device_credential"],
    })

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert len(detail) == 1
    assert detail[0]["type"] == "value_error"
    assert "new_push_token" in detail[0]["msg"]
    assert "MARKERxyz" not in response.text
    assert all("MARKERxyz" not in record.getMessage() for record in caplog.records)
    assert sql("SELECT push_token FROM token_subscriptions") == stored
    assert sql("SELECT push_id_hash FROM token_subscriptions") == body["push_id_hash"]


def test_a_bad_platform_gives_one_error_not_also_a_token_error(client):
    response = client.post("/subscribe", json=_subscribe_body("windows", "not a token"))

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert len(detail) == 1
    assert detail[0]["loc"][-1] == "platform"


def test_update_push_id_bad_platform_gives_one_error(client):
    body = subscribe(client, device="bob", card="pink")
    response = client.post("/update-push-id", json={
        "old_push_id_hash": body["push_id_hash"],
        "new_push_id_hash": sha256_hex("push:bob2"),
        "new_push_token": "not a token",
        "new_platform": "windows",
        "device_credential": body["device_credential"],
    })

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert len(detail) == 1
    assert detail[0]["loc"][-1] == "new_platform"


def test_an_upper_case_ios_token_is_accepted_and_stored_unchanged(client):
    token = ("ABCDEF0123456789" * 4)
    assert len(token) == 64
    response = client.post("/subscribe", json=_subscribe_body("ios", token))

    assert response.status_code == 200, response.text
    assert sql("SELECT push_token FROM token_subscriptions") == token


@pytest.mark.parametrize("stub_mode", [False, True])
def test_send_push_refuses_a_stored_malformed_token_before_any_send(
    client, monkeypatch, caplog, stub_mode
):
    subscribe(client, device="bob", card="pink")
    bad = "MARKERxyz/../3/device/x?y=1"
    # Simulates a row written before the format was checked.
    sql(f"UPDATE token_subscriptions SET push_token = '{bad}'")
    stored = sql("SELECT push_token FROM token_subscriptions")
    assert stored == bad

    attempts = []

    async def record(push_token, lang):
        attempts.append(push_token)
        return push.PUSH_OK

    monkeypatch.setattr(push, "_send_apns", record)
    monkeypatch.setattr(push, "_send_fcm", record)
    monkeypatch.setattr(settings, "push_stub_mode", stub_mode)

    caplog.set_level(logging.DEBUG)
    for platform in ("ios", "android"):
        assert asyncio.run(push.send_push(stored, platform)) == push.PUSH_DEAD_TOKEN

    assert attempts == []
    assert "MARKERxyz" not in caplog.text
    assert "device/x" not in caplog.text
    assert "[PUSH STUB]" not in caplog.text
