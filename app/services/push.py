# app/services/push.py
#
# Sends push notifications to iOS (APNs) and Android (FCM).
#
# Lock screen (visible to Apple/Google and anyone who sees the phone):
#   "You have a new message. Open the app to read it."
#
# The push carries NO ciphertext: it only wakes the phone. The app then
# fetches its messages from the server over its own TLS connection (/inbox).
# Apple and Google therefore never see the encrypted payloads.

import json
import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

PUSH_ALERT_BODY = "You have a new message. Open the app to read it."

APNS_HOST_PROD = "https://api.push.apple.com"
APNS_HOST_DEV = "https://api.sandbox.push.apple.com"


# Outcome of one push to one device.
PUSH_OK = "ok"
PUSH_FAILED = "failed"        # try again later (network, 5xx, rate limit...)
PUSH_DEAD_TOKEN = "dead"      # the provider says this token will never work again

def _apns_host() -> str:
    return APNS_HOST_PROD if settings.apns_production else APNS_HOST_DEV


def _make_apns_jwt() -> str:
    import jwt

    if settings.apns_key:
        # Env vars sometimes carry the line breaks as a literal backslash-n.
        private_key = settings.apns_key.replace("\\n", "\n")
    else:
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


async def _send_apns(push_token: str) -> str:
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
            },
            headers={
                "authorization": f"bearer {auth_token}",
                "apns-topic": settings.apns_bundle_id,
                "apns-push-type": "alert",
                "apns-priority": "10",
            },
            timeout=10.0,
        )

    if response.status_code == 200:
        return PUSH_OK
    # 410 = Unregistered; 400 with BadDeviceToken = the token was never valid.
    try:
        reason = response.json().get("reason")
    except Exception:
        reason = None
    # Apple's reason is a fixed word such as BadDeviceToken or TopicDisallowed,
    # which tells us at once whether the key, the bundle ID or the environment is
    # wrong. Anything else is not logged, so a surprise can never leak data.
    logged = reason if isinstance(reason, str) and reason.isalpha() and len(reason) <= 40 else "-"
    logger.error("APNs push failed: status=%s reason=%s", response.status_code, logged)
    if response.status_code == 410 or reason in ("Unregistered", "BadDeviceToken"):
        return PUSH_DEAD_TOKEN
    return PUSH_FAILED


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


async def _send_fcm(push_token: str) -> str:
    access_token, project_id = _get_fcm_access_token()
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            json={
                "message": {
                    "token": push_token,
                    "notification": {"body": PUSH_ALERT_BODY},
                }
            },
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )

    if response.status_code == 200:
        return PUSH_OK
    logger.error("FCM push failed: status=%s", response.status_code)
    # 404 (UNREGISTERED) = the app was uninstalled or the token was replaced.
    return PUSH_DEAD_TOKEN if response.status_code == 404 else PUSH_FAILED


async def send_push(push_token: str, platform: str) -> str:
    """
    Send ONE wake-up push. Returns PUSH_OK, PUSH_FAILED or PUSH_DEAD_TOKEN.
    Never raises: one broken recipient must not stop the others.
    Logs the error type only, never the token.
    """
    if settings.push_stub_mode:
        logger.info("[PUSH STUB] would send to %s device", platform)
        return PUSH_OK

    try:
        if platform == "ios":
            return await _send_apns(push_token)
        if platform == "android":
            return await _send_fcm(push_token)
        logger.error("Unknown platform")
    except Exception as exc:
        logger.error("Push failed: %s", type(exc).__name__)
    return PUSH_FAILED
