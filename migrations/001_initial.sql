-- AfterCare — Initial Schema
-- v1: paper card (club) flow only
-- Sauna/bracelet tables will be added in a later migration
--
-- Privacy principles reflected in this schema:
--   - No user table, no names, no emails, no phone numbers
--   - No IP addresses stored anywhere
--   - created_date is a UTC calendar date of the home scan, not the encounter
--   - Raw tokens and raw push IDs never stored — only their SHA-256 hashes
--   - push_token is the one exception: stored in plaintext because APNs/FCM
--     require the real token to deliver pushes. It is never logged.
--   - STI type is never stored. Queued rows (failed pushes waiting for retry) keep only client-encrypted bytes
--     until dispatch, then the row is deleted.
--   - All subscription data expires after SUBSCRIPTION_TTL_DAYS (see app/config.py)


-- ============================================================
-- TABLE 1: devices
--
-- One row per app install. Proves later requests come from the same
-- device that subscribed — not from someone who copied a push_id_hash.
-- The raw 256-bit credential never leaves the phone except over TLS
-- for one request; we store only SHA-256(credential bytes).
-- ============================================================

CREATE TABLE devices (
    push_id_hash        CHAR(64)    NOT NULL,
    credential_hash     CHAR(64)    NOT NULL,
    created_date        DATE        NOT NULL DEFAULT CURRENT_DATE,

    PRIMARY KEY (push_id_hash),
    UNIQUE (credential_hash)
);


-- ============================================================
-- TABLE 2: token_subscriptions
--
-- One row per (token, device) pair.
-- ============================================================

CREATE TABLE token_subscriptions (
    et_hash         CHAR(64)        NOT NULL,
    push_id_hash    CHAR(64)        NOT NULL,
    push_token      TEXT            NOT NULL,
    platform        VARCHAR(7)      NOT NULL CHECK (platform IN ('ios', 'android')),
    created_date    DATE            NOT NULL DEFAULT CURRENT_DATE,

    PRIMARY KEY (et_hash, push_id_hash),
    FOREIGN KEY (push_id_hash) REFERENCES devices (push_id_hash) ON DELETE CASCADE
);

CREATE INDEX idx_token_subscriptions_created_date
    ON token_subscriptions (created_date);

CREATE INDEX idx_token_subscriptions_push_id_hash
    ON token_subscriptions (push_id_hash);


-- ============================================================
-- TABLE 3: pending_notifications
--
-- Future deliveries. encrypted_payload is opaque ciphertext produced
-- on the sender's phone using a key derived from the raw card token.
-- The server cannot read it. Deleted immediately after dispatch.
-- ============================================================

CREATE TABLE pending_notifications (
    id                      UUID            NOT NULL DEFAULT gen_random_uuid(),
    et_hash                 CHAR(64)        NOT NULL,
    sender_push_id_hash     CHAR(64)        NOT NULL,
    encrypted_payload       TEXT            NOT NULL,
    scheduled_at            TIMESTAMPTZ     NOT NULL,

    PRIMARY KEY (id)
);

CREATE INDEX idx_pending_notifications_scheduled_at
    ON pending_notifications (scheduled_at);

CREATE INDEX idx_pending_notifications_et_hash
    ON pending_notifications (et_hash);


-- ============================================================
-- TABLE 4: notification_campaigns
--
-- One UUID per "Notify contacts" tap in the app.
-- Rate limit: at least 1 day between campaigns, and 4 per rolling 30 days.
-- Repeat /notify calls with the same campaign_id do not add a row.
-- ============================================================

CREATE TABLE notification_campaigns (
    campaign_id     UUID            NOT NULL,
    push_id_hash    CHAR(64)        NOT NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    PRIMARY KEY (campaign_id)
);

CREATE INDEX idx_notification_campaigns_created_at
    ON notification_campaigns (created_at);

CREATE INDEX idx_notification_campaigns_push_id_hash
    ON notification_campaigns (push_id_hash);


-- Unique card tokens already included in a campaign.
-- Caps one tap at 100 contacts, even across several HTTP calls.
CREATE TABLE campaign_contacts (
    campaign_id     UUID            NOT NULL,
    et_hash         CHAR(64)        NOT NULL,

    PRIMARY KEY (campaign_id, et_hash),
    FOREIGN KEY (campaign_id) REFERENCES notification_campaigns (campaign_id) ON DELETE CASCADE
);
