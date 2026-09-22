# app/services/captcha.py
#
# Friendly Captcha verification — NOT ACTIVE IN v1.
#
# Decision: CAPTCHA will be triggered adaptively (only on suspicious requests)
# as part of anomaly detection, which is a v2 feature. Adding CAPTCHA to every
# request creates unnecessary friction without the intelligence to back it up.
#
# This file is kept ready to wire in when anomaly detection is built.
#
# How Friendly Captcha works (for reference):
#   1. The app runs a background proof-of-work puzzle (no visible widget needed).
#   2. The puzzle produces a solution token, sent with the request.
#   3. We verify the token here with Friendly Captcha's API.
#   4. If valid → allow. If not → reject with 400.
#
# Friendly Captcha uses proof-of-work (not image puzzles), so it works without
# cookies or fingerprinting — consistent with our privacy model.

import httpx
from fastapi import HTTPException
from app.config import settings

FRIENDLY_CAPTCHA_VERIFY_URL = "https://api.friendlycaptcha.com/api/v1/siteverify"


async def verify_captcha(token: str) -> None:
    """
    Verify a Friendly Captcha solution token.
    Raises HTTP 400 if the token is invalid.
    Skips verification entirely if CAPTCHA_SKIP=true (local dev only).
    """
    if settings.captcha_skip:
        # Development mode — skip captcha verification.
        # This must never be true in production.
        return

    async with httpx.AsyncClient() as client:
        response = await client.post(
            FRIENDLY_CAPTCHA_VERIFY_URL,
            json={
                "solution": token,
                "secret": settings.friendly_captcha_secret,
            },
            timeout=5.0,
        )

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Captcha verification failed")

    data = response.json()
    if not data.get("success"):
        raise HTTPException(status_code=400, detail="Captcha solution invalid")
