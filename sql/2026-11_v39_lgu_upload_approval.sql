-- =====================================================================
-- sql/2026-11_v39_lgu_upload_approval.sql
-- ---------------------------------------------------------------------
-- V3.9: Gov't Data Upload is locked for an LGU account until an
-- Administrator approves it in Admin > Manage Users. As plain MySQL, for
-- the ERD and for anyone who prefers to apply it by hand.
--
-- YOU DO NOT HAVE TO RUN THIS. The app adds these columns on its own at
-- start-up (app/services/startup_migrations.py). Running it on a
-- database the app has already started against fails on "Duplicate
-- column" -- that just means it is already done.
--
--   upload_approved_at  when an Administrator approved the account for
--                       Gov't Data Upload; NULL = locked (a new LGU
--                       sign-up, or approval removed with "Lock").
--   upload_approved_by  the approving Administrator's user_id (NULL for
--                       the demo account, or approval kept from V3.7).
--   upload_template     the Custom Template: which columns this LGU office
--                       can provide, per template, as JSON. NULL = the
--                       suggested columns.
-- =====================================================================

ALTER TABLE `user` ADD COLUMN upload_approved_at DATETIME NULL;
ALTER TABLE `user` ADD COLUMN upload_approved_by INT NULL;
ALTER TABLE `user` ADD COLUMN upload_template TEXT NULL;

-- To approve an LGU account by hand (normally: Manage Users > Approve upload):
--   UPDATE `user` SET upload_approved_at = UTC_TIMESTAMP() WHERE email = 'someone@tarlaccity.gov.ph' AND role = 'LGU';
