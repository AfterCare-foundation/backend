# app/config.py

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str

    # development → /docs enabled. production → /docs off.
    environment: str = "development"

    # When several app processes share one database, advisory locks still
    # prevent double-send. Set false on extra web workers if you add a
    # dedicated clock process later.
    run_background_jobs: bool = True

    friendly_captcha_secret: str = ""
    captcha_skip: bool = False

    apns_key_id: str = ""
    apns_key_file: str = "secrets/apns_key.p8"
    apns_team_id: str = ""
    apns_bundle_id: str = "eu.after-care.app"
    apns_production: bool = False

    fcm_service_account_file: str = "secrets/fcm_service_account.json"
    push_stub_mode: bool = True

    # How long a subscription (card scan) is kept. Cleanup deletes older ones.
    # Set from the partner look-back periods in the STI guidelines (up to 6 months
    # for chlamydia). Applies to existing rows too: expiry is created_date + this.
    subscription_ttl_days: int = 180

    notify_rate_limit_days: int = 30
    notify_max_campaigns: int = 6
    notify_max_campaigns_per_day: int = 3  # rolling 24 hours
    notify_max_contacts_per_campaign: int = 100


settings = Settings()
