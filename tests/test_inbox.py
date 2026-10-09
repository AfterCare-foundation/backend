# Mailbox: messages wait for the recipient, are deleted only on confirm,
# and expire if nobody collects them.

from app.cron import cleanup_expired_subscriptions
from tests.conftest import notify, pull_inbox, sha256_hex, subscribe, waiting
from tests.test_club_flow import _sql


def _credentials(device: str) -> dict:
    return {
        "push_id_hash": sha256_hex(f"push:{device}"),
        "device_credential": sha256_hex(f"secret:{device}"),
    }


def _two_phones_with_a_waiting_message(client):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    response, _ = notify(client, device="alice", card="pink")
    assert response.status_code == 200
    return pull_inbox(client, device="bob").json()["notifications"]


def test_fetching_does_not_delete(client, sent):
    first = _two_phones_with_a_waiting_message(client)
    assert [n["enc"] for n in first] == ["ciphertext-for-pink"]
    assert set(first[0]) == {"id", "enc"}  # nothing else about the message

    assert waiting(client, device="bob") == ["ciphertext-for-pink"]  # still there


def test_confirm_deletes_only_what_was_confirmed(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="alice", card="green")
    subscribe(client, device="bob", card="pink")
    subscribe(client, device="bob", card="green")
    notify(client, device="alice", card="pink")
    notify(client, device="alice", card="green")

    notes = pull_inbox(client, device="bob").json()["notifications"]
    assert len(notes) == 2

    done = client.post("/inbox/confirm", json={**_credentials("bob"), "ids": [notes[0]["id"]]})
    assert done.status_code == 200
    assert done.json()["deleted"] == 1
    assert waiting(client, device="bob") == [notes[1]["enc"]]


def test_a_device_cannot_confirm_someone_elses_message(client, sent):
    notes = _two_phones_with_a_waiting_message(client)

    response = client.post("/inbox/confirm", json={**_credentials("alice"), "ids": [notes[0]["id"]]})
    assert response.status_code == 200
    assert response.json()["deleted"] == 0
    assert waiting(client, device="bob") == ["ciphertext-for-pink"]


def test_wrong_credential_cannot_read_or_confirm(client, sent):
    notes = _two_phones_with_a_waiting_message(client)
    eve = {"push_id_hash": sha256_hex("push:bob"), "device_credential": sha256_hex("secret:eve")}

    assert client.post("/inbox", json=eve).status_code == 403
    assert client.post("/inbox/confirm", json={**eve, "ids": [notes[0]["id"]]}).status_code == 403
    assert waiting(client, device="bob") == ["ciphertext-for-pink"]


def test_unsubscribed_device_inbox_stays_empty(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    subscribe(client, device="cara", card="other")

    notify(client, device="alice", card="pink")
    assert waiting(client, device="cara") == []


def test_unfetched_messages_expire_after_the_retention_period(client, sent):
    _two_phones_with_a_waiting_message(client)
    subscribe(client, device="alice", card="green")
    subscribe(client, device="bob", card="green")
    notify(client, device="alice", card="green")

    # The pink message is 8 days old, the green one 3.
    _sql("UPDATE mailbox SET created_date = CURRENT_DATE - 8 WHERE encrypted_payload = 'ciphertext-for-pink'")
    _sql("UPDATE mailbox SET created_date = CURRENT_DATE - 3 WHERE encrypted_payload = 'ciphertext-for-green'")

    client.portal.call(cleanup_expired_subscriptions)
    assert waiting(client, device="bob") == ["ciphertext-for-green"]


def test_deleting_a_device_deletes_its_mailbox(client, sent):
    _two_phones_with_a_waiting_message(client)

    done = client.request("DELETE", "/subscribe", json=_credentials("bob"))
    assert done.status_code == 200
    assert _sql("SELECT COUNT(*) FROM mailbox") == "0"


def test_the_push_carries_no_ciphertext(client, monkeypatch):
    """The lock-screen push is built from the token and platform only."""
    import inspect

    from app.services import push

    assert list(inspect.signature(push.send_push).parameters) == ["push_token", "platform"]
