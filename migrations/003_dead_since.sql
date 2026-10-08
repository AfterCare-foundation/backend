-- Push tokens that APNs/FCM report as dead (uninstalled app, replaced token).
-- The date of the first report; cleared when the app sends a new token.
-- Cleanup deletes subscriptions that stay dead past DEAD_TOKEN_GRACE_DAYS.

ALTER TABLE token_subscriptions ADD COLUMN dead_since DATE;

CREATE INDEX idx_token_subscriptions_dead_since
    ON token_subscriptions (dead_since) WHERE dead_since IS NOT NULL;
