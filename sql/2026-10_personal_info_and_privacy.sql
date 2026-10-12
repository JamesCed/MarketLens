-- =====================================================================
-- sql/2026-10_personal_info_and_privacy.sql
-- ---------------------------------------------------------------------
-- What the IT expert review (October 2026) adds to dss_db, as plain
-- MySQL, for the ERD and for anyone who prefers to apply it by hand.
--
-- YOU DO NOT HAVE TO RUN THIS. The app applies exactly these changes on
-- its own at start-up (app/services/startup_migrations.py): missing
-- columns are ADDed, nothing is dropped. Running this file on a database
-- the app has already started against fails on "Duplicate column" --
-- that just means it is already done.
--
--   * Personal information in full: the name in parts (first, middle,
--     last, suffix). `user.name` stays the display name, composed from
--     the parts.
--
--   * Gender, birthday, contact number and address are stored ENCRYPTED
--     (Fernet: AES-128 + HMAC-SHA256, app/utils/privacy.py), hence TEXT
--     columns ending in _enc. They hold ciphertext beginning "enc1:".
--     The original `contact_number` VARCHAR(20) is too short for
--     ciphertext; it is kept (the given schema is not altered) and the
--     app moves any number still in it into contact_number_enc, then
--     clears it.
--
--   * terms_accepted_at: when the account accepted the Terms and
--     Conditions and Data Privacy Notice at sign-up.
--
--   * The age is never stored -- it is worked out from the birthday.
--
--   * The minimum capital (PHP 5,000) and the barangay dropdown are
--     application rules (app/services/plan_params.py), not schema.
-- =====================================================================

ALTER TABLE `user` ADD COLUMN first_name VARCHAR(60) NULL;
ALTER TABLE `user` ADD COLUMN middle_name VARCHAR(60) NULL;
ALTER TABLE `user` ADD COLUMN last_name VARCHAR(60) NULL;
ALTER TABLE `user` ADD COLUMN suffix VARCHAR(10) NULL;
ALTER TABLE `user` ADD COLUMN contact_number_enc TEXT NULL;
ALTER TABLE `user` ADD COLUMN gender_enc TEXT NULL;
ALTER TABLE `user` ADD COLUMN birthdate_enc TEXT NULL;
ALTER TABLE `user` ADD COLUMN address_line_enc TEXT NULL;
ALTER TABLE `user` ADD COLUMN address_barangay_enc TEXT NULL;
ALTER TABLE `user` ADD COLUMN terms_accepted_at DATETIME NULL;

-- Plain-text contact numbers are encrypted by the APP at start-up (it
-- holds the key; SQL cannot). Nothing to do here for them.
