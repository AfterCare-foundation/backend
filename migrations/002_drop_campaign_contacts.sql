-- Data minimisation: a campaign_id can now be used only once, so the server no
-- longer needs to remember which contacts each campaign reached.
-- A campaign is now just: campaign_id, sender device, time.

DROP TABLE IF EXISTS campaign_contacts;
