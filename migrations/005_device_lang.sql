-- Language of the generic wake-up push, chosen by the app ('en' or 'de').
-- Anything else is stored as 'en'. Never logged and never returned by the API.

ALTER TABLE devices
    ADD COLUMN lang TEXT NOT NULL DEFAULT 'en' CHECK (lang IN ('en', 'de'));
