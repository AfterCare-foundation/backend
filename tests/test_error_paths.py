# Error paths must not put values into logs or responses (CLAUDE.md, privacy
# invariant 3). Markers stand in for secrets such as a credential hash.

import asyncio
import logging
import traceback
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import cron
from app.database import get_db
from app.main import app
from tests.conftest import sha256_hex, subscribe

SECRET = "SECRET-MARKER-aaa111"
CAUSE_SECRET = "CAUSE-MARKER-bbb222"
QUERY_SECRET = "QUERY-MARKER-ccc333"
ALL_MARKERS = (SECRET, CAUSE_SECRET, QUERY_SECRET)


class FakeDbError(Exception):
    pass


def _record_text(record: logging.LogRecord) -> str:
    parts = [record.getMessage(), record.exc_text or ""]
    if record.exc_info:
        parts.append("".join(traceback.format_exception(*record.exc_info)))
    parts.append(str(record.__dict__.get("stack_info") or ""))
    return "\n".join(parts)


def assert_no_marker_logged(caplog, markers=ALL_MARKERS):
    for record in caplog.records:
        text = _record_text(record)
        for marker in markers:
            assert marker not in text, f"{marker} leaked via logger {record.name}"


@pytest.fixture
def boom_route():
    """A route that always fails with a secret in the message and in its cause."""
    state = {"raised": 0}

    async def boom(item: str):
        state["raised"] += 1
        try:
            raise ValueError(CAUSE_SECRET)
        except ValueError as cause:
            raise FakeDbError(f"duplicate key Key (credential_hash)=({SECRET})") from cause

    app.add_api_route("/boom/{item}", boom, methods=["GET", "POST"])
    route = app.router.routes[-1]
    yield state
    app.router.routes.remove(route)


@pytest.fixture
def plain_client():
    # raise_server_exceptions=True is the default: if the exception escaped
    # the app (where uvicorn would log it), the request would raise here.
    # These tests fail before any query runs, so they need no database.
    async def no_connection():
        yield None

    app.dependency_overrides[get_db] = no_connection
    yield TestClient(app, raise_server_exceptions=True)
    app.dependency_overrides.pop(get_db, None)


def test_unexpected_errors_are_caught_and_logged_without_values(boom_route, plain_client, caplog):
    caplog.set_level(logging.DEBUG)

    response = plain_client.post("/boom/abc")  # would raise here if it escaped the app

    assert boom_route["raised"] == 1
    assert response.status_code == 500
    assert response.json() == {"detail": "internal_error"}
    for marker in (SECRET, CAUSE_SECRET):
        assert marker not in response.text
    assert_no_marker_logged(caplog)

    app_lines = [r for r in caplog.records if "Unhandled" in r.getMessage()]
    assert len(app_lines) == 1
    line = app_lines[0]
    assert line.getMessage() == "Unhandled FakeDbError on POST /boom/{item}"
    assert line.exc_info is None and line.exc_text is None
    # Nothing else logged the failure: no uvicorn traceback, no other error line.
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == [line]
    assert not [r for r in caplog.records if r.name.startswith("uvicorn")]


def test_query_string_is_not_logged_on_failure(boom_route, plain_client, caplog):
    caplog.set_level(logging.DEBUG)

    response = plain_client.get(f"/boom/abc?token={QUERY_SECRET}")

    assert response.status_code == 500
    assert QUERY_SECRET not in response.text
    assert_no_marker_logged(caplog)
    assert any(r.getMessage() == "Unhandled FakeDbError on GET /boom/{item}" for r in caplog.records)


def test_unmatched_route_still_404s(plain_client, caplog):
    caplog.set_level(logging.DEBUG)
    response = plain_client.get("/nope")
    assert response.status_code == 404
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_http_exceptions_are_unchanged(client, caplog):
    caplog.set_level(logging.DEBUG)
    subscribe(client, device="a", card="shared")
    body = {
        "et_hash": sha256_hex("card:shared"),
        "push_id_hash": sha256_hex("push:b"),
        "push_token": "push-token-b",
        "platform": "ios",
        "device_credential": sha256_hex("secret:b"),
    }
    taken = client.post("/subscribe", json=body)
    assert taken.status_code == 409
    assert taken.json() == {"detail": "code_in_use"}

    wrong = client.post(
        "/inbox",
        json={"push_id_hash": sha256_hex("push:a"), "device_credential": sha256_hex("secret:wrong")},
    )
    assert wrong.status_code == 403
    assert set(wrong.json()) == {"detail"} and isinstance(wrong.json()["detail"], str)
    assert not [r for r in caplog.records if "Unhandled" in r.getMessage()]


# ---- 422 ----

ALMOST_CREDENTIAL = "a" * 63 + "G"  # 64 chars, one not hex


def _post_raw(client, path, content):
    return client.post(path, content=content, headers={"content-type": "application/json"})


def _subscribe_body(**override):
    body = {
        "et_hash": sha256_hex("card:x"),
        "push_id_hash": sha256_hex("push:x"),
        "push_token": "tok",
        "platform": "ios",
        "device_credential": sha256_hex("secret:x"),
    }
    body.update(override)
    return body


def _assert_clean_422(response, forbidden):
    assert response.status_code == 422
    payload = response.json()
    assert list(payload) == ["detail"]
    assert payload["detail"], "expected at least one error"
    for err in payload["detail"]:
        assert set(err) == {"loc", "type", "msg"}
    for value in forbidden:
        assert value not in response.text


def test_validation_errors_do_not_echo_the_input(plain_client):
    # Almost-valid credential.
    r = plain_client.post("/subscribe", json=_subscribe_body(device_credential=ALMOST_CREDENTIAL))
    _assert_clean_422(r, [ALMOST_CREDENTIAL])
    assert r.json()["detail"][0]["loc"] == ["body", "device_credential"]
    assert r.json()["detail"][0]["type"] == "value_error"
    assert "64 lowercase hex" in r.json()["detail"][0]["msg"]  # our fixed text survives

    # Over-long string.
    long_payload = "Z9" * 3000
    delivery = {"et_hash": sha256_hex("card:x"), "encrypted_payload": long_payload}
    notify_body = {
        "sender_push_id_hash": sha256_hex("push:x"),
        "device_credential": sha256_hex("secret:x"),
        "campaign_id": str(uuid4()),
        "deliveries": [delivery],
    }
    r = plain_client.post("/notify", json=notify_body)
    _assert_clean_422(r, [long_payload])
    assert r.json()["detail"][0]["type"] == "string_too_long"

    # Wrong type.
    r = plain_client.post("/subscribe", json=_subscribe_body(push_token={"k": "WRONGTYPE-MARKER"}))
    _assert_clean_422(r, ["WRONGTYPE-MARKER"])

    # Malformed JSON.
    r = _post_raw(plain_client, "/subscribe", '{"push_token": "JSON-MARKER", oops')
    _assert_clean_422(r, ["JSON-MARKER", "oops"])

    # Unknown field is rejected; its value is not echoed.
    notify_body["deliveries"] = [
        {"et_hash": sha256_hex("card:x"), "encrypted_payload": "x", "scheduled_at": "SCHEDULED-MARKER"}
    ]
    r = plain_client.post("/notify", json=notify_body)
    _assert_clean_422(r, ["SCHEDULED-MARKER"])


def test_uuid_errors_do_not_quote_the_input(plain_client):
    base = {
        "push_id_hash": sha256_hex("push:x"),
        "device_credential": sha256_hex("secret:x"),
    }
    bad_ids = "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz"
    r = plain_client.post("/inbox/confirm", json={**base, "ids": [bad_ids]})
    _assert_clean_422(r, [bad_ids])
    for err in r.json()["detail"]:
        assert "found `" not in err["msg"]

    notify_body = {
        "sender_push_id_hash": base["push_id_hash"],
        "device_credential": base["device_credential"],
        "campaign_id": "12345678-1234-1234-1234-12345678901Q",
        "deliveries": [{"et_hash": sha256_hex("card:x"), "encrypted_payload": "x"}],
    }
    r = plain_client.post("/notify", json=notify_body)
    _assert_clean_422(r, ["901Q"])
    assert all("found `" not in e["msg"] for e in r.json()["detail"])


def test_validation_errors_are_not_logged_with_the_body(plain_client, caplog):
    caplog.set_level(logging.DEBUG)
    plain_client.post("/subscribe", json=_subscribe_body(device_credential=ALMOST_CREDENTIAL))
    assert_no_marker_logged(caplog, [ALMOST_CREDENTIAL])


# ---- cron ----


class StopLoop(BaseException):
    pass


@pytest.mark.parametrize(
    "runner, job",
    [
        (cron.run_dispatcher, "dispatch_wakeups"),
        (cron.run_cleanup, "cleanup_expired_subscriptions"),
    ],
)
def test_cron_failures_log_job_and_type_only(runner, job, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    async def failing():
        try:
            raise ValueError(CAUSE_SECRET)
        except ValueError as cause:
            raise FakeDbError(f"Key (credential_hash)=({SECRET})") from cause

    async def stop_after_one_iteration(_seconds):
        raise StopLoop

    monkeypatch.setattr(cron, job, failing)
    monkeypatch.setattr(cron.asyncio, "sleep", stop_after_one_iteration)

    with pytest.raises(StopLoop):
        asyncio.run(runner())

    assert_no_marker_logged(caplog)
    lines = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(lines) == 1
    assert lines[0].getMessage() == f"Job {job} failed: FakeDbError"
    assert lines[0].exc_info is None
