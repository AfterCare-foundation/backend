-- Codes closed for good: when a device deletes all its data, every code it was
-- on is burned, so nobody new can take the freed slot. Only the card hash is
-- stored, no device and no date of any person. Cleanup removes a row after
-- SUBSCRIPTION_TTL_DAYS, when the other holder's subscription has expired too.

CREATE TABLE burned_codes (
    et_hash     CHAR(64) PRIMARY KEY,
    burned_date DATE NOT NULL DEFAULT CURRENT_DATE
);

CREATE INDEX idx_burned_codes_burned_date ON burned_codes (burned_date);
