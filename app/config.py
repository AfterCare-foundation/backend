# app/config.py

import os
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # hide_input_in_errors: a startup error must never echo setting values
    # (database password, APNs private key). Errors name settings only.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", hide_input_in_errors=True
    )

    database_url: str

    # No default on purpose: a forgotten or misspelled ENVIRONMENT stops startup
    # instead of silently running as development.
    # development → /docs and /openapi.json enabled. production → off, and the
    # push settings below are enforced at startup.
    environment: Literal["development", "production"]

    # When several app processes share one database, advisory locks still
    # prevent double-send. Set false on extra web workers if you add a
    # dedicated clock process later.
    run_background_jobs: bool = True

    friendly_captcha_secret: str = ""
    captcha_skip: bool = False

    apns_key_id: str = ""
    apns_key_file: str = "secrets/apns_key.p8"
    # Alternative to the file: the .p8 text itself (for hosts without secret files).
    apns_key: str = ""
    apns_team_id: str = ""
    apns_bundle_id: str = "eu.after-care.app"
    apns_production: bool = False

    fcm_service_account_file: str = "secrets/fcm_service_account.json"
    push_stub_mode: bool = True

    # How long a subscription (card scan) is kept. Cleanup deletes older ones.
    # Set from the partner look-back periods in the STI guidelines (up to 6 months
    # for chlamydia). Applies to existing rows too: expiry is created_date + this.
    subscription_ttl_days: int = 180

    # A push token the provider reports as dead is erased after this many days,
    # unless the app fixes it first. The subscription itself stays until its
    # normal end (SUBSCRIPTION_TTL_DAYS), so the device can move back to it.
    dead_token_grace_days: int = 30

    # New connections (card scans) one device may add per UTC day.
    max_subscriptions_per_day: int = 30

    # How long a notification waits in the recipient's mailbox for the app to fetch it.
    mailbox_ttl_days: int = 30

    notify_rate_limit_days: int = 30
    notify_max_campaigns: int = 6
    notify_max_campaigns_per_day: int = 3  # rolling 24 hours
    notify_max_contacts_per_campaign: int = 100

    @model_validator(mode="after")
    def _production_must_be_safe(self):
        if self.environment != "production":
            return self
        problems = []
        if self.push_stub_mode:
            problems.append("PUSH_STUB_MODE must be false")
        if not self.apns_production:
            problems.append("APNS_PRODUCTION must be true")
        if not self.apns_key_id.strip():
            problems.append("APNS_KEY_ID must be set")
        if not self.apns_team_id.strip():
            problems.append("APNS_TEAM_ID must be set")
        if not self.apns_key.strip() and not os.path.isfile(self.apns_key_file):
            problems.append("APNS_KEY must be set (or APNS_KEY_FILE must exist)")
        if problems:
            raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        return self


settings = Settings()
