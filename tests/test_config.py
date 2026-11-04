# Startup configuration checks. No database needed.

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import app_kwargs

SAFE_PROD = dict(
    database_url="postgresql://x/y_test",
    environment="production",
    push_stub_mode=False,
    apns_production=True,
    apns_key_id="KEYID12345",
    apns_team_id="TEAMID1234",
    apns_key="-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
)


def make(**overrides):
    # _env_file=None: a developer's .env must not leak into these tests.
    return Settings(_env_file=None, **{**SAFE_PROD, **overrides})


def test_production_accepts_complete_safe_settings():
    assert make().environment == "production"


@pytest.mark.parametrize(
    "overrides, named",
    [
        (dict(push_stub_mode=True), "PUSH_STUB_MODE"),
        (dict(apns_production=False), "APNS_PRODUCTION"),
        (dict(apns_key_id=""), "APNS_KEY_ID"),
        (dict(apns_team_id=""), "APNS_TEAM_ID"),
        (dict(apns_key="", apns_key_file="no/such/file.p8"), "APNS_KEY"),
    ],
)
def test_production_refuses_each_unsafe_setting(overrides, named):
    with pytest.raises(ValidationError) as exc:
        make(**overrides)
    assert named in str(exc.value)


def test_production_refuses_to_start_with_unsafe_settings():
    # F5.2: production used to start with push stub mode on (no phone would buzz).
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql://x/y_test",
                 environment="production", push_stub_mode=True)


def test_production_accepts_key_file_instead_of_key_text(tmp_path):
    key_file = tmp_path / "key.p8"
    key_file.write_text("x")
    assert make(apns_key="", apns_key_file=str(key_file))


def test_development_accepts_defaults(monkeypatch):
    for name in ("PUSH_STUB_MODE", "APNS_PRODUCTION", "APNS_KEY_ID", "APNS_TEAM_ID", "APNS_KEY"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None, database_url="postgresql://x/y_test", environment="development")
    assert s.push_stub_mode is True
    assert s.apns_production is False


def test_missing_environment_refuses(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql://x/y_test")


@pytest.mark.parametrize("value", ["prod", "Production", "", "dev", "staging"])
def test_misspelled_environment_refuses(value):
    with pytest.raises(ValidationError):
        make(environment=value)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(push_stub_mode=True),  # model validator failure
        dict(environment="Production"),  # field validation failure
    ],
)
def test_startup_error_never_contains_setting_values(kwargs):
    secrets = dict(
        apns_key="-----BEGIN PRIVATE KEY-----SECRETMARKER",
        database_url="postgresql://u:PASSWORDMARKER@h/d",
        apns_key_id="KEYIDMARKER",
    )
    with pytest.raises(ValidationError) as exc:
        make(**{**secrets, **kwargs})
    for text in (str(exc.value), repr(exc.value)):
        for marker in ("SECRETMARKER", "PASSWORDMARKER", "KEYIDMARKER", "PRIVATE KEY"):
            assert marker not in text


def test_missing_environment_error_never_contains_values(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, database_url="postgresql://u:PASSWORDMARKER@h/d",
                 apns_key="SECRETMARKER")
    for text in (str(exc.value), repr(exc.value)):
        assert "PASSWORDMARKER" not in text
        assert "SECRETMARKER" not in text


def _client_for(environment):
    # Plain app with no lifespan, so no database or APNs key is needed.
    kwargs = app_kwargs(environment)
    kwargs.pop("lifespan")
    return TestClient(FastAPI(**kwargs))


def test_production_app_serves_no_docs_or_schema():
    client = _client_for("production")
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_development_app_serves_docs_and_schema():
    client = _client_for("development")
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


TELEMETRY_SWITCHES = ("tracing", "metrics", "logs", "operation_spans", "auto_configure")


@pytest.mark.parametrize("environment", ["development", "production"])
def test_fastapi_telemetry_is_fully_off(environment):
    kwargs = app_kwargs(environment)
    kwargs.pop("lifespan")
    app = FastAPI(**kwargs)
    assert {k: app._telemetry[k] for k in TELEMETRY_SWITCHES} == dict.fromkeys(TELEMETRY_SWITCHES, False)


def test_no_opentelemetry_sdk_is_installed():
    # opentelemetry-api comes with fastapi and is inert alone. The SDK is what
    # exports; it would arrive with fastapi[standard] or fastapi[opentelemetry].
    import importlib.util

    assert importlib.util.find_spec("opentelemetry.sdk") is None


@pytest.mark.parametrize("environment", ["development", "production"])
def test_otel_environment_variables_wire_up_no_exporter(environment, monkeypatch, caplog):
    from opentelemetry import trace

    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9/")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:9/v1/traces")
    kwargs = app_kwargs(environment)
    kwargs.pop("lifespan")
    with TestClient(FastAPI(**kwargs)) as client:  # runs startup, where FastAPI auto-configures
        client.get("/nothing-here")
    # FastAPI swallows a failed auto-configure (here: no SDK installed) and only
    # logs a warning, so the warning is the sign that it tried at all.
    assert not [r for r in caplog.records if "telemetry" in r.getMessage().lower()]
    assert isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider)
