# Club flow: two devices on one card, notify, self-exclusion, rate limits.

from uuid import uuid4

from tests.conftest import notify, pull_inbox, sha256_hex, subscribe


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_two_devices_notify_excludes_sender(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink", platform="android")

    response, _ = notify(client, device="alice", card="pink")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pushed"] == 1

    assert len(sent) == 1
    recipients = sent[0]["recipients"]
    tokens = {row["push_token"] for row in recipients}
    assert tokens == {"push-token-bob"}
    assert sent[0]["enc"] == "ciphertext-for-pink"


def test_cannot_notify_card_you_did_not_scan(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="blue")

    response, _ = notify(client, device="alice", card="blue")
    assert response.status_code == 403
    assert sent == []


def test_wrong_device_credential_rejected(client):
    subscribe(client, device="alice", card="pink")
    response = client.post(
        "/notify",
        json={
            "sender_push_id_hash": sha256_hex("push:alice"),
            "device_credential": sha256_hex("secret:eve"),
            "campaign_id": str(uuid4()),
            "deliveries": [
                {
                    "et_hash": sha256_hex("card:pink"),
                    "encrypted_payload": "x",
                }
            ],
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
    """Only Alice scanned — nobody else to push. Campaign must not be recorded."""
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
    import os
    import subprocess
    from tests.conftest import PGPASSWORD

    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")

    alice_push = sha256_hex("push:alice")
    env = os.environ.copy()
    env["PGPASSWORD"] = PGPASSWORD
    # Six older campaigns spread over several days, none within the last 24 hours.
    sql = f"""
        INSERT INTO notification_campaigns (campaign_id, push_id_hash, created_at) VALUES
          ('11111111-1111-1111-1111-111111111111', '{alice_push}', NOW() - INTERVAL '28 days'),
          ('22222222-2222-2222-2222-222222222222', '{alice_push}', NOW() - INTERVAL '23 days'),
          ('33333333-3333-3333-3333-333333333333', '{alice_push}', NOW() - INTERVAL '18 days'),
          ('44444444-4444-4444-4444-444444444444', '{alice_push}', NOW() - INTERVAL '13 days'),
          ('55555555-5555-5555-5555-555555555555', '{alice_push}', NOW() - INTERVAL '8 days'),
          ('66666666-6666-6666-6666-666666666666', '{alice_push}', NOW() - INTERVAL '2 days');
    """
    subprocess.check_call(
        [
            "psql", "-h", "localhost", "-U", "aftercare_dev", "-d", "aftercare_dev",
            "-v", "ON_ERROR_STOP=1", "-c", sql,
        ],
        env=env,
        stdout=subprocess.DEVNULL,
    )

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


def test_reinstalled_phone_keeps_its_slot_via_update_push_id(client):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")

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


def test_bad_item_rejects_whole_request_and_sends_nothing(client, sent):
    _three_contacts(client)
    subscribe(client, device="erin", card="stranger")  # alice is not on this code

    response = _multi_notify(
        client, device="alice", cards=["pink", "stranger", "blue"], campaign_id=str(uuid4())
    )
    assert response.status_code == 403
    assert sent == []  # not even the valid first contact


def _run_dispatcher(client):
    from app.cron import dispatch_pending_notifications

    client.portal.call(dispatch_pending_notifications)


def test_one_failed_push_does_not_stop_the_rest_and_the_server_retries_it(client, monkeypatch):
    _three_contacts(client)
    delivered = []
    broken = {"on": True}

    async def flaky(recipient, payloads):
        if broken["on"] and payloads == ["ciphertext-for-green"]:
            return -1  # provider rejected this one
        delivered.extend(payloads)
        return 1

    monkeypatch.setattr("app.services.notify_flow.send_bundle", flaky)
    monkeypatch.setattr("app.cron.send_bundle", flaky)

    response = _multi_notify(client, device="alice", cards=["pink", "green", "blue"], campaign_id=str(uuid4()))
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["pushed"] == 2
    assert body["retrying"] == 1
    assert sorted(delivered) == ["ciphertext-for-blue", "ciphertext-for-pink"]

    # Still broken: the dispatcher tries again and keeps it queued.
    _run_dispatcher(client)
    assert "ciphertext-for-green" not in delivered

    # Provider recovers: the next dispatcher run delivers it, only once.
    broken["on"] = False
    _run_dispatcher(client)
    _run_dispatcher(client)
    assert sorted(delivered) == [
        "ciphertext-for-blue", "ciphertext-for-green", "ciphertext-for-pink",
    ]


def test_all_pushes_failing_still_uses_one_slot_and_is_retried(client, monkeypatch):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    delivered = []
    broken = {"on": True}

    async def sometimes(recipient, payloads):
        if broken["on"]:
            return -1
        delivered.extend(payloads)
        return 1

    monkeypatch.setattr("app.services.notify_flow.send_bundle", sometimes)
    monkeypatch.setattr("app.cron.send_bundle", sometimes)

    first, body = notify(client, device="alice", card="pink")
    assert first.json()["retrying"] == 1
    assert first.json()["pushed"] == 0

    again = client.post("/notify", json=body)  # same campaign_id
    assert again.status_code == 409  # used once; the server owns the retry

    broken["on"] = False
    _run_dispatcher(client)
    assert delivered == ["ciphertext-for-pink"]


def _two_codes_with_same_person(client):
    for card in ("pink", "green"):
        subscribe(client, device="bob", card=card)
        subscribe(client, device="alice", card=card)


def test_same_device_on_two_contacts_gets_one_push_with_both_ciphertexts(client, sent):
    _two_codes_with_same_person(client)

    response = _multi_notify(client, device="bob", cards=["pink", "green"], campaign_id=str(uuid4()))
    body = response.json()
    assert body["status"] == "ok"
    assert body["pushed"] == 1
    assert body["contacts"] == 2  # both contacts count as handled
    assert len(sent) == 1
    # Nothing is dropped: a second STI for the same person must still arrive.
    assert sent[0]["payloads"] == ["ciphertext-for-pink", "ciphertext-for-green"]


def test_dedupe_applies_to_the_dev_inbox_too(client):
    _two_codes_with_same_person(client)
    _multi_notify(client, device="bob", cards=["pink", "green"], campaign_id=str(uuid4()))

    notes = pull_inbox(client, device="alice").json()["notifications"]
    assert len(notes) == 1
    assert notes[0]["enc"] == "ciphertext-for-pink"
    assert notes[0]["more"] == ["ciphertext-for-green"]


def test_separate_campaigns_both_deliver(client, sent):
    _two_codes_with_same_person(client)
    first, _ = notify(client, device="bob", card="pink")
    assert first.json()["pushed"] == 1

    # Next day is simulated by clearing the rate limit rows.
    from tests.conftest import PGPASSWORD  # noqa: F401
    import os, subprocess
    env = {**os.environ, "PGPASSWORD": PGPASSWORD}
    subprocess.check_call(
        ["psql", "-h", "localhost", "-U", "aftercare_dev", "-d", "aftercare_dev",
         "-c", "UPDATE notification_campaigns SET created_at = NOW() - INTERVAL '2 days'"],
        env=env, stdout=subprocess.DEVNULL,
    )
    second, _ = notify(client, device="bob", card="green")
    assert second.json()["pushed"] == 1
    assert len(sent) == 2


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


def test_retries_for_the_same_device_are_sent_as_one_push(client, monkeypatch):
    _two_codes_with_same_person(client)
    broken = {"on": True}

    async def flaky_real_push(recipient, payloads):
        if broken["on"]:
            return -1
        from app.services.push import PUSH_OK, send_push
        result = await send_push(recipient["push_token"], recipient["platform"], payloads, recipient["push_id_hash"])
        return 1 if result == PUSH_OK else -1

    monkeypatch.setattr("app.services.notify_flow.send_bundle", flaky_real_push)
    monkeypatch.setattr("app.cron.send_bundle", flaky_real_push)

    response = _multi_notify(client, device="bob", cards=["pink", "green"], campaign_id=str(uuid4()))
    assert response.json()["retrying"] == 2

    broken["on"] = False
    _run_dispatcher(client)

    notes = pull_inbox(client, device="alice").json()["notifications"]
    assert len(notes) == 1  # one buzz
    assert [notes[0]["enc"], *notes[0]["more"]] == ["ciphertext-for-pink", "ciphertext-for-green"]


def _sql(statement: str) -> str:
    import os, subprocess
    from tests.conftest import PGPASSWORD

    return subprocess.check_output(
        ["psql", "-h", "localhost", "-U", "aftercare_dev", "-d", "aftercare_dev", "-t", "-A", "-c", statement],
        env={**os.environ, "PGPASSWORD": PGPASSWORD}, text=True,
    ).strip()


def test_dead_token_is_marked_and_the_push_is_still_retried(client, monkeypatch):
    from app.services.push import BUNDLE_DEAD_TOKEN

    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")

    async def dead(recipient, payloads):
        return BUNDLE_DEAD_TOKEN

    monkeypatch.setattr("app.services.notify_flow.send_bundle", dead)
    response, _ = notify(client, device="alice", card="pink")
    assert response.json()["retrying"] == 1  # the app may fix its token, so we retry

    bob = sha256_hex("push:bob")
    assert _sql(f"SELECT dead_since IS NOT NULL FROM token_subscriptions WHERE push_id_hash = '{bob}'") == "t"
    alice = sha256_hex("push:alice")
    assert _sql(f"SELECT dead_since IS NOT NULL FROM token_subscriptions WHERE push_id_hash = '{alice}'") == "f"


def test_rescan_clears_the_dead_mark(client, monkeypatch):
    from app.services.push import BUNDLE_DEAD_TOKEN

    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")

    async def dead(recipient, payloads):
        return BUNDLE_DEAD_TOKEN

    monkeypatch.setattr("app.services.notify_flow.send_bundle", dead)
    notify(client, device="alice", card="pink")

    subscribe(client, device="bob", card="pink")  # the app came back with a fresh token
    bob = sha256_hex("push:bob")
    assert _sql(f"SELECT dead_since IS NULL FROM token_subscriptions WHERE push_id_hash = '{bob}'") == "t"


def test_cleanup_removes_long_dead_subscriptions_and_frees_the_slot(client):
    from app.cron import cleanup_expired_subscriptions

    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    subscribe(client, device="cara", card="green")
    bob, cara = sha256_hex("push:bob"), sha256_hex("push:cara")
    _sql(f"UPDATE token_subscriptions SET dead_since = CURRENT_DATE - 30 WHERE push_id_hash = '{bob}'")
    _sql(f"UPDATE token_subscriptions SET dead_since = CURRENT_DATE - 3 WHERE push_id_hash = '{cara}'")

    client.portal.call(cleanup_expired_subscriptions)

    left = set(_sql("SELECT push_id_hash FROM token_subscriptions").split())
    assert bob not in left            # dead 30 days: gone
    assert cara in left               # dead 3 days: still within the grace period

    subscribe(client, device="dan", card="pink")  # the freed slot can be used
