# app/services/push.py
#
# Sends push notifications to iOS (APNs) and Android (FCM).
#
# Lock screen (visible to Apple/Google and anyone who sees the phone):
#   "You have a new message. Open the app to read it."
#
# Custom data field `enc` (ciphertext from the sender's phone):
#   Apple and Google transport it. They do not have the card token, so they
#   cannot decrypt the STI type. The recipient app decrypts locally.

import json
import logging
import time

import httpx

from app.config import settings
from app.services import dev_inbox

logger = logging.getLogger(__name__)

PUSH_ALERT_BODY = "You have a new message. Open the app to read it."

APNS_HOST_PROD = "https://api.push.apple.com"
APNS_HOST_DEV = "https://api.sandbox.push.apple.com"


# Keep one push well under the ~4 KB APNs limit.
MAX_PUSH_CIPHERTEXT_CHARS = 3000


def _custom_fields(payloads: list[str]) -> dict:
    """`enc` = first ciphertext (as always); `more` = any further ones."""
    fields = {"enc": payloads[0]}
    if len(payloads) > 1:
        fields["more"] = payloads[1:]
    return fields


def _apns_host() -> str:
    return APNS_HOST_PROD if settings.apns_production else APNS_HOST_DEV


def _make_apns_jwt() -> str:
    import jwt

    with open(settings.apns_key_file, "r") as f:
        private_key = f.read()

    return jwt.encode(
        payload={
            "iss": settings.apns_team_id,
            "iat": int(time.time()),
        },
        key=private_key,
        algorithm="ES256",
        headers={"kid": settings.apns_key_id},
    )


async def _send_apns(push_token: str, payloads: list[str]) -> bool:
    auth_token = _make_apns_jwt()
    url = f"{_apns_host()}/3/device/{push_token}"

    async with httpx.AsyncClient(http2=True) as client:
        response = await client.post(
            url,
            json={
                "aps": {
                    "alert": PUSH_ALERT_BODY,
                    "sound": "default",
                },
                **_custom_fields(payloads),
            },
            headers={
                "authorization": f"bearer {auth_token}",
                "apns-topic": settings.apns_bundle_id,
                "apns-push-type": "alert",
                "apns-priority": "10",
            },
            timeout=10.0,
        )

    if response.status_code != 200:
        logger.error("APNs push failed: status=%s", response.status_code)
    return response.status_code == 200


def _get_fcm_access_token() -> tuple[str, str]:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request

    credentials = service_account.Credentials.from_service_account_file(
        settings.fcm_service_account_file,
        scopes=["https://www.googleapis.com/auth/firebase.messaging"],
    )
    credentials.refresh(Request())
    with open(settings.fcm_service_account_file) as f:
        project_id = json.load(f)["project_id"]
    return credentials.token, project_id


async def _send_fcm(push_token: str, payloads: list[str]) -> bool:
    access_token, project_id = _get_fcm_access_token()
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            json={
                "message": {
                    "token": push_token,
                    "notification": {"body": PUSH_ALERT_BODY},
                    "data": {k: v if isinstance(v, str) else json.dumps(v)
                             for k, v in _custom_fields(payloads).items()},
                }
            },
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )

    if response.status_code != 200:
        logger.error("FCM push failed: status=%s", response.status_code)
    return response.status_code == 200


async def send_push(
    push_token: str,
    platform: str,
    encrypted_payloads: list[str],
    push_id_hash: str,
) -> bool:
    """
    Send ONE push carrying one or more ciphertexts. True if the provider accepted it.
    Never raises: one broken recipient must not stop the others.
    Logs the error type only, never the token or payload.
    """
    if settings.push_stub_mode:
        logger.info("[PUSH STUB] would send to %s device", platform)
        if settings.environment == "development":
            dev_inbox.append(push_id_hash, encrypted_payloads, PUSH_ALERT_BODY)
        return True

    try:
        if platform == "ios":
            return await _send_apns(push_token, encrypted_payloads)
        if platform == "android":
            return await _send_fcm(push_token, encrypted_payloads)
        logger.error("Unknown platform")
    except Exception as exc:
        logger.error("Push failed: %s", type(exc).__name__)
    return False


async def send_bundle(recipient: dict, encrypted_payloads: list[str]) -> int:
    """
    One device, several ciphertexts (same person reached through several codes).
    The device buzzes once per push; ciphertexts are packed together so it
    normally gets exactly one. Returns the number of pushes accepted, or -1 if
    any push of the bundle failed.
    """
    chunks: list[list[str]] = [[]]
    size = 0
    for payload in encrypted_payloads:
        if chunks[-1] and size + len(payload) > MAX_PUSH_CIPHERTEXT_CHARS:
            chunks.append([])
            size = 0
        chunks[-1].append(payload)
        size += len(payload)

    accepted = 0
    for chunk in chunks:
        ok = await send_push(
            recipient["push_token"], recipient["platform"], chunk, recipient["push_id_hash"]
        )
        if not ok:
            return -1
        accepted += 1
    return accepted


async def send_push_to_all(recipients: list[dict], encrypted_payload: str) -> int:
    """Same ciphertext to every recipient (scheduled dispatch). Returns pushes accepted."""
    accepted = 0
    for recipient in recipients:
        if await send_push(
            recipient["push_token"], recipient["platform"], [encrypted_payload], recipient["push_id_hash"]
        ):
            accepted += 1
    return accepted
