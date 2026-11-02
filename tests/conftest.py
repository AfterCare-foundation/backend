# Shared fixtures for HTTP tests.
#
# Tests run against their own database (TEST_DATABASE_URL, default
# aftercare_test on localhost), never the one in .env or DATABASE_URL, because
# every test wipes all tables. check_test_database_url() refuses anything that
# is not a local database named *_test, and the whole run stops before any
# connection if it fails.

import asyncio
import hashlib
import os
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import asyncpg
import pytest

DEFAULT_TEST_DATABASE_URL = "postgresql://aftercare_dev:aftercare_dev_password@localhost:5432/aftercare_test"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def check_test_database_url(url: str) -> None:
    """Raise ValueError unless url is a plain local PostgreSQL URL for a *_test database."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # raises ValueError if malformed
    except ValueError as exc:
        raise ValueError("cannot parse the URL") from exc
    if parts.scheme not in ("postgresql", "postgres"):
        raise ValueError("scheme must be postgresql:// or postgres://")
    # A comma means several hosts; libpq would try them in turn. A second "@"
    # makes urlsplit (host after the last "@") and asyncpg (after the first)
    # disagree about which host this is.
    if "," in parts.netloc or parts.netloc.count("@") > 1 or host not in LOCAL_HOSTS:
        raise ValueError("host must be exactly one of localhost, 127.0.0.1 or ::1")
    # ?host=... or ?dbname=... would override the parts checked here.
    if "?" in url or "#" in url or parts.query or parts.fragment:
        raise ValueError("no query string or fragment allowed")
    name = unquote(parts.path.removeprefix("/"))
    if "/" in name or not name.endswith("_test"):
        raise ValueError("database name must end in _test")


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
try:
    check_test_database_url(TEST_DATABASE_URL)
except ValueError as exc:
    pytest.exit(
        f"Refusing to run: TEST_DATABASE_URL is not a safe test database ({exc}). "
        "Tests wipe every table, so it must be local and named *_test.",
        returncode=2,
    )

# Order matters: app.config reads DATABASE_URL when app.main is imported, and
# environment variables beat .env. Set it first, import the app second.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
# ENVIRONMENT has no default. Tests must not run as production: it would demand
# real APNs settings and switch /docs off. Forced, not setdefault, so a stray
# ENVIRONMENT=production in the shell or .env can't change what the tests cover.
os.environ["ENVIRONMENT"] = "development"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def sha256_hex(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def _wipe() -> None:
    conn = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        names = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        if names:
            tables = ", ".join(_quote_ident(r["tablename"]) for r in names)
            await conn.execute(f"TRUNCATE {tables} CASCADE")
    finally:
        await conn.close()


def wipe_db():
    asyncio.run(_wipe())


def _psql_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "t" if value else "f"
    return str(value)


async def _sql(statement: str) -> str:
    conn = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        prepared = await conn.prepare(statement)
        if not prepared.get_attributes():
            await conn.execute(statement)
            return ""
        rows = await prepared.fetch()
        return "\n".join("|".join(_psql_text(v) for v in row.values()) for row in rows)
    finally:
        await conn.close()


def sql(statement: str) -> str:
    """Run one statement on the test database; rows come back like `psql -t -A`."""
    return asyncio.run(_sql(statement)).strip()


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

    async def fake(push_token, platform, lang="en"):
        calls.append({"push_token": push_token, "platform": platform, "lang": lang})
        return PUSH_OK

    monkeypatch.setattr("app.services.notify_flow.send_push", fake)
    monkeypatch.setattr("app.cron.send_push", fake)
    return calls


def subscribe(client, *, device: str, card: str, platform: str = "ios", **extra):
    body = {
        "et_hash": sha256_hex(f"card:{card}"),
        "push_id_hash": sha256_hex(f"push:{device}"),
        "push_token": f"push-token-{device}",
        "platform": platform,
        "device_credential": sha256_hex(f"secret:{device}"),
        **extra,
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
