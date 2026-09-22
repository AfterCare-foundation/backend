# AfterCare backend

Anonymous STI exposure notification for [AfterCare](https://after-care.eu/). People connect with a tearable paper card; later, if someone tests positive, they can warn those connections without names, numbers, or an account.

This repo is the **notification server** (Python / FastAPI / PostgreSQL). It is open source under [AGPL-3.0](LICENSE).

**Status:** club (paper card) flow only. In development. Not production-ready.

Sauna wristband pairing, CAPTCHA, and anomaly detection are not in this codebase yet.

---

## What the server stores

| Stored | Not stored |
|---|---|
| `SHA-256` of the card token | Raw QR token |
| `SHA-256` of the device credential | Names, email, phone, location |
| Hashed push ID + the real push token (Apple/Google need it to deliver) | IP addresses |
| UTC **date** a card was scanned (`created_date`) | Encounter time |
| Encrypted bytes for scheduled sends, deleted after dispatch | Plaintext STI type |

Subscriptions expire after **60 days** and are deleted automatically.

The infection name (gonorrhoea, syphilis, HIV, Mpox, HPV, …) is chosen in the **app** at notify time. The card is not tied to an STI. The phone should encrypt that choice; the lock-screen text is always generic: *Someone you connected with may have an STI.*

Each card has its own token, so the app encrypts the STI type **once per contact**. Hashing and AES-GCM details: [docs/CRYPTO.md](docs/CRYPTO.md).

---

## How a card becomes a notification

1. Both people scan their half → `POST /subscribe` (same token hash on both phones).
2. Later, one person taps Notify → `POST /notify` (must already be subscribed to that token).
3. The sender is excluded from the recipient list.
4. Immediate sends go to APNs/FCM (or the console in stub mode). Future sends sit in `pending_notifications` until a background job runs (every 15 minutes).

### Notify limits (per device)

- At least **1 day** between campaigns (not two on the same day).
- At most **4 campaigns** in a rolling **30 days**.
- At most **100 unique card tokens** per campaign.

A *campaign* is one tap of Notify in the app (`campaign_id`). Several HTTP calls with the same id count as one campaign.

---

## Setup (local)

You need **Python 3.11+** and **PostgreSQL**. This project uses a dedicated database so it stays separate from other work on the same machine.

```bash
# Database (once)
createuser aftercare_dev --pwprompt   # or use the role already created
createdb aftercare_dev --owner=aftercare_dev
psql -h localhost -U aftercare_dev -d aftercare_dev -f migrations/001_initial.sql

# App
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit DATABASE_URL if needed. Leave PUSH_STUB_MODE=true until you have APNs/FCM keys.

uvicorn app.main:app --reload --no-access-log
```

- Health: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)
- API docs (only if `ENVIRONMENT=development`): [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

Never commit `.env`, `.venv/`, or `secrets/`.

```bash
pytest
```

The connection pool sets PostgreSQL `TimeZone` to **UTC** for this app only.

---

## Configuration

See `.env.example`. Important flags:

| Variable | Meaning |
|---|---|
| `ENVIRONMENT` | `development` enables `/docs`. Use `production` on the host. |
| `PUSH_STUB_MODE` | `true` logs pushes instead of calling Apple/Google. |
| `RUN_BACKGROUND_JOBS` | Dispatcher + 60-day cleanup in this process. |
| `NOTIFY_*` | Campaign and contact caps (see above). |

Production must use **HTTPS**. The device credential is sent on each mutating request; it is a bearer secret.

On the load balancer, disable access logs and strip client IPs (see comments in `Procfile`).

---

## HTTP API

All hashes are **64 lowercase hex characters** (SHA-256).  
`device_credential` is **32 random bytes** as 64 lowercase hex; stored as `SHA-256` of those bytes. Keep it in the OS keystore on the phone.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness |
| `POST` | `/subscribe` | Register this device on a scanned card |
| `DELETE` | `/subscribe` | Erase this device (GDPR) |
| `POST` | `/notify` | Send or schedule notifications for one or more tokens |
| `POST` | `/schedule` | Same as notify for a single future delivery |
| `POST` | `/update-push-id` | Push token changed (reinstall / permissions) |

`POST /notify` body (shape):

```json
{
  "sender_push_id_hash": "<sha256 hex>",
  "device_credential": "<64 hex>",
  "campaign_id": "<uuid>",
  "deliveries": [
    { "et_hash": "<sha256 hex>", "encrypted_payload": "<ciphertext for this card>" },
    {
      "et_hash": "<sha256 hex>",
      "encrypted_payload": "<ciphertext for this card>",
      "scheduled_at": "2026-10-01T12:00:00+00:00"
    }
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
app/cron.py              Pending dispatch + TTL cleanup
migrations/001_initial.sql
```

`app/services/captcha.py` is unused. It is reserved for later, risk-based checks only.

---

## Licence

[GNU Affero General Public License v3.0](LICENSE). Networked modifications must be shared.
