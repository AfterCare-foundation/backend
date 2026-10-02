# app/models.py
#
# Expected shape of every API request body.
# FastAPI rejects invalid JSON with 422 before our route code runs.

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.hashes import is_sha256_hex

CREDENTIAL_HEX = 64  # 256-bit secret, lowercase hex
MAX_ENCRYPTED_PAYLOAD = 4096


def require_hash(value: str) -> str:
    if not is_sha256_hex(value):
        raise ValueError("Must be a 64-character lowercase hex string (SHA-256)")
    return value


def require_credential(value: str) -> str:
    if not is_sha256_hex(value):
        raise ValueError("device_credential must be 32 random bytes as 64 lowercase hex chars")
    return value


class SubscribeRequest(BaseModel):
    et_hash: str
    push_id_hash: str
    push_token: str
    platform: str
    device_credential: str = Field(description="256-bit secret from Keychain / Keystore, hex-encoded")

    @field_validator("et_hash", "push_id_hash")
    @classmethod
    def validate_hash(cls, v: str) -> str:
        return require_hash(v)

    @field_validator("device_credential")
    @classmethod
    def validate_credential(cls, v: str) -> str:
        return require_credential(v)

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, v: str) -> str:
        if v not in ("ios", "android"):
            raise ValueError("Must be 'ios' or 'android'")
        return v


class Delivery(BaseModel):
    et_hash: str
    encrypted_payload: str = Field(
        description=(
            "STI type encrypted on the phone with a key derived from THIS card's "
            "raw token. Each contact needs its own ciphertext. Server never decrypts."
        ),
        min_length=1,
        max_length=MAX_ENCRYPTED_PAYLOAD,
    )
    scheduled_at: datetime | None = Field(
        default=None,
        description="UTC time to send. Omit for immediate delivery.",
    )

    @field_validator("et_hash")
    @classmethod
    def validate_hash(cls, v: str) -> str:
        return require_hash(v)


class NotifyRequest(BaseModel):
    """One 'Notify contacts' tap. Repeat with the same campaign_id for extra tokens."""
    sender_push_id_hash: str
    device_credential: str
    campaign_id: UUID
    deliveries: list[Delivery] = Field(min_length=1, max_length=100)

    @field_validator("sender_push_id_hash")
    @classmethod
    def validate_hash(cls, v: str) -> str:
        return require_hash(v)

    @field_validator("device_credential")
    @classmethod
    def validate_credential(cls, v: str) -> str:
        return require_credential(v)


class ScheduleRequest(BaseModel):
    et_hash: str
    sender_push_id_hash: str
    device_credential: str
    campaign_id: UUID
    encrypted_payload: str = Field(min_length=1, max_length=MAX_ENCRYPTED_PAYLOAD)
    scheduled_at: datetime

    @field_validator("et_hash", "sender_push_id_hash")
    @classmethod
    def validate_hash(cls, v: str) -> str:
        return require_hash(v)

    @field_validator("device_credential")
    @classmethod
    def validate_credential(cls, v: str) -> str:
        return require_credential(v)


class DevInboxRequest(BaseModel):
    """Pull stub pushes for this device. Development only."""
    push_id_hash: str
    device_credential: str

    @field_validator("push_id_hash")
    @classmethod
    def validate_hash(cls, v: str) -> str:
        return require_hash(v)

    @field_validator("device_credential")
    @classmethod
    def validate_credential(cls, v: str) -> str:
        return require_credential(v)


class DeleteSubscriptionRequest(BaseModel):
    push_id_hash: str
    device_credential: str

    @field_validator("push_id_hash")
    @classmethod
    def validate_hash(cls, v: str) -> str:
        return require_hash(v)

    @field_validator("device_credential")
    @classmethod
    def validate_credential(cls, v: str) -> str:
        return require_credential(v)


class UpdatePushIdRequest(BaseModel):
    old_push_id_hash: str
    new_push_id_hash: str
    new_push_token: str
    new_platform: str
    device_credential: str

    @field_validator("old_push_id_hash", "new_push_id_hash")
    @classmethod
    def validate_hash(cls, v: str) -> str:
        return require_hash(v)

    @field_validator("device_credential")
    @classmethod
    def validate_credential(cls, v: str) -> str:
        return require_credential(v)

    @field_validator("new_platform")
    @classmethod
    def validate_platform(cls, v: str) -> str:
        if v not in ("ios", "android"):
            raise ValueError("Must be 'ios' or 'android'")
        return v
