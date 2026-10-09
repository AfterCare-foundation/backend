# Club flow: two devices on one card, notify, self-exclusion, rate limits,
# mailbox, wake-up retries, dead tokens.

from uuid import uuid4

from app.services.push import PUSH_DEAD_TOKEN, PUSH_FAILED, PUSH_OK
from tests.conftest import notify, sha256_hex, subscribe, waiting


def _sql(statement: str) -> str:
    import os, subprocess
    from tests.conftest import PGPASSWORD

    return subprocess.check_output(
        ["psql", "-h", "localhost", "-U", "aftercare_dev", "-d", "aftercare_dev", "-t", "-A", "-c", statement],
        env={**os.environ, "PGPASSWORD": PGPASSWORD}, text=True,
    ).strip()


def _multi_notify(client, *, device, cards, campaign_id):
    return client.post(
        "/notify",
        json={
            "sender_push_id_hash": sha256_hex(f"push:{device}"),
            "device_credential": sha256_hex(f"secret:{device}"),
            "campaign_id": campaign_id,
            "deliveries": [
                {"et_hash": sha256_hex(f"card:{c}"), "encrypted_payload": f"ciphertext-for-{c}"}
                for c in cards
            ],
        },
    )


def _three_contacts(client):
    for card, other in (("pink", "bob"), ("green", "cara"), ("blue", "dan")):
        subscribe(client, device="alice", card=card)
        subscribe(client, device=other, card=card)


def _two_codes_with_same_person(client):
    for card in ("pink", "green"):
        subscribe(client, device="bob", card=card)
        subscribe(client, device="alice", card=card)


def _run_dispatcher(client):
    from app.cron import dispatch_wakeups

    client.portal.call(dispatch_wakeups)


def _answering_push(monkeypatch, answer):
    """Replace the push with one that answers `answer(push_token)` and records the calls."""
    calls = []

    async def fake(push_token, platform):
        calls.append(push_token)
        return answer(push_token)

    monkeypatch.setattr("app.services.notify_flow.send_push", fake)
    monkeypatch.setattr("app.cron.send_push", fake)
    return calls


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_two_devices_notify_excludes_sender(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink", platform="android")

    response, _ = notify(client, device="alice", card="pink")
    assert response.status_code == 200, response.text
    assert response.json()["pushed"] == 1

    assert [call["push_token"] for call in sent] == ["push-token-bob"]
    assert waiting(client, device="bob") == ["ciphertext-for-pink"]
    assert waiting(client, device="alice") == []  # the sender gets nothing


def test_cannot_notify_card_you_did_not_scan(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="blue")

    response, _ = notify(client, device="alice", card="blue")
    assert response.status_code == 403
    assert sent == []
    assert waiting(client, device="bob") == []


def test_wrong_device_credential_rejected(client):
    subscribe(client, device="alice", card="pink")
    response = client.post(
        "/notify",
        json={
            "sender_push_id_hash": sha256_hex("push:alice"),
            "device_credential": sha256_hex("secret:eve"),
            "campaign_id": str(uuid4()),
            "deliveries": [{"et_hash": sha256_hex("card:pink"), "encrypted_payload": "x"}],
        },
    )
    assert response.status_code == 403


def test_one_campaign_can_cover_several_contacts_in_one_request(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    subscribe(client, device="alice", card="green")
    subscribe(client, device="cara", card="green")

    response = _multi_notify(client, device="alice", cards=["pink", "green"], campaign_id=str(uuid4()))
    assert response.status_code == 200, response.text
    assert response.json()["pushed"] == 2
    assert len(sent) == 2


def test_campaign_id_can_only_be_used_once(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")

    first, body = notify(client, device="alice", card="pink")
    assert first.status_code == 200

    again = client.post("/notify", json=body)  # same campaign_id
    assert again.status_code == 409
    assert again.json()["detail"] == "campaign_already_used"
    assert len(sent) == 1  # bob was not pushed twice
    assert waiting(client, device="bob") == ["ciphertext-for-pink"]  # nor stored twice


def test_three_campaigns_in_a_day_succeed_and_the_fourth_is_blocked(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")

    for _ in range(3):  # e.g. three infections in one sitting
        response, _body = notify(client, device="alice", card="pink")
        assert response.status_code == 200, response.text

    fourth, _ = notify(client, device="alice", card="pink")
    assert fourth.status_code == 429
    assert fourth.json()["detail"] == "At most 3 campaigns per day"
    assert len(sent) == 3


def test_empty_notify_does_not_consume_rate_limit(client, sent):
    """Only Alice scanned: nobody else to notify. Campaign must not be recorded."""
    subscribe(client, device="alice", card="pink")

    first, _ = notify(client, device="alice", card="pink")
    assert first.status_code == 200
    assert first.json()["pushed"] == 0
    assert sent == []

    subscribe(client, device="bob", card="pink")
    second, _ = notify(client, device="alice", card="pink")
    assert second.status_code == 200, second.text
    assert second.json()["pushed"] == 1
    assert len(sent) == 1


def test_six_campaigns_in_thirty_days_blocks_the_seventh(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")

    alice_push = sha256_hex("push:alice")
    # Six older campaigns spread over several days, none within the last 24 hours.
    _sql(f"""
        INSERT INTO notification_campaigns (campaign_id, push_id_hash, created_at) VALUES
          ('11111111-1111-1111-1111-111111111111', '{alice_push}', NOW() - INTERVAL '28 days'),
          ('22222222-2222-2222-2222-222222222222', '{alice_push}', NOW() - INTERVAL '23 days'),
          ('33333333-3333-3333-3333-333333333333', '{alice_push}', NOW() - INTERVAL '18 days'),
          ('44444444-4444-4444-4444-444444444444', '{alice_push}', NOW() - INTERVAL '13 days'),
          ('55555555-5555-5555-5555-555555555555', '{alice_push}', NOW() - INTERVAL '8 days'),
          ('66666666-6666-6666-6666-666666666666', '{alice_push}', NOW() - INTERVAL '2 days');
    """)

    response, _ = notify(client, device="alice", card="pink")
    assert response.status_code == 429
    assert response.json()["detail"] == "At most 6 campaigns per 30 days"
    assert sent == []


def test_third_device_cannot_join_a_code(client):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")

    third = client.post(
        "/subscribe",
        json={
            "et_hash": sha256_hex("card:pink"),
            "push_id_hash": sha256_hex("push:cara"),
            "push_token": "push-token-cara",
            "platform": "ios",
            "device_credential": sha256_hex("secret:cara"),
        },
    )
    assert third.status_code == 409
    assert third.json()["detail"] == "code_in_use"


def test_holder_can_rescan_own_code(client):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    subscribe(client, device="alice", card="pink")  # must still be 200


def test_reinstalled_phone_keeps_its_slot_and_its_messages_via_update_push_id(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    notify(client, device="alice", card="pink")  # a message is waiting for bob

    moved = client.post(
        "/update-push-id",
        json={
            "old_push_id_hash": sha256_hex("push:bob"),
            "new_push_id_hash": sha256_hex("push:bob2"),
            "new_push_token": "push-token-bob2",
            "new_platform": "ios",
            "device_credential": sha256_hex("secret:bob"),
        },
    )
    assert moved.status_code == 200, moved.text

    # The new identity is on the code, so it can re-scan; a stranger still cannot.
    again = client.post(
        "/subscribe",
        json={
            "et_hash": sha256_hex("card:pink"),
            "push_id_hash": sha256_hex("push:bob2"),
            "push_token": "push-token-bob2",
            "platform": "ios",
            "device_credential": sha256_hex("secret:bob"),
        },
    )
    assert again.status_code == 200, again.text

    # The waiting message moved with the device.
    inbox = client.post(
        "/inbox",
        json={"push_id_hash": sha256_hex("push:bob2"), "device_credential": sha256_hex("secret:bob")},
    )
    assert [n["enc"] for n in inbox.json()["notifications"]] == ["ciphertext-for-pink"]


def test_bad_item_rejects_whole_request_and_sends_nothing(client, sent):
    _three_contacts(client)
    subscribe(client, device="erin", card="stranger")  # alice is not on this code

    response = _multi_notify(
        client, device="alice", cards=["pink", "stranger", "blue"], campaign_id=str(uuid4())
    )
    assert response.status_code == 403
    assert sent == []  # not even the valid first contact
    assert waiting(client, device="bob") == []


def test_one_failed_push_does_not_stop_the_rest_and_the_server_retries_it(client, monkeypatch):
    _three_contacts(client)
    broken = {"on": True}
    calls = _answering_push(
        monkeypatch,
        lambda token: PUSH_FAILED if broken["on"] and token == "push-token-cara" else PUSH_OK,
    )

    response = _multi_notify(client, device="alice", cards=["pink", "green", "blue"], campaign_id=str(uuid4()))
    body = response.json()
    assert response.status_code == 200
    assert body["pushed"] == 2
    assert body["retrying"] == 1

    # Every message is already in its mailbox, whatever happened to the push.
    assert waiting(client, device="bob") == ["ciphertext-for-pink"]
    assert waiting(client, device="cara") == ["ciphertext-for-green"]
    assert waiting(client, device="dan") == ["ciphertext-for-blue"]

    # Still broken: the dispatcher tries only the device that was not woken.
    calls.clear()
    _run_dispatcher(client)
    assert calls == ["push-token-cara"]

    # Provider recovers: woken once, then left alone.
    broken["on"] = False
    calls.clear()
    _run_dispatcher(client)
    _run_dispatcher(client)
    assert calls == ["push-token-cara"]


def test_all_pushes_failing_still_uses_one_slot_and_is_retried(client, monkeypatch):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    broken = {"on": True}
    calls = _answering_push(monkeypatch, lambda token: PUSH_FAILED if broken["on"] else PUSH_OK)

    first, body = notify(client, device="alice", card="pink")
    assert first.json()["retrying"] == 1
    assert first.json()["pushed"] == 0

    again = client.post("/notify", json=body)  # same campaign_id
    assert again.status_code == 409  # used once; the server owns the retry

    broken["on"] = False
    calls.clear()
    _run_dispatcher(client)
    assert calls == ["push-token-bob"]
    assert waiting(client, device="bob") == ["ciphertext-for-pink"]


def test_same_device_on_two_contacts_is_woken_once_and_gets_both_ciphertexts(client, sent):
    _two_codes_with_same_person(client)

    response = _multi_notify(client, device="bob", cards=["pink", "green"], campaign_id=str(uuid4()))
    body = response.json()
    assert body["pushed"] == 1
    assert body["contacts"] == 2  # both contacts count as handled
    assert len(sent) == 1
    # Nothing is dropped: a second STI for the same person must still arrive.
    assert waiting(client, device="alice") == ["ciphertext-for-pink", "ciphertext-for-green"]


def test_separate_campaigns_both_deliver(client, sent):
    _two_codes_with_same_person(client)
    first, _ = notify(client, device="bob", card="pink")
    assert first.json()["pushed"] == 1

    # Next day is simulated by moving the rate limit rows into the past.
    _sql("UPDATE notification_campaigns SET created_at = NOW() - INTERVAL '2 days'")
    second, _ = notify(client, device="bob", card="green")
    assert second.json()["pushed"] == 1
    assert len(sent) == 2
    assert waiting(client, device="alice") == ["ciphertext-for-pink", "ciphertext-for-green"]


def test_scheduled_at_is_rejected(client, sent):
    from datetime import datetime, timedelta, timezone

    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    later = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    response = client.post(
        "/notify",
        json={
            "sender_push_id_hash": sha256_hex("push:alice"),
            "device_credential": sha256_hex("secret:alice"),
            "campaign_id": str(uuid4()),
            "deliveries": [
                {"et_hash": sha256_hex("card:pink"), "encrypted_payload": "x", "scheduled_at": later}
            ],
        },
    )
    assert response.status_code == 422
    assert sent == []
    assert client.post("/schedule", json={}).status_code == 404


def test_dead_token_is_marked_and_the_message_still_waits(client, monkeypatch):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    _answering_push(monkeypatch, lambda token: PUSH_DEAD_TOKEN)

    response, _ = notify(client, device="alice", card="pink")
    assert response.json()["retrying"] == 1

    bob, alice = sha256_hex("push:bob"), sha256_hex("push:alice")
    assert _sql(f"SELECT dead_since IS NOT NULL FROM token_subscriptions WHERE push_id_hash = '{bob}'") == "t"
    assert _sql(f"SELECT dead_since IS NOT NULL FROM token_subscriptions WHERE push_id_hash = '{alice}'") == "f"
    # The message does not depend on the token: it is in the mailbox.
    assert waiting(client, device="bob") == ["ciphertext-for-pink"]


def test_rescan_clears_the_dead_mark(client, monkeypatch):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    _answering_push(monkeypatch, lambda token: PUSH_DEAD_TOKEN)
    notify(client, device="alice", card="pink")

    subscribe(client, device="bob", card="pink")  # the app came back with a fresh token
    bob = sha256_hex("push:bob")
    assert _sql(f"SELECT dead_since IS NULL FROM token_subscriptions WHERE push_id_hash = '{bob}'") == "t"


def test_dispatcher_does_not_call_a_dead_token(client, monkeypatch):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    _answering_push(monkeypatch, lambda token: PUSH_DEAD_TOKEN)
    notify(client, device="alice", card="pink")

    calls = _answering_push(monkeypatch, lambda token: PUSH_OK)
    _run_dispatcher(client)

    assert calls == []
    assert waiting(client, device="bob") == ["ciphertext-for-pink"]


def test_dispatcher_stops_retrying_a_wake_up_after_about_a_day(client, monkeypatch):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    _answering_push(monkeypatch, lambda token: PUSH_FAILED)
    notify(client, device="alice", card="pink")

    _sql("UPDATE mailbox SET created_date = CURRENT_DATE - 3")
    calls = _answering_push(monkeypatch, lambda token: PUSH_OK)
    _run_dispatcher(client)

    assert calls == []
    assert waiting(client, device="bob") == ["ciphertext-for-pink"]  # still collectable


def test_cleanup_removes_long_dead_subscriptions_and_frees_the_slot(client):
    from app.cron import cleanup_expired_subscriptions

    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    subscribe(client, device="cara", card="green")
    bob, cara = sha256_hex("push:bob"), sha256_hex("push:cara")
    _sql(f"UPDATE token_subscriptions SET dead_since = CURRENT_DATE - 40 WHERE push_id_hash = '{bob}'")
    _sql(f"UPDATE token_subscriptions SET dead_since = CURRENT_DATE - 3 WHERE push_id_hash = '{cara}'")

    client.portal.call(cleanup_expired_subscriptions)

    left = set(_sql("SELECT push_id_hash FROM token_subscriptions").split())
    assert bob not in left            # dead 40 days: gone
    assert cara in left               # dead 3 days: still within the grace period

    subscribe(client, device="dan", card="pink")  # the freed slot can be used


def test_a_device_can_add_at_most_30_new_connections_per_day(client):
    for i in range(30):
        subscribe(client, device="busy", card=f"card-{i}")

    over = client.post(
        "/subscribe",
        json={
            "et_hash": sha256_hex("card:card-30"),
            "push_id_hash": sha256_hex("push:busy"),
            "push_token": "push-token-busy",
            "platform": "ios",
            "device_credential": sha256_hex("secret:busy"),
        },
    )
    assert over.status_code == 429
    assert over.json()["detail"] == "At most 30 new connections per day"

    subscribe(client, device="busy", card="card-0")   # re-scanning a code it already has is fine
    subscribe(client, device="other", card="card-30")  # other devices are unaffected
