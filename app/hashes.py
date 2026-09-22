# app/hashes.py
#
# SHA-256 helpers. The mobile app hashes tokens and push IDs before
# they reach us. Device credentials are hashed here on arrival.

import hashlib
import re

SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def is_sha256_hex(value: str) -> bool:
    return bool(SHA256_HEX.match(value))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_device_credential(hex_secret: str) -> str:
    """
    hex_secret is 32 random bytes, sent as 64 lowercase hex chars.
    We hash the raw bytes (not the hex string) so a database leak
    does not equal the secret the phone holds.
    """
    return sha256_bytes(bytes.fromhex(hex_secret))
