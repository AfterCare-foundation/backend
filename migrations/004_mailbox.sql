-- Mailbox: the server holds each notification's ciphertext until the
-- recipient's app fetches and confirms it (or MAILBOX_TTL_DAYS pass).
-- The push itself carries no ciphertext, only a wake-up.
--
-- Privacy: a row knows only the recipient device, the opaque ciphertext and
-- a date. No card hash, no sender, no time of day.
-- `wake_pending` = the lock-screen push has not been accepted yet.
-- `seq` only keeps the order in which messages were stored.

CREATE TABLE mailbox (
    id                  UUID        NOT NULL DEFAULT gen_random_uuid(),
    seq                 BIGINT      GENERATED ALWAYS AS IDENTITY,
    push_id_hash        CHAR(64)    NOT NULL,
    encrypted_payload   TEXT        NOT NULL,
    created_date        DATE        NOT NULL DEFAULT CURRENT_DATE,
    wake_pending        BOOLEAN     NOT NULL DEFAULT TRUE,

    PRIMARY KEY (id),
    FOREIGN KEY (push_id_hash) REFERENCES devices (push_id_hash)
        ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE INDEX idx_mailbox_push_id_hash ON mailbox (push_id_hash);
CREATE INDEX idx_mailbox_created_date ON mailbox (created_date);

-- The retry queue is replaced by the mailbox. It also held the sender hash.
DROP TABLE pending_notifications;
