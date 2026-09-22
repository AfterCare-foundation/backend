# app/services/devices.py
#
# Device-bound proof.
#
# push_id_hash is a SHA-256 value — it cannot be guessed.
# It can be leaked (logs, a copied request, a compromised phone).
# Anyone who has only the hash could call delete/notify as that device.
#
# Fix: the app generates a 256-bit secret on first launch, keeps it in
# Keychain / Keystore, and sends it on every request. We store only
# SHA-256(secret). Possession of the secret proves it is the same install.

from fastapi import HTTPException
import asyncpg

from app.hashes import hash_device_credential


async def register_or_verify_device(
    conn: asyncpg.Connection,
    push_id_hash: str,
    device_credential: str,
) -> None:
    credential_hash = hash_device_credential(device_credential)
    existing = await conn.fetchrow(
        "SELECT credential_hash FROM devices WHERE push_id_hash = $1",
        push_id_hash,
    )
    if existing is None:
        await conn.execute(
            """
            INSERT INTO devices (push_id_hash, credential_hash, created_date)
            VALUES ($1, $2, CURRENT_DATE)
            """,
            push_id_hash,
            credential_hash,
        )
        return

    if existing["credential_hash"] != credential_hash:
        raise HTTPException(status_code=403, detail="Device credential does not match")


async def verify_device(
    conn: asyncpg.Connection,
    push_id_hash: str,
    device_credential: str,
) -> None:
    credential_hash = hash_device_credential(device_credential)
    stored = await conn.fetchval(
        "SELECT credential_hash FROM devices WHERE push_id_hash = $1",
        push_id_hash,
    )
    if stored is None or stored != credential_hash:
        raise HTTPException(status_code=403, detail="Device credential does not match")
