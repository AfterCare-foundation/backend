# AfterCare backend

Anonymous STI exposure notification for [AfterCare](https://after-care.eu/). People connect with a tearable paper card; later, if someone tests positive, they can warn those connections without names, numbers, or an account.

This repo is the **notification server** (Python / FastAPI / PostgreSQL). It is open source under [AGPL-3.0](LICENSE).

**Status:** club (paper card) flow only. In development. Not production-ready.

Found a security problem? See [SECURITY.md](SECURITY.md).

Sauna wristband pairing, CAPTCHA, and anomaly detection are not in this codebase yet.

---

## What the server stores

| Stored | Not stored |
|---|---|
| `SHA-256` of the card token | Raw QR token |
| `SHA-256` of the device credential | Names, email, phone, location |
| Hashed push ID + the real push token (Apple/Google need it to deliver) | IP addresses |
| UTC **date** a card was scanned (`created_date`) | Encounter time |
| Per Notify tap: `campaign_id`, sender device hash and the exact time, for rate limits (30 days) | Which contacts a campaign reached |
| Encrypted bytes of a push that failed, with the card hash and sender, until the retry succeeds (at most a day) | Plaintext STI type |

Subscriptions expire after **180 days** (`SUBSCRIPTION_TTL_DAYS`) and are deleted automatically. The clock starts at the first scan and a re-scan does not extend it. The value is a single setting, so it is easy to change; a change applies to existing rows too, because expiry is `created_date` plus this number.

The infection name (gonorrhoea, syphilis, HIV, Mpox, HPV, …) is chosen in the **app** at notify time. The card is not tied to an STI. The phone should encrypt that choice; the lock-screen text is always generic: *You have a new message. Open the app to read it.*

Each card has its own token, so the app encrypts the STI type **once per contact**. Hashing and AES-GCM details: [docs/CRYPTO.md](docs/CRYPTO.md).

---

## How a card becomes a notification

1. Both people scan their half → `POST /subscribe` (same token hash on both phones).
2. Later, one person taps Notify → `POST /notify` (must already be subscribed to that token).
3. The sender is excluded from the recipient list.
4. Every notification is sent **immediately** to APNs/FCM (or to the dev inbox in stub mode). There is no scheduling.
5. If a push fails, the server queues it in `pending_notifications` and the background job retries it every 15 minutes for up to a day. The app never retries.

### Notify rules

- The whole request is validated first. One bad item (for example a code the sender is not subscribed to) rejects everything and nothing is sent.
- One person reached through several of the sender's codes gets **one** push. `enc` holds the first ciphertext and `more` the rest; the app decrypts all and shows each STI once. The server never drops a delivery, because it cannot see the STI type.
- A code connects at most **2 devices**, once. A third device is refused at subscribe time.
- Response: `{"status": "ok", "pushed": n, "retrying": n, "contacts": n}`.

### Notify limits (per device)

- At most **3 campaigns per rolling 24 hours** (for example one per infection in one sitting).
- At most **6 campaigns** in a rolling **30 days**.
- At most **100 unique card tokens** per campaign.

A *campaign* is one tap of Notify in the app (`campaign_id`), sent in **one** request. A `campaign_id` can be used once: a second request with the same id gets `409 campaign_already_used`. If a push fails, the server retries it itself (every 15 minutes, for up to a day) and the response says how many are `retrying`; the app never retries. If nobody could be reached at all (no one else on the codes yet), the id and the slot are given back. The server keeps only the campaign id, the sender device and the time, never which contacts were reached.

---

## Setup (local)

You need **Python 3.11**, [**uv**](https://docs.astral.sh/uv/) and **PostgreSQL**. This project uses a dedicated database so it stays separate from other work on the same machine.

```bash
# Database (once)
createuser aftercare_dev --pwprompt   # or use the role already created
createdb aftercare_dev --owner=aftercare_dev
for f in migrations/*.sql; do psql -h localhost -U aftercare_dev -d aftercare_dev -f "$f"; done

# App
uv sync            # creates .venv with the exact versions in uv.lock
cp .env.example .env
# Edit DATABASE_URL if needed. Leave PUSH_STUB_MODE=true until you have APNs/FCM keys.

uv run uvicorn app.main:app --reload --no-access-log
```

- Health: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)
- API docs (only if `ENVIRONMENT=development`): [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

Never commit `.env`, `.venv/`, or `secrets/`.

```bash
uv run pytest
```

Dependencies are listed in `pyproject.toml` and pinned, including indirect ones, in `uv.lock`. Change a dependency with `uv add <package>` or `uv lock --upgrade-package <package>`, then commit both files.

The connection pool sets PostgreSQL `TimeZone` to **UTC** for this app only.

---

## Configuration

See `.env.example`. Important flags:

| Variable | Meaning |
|---|---|
| `ENVIRONMENT` | `development` enables `/docs`. Use `production` on the host. |
| `PUSH_STUB_MODE` | `true` logs pushes instead of calling Apple/Google. |
| `RUN_BACKGROUND_JOBS` | Dispatcher + daily cleanup (subscriptions older than `SUBSCRIPTION_TTL_DAYS`) in this process. |
| `SUBSCRIPTION_TTL_DAYS` | Days a card scan is kept (default 180). |
| `NOTIFY_MAX_CAMPAIGNS_PER_DAY` | Campaigns per rolling 24 hours (default 3). |
| `NOTIFY_MAX_CAMPAIGNS`, `NOTIFY_RATE_LIMIT_DAYS` | Campaigns per rolling window (default 6 per 30 days). |
| `NOTIFY_MAX_CONTACTS_PER_CAMPAIGN` | Contacts per Notify tap (default 100). |

### Test instance on Scalingo

Procfile and `.python-version` are all it needs. Set `ENVIRONMENT=development`, `PUSH_STUB_MODE=true`, `RUN_BACKGROUND_JOBS=true` and `WEB_CONCURRENCY=1` (the dev inbox lives in memory, so run **one** process and one instance), add the PostgreSQL add-on, and apply every file in `migrations/` in order. Deploy new code **before** a migration that drops something the old code still reads. `/docs` and `/dev/inbox` are open in this mode, so use it for test data only.

Production must use **HTTPS**. The device credential is sent on each mutating request; it is a bearer secret.

On the load balancer, disable access logs and strip client IPs (see comments in `Procfile`).

---

## HTTP API

All hashes are **64 lowercase hex characters** (SHA-256).  
`device_credential` is **32 random bytes** as 64 lowercase hex; stored as `SHA-256` of those bytes. Keep it in the OS keystore on the phone.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness |
| `POST` | `/subscribe` | Register this device on a scanned card. A code holds at most 2 devices; a third gets `409 code_in_use` |
| `DELETE` | `/subscribe` | Erase this device (GDPR) |
| `POST` | `/notify` | Send notifications to one or more contacts, immediately |
| `POST` | `/update-push-id` | Push token changed (reinstall / permissions): moves the device's subscriptions to the new ID, keeping their expiry. Call it before `/subscribe` after a reinstall |

`POST /notify` body (shape):

```json
{
  "sender_push_id_hash": "<sha256 hex>",
  "device_credential": "<64 hex>",
  "campaign_id": "<uuid>",
  "deliveries": [
    { "et_hash": "<sha256 hex>", "encrypted_payload": "<ciphertext for this card>" },
    { "et_hash": "<sha256 hex>", "encrypted_payload": "<ciphertext for this card>" }
  ]
}
```

`platform` on subscribe is `ios` or `android`.

---

## Layout

```
app/main.py              FastAPI app, logging, no access log
app/routes/              HTTP handlers
app/services/            Device proof, rate limits, notify flow, push (stub/APNs/FCM)
app/cron.py              Retry queue dispatch + daily cleanup
migrations/                SQL, applied in order (001 schema, 002 drops campaign_contacts)
docs/CRYPTO.md           What the app must do so both sides agree (hashing, encryption)
pyproject.toml, uv.lock  Dependencies (ranges) and the exact pinned versions
```

`app/services/captcha.py` is unused. It is reserved for later, risk-based checks only.

---

## Licence

[GNU Affero General Public License v3.0](LICENSE). Networked modifications must be shared.
