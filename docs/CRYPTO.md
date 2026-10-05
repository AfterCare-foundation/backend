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
| `campaign_id` | New UUID per tap of Notify, sent in ONE request. An id can be used once (`409 campaign_already_used` if repeated). Never retry a failed push yourself: the server retries failed pushes for up to a day (`retrying` in the response says how many). If `pushed` and `scheduled` and `retrying` are all 0 (nobody else on the codes yet), the same id may be reused. |
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

Push custom field: `enc` = the same Base64 string you put in `encrypted_payload`.

If one person is reached through several of the sender's codes in the same
`/notify` call, they get ONE push. `enc` holds the first ciphertext and `more`
(a list of Base64 strings, present only then) holds the others. The app must
decrypt `enc` and every entry of `more` (each with the key from its own card)
and show each STI once. Android delivers `more` as a JSON-encoded string.

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
      "encrypted_payload": "<base64 nonce||ciphertext||tag>",
      "scheduled_at": null
    }
  ]
}
```

Omit `scheduled_at` for immediate send. If present, it must be UTC with a timezone offset.

Testing windows (when to schedule) are **client-only**. The server just stores `scheduled_at`.
