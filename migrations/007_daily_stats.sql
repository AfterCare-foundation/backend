-- Anonymous usage counts, one number per metric per day, kept indefinitely
-- (the 180-day cleanup does not touch this table). No device, no card hash,
-- no STI type, no location: nothing here can point back to a person.
-- Retrieve with plain SQL, e.g.:
--   SELECT date_trunc('month', day) AS month, metric, SUM(value)
--   FROM daily_stats GROUP BY 1, 2 ORDER BY 1, 2;

CREATE TABLE daily_stats (
    day    DATE   NOT NULL,
    metric TEXT   NOT NULL,
    value  BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, metric)
);
