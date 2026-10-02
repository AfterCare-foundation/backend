# Stub-mode inbox: the other device can pull its ciphertext; the sender cannot.

from app.config import settings
from tests.conftest import notify, pull_inbox, sha256_hex, subscribe


def test_other_device_receives_ciphertext_sender_does_not(client):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink", platform="android")

    response, _ = notify(client, device="alice", card="pink")
    assert response.status_code == 200
    assert response.json()["pushed"] == 1

    alice = pull_inbox(client, device="alice")
    assert alice.status_code == 200
    assert alice.json()["notifications"] == []

    bob = pull_inbox(client, device="bob")
    assert bob.status_code == 200
    notes = bob.json()["notifications"]
    assert len(notes) == 1
    assert notes[0]["enc"] == "ciphertext-for-pink"
    assert "STI" in notes[0]["alert"]

    # Pulling consumes the inbox.
    again = pull_inbox(client, device="bob")
    assert again.json()["notifications"] == []


def test_unsubscribed_device_inbox_stays_empty(client):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    subscribe(client, device="cara", card="other")

    response, _ = notify(client, device="alice", card="pink")
    assert response.status_code == 200

    cara = pull_inbox(client, device="cara")
    assert cara.status_code == 200
    assert cara.json()["notifications"] == []


def test_wrong_credential_cannot_read_inbox(client):
    subscribe(client, device="bob", card="pink")
    response = client.post(
        "/dev/inbox",
        json={
            "push_id_hash": sha256_hex("push:bob"),
            "device_credential": sha256_hex("secret:eve"),
        },
    )
    assert response.status_code == 403


def test_inbox_hidden_outside_development(client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    subscribe(client, device="bob", card="pink")
    response = pull_inbox(client, device="bob")
    assert response.status_code == 404
