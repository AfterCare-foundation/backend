# Request bodies are capped at 128 KB (app/body_limit.py).
#
# Every 413 test also proves the route did not run: the /subscribe bodies are
# valid apart from a padding field that pydantic would ignore, so without the
# cap they would be stored.

import asyncio
import json
import logging

from app.body_limit import MAX_REQUEST_BODY, LimitRequestBody
from app.main import CatchUnexpectedErrors, app
from tests.conftest import notify, sha256_hex, sql, subscribe, push_token_for

TOO_LARGE = {"detail": "body_too_large"}


def subscribe_body_of_size(size: int, marker: str = "") -> bytes:
    """A valid /subscribe JSON body of exactly `size` bytes (padding is an unknown field)."""
    fields = {
        "et_hash": sha256_hex("card:big"),
        "push_id_hash": sha256_hex("push:big"),
        "push_token": push_token_for("big"),
        "platform": "ios",
        "device_credential": sha256_hex("secret:big"),
        "pad": "",
    }
    base = len(json.dumps(fields, separators=(",", ":")).encode())
    fields["pad"] = marker + "x" * (size - base - len(marker))
    body = json.dumps(fields, separators=(",", ":")).encode()
    assert len(body) == size
    return body


def stored() -> str:
    return sql("SELECT count(*) FROM token_subscriptions")


def post_raw(client, body, headers=None):
    return client.post(
        "/subscribe",
        content=body,
        headers={"content-type": "application/json", **(headers or {})},
    )


def test_declared_content_length_over_cap_is_refused(client):
    body = subscribe_body_of_size(MAX_REQUEST_BODY + 1)
    request = client.build_request("POST", "/subscribe", content=body,
                                   headers={"content-type": "application/json"})
    assert request.headers["content-length"] == str(MAX_REQUEST_BODY + 1)
    response = client.send(request)
    assert response.status_code == 413
    assert response.json() == TOO_LARGE
    assert stored() == "0"


def test_chunked_body_without_content_length_over_cap_is_refused(client):
    body = subscribe_body_of_size(MAX_REQUEST_BODY + 1)

    def chunks():
        for i in range(0, len(body), 8192):
            yield body[i : i + 8192]

    request = client.build_request("POST", "/subscribe", content=chunks(),
                                   headers={"content-type": "application/json"})
    # httpx sends a generator as chunked, with no content-length at all.
    assert "content-length" not in request.headers
    assert request.headers["transfer-encoding"] == "chunked"
    response = client.send(request)
    assert response.status_code == 413
    assert response.json() == TOO_LARGE
    assert stored() == "0"


def test_body_exactly_at_cap_is_accepted(client):
    body = subscribe_body_of_size(MAX_REQUEST_BODY)
    response = post_raw(client, body)
    assert response.status_code == 200, response.text
    assert stored() == "1"


def test_chunked_body_exactly_at_cap_is_accepted(client):
    body = subscribe_body_of_size(MAX_REQUEST_BODY)

    def chunks():
        for i in range(0, len(body), 8192):
            yield body[i : i + 8192]

    request = client.build_request("POST", "/subscribe", content=chunks(),
                                   headers={"content-type": "application/json"})
    assert "content-length" not in request.headers
    response = client.send(request)
    assert response.status_code == 200, response.text
    assert stored() == "1"


def test_worst_legal_notify_fits_under_the_cap(client):
    sender = "sender"
    subscribe(client, device=sender, card="c0")
    body = {
        "sender_push_id_hash": sha256_hex(f"push:{sender}"),
        "device_credential": sha256_hex(f"secret:{sender}"),
        "campaign_id": "12345678-1234-4234-8234-123456789abc",
        "deliveries": [
            {"et_hash": sha256_hex(f"card:c{i}"), "encrypted_payload": "A" * 380}
            for i in range(100)
        ],
    }
    assert len(json.dumps(body).encode()) < MAX_REQUEST_BODY

    response = client.post("/notify", json=body)
    # The route ran: 99 of the 100 codes are not the sender's, so they are
    # reported as skipped (notify_flow.run_deliveries); nobody else is on c0.
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "ok"
    assert len(result["skipped"]) == 100
    assert {s["reason"] for s in result["skipped"]} == {"not_subscribed", "no_recipient"}


def test_normal_notify_still_works(client, sent):
    subscribe(client, device="a", card="shared")
    subscribe(client, device="b", card="shared")
    response, _ = notify(client, device="a", card="shared")
    assert response.status_code == 200
    assert response.json()["contacts"] == 1
    assert len(sent) == 1


def test_cap_is_inside_the_error_catcher():
    # The last middleware added is outermost, so CatchUnexpectedErrors must come
    # before LimitRequestBody in the list.
    classes = [m.cls for m in app.user_middleware]
    assert classes.index(CatchUnexpectedErrors) < classes.index(LimitRequestBody)


def test_no_log_record_holds_the_body_on_413(client, caplog):
    marker = "SECRETMARKER-7f3a"
    body = subscribe_body_of_size(MAX_REQUEST_BODY + 1, marker=marker)
    with caplog.at_level(logging.DEBUG):
        response = post_raw(client, body)
    assert response.status_code == 413
    assert response.json() == TOO_LARGE
    assert stored() == "0"
    assert marker not in caplog.text
    assert marker not in response.text
    for record in caplog.records:
        assert marker not in record.getMessage()


# Direct ASGI tests: what httpx cannot send (a lying Content-Length, a
# disconnect in the middle of a body).


def run_middleware(scope, messages):
    """Drive LimitRequestBody with fake receive messages; return what the app and send saw."""
    seen = {"app_called": False, "app_messages": [], "sent": []}
    queue = list(messages)

    async def receive():
        if queue:
            return queue.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        seen["sent"].append(message)

    async def fake_app(scope, receive, send):
        seen["app_called"] = True
        seen["app_messages"].append(await receive())
        seen["app_messages"].append(await receive())

    asyncio.run(LimitRequestBody(fake_app)(scope, receive, send))
    seen["remaining"] = queue
    return seen


def http_scope(content_length=None):
    headers = []
    if content_length is not None:
        headers.append((b"content-length", content_length))
    return {"type": "http", "method": "POST", "path": "/subscribe", "headers": headers}


def test_lying_short_content_length_is_caught_by_counting():
    third = (MAX_REQUEST_BODY + 100) // 3 + 1
    messages = [
        {"type": "http.request", "body": b"x" * third, "more_body": True},
        {"type": "http.request", "body": b"x" * third, "more_body": True},
        {"type": "http.request", "body": b"x" * third, "more_body": False},
    ]
    seen = run_middleware(http_scope(b"10"), messages)
    assert not seen["app_called"]
    assert seen["sent"][0]["type"] == "http.response.start"
    assert seen["sent"][0]["status"] == 413
    assert json.loads(seen["sent"][1]["body"]) == TOO_LARGE


def test_oversized_body_stops_reading():
    big = b"x" * (MAX_REQUEST_BODY + 1)
    messages = [
        {"type": "http.request", "body": big, "more_body": True},
        {"type": "http.request", "body": b"never read", "more_body": False},
    ]
    seen = run_middleware(http_scope(), messages)
    assert seen["sent"][0]["status"] == 413
    assert len(seen["remaining"]) == 1


def test_disconnect_mid_body_sends_nothing():
    messages = [
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.disconnect"},
    ]
    seen = run_middleware(http_scope(b"100"), messages)
    assert seen["sent"] == []
    assert not seen["app_called"]


def test_unparsable_content_length_is_unknown_and_body_is_replayed():
    messages = [
        {"type": "http.request", "body": b"hel", "more_body": True},
        {"type": "http.request", "body": b"lo", "more_body": False},
    ]
    seen = run_middleware(http_scope(b"abc"), messages)
    assert seen["app_called"]
    assert seen["sent"] == []
    assert seen["app_messages"][0] == {
        "type": "http.request", "body": b"hello", "more_body": False,
    }
    # After the body, the app still sees what the server sends next.
    assert seen["app_messages"][1] == {"type": "http.disconnect"}


def test_negative_content_length_is_unknown():
    messages = [{"type": "http.request", "body": b"ok", "more_body": False}]
    seen = run_middleware(http_scope(b"-5"), messages)
    assert seen["app_called"]
    assert seen["app_messages"][0]["body"] == b"ok"


def test_non_http_scope_passes_through_untouched():
    calls = []

    async def fake_app(scope, receive, send):
        calls.append((scope, receive, send))

    async def receive():
        raise AssertionError("must not be read")

    async def send(message):
        raise AssertionError("must not send")

    scope = {"type": "lifespan"}
    asyncio.run(LimitRequestBody(fake_app)(scope, receive, send))
    assert calls == [(scope, receive, send)]
