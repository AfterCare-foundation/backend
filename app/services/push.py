# app/services/push.py
#
# Sends push notifications to iOS (APNs) and Android (FCM).
#
# Lock screen (visible to Apple/Google and anyone who sees the phone), title
# "AfterCare", in the device's language (en or de):
#   "You have a new message. Open the app to read it."
#
# The push carries NO ciphertext: it only wakes the phone. The app then
# fetches its messages from the server over its own TLS connection (/inbox).
# Apple and Google therefore never see the encrypted payloads.

import asyncio
import json
import logging
import time

import httpx

from app.config import settings
from app.push_tokens import is_valid_push_token

logger = logging.getLogger(__name__)

PUSH_TITLE = "AfterCare"
PUSH_ALERT_BODIES = {
    "en": "You have a new message. Open the app to read it.",
    "de": "Du hast eine neue Nachricht. Öffne die App, um sie zu lesen.",
}


def alert_body(lang: str) -> str:
    """The text for this language; any other value gets English."""
    return PUSH_ALERT_BODIES.get(lang, PUSH_ALERT_BODIES["en"])

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


# Apple wants one long-lived connection and a provider token refreshed no more
# than every 20 minutes (valid up to 60). One client and one token are shared.
APNS_JWT_MAX_AGE_SECONDS = 50 * 60
_EXPIRED_TOKEN_REASONS = ("ExpiredProviderToken", "InvalidProviderToken")

_apns_client = None
_apns_client_loop = None
_apns_jwt: str | None = None
_apns_jwt_at = 0.0


def _get_apns_client():
    """
    The shared client. It belongs to the event loop it was created on; a
    different (or closed) loop gets a new one, since tests and asyncio.run()
    each use a fresh loop.
    """
    global _apns_client, _apns_client_loop
    loop = asyncio.get_running_loop()
    if (
        _apns_client is None
        or _apns_client_loop is not loop
        or _apns_client_loop.is_closed()
    ):
        _apns_client = httpx.AsyncClient(http2=True, timeout=10.0)
        _apns_client_loop = loop
    return _apns_client


def _apns_auth_token() -> str:
    """
    The cached provider token, signed anew after 50 minutes. There is no await
    in here, so parallel sends cannot interleave between the age check and the
    cache write: at most one signs per refresh (and no asyncio.Lock, which
    would bind to a single loop).
    """
    global _apns_jwt, _apns_jwt_at
    now = time.monotonic()
    if _apns_jwt is None or now - _apns_jwt_at >= APNS_JWT_MAX_AGE_SECONDS:
        _apns_jwt = _make_apns_jwt()
        _apns_jwt_at = now
    return _apns_jwt


async def close_apns_client() -> None:
    """Close the shared client and forget the cached token. Never raises."""
    global _apns_client, _apns_client_loop, _apns_jwt, _apns_jwt_at
    client, loop = _apns_client, _apns_client_loop
    _apns_client = _apns_client_loop = _apns_jwt = None
    _apns_jwt_at = 0.0
    if client is None:
        return
    try:
        # A client of another (dead) loop cannot be closed from here: drop it.
        if loop is asyncio.get_running_loop():
            await client.aclose()
    except Exception as exc:
        logger.error("Closing the APNs client failed: %s", type(exc).__name__)


async def _send_apns(push_token: str, lang: str) -> str:
    auth_token = _apns_auth_token()
    url = f"{_apns_host()}/3/device/{push_token}"

    response = await _get_apns_client().post(
        url,
        json={
            "aps": {
                "alert": {"title": PUSH_TITLE, "body": alert_body(lang)},
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
    if response.status_code == 403 and reason in _EXPIRED_TOKEN_REASONS:
        # Sign a fresh token on the next push; this one is retried by the
        # dispatcher. Only drop the cache if no parallel send already replaced it.
        global _apns_jwt
        if _apns_jwt == auth_token:
            _apns_jwt = None
        return PUSH_FAILED
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


async def _send_fcm(push_token: str, lang: str) -> str:
    access_token, project_id = _get_fcm_access_token()
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            json={
                "message": {
                    "token": push_token,
                    "notification": {"title": PUSH_TITLE, "body": alert_body(lang)},
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


WAKE_CONCURRENCY = 10
WAKE_TIMEOUT_SECONDS = 10.0


async def wake_all(targets: list[tuple[str, str, str]], send) -> list[str]:
    """
    Run `send(token, platform, lang=...)` for every (token, platform, lang)
    target, up to WAKE_CONCURRENCY at a time, each limited to
    WAKE_TIMEOUT_SECONDS. Results come back in the order of `targets`. A push
    that raises or times out counts as PUSH_FAILED. Network only: callers do
    their database writes afterwards, one at a time, on their own connection.

    The time limit starts once a push has a free slot, so a queued push is not
    charged for waiting. It only bounds code that awaits: the blocking FCM
    token refresh cannot be interrupted by it.
    """
    sem = asyncio.Semaphore(WAKE_CONCURRENCY)

    async def one(token: str, platform: str, lang: str) -> str:
        async with sem:
            try:
                return await asyncio.wait_for(
                    send(token, platform, lang=lang), WAKE_TIMEOUT_SECONDS
                )
            except Exception as exc:
                logger.error("Push failed: %s", type(exc).__name__)
                return PUSH_FAILED

    return list(await asyncio.gather(*(one(*t) for t in targets)))


async def send_push(push_token: str, platform: str, lang: str = "en") -> str:
    """
    Send ONE wake-up push. Returns PUSH_OK, PUSH_FAILED or PUSH_DEAD_TOKEN.
    Never raises: one broken recipient must not stop the others.
    Logs the error type only, never the token or the language.
    A stored token in the wrong format (e.g. from before it was checked on
    subscribe) is refused as dead before any network call, in stub mode too.
    """
    if not is_valid_push_token(platform, push_token):
        logger.warning("Push refused: stored token has an invalid format")
        return PUSH_DEAD_TOKEN

    if settings.push_stub_mode:
        logger.info("[PUSH STUB] would send to %s device", platform)
        return PUSH_OK

    try:
        if platform == "ios":
            return await _send_apns(push_token, lang)
        if platform == "android":
            return await _send_fcm(push_token, lang)
        logger.error("Unknown platform")
    except Exception as exc:
        logger.error("Push failed: %s", type(exc).__name__)
    return PUSH_FAILED
