# Deleting all your data closes your codes, so the freed slot cannot be taken.

from app.cron import cleanup_expired_subscriptions
from tests.conftest import notify, sha256_hex, subscribe
from tests.test_club_flow import _sql


def _delete_all(client, device: str):
    return client.request("DELETE", "/subscribe", json={
        "push_id_hash": sha256_hex(f"push:{device}"),
        "device_credential": sha256_hex(f"secret:{device}"),
    })


def _try_subscribe(client, device: str, card: str):
    return client.post("/subscribe", json={
        "et_hash": sha256_hex(f"card:{card}"),
        "push_id_hash": sha256_hex(f"push:{device}"),
        "push_token": f"push-token-{device}",
        "platform": "ios",
        "device_credential": sha256_hex(f"secret:{device}"),
    })


def test_a_third_phone_cannot_take_a_freed_slot(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    assert _delete_all(client, "alice").status_code == 200

    response = _try_subscribe(client, "eve", "pink")
    assert response.status_code == 409
    assert response.json()["detail"] == "code_in_use"
    assert _sql("SELECT COUNT(*) FROM burned_codes") == "1"


def test_the_remaining_phone_can_still_rescan_and_notify(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    _delete_all(client, "alice")

    assert _try_subscribe(client, "bob", "pink").status_code == 200
    response, _ = notify(client, device="bob", card="pink")
    assert response.status_code == 200
    assert sent == []  # nobody else is on the code


def test_other_codes_stay_open(client, sent):
    subscribe(client, device="alice", card="pink")
    subscribe(client, device="bob", card="pink")
    subscribe(client, device="cara", card="green")
    _delete_all(client, "alice")

    assert _try_subscribe(client, "dan", "green").status_code == 200


def test_burned_codes_are_forgotten_after_the_subscription_lifetime(client, sent):
    subscribe(client, device="alice", card="pink")
    _delete_all(client, "alice")
    _sql("UPDATE burned_codes SET burned_date = CURRENT_DATE - 181")

    client.portal.call(cleanup_expired_subscriptions)
    assert _sql("SELECT COUNT(*) FROM burned_codes") == "0"
