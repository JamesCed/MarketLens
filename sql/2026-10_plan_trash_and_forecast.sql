-- =====================================================================
-- sql/2026-10_plan_trash_and_forecast.sql
-- ---------------------------------------------------------------------
-- What the October 2026 revisions add to dss_db, as plain MySQL, for
-- the ERD and for anyone who prefers to apply it by hand.
--
-- YOU DO NOT HAVE TO RUN THIS. The app applies exactly these changes on
-- its own at start-up (app/services/startup_migrations.py): missing
-- columns are ADDed, nothing is dropped and no existing row is changed.
-- Running this file on a database the app has already started against
-- will fail on "Duplicate column" -- that just means it is already done.
--
--   * The plan Trash: archived_at / archived_by / archive_reason on
--     sme_profile. Removing a plan from the Home page moves it to Trash
--     (these three are set); restoring it clears them. A plan, and the
--     forecast history built on it, is never hard-deleted. The app hides
--     archived plans from every ordinary query -- see
--     app/models/archive.py.
--
--   * Capital is REQUIRED (> 0) from now on, and is labelled "Capital"
--     everywhere. That rule lives in the application
--     (app/services/plan_params.py), NOT in the schema: the column keeps
--     its original name, sme_profile.startup_capital, and stays NULLable,
--     because renaming a live column is a destructive migration and
--     plans saved before the rule must still load. Those older plans
--     still forecast (with capital adequacy 0) and the Home page asks
--     their owner to add a capital.
--
--   * The trained plan viability forecast needs no schema change: its
--     payload (inputs, financials, component scores and the per-driver
--     explanation of the model's output) is stored inside the existing
--     forecast_result.recommendation JSON, under the key "forecast", and
--     the narration under "explanation". model_version reads e.g.
--     "rf_v1+plan_rf_v1" (still VARCHAR(20)). See
--     Reference/FORECAST_MODEL.md.
-- =====================================================================

-- sme_profile: the plan Trash
ALTER TABLE sme_profile ADD COLUMN archived_at DATETIME NULL;
ALTER TABLE sme_profile ADD COLUMN archived_by INT NULL;
ALTER TABLE sme_profile ADD COLUMN archive_reason VARCHAR(255) NULL;

-- sme_profile.startup_capital: unchanged (DECIMAL(12,2) NULL). Shown as
-- "Capital"; required (> 0) by the application, not by the schema.
