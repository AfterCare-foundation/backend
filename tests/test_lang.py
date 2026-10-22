# Language of the generic push: 'en' (default) or 'de', anything else is English.

import asyncio
import logging

import httpx

from app.config import settings
from app.services import push
from tests.test_club_flow import _sql as sql
from tests.conftest import notify, sha256_hex, subscribe

DE = "Du hast eine neue Nachricht. Öffne die App, um sie zu lesen."
EN = "You have a new message. Open the app to read it."


def _lang_of(device: str) -> str:
    return sql(f"SELECT lang FROM devices WHERE push_id_hash = '{sha256_hex(f'push:{device}')}'")


def test_default_is_english_and_german_is_stored(client):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink", lang="de")
    assert _lang_of("alice") == "en"
    assert _lang_of("bob") == "de"


def test_other_values_become_english_and_are_never_echoed(client):
    for bad in ("fr", "DE", "", 5, ["de"]):
        body = subscribe(client, device="cara", card="pink", lang=bad)
        assert _lang_of("cara") == "en"
    response = client.post("/subscribe", json={**body, "lang": "<script>"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "script" not in response.text


def test_omitting_lang_keeps_the_stored_language(client):
    subscribe(client, device="bob", card="pink", lang="de")
    subscribe(client, device="bob", card="green")
    assert _lang_of("bob") == "de"


def test_update_push_id_sets_the_language(client):
    body = subscribe(client, device="bob", card="pink")
    response = client.post("/update-push-id", json={
        "old_push_id_hash": body["push_id_hash"],
        "new_push_id_hash": sha256_hex("push:bob2"),
        "new_push_token": "new-token",
        "new_platform": "ios",
        "device_credential": body["device_credential"],
        "lang": "de",
    })
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert _lang_of("bob2") == "de"


def test_notify_and_retry_use_the_recipients_language(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink", lang="de")
    subscribe(client, device="cara", card="green")
    subscribe(client, device="alice", card="green")

    notify(client, device="alice", card="pink")
    notify(client, device="alice", card="green")
    assert {c["push_token"]: c["lang"] for c in sent} == {
        "push-token-bob": "de",
        "push-token-cara": "en",
    }


def test_retry_dispatcher_passes_the_language(client, sent):
    from app.cron import dispatch_wakeups

    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink", lang="de")
    notify(client, device="alice", card="pink")
    sql("UPDATE mailbox SET wake_pending = TRUE")
    sent.clear()

    client.portal.call(dispatch_wakeups)
    assert [c["lang"] for c in sent] == ["de"]


def test_text_and_title_per_language():
    assert push.PUSH_TITLE == "AfterCare"
    assert push.alert_body("en") == EN
    assert push.alert_body("de") == DE
    assert push.alert_body("fr") == EN


def test_apns_payload_has_title_and_the_right_text(monkeypatch, caplog):
    monkeypatch.setattr(push, "_make_apns_jwt", lambda: "jwt")
    monkeypatch.setattr(settings, "push_stub_mode", False)
    seen = []

    class Fake:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw):
            seen.append(kw["json"])
            return httpx.Response(200)

    monkeypatch.setattr(push.httpx, "AsyncClient", Fake)
    with caplog.at_level(logging.DEBUG):  # nothing here may log the language
        asyncio.run(push.send_push("tok", "ios", lang="de"))
        asyncio.run(push.send_push("tok", "ios", lang="xx"))

    assert seen[0]["aps"]["alert"] == {"title": "AfterCare", "body": DE}
    assert seen[1]["aps"]["alert"] == {"title": "AfterCare", "body": EN}
