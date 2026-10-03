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


async def _send_apns(push_token: str, encrypted_payload: str) -> None:
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
                "enc": encrypted_payload,
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


async def _send_fcm(push_token: str, encrypted_payload: str) -> None:
    access_token, project_id = _get_fcm_access_token()
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            json={
                "message": {
                    "token": push_token,
                    "notification": {"body": PUSH_ALERT_BODY},
                    "data": {"enc": encrypted_payload},
                }
            },
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )

    if response.status_code != 200:
        logger.error("FCM push failed: status=%s", response.status_code)


async def send_push(
    push_token: str,
    platform: str,
    encrypted_payload: str,
    push_id_hash: str,
) -> None:
    if settings.push_stub_mode:
        logger.info("[PUSH STUB] would send to %s device", platform)
        if settings.environment == "development":
            dev_inbox.append(push_id_hash, encrypted_payload, PUSH_ALERT_BODY)
        return

    if platform == "ios":
        await _send_apns(push_token, encrypted_payload)
    elif platform == "android":
        await _send_fcm(push_token, encrypted_payload)
    else:
        logger.error("Unknown platform")


async def send_push_to_all(recipients: list[dict], encrypted_payload: str) -> None:
    for recipient in recipients:
        await send_push(
            recipient["push_token"],
            recipient["platform"],
            encrypted_payload,
            recipient["push_id_hash"],
        )
