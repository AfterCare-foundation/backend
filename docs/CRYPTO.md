# AfterCare — app ↔ server crypto contract (v1)

The server never sees the raw card token and never decrypts STI types. Phones must hash and encrypt **exactly** as below so both sides interoperate.

Card QR: `https://after-care.eu/connect#et=<base64url(TOKEN)>`  
`TOKEN` = 16 cryptographically random bytes. The fragment is not sent in HTTP logs.

Hex means **lowercase**, no `0x`, no colons.

---

## 1. Values sent to the API

| Field | How to compute |
|---|---|
| `et_hash` | `SHA-256(TOKEN)` as 64 hex chars. Hash the **raw 16 bytes**, not the base64 in the URL. |
| `push_id_hash` | `SHA-256(UTF-8 bytes of the APNs/FCM token string)` as 64 hex. |
| `push_token` | The real APNs/FCM token string (needed to deliver). |
| `device_credential` | 32 random bytes as 64 hex. Create once, store in Keychain / Keystore. Send the hex secret; the server stores `SHA-256(those 32 bytes)`. |
| `platform` | `ios` or `android` |
| `campaign_id` | New UUID per tap of Notify, sent in ONE request. An id can be used once (`409 campaign_already_used` if repeated). Never retry a failed push yourself: the server retries a failed wake-up push for about a day. The message itself is already safe in the recipient's mailbox for 7 days, whatever happens to the push (`retrying` in the response says how many contacts have not been woken yet). If `pushed` and `retrying` are both 0 (nobody else on the codes yet), the same id may be reused. |
| `encrypted_payload` | Per card. See §2. |

Do not send the raw `TOKEN` to the server.

---

## 2. Encrypting the STI type (per contact)

Each card has its **own** `TOKEN`. Encrypt separately for each `et_hash`.

**Plaintext** (UTF-8 JSON), `sti` is an app-defined string:

```json
{"v":1,"sti":"gonorrhoea"}
```

Suggested `sti` values: `gonorrhoea`, `chlamydia`, `syphilis`, `hiv`, `mpox`, `hpv`, `other`. The server does not validate this list.

**Key** (AES-256, 32 bytes) — must **not** be `SHA-256(TOKEN)` alone. That value is `et_hash`; the server has it and could decrypt.

```
enc_key = SHA-256(  UTF-8("aftercare-enc-v1")  ||  TOKEN  )
```

`||` is byte concatenation. `TOKEN` is the 16 raw bytes.

**Cipher:** AES-256-GCM.

- 12-byte random nonce
- No extra AAD
- `encrypted_payload` = standard Base64 of `nonce || ciphertext || 16-byte tag`

The partner decrypts with the same `TOKEN` from their scan. Lock screen stays generic; show `sti` only inside the app after biometric unlock.

The push does **not** carry the ciphertext (no `enc`, no `more`). It only wakes the phone with the generic lock-screen text. The ciphertext waits on the server in the recipient's mailbox; the app collects it:

- `POST /inbox` with `{push_id_hash, device_credential}` returns `{"notifications": [{"id": "<uuid>", "enc": "<the Base64 string the sender put in encrypted_payload>"}]}`, oldest first, at most 200 per call. It deletes nothing.
- The app decrypts each `enc` with the key from the matching card, stores the result on the phone, and only then calls `POST /inbox/confirm` with `{push_id_hash, device_credential, ids: ["<uuid>", ...]}` (1 to 200 ids). Only confirmed messages are deleted.
- Fetch on every app open, on a tapped or foreground push, and again after confirming (there may be more than 200). A message nobody fetches is deleted after 7 days.
- If one person is reached through several of the sender's codes, they get one push but several messages. The app must try each card key on each message and show each STI once.
- `/dev/inbox` no longer exists. `/inbox` works in every mode, including stub mode.

---

## 3. Notify body (reminder)

```json
{
  "sender_push_id_hash": "<sha256 of this device's push token>",
  "device_credential": "<64 hex>",
  "campaign_id": "<uuid>",
  "deliveries": [
    {
      "et_hash": "<sha256 of this card's TOKEN>",
      "encrypted_payload": "<base64 nonce||ciphertext||tag>"
    }
  ]
}
```

Every notification is sent immediately. There is no scheduling: when a test is reliable again is a clinical question that the recipient's clinic answers, so the server has no timing logic. A delivery with a `scheduled_at` (or any other unknown field) is rejected with `422`. Response: `{"status": "ok", "pushed": n, "retrying": n, "contacts": n}`.
