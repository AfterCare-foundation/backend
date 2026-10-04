# Club flow: two devices on one card, notify, self-exclusion, rate limits.

from uuid import uuid4

from tests.conftest import notify, sha256_hex, subscribe


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
    assert body["scheduled"] == 0

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


def test_same_campaign_can_cover_more_tokens(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    subscribe(client, device="alice", card="green")
    subscribe(client, device="cara", card="green")

    campaign_id = str(uuid4())
    first, _ = notify(client, device="alice", card="pink", campaign_id=campaign_id)
    assert first.status_code == 200
    second, _ = notify(client, device="alice", card="green", campaign_id=campaign_id)
    assert second.status_code == 200, second.text
    assert len(sent) == 2


def test_second_campaign_same_day_is_rate_limited(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")

    first, _ = notify(client, device="alice", card="pink")
    assert first.status_code == 200

    second, _ = notify(client, device="alice", card="pink")
    assert second.status_code == 429
    assert len(sent) == 1


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


def test_four_campaigns_in_thirty_days_blocks_the_fifth(client, sent):
    import os
    import subprocess
    from tests.conftest import PGPASSWORD

    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")

    alice_push = sha256_hex("push:alice")
    env = os.environ.copy()
    env["PGPASSWORD"] = PGPASSWORD
    # Four older campaigns, last one 2 days ago so the 1-day gap is satisfied.
    sql = f"""
        INSERT INTO notification_campaigns (campaign_id, push_id_hash, created_at) VALUES
          ('11111111-1111-1111-1111-111111111111', '{alice_push}', NOW() - INTERVAL '20 days'),
          ('22222222-2222-2222-2222-222222222222', '{alice_push}', NOW() - INTERVAL '14 days'),
          ('33333333-3333-3333-3333-333333333333', '{alice_push}', NOW() - INTERVAL '8 days'),
          ('44444444-4444-4444-4444-444444444444', '{alice_push}', NOW() - INTERVAL '2 days');
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
