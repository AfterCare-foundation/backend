# Shared fixtures for HTTP tests against the local aftercare_dev database.

import hashlib
import os
import subprocess
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import dev_inbox

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
            TRUNCATE campaign_contacts, pending_notifications,
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
    dev_inbox.clear()
    with TestClient(app) as test_client:
        yield test_client
    dev_inbox.clear()
    wipe_db()


@pytest.fixture
def sent(monkeypatch):
    """Capture push dispatches instead of calling APNs/FCM."""
    calls = []

    async def fake(recipients, encrypted_payload):
        calls.append({"recipients": recipients, "enc": encrypted_payload})
        return len(recipients)

    monkeypatch.setattr("app.services.notify_flow.send_push_to_all", fake)
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


def notify(client, *, device: str, card: str, campaign_id: str | None = None, scheduled_at: str | None = None):
    delivery = {
        "et_hash": sha256_hex(f"card:{card}"),
        "encrypted_payload": f"ciphertext-for-{card}",
    }
    if scheduled_at:
        delivery["scheduled_at"] = scheduled_at
    body = {
        "sender_push_id_hash": sha256_hex(f"push:{device}"),
        "device_credential": sha256_hex(f"secret:{device}"),
        "campaign_id": campaign_id or str(uuid4()),
        "deliveries": [delivery],
    }
    return client.post("/notify", json=body), body


def pull_inbox(client, *, device: str):
    return client.post(
        "/dev/inbox",
        json={
            "push_id_hash": sha256_hex(f"push:{device}"),
            "device_credential": sha256_hex(f"secret:{device}"),
        },
    )
