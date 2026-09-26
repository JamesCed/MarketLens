-- =====================================================================
-- Decision Support System for SME Business Planning and Market Entry
-- Using AI-Based Market Saturation Forecasting
--
-- Database: dss_db  (MySQL 8.0+)
--
-- *** WARNING -- READ BEFORE RUNNING THIS FILE ***
-- Every CREATE TABLE below is preceded by DROP TABLE IF EXISTS. Your
-- team already has a LIVE dss_db database with a real registered user
-- in it (from the dss_db.zip export). Running this file against that
-- database WILL DELETE that data.
--
-- This file exists for two situations only:
--   1. Setting up a BRAND-NEW, EMPTY database for someone else on the
--      team (or a fresh grading machine) that mirrors the schema you
--      already supplied, exactly, plus the 4 small additive tables the
--      Admin/SME notification features need (see below).
--   2. Reference -- reading it to see the full table layout in one
--      place instead of jumping between 5 model files.
--
-- For your EXISTING database, just run `python seed.py` instead -- it
-- uses db.create_all(), which only creates tables that don't already
-- exist and NEVER drops or touches your 5 real tables.
--
-- The 5 CORE tables below (user, sme_profile, market_data, lgu_data,
-- forecast_result) are transcribed EXACTLY from the dss_db_*.sql dump
-- your team supplied -- column names, types, ENUM values, and FKs are
-- unchanged. notifications / plan_saves / audit_logs / system_settings
-- are ADDITIVE tables layered on top for the Admin Module / AI early
-- warning / "Save to My Plans" features; they only reference
-- user.user_id and forecast_result.forecast_id and do not alter your
-- given tables in any way.
-- =====================================================================

CREATE DATABASE IF NOT EXISTS dss_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE dss_db;

SET FOREIGN_KEY_CHECKS = 0;

-- ---------------------------------------------------------------------
-- USER — one row per login account. role decides which module of the
-- system (Admin / SME / LGU) the account can reach (RBAC).
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS user;
CREATE TABLE user (
    user_id         INT AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(100)  NOT NULL,
    email           VARCHAR(150)  NOT NULL UNIQUE,
    password        VARCHAR(255)  NOT NULL,
    role            ENUM('Admin','SME','LGU') NOT NULL,
    contact_number  VARCHAR(20),
    status          ENUM('active','inactive') NOT NULL DEFAULT 'active',
    created_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- SME_PROFILE — a business plan/scenario an SME user wants analyzed.
-- USER 1—M SME_PROFILE. `location` is free text (no barangays table).
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS sme_profile;
CREATE TABLE sme_profile (
    sme_id              INT AUTO_INCREMENT PRIMARY KEY,
    user_id             INT           NOT NULL,
    business_name       VARCHAR(150)  NOT NULL,
    industry_type       VARCHAR(100)  NOT NULL,
    location            VARCHAR(150)  NOT NULL,
    startup_capital     DECIMAL(12,2),
    registration_date   DATE,
    employee_count      INT,
    business_stage      ENUM('startup','existing') NOT NULL DEFAULT 'startup',
    monthly_revenue_est DECIMAL(12,2),
    status              ENUM('active','inactive') NOT NULL DEFAULT 'active',
    CONSTRAINT fk_sme_profile_user FOREIGN KEY (user_id) REFERENCES user(user_id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- MARKET_DATA — a snapshot of market conditions for one
-- industry_type + location on one date. Populated from the Google
-- Places API (competitor_count) or PSA/DTI/Manual uploads.
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS market_data;
CREATE TABLE market_data (
    market_id                INT AUTO_INCREMENT PRIMARY KEY,
    industry_type            VARCHAR(100)  NOT NULL,
    location                 VARCHAR(150)  NOT NULL,
    competitor_count         INT,
    population_density       DECIMAL(10,2),
    historical_success_rate  DECIMAL(5,2)  COMMENT '0.00-1.00 fraction',
    foot_traffic_index       DECIMAL(6,2),
    average_rent             DECIMAL(10,2),
    source                   ENUM('Google Places API','Manual','PSA','DTI') NOT NULL,
    date_recorded            DATE          NOT NULL,
    INDEX idx_market_data_lookup (industry_type, location, date_recorded)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- LGU_DATA — the parsed government record itself (permits, zoning,
-- closures, business density) per barangay. Inserted directly by the
-- Gov't Data Upload page (app/services/data_import_service.py) -- there
-- is no separate uploaded-file-metadata table.
-- USER 1—M LGU_DATA (uploaded_by)
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS lgu_data;
CREATE TABLE lgu_data (
    lgu_id            INT AUTO_INCREMENT PRIMARY KEY,
    source            ENUM('DTI','CLUP','Other') NOT NULL,
    zoning_info       TEXT,
    closure_records   INT,
    barangay          VARCHAR(100)  NOT NULL,
    permit_count      INT,
    business_density  DECIMAL(10,2),
    effective_date    DATE,
    upload_date       DATE          NOT NULL DEFAULT (CURRENT_DATE),
    uploaded_by       INT           NOT NULL,
    CONSTRAINT fk_lgu_data_user FOREIGN KEY (uploaded_by) REFERENCES user(user_id) ON DELETE RESTRICT,
    INDEX idx_lgu_data_barangay (barangay)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- FORECAST_RESULT — one row per time the AI engine is run for a real
-- SME plan. sme_id/market_id/lgu_id are ALL NOT NULL -- every forecast
-- must be tied to a real SmeProfile + a real MarketData snapshot + a
-- real (possibly auto-generated placeholder) LguData row. There is no
-- "anonymous"/aggregate forecast row and no stored cluster_label
-- (derived live from saturation_index -- see app/ml/constants.py).
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS forecast_result;
CREATE TABLE forecast_result (
    forecast_id           INT AUTO_INCREMENT PRIMARY KEY,
    sme_id                INT           NOT NULL,
    market_id             INT           NOT NULL,
    lgu_id                INT           NOT NULL,
    viability_score       DECIMAL(5,2)  COMMENT '0-10 scale shown to users',
    input_industry_type   VARCHAR(100),
    input_location        VARCHAR(150),
    saturation_index      DECIMAL(5,2)  COMMENT '0-100 percentage. >75 = oversaturated',
    recommendation        TEXT,
    confidence_level      DECIMAL(5,2)  COMMENT '0-100, from Random Forest tree agreement',
    model_version         VARCHAR(20),
    forecast_date         DATE          NOT NULL,
    CONSTRAINT fk_forecast_sme    FOREIGN KEY (sme_id)    REFERENCES sme_profile(sme_id)   ON DELETE CASCADE,
    CONSTRAINT fk_forecast_market FOREIGN KEY (market_id) REFERENCES market_data(market_id) ON DELETE CASCADE,
    CONSTRAINT fk_forecast_lgu    FOREIGN KEY (lgu_id)    REFERENCES lgu_data(lgu_id)       ON DELETE CASCADE,
    INDEX idx_forecast_lookup (sme_id, forecast_date)
) ENGINE=InnoDB;

-- =====================================================================
-- ADDITIVE TABLES -- not part of the 5-table ERD you supplied. Only
-- reference user.user_id / forecast_result.forecast_id; safe to add
-- alongside your existing data.
-- =====================================================================

-- ---------------------------------------------------------------------
-- NOTIFICATIONS — AI early-warning alerts + generic info messages shown
-- on the notification bell.
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS notifications;
CREATE TABLE notifications (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    user_id             INT NOT NULL,
    forecast_result_id  INT NULL,
    type                ENUM('early_warning','info','system') NOT NULL DEFAULT 'info',
    message             VARCHAR(500) NOT NULL,
    is_read             TINYINT(1) NOT NULL DEFAULT 0,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_notifications_user     FOREIGN KEY (user_id)            REFERENCES user(user_id)                 ON DELETE CASCADE,
    CONSTRAINT fk_notifications_forecast FOREIGN KEY (forecast_result_id) REFERENCES forecast_result(forecast_id)  ON DELETE SET NULL
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- PLAN_SAVES — "Save to My Plans" button on the Recommendations page.
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS plan_saves;
CREATE TABLE plan_saves (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    user_id             INT NOT NULL,
    forecast_result_id  INT NOT NULL,
    saved_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_plan_saves_user     FOREIGN KEY (user_id)            REFERENCES user(user_id)                ON DELETE CASCADE,
    CONSTRAINT fk_plan_saves_forecast FOREIGN KEY (forecast_result_id) REFERENCES forecast_result(forecast_id) ON DELETE CASCADE,
    UNIQUE KEY uq_plan_saves (user_id, forecast_result_id)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- AUDIT_LOGS — Admin Module "Audit Trail". Written on login, uploads,
-- account changes, and config changes.
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS audit_logs;
CREATE TABLE audit_logs (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    user_id             INT NULL,
    action              VARCHAR(100) NOT NULL,
    details             VARCHAR(500),
    ip_address          VARCHAR(45),
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_audit_logs_user FOREIGN KEY (user_id) REFERENCES user(user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------
-- SYSTEM_SETTINGS — Admin Module "System configuration". Key/value store
-- for clustering K, MSI weights (w1/w2/w3), early-warning threshold,
-- Places result cap, and the LLM-recommendation on/off switch.
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS system_settings;
CREATE TABLE system_settings (
    setting_key         VARCHAR(100) PRIMARY KEY,
    setting_value       VARCHAR(255) NOT NULL,
    description         VARCHAR(255),
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

SET FOREIGN_KEY_CHECKS = 1;
