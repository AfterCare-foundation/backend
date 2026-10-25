# Anonymous daily counts: right numbers, no identifiers, never breaks the app.

import json

from app.cron import cleanup_expired_subscriptions
from tests.conftest import notify, pull_inbox, sha256_hex, subscribe, waiting
from tests.test_club_flow import _sql


def _stats() -> dict:
    rows = _sql("SELECT metric || '=' || value FROM daily_stats ORDER BY metric")
    return dict(line.split("=") for line in rows.splitlines() if line)


def test_a_normal_day_is_counted(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    notify(client, device="alice", card="pink")
    notes = pull_inbox(client, device="bob").json()["notifications"]
    client.post("/inbox/confirm", json={
        "push_id_hash": sha256_hex("push:bob"),
        "device_credential": sha256_hex("secret:bob"),
        "ids": [notes[0]["id"]],
    })

    counts = _stats()
    assert counts["devices_new"] == "2"
    assert counts["codes_joined"] == "2"
    assert counts["pairings_completed"] == "1"
    assert counts["notify_taps"] == "1"
    assert counts["messages_sent"] == "1"
    assert counts["pushes_ok"] == "1"
    assert counts["messages_collected"] == "1"


def test_rescans_do_not_count_twice(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    subscribe(client, device="bob", card="pink")

    counts = _stats()
    assert counts["codes_joined"] == "2"
    assert counts["pairings_completed"] == "1"


def test_a_notify_to_nobody_is_not_a_tap(client, sent):
    subscribe(client, device="alice", card="pink")
    notify(client, device="alice", card="pink")
    assert "notify_taps" not in _stats()


def test_delete_requests_are_counted(client, sent):
    subscribe(client, device="alice", card="pink")
    client.request("DELETE", "/subscribe", json={
        "push_id_hash": sha256_hex("push:alice"),
        "device_credential": sha256_hex("secret:alice"),
    })
    assert _stats()["delete_requests"] == "1"


def test_cleanup_counts_expiry_and_takes_a_snapshot(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    subscribe(client, device="alice", card="green")
    _sql("UPDATE token_subscriptions SET created_date = CURRENT_DATE - 181 "
         f"WHERE et_hash = '{sha256_hex('card:green')}'")
    notify(client, device="alice", card="pink")
    _sql("UPDATE mailbox SET created_date = CURRENT_DATE - 31")

    client.portal.call(cleanup_expired_subscriptions)
    counts = _stats()
    assert counts["subscriptions_expired"] == "1"
    assert counts["messages_expired"] == "1"
    assert counts["active_codes"] == "1"
    assert counts["active_devices"] == "2"
    assert counts["waiting_messages"] == "0"

    client.portal.call(cleanup_expired_subscriptions)  # twice a day is fine
    assert _stats()["active_codes"] == "1"


def test_the_stats_table_holds_only_names_and_numbers(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    notify(client, device="alice", card="pink")

    columns = _sql("SELECT column_name FROM information_schema.columns "
                   "WHERE table_name = 'daily_stats' ORDER BY ordinal_position")
    assert columns.split() == ["day", "metric", "value"]
    dump = json.dumps(_sql("SELECT * FROM daily_stats"))
    for label in ("alice", "bob", "pink"):
        assert sha256_hex(f"push:{label}") not in dump
        assert sha256_hex(f"card:{label}") not in dump


def test_a_stats_failure_never_breaks_the_request(client, sent):
    _sql("ALTER TABLE daily_stats RENAME TO daily_stats_off")
    try:
        subscribe(client, device="alice", card="pink")
        subscribe(client, device="bob", card="pink")
        response, _ = notify(client, device="alice", card="pink")
        assert response.status_code == 200
        assert waiting(client, device="bob") == ["ciphertext-for-pink"]
    finally:
        _sql("ALTER TABLE daily_stats_off RENAME TO daily_stats")
