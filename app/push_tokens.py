# app/push_tokens.py
#
# Format check for push tokens. An APNs token is pasted into Apple's URL
# path and every token is stored, so the format is checked before either.

import re

APNS_TOKEN = re.compile(r"[0-9a-fA-F]+")
FCM_TOKEN = re.compile(r"[A-Za-z0-9_:.\-]+")


def is_valid_push_token(platform: str, token: str) -> bool:
    if not isinstance(token, str):
        return False
    if platform == "ios":
        return (
            64 <= len(token) <= 200
            and len(token) % 2 == 0
            and APNS_TOKEN.fullmatch(token) is not None
        )
    if platform == "android":
        return 32 <= len(token) <= 4096 and FCM_TOKEN.fullmatch(token) is not None
    return False
