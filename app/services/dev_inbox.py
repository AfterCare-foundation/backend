# app/services/dev_inbox.py
#
# In-memory stand-in for APNs/FCM while PUSH_STUB_MODE is on.
# Keyed by push_id_hash. A device can pull only its own rows.
# Nothing is written to Postgres. Cleared when the process restarts.
# Never used in production.

from collections import defaultdict

_items: dict[str, list[dict]] = defaultdict(list)


def clear() -> None:
    _items.clear()


def append(push_id_hash: str, encrypted_payloads: list[str], alert: str) -> None:
    """Same fields as the real push: `enc` is the first ciphertext, `more` the rest."""
    item = {"enc": encrypted_payloads[0], "alert": alert}
    if len(encrypted_payloads) > 1:
        item["more"] = encrypted_payloads[1:]
    _items[push_id_hash.strip()].append(item)


def take(push_id_hash: str) -> list[dict]:
    return _items.pop(push_id_hash.strip(), [])
