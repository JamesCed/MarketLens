-- =====================================================================
-- sql/2026-09_revisions.sql
-- ---------------------------------------------------------------------
-- What the September 2026 revisions round adds to dss_db, as plain MySQL,
-- for the ERD and for anyone who prefers to apply it by hand.
--
-- YOU DO NOT HAVE TO RUN THIS. The app applies exactly these changes on
-- its own at start-up (app/services/startup_migrations.py): missing
-- columns are ADDed, missing tables are CREATEd, nothing is dropped and
-- no existing row is changed. Running this file on a database the app
-- has already started against will fail on "Duplicate column" -- that
-- just means it is already done.
--
--   * Archive instead of delete: archived_at / archived_by /
--     archive_reason on user, market_data and lgu_data.
--   * The five W's on audit_logs: who (actor_name, actor_role), what
--     (target_type, target_id, target_label), where (http_method, route,
--     user_agent), why (reason). When stays created_at.
--   * Broader business parameters on sme_profile: subcategory,
--     product_offering, innovation_idea, offering_details (JSON price
--     list). monthly_revenue_est is KEPT but no longer collected or read.
--   * First-time walkthrough: user.onboarding_state.
--   * New tables: subcategory_market_data (direct-competitor counts) and
--     the community forum (forum_channels, forum_posts, forum_comments,
--     forum_reports, forum_helpful).
-- =====================================================================

-- user
ALTER TABLE `user` ADD COLUMN onboarding_state VARCHAR(20) NULL;
ALTER TABLE `user` ADD COLUMN archived_at DATETIME NULL;
ALTER TABLE `user` ADD COLUMN archived_by INT NULL;
ALTER TABLE `user` ADD COLUMN archive_reason VARCHAR(255) NULL;

-- lgu_data
ALTER TABLE lgu_data ADD COLUMN archived_at DATETIME NULL;
ALTER TABLE lgu_data ADD COLUMN archived_by INT NULL;
ALTER TABLE lgu_data ADD COLUMN archive_reason VARCHAR(255) NULL;

-- market_data
ALTER TABLE market_data ADD COLUMN archived_at DATETIME NULL;
ALTER TABLE market_data ADD COLUMN archived_by INT NULL;
ALTER TABLE market_data ADD COLUMN archive_reason VARCHAR(255) NULL;

-- sme_profile
ALTER TABLE sme_profile ADD COLUMN subcategory VARCHAR(100) NULL;
ALTER TABLE sme_profile ADD COLUMN product_offering TEXT NULL;
ALTER TABLE sme_profile ADD COLUMN innovation_idea TEXT NULL;
ALTER TABLE sme_profile ADD COLUMN offering_details TEXT NULL;

-- audit_logs
ALTER TABLE audit_logs ADD COLUMN actor_name VARCHAR(100) NULL;
ALTER TABLE audit_logs ADD COLUMN actor_role VARCHAR(20) NULL;
ALTER TABLE audit_logs ADD COLUMN target_type VARCHAR(50) NULL;
ALTER TABLE audit_logs ADD COLUMN target_id VARCHAR(50) NULL;
ALTER TABLE audit_logs ADD COLUMN target_label VARCHAR(255) NULL;
ALTER TABLE audit_logs ADD COLUMN http_method VARCHAR(10) NULL;
ALTER TABLE audit_logs ADD COLUMN route VARCHAR(255) NULL;
ALTER TABLE audit_logs ADD COLUMN user_agent VARCHAR(255) NULL;
ALTER TABLE audit_logs ADD COLUMN reason VARCHAR(255) NULL;

CREATE TABLE subcategory_market_data (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	industry_type VARCHAR(100) NOT NULL, 
	subcategory VARCHAR(100) NOT NULL, 
	location VARCHAR(150) NOT NULL, 
	competitor_count INTEGER NOT NULL, 
	source VARCHAR(40) NOT NULL, 
	date_recorded DATE NOT NULL, 
	PRIMARY KEY (id)
) ENGINE=InnoDB;

CREATE TABLE forum_channels (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	slug VARCHAR(60) NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	description VARCHAR(255), 
	icon VARCHAR(40) NOT NULL, 
	sort_order INTEGER NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY (id)
) ENGINE=InnoDB;

CREATE TABLE forum_posts (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	channel_id INTEGER NOT NULL, 
	author_id INTEGER NOT NULL, 
	title VARCHAR(120) NOT NULL, 
	body TEXT NOT NULL, 
	helpful_count INTEGER NOT NULL DEFAULT '0', 
	reviewed_by INTEGER, 
	status VARCHAR(20) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	reviewed_at DATETIME, 
	moderation_note VARCHAR(255), 
	filter_flags VARCHAR(255), 
	ai_verdict VARCHAR(20), 
	ai_reason VARCHAR(255), 
	PRIMARY KEY (id), 
	FOREIGN KEY(channel_id) REFERENCES forum_channels (id), 
	FOREIGN KEY(author_id) REFERENCES user (user_id) ON DELETE CASCADE, 
	FOREIGN KEY(reviewed_by) REFERENCES user (user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE forum_comments (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	post_id INTEGER NOT NULL, 
	author_id INTEGER NOT NULL, 
	body TEXT NOT NULL, 
	reviewed_by INTEGER, 
	status VARCHAR(20) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	reviewed_at DATETIME, 
	moderation_note VARCHAR(255), 
	filter_flags VARCHAR(255), 
	ai_verdict VARCHAR(20), 
	ai_reason VARCHAR(255), 
	PRIMARY KEY (id), 
	FOREIGN KEY(post_id) REFERENCES forum_posts (id) ON DELETE CASCADE, 
	FOREIGN KEY(author_id) REFERENCES user (user_id) ON DELETE CASCADE, 
	FOREIGN KEY(reviewed_by) REFERENCES user (user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE forum_reports (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	reporter_id INTEGER NOT NULL, 
	content_type VARCHAR(10) NOT NULL, 
	content_id INTEGER NOT NULL, 
	reason VARCHAR(20) NOT NULL, 
	note VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	resolved_at DATETIME, 
	resolved_by INTEGER, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_forum_report_once UNIQUE (reporter_id, content_type, content_id), 
	FOREIGN KEY(reporter_id) REFERENCES user (user_id) ON DELETE CASCADE, 
	FOREIGN KEY(resolved_by) REFERENCES user (user_id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE forum_helpful (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id INTEGER NOT NULL, 
	post_id INTEGER NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_forum_helpful_once UNIQUE (user_id, post_id), 
	FOREIGN KEY(user_id) REFERENCES user (user_id) ON DELETE CASCADE, 
	FOREIGN KEY(post_id) REFERENCES forum_posts (id) ON DELETE CASCADE
) ENGINE=InnoDB;
