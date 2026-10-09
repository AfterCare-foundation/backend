# Shared fixtures for HTTP tests against the local aftercare_dev database.

import hashlib
import os
import subprocess
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app

PGPASSWORD = "aftercare_dev_password"


def sha256_hex(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def wipe_db():
    env = os.environ.copy()
    env["PGPASSWORD"] = PGPASSWORD
    subprocess.check_call(
        [
            "psql",
            "-h", "localhost",
            "-U", "aftercare_dev",
            "-d", "aftercare_dev",
            "-v", "ON_ERROR_STOP=1",
            "-c",
            """
            TRUNCATE mailbox,
                     notification_campaigns, token_subscriptions, devices
            CASCADE;
            """,
        ],
        env=env,
        stdout=subprocess.DEVNULL,
    )


@pytest.fixture
def client():
    wipe_db()
    with TestClient(app) as test_client:
        yield test_client
    wipe_db()


@pytest.fixture
def sent(monkeypatch):
    """Capture wake-up pushes instead of calling APNs/FCM."""
    from app.services.push import PUSH_OK

    calls = []

    async def fake(push_token, platform):
        calls.append({"push_token": push_token, "platform": platform})
        return PUSH_OK

    monkeypatch.setattr("app.services.notify_flow.send_push", fake)
    monkeypatch.setattr("app.cron.send_push", fake)
    return calls


def subscribe(client, *, device: str, card: str, platform: str = "ios"):
    body = {
        "et_hash": sha256_hex(f"card:{card}"),
        "push_id_hash": sha256_hex(f"push:{device}"),
        "push_token": f"push-token-{device}",
        "platform": platform,
        "device_credential": sha256_hex(f"secret:{device}"),
    }
    response = client.post("/subscribe", json=body)
    assert response.status_code == 200, response.text
    return body


def notify(client, *, device: str, card: str, campaign_id: str | None = None):
    delivery = {
        "et_hash": sha256_hex(f"card:{card}"),
        "encrypted_payload": f"ciphertext-for-{card}",
    }
    body = {
        "sender_push_id_hash": sha256_hex(f"push:{device}"),
        "device_credential": sha256_hex(f"secret:{device}"),
        "campaign_id": campaign_id or str(uuid4()),
        "deliveries": [delivery],
    }
    return client.post("/notify", json=body), body


def pull_inbox(client, *, device: str):
    return client.post(
        "/inbox",
        json={
            "push_id_hash": sha256_hex(f"push:{device}"),
            "device_credential": sha256_hex(f"secret:{device}"),
        },
    )


def waiting(client, *, device: str) -> list[str]:
    """Ciphertexts waiting for this device, oldest first."""
    response = pull_inbox(client, device=device)
    assert response.status_code == 200, response.text
    return [n["enc"] for n in response.json()["notifications"]]
