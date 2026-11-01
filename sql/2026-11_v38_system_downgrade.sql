-- =====================================================================
-- sql/2026-11_v38_system_downgrade.sql
-- ---------------------------------------------------------------------
-- V3.8 system downgrade: what the database loses when the LGU Encoder /
-- Data Steward staging workflow and the LGU access verification
-- (V3.4-V3.6) are removed, as plain MySQL, for the ERD and for anyone
-- who prefers to apply it by hand.
--
-- YOU DO NOT HAVE TO RUN THIS. The app does exactly this on its own at
-- start-up (app/services/startup_migrations.py,
-- _retire_lgu_staging_schema), once, on the first deploy of V3.8. Run
-- by hand on a database that is already reverted, the DROP COLUMN lines
-- fail with "Can't DROP ... check that column/key exists" -- that just
-- means it is already done.
--
-- Afterwards the schema is the V3.3 one plus the four LGU context
-- columns the LGU registration form still fills in (user.lgu_office,
-- lgu_position, lgu_scope, lgu_barangay). lgu_data, market_data and
-- subcategory_market_data are not touched.
-- =====================================================================

-- 1. Keep the System Admin's decisions: an LGU account that was rejected
--    or whose access was removed is suspended. Every other LGU account
--    (verified or still pending) stays an ordinary active LGU account.
UPDATE `user` SET status = 'inactive'
 WHERE role = 'LGU' AND lgu_status IN ('rejected', 'revoked') AND status = 'active';

-- 2. Bell notifications about staged batches and access requests point
--    at pages that no longer exist.
DELETE FROM notifications
 WHERE type = 'system' AND (message LIKE '%batch #%' OR message LIKE '%LGU access%');

-- 3. The staging area and the LGU access requests.
DROP TABLE IF EXISTS upload_batches;
DROP TABLE IF EXISTS lgu_access_requests;

-- 4. LGU sub-role and verification columns on the account.
ALTER TABLE `user` DROP COLUMN lgu_role;
ALTER TABLE `user` DROP COLUMN lgu_status;
ALTER TABLE `user` DROP COLUMN lgu_verified_by;
ALTER TABLE `user` DROP COLUMN lgu_verified_at;

-- 5. Retired System Settings rows (LGU access verification, the K-Means
--    clusters field, the data retention card -- retention now runs with
--    fixed periods: uploads 1,095 days, audit trail 365 days).
DELETE FROM system_settings WHERE setting_key IN (
    'lgu_email_domains', 'lgu_verification_email', 'lgu_verification_office', 'lgu_access_review_days',
    'kmeans_n_clusters', 'upload_retention_days', 'audit_log_retention_days', 'retention_last_run'
);
