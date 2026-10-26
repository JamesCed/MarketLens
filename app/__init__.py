"""
app/__init__.py
-----------------
The Flask "application factory". Everything the app needs -- config,
database, login manager, CSRF protection, blueprints (controllers) and
error pages -- is wired together in create_app() so tests and the real
server (app.py) both start from the same place.
"""

import os
from flask import Flask, render_template

from app.config import config_by_name
from app.extensions import db, login_manager, csrf, cache


def _harden_mysql_engine(app):
    """MySQL (Aiven) connections that never hang a worker: give up on a
    connection after 10 s and on a query after DB_QUERY_TIMEOUT seconds
    (default 110, just inside gunicorn's 120 s), and recycle pooled
    connections before the server's idle timeout drops them. An upload
    that cannot reach the database then fails with a message instead of
    a progress card that waits forever. SQLite (tests) is left alone."""
    uri = app.config.get("SQLALCHEMY_DATABASE_URI") or ""
    if not uri.startswith("mysql"):
        return
    timeout = int(float(os.environ.get("DB_QUERY_TIMEOUT", "110") or 110))
    options = dict(app.config.get("SQLALCHEMY_ENGINE_OPTIONS") or {})
    options.setdefault("pool_pre_ping", True)
    options.setdefault("pool_recycle", 280)
    connect_args = dict(options.get("connect_args") or {})
    connect_args.setdefault("connect_timeout", 10)
    connect_args.setdefault("read_timeout", timeout)
    connect_args.setdefault("write_timeout", timeout)
    options["connect_args"] = connect_args
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = options


def create_app(config_name=None):
    config_name = config_name or os.environ.get("FLASK_CONFIG", "development")

    app = Flask(__name__, instance_relative_config=True)
    selected = config_by_name[config_name]
    app.config.from_object(selected)

    selected.validate(app)

    if not app.config.get("DEBUG") and not app.config.get("TESTING"):
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["MODEL_DIR"], exist_ok=True)

    _harden_mysql_engine(app)
    db.init_app(app)

    if cache is not None:
        cache.init_app(app, config={
            "CACHE_TYPE": "FileSystemCache",
            "CACHE_DIR": os.path.join(app.instance_path, "response_cache"),
            "CACHE_DEFAULT_TIMEOUT": 900,
            "CACHE_THRESHOLD": 200,
        })
    login_manager.init_app(app)
    csrf.init_app(app)

    from app import models

    from app.models.archive import install_archive_filter

    install_archive_filter()

    @app.before_request
    def _record_last_seen():
        from flask_login import current_user as _user

        try:
            if _user.is_authenticated and _user.touch_last_seen():
                from app.extensions import db as _db

                _db.session.commit()
        except Exception:
            from app.extensions import db as _db

            _db.session.rollback()

    @app.route("/healthz")
    def healthz():
        return "ok", 200, {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"}

    from app.controllers.auth_controller import auth_bp
    from app.controllers.sme_controller import sme_bp
    from app.controllers.lgu_controller import lgu_bp
    from app.controllers.admin_controller import admin_bp
    from app.controllers.profile_controller import profile_bp
    from app.controllers.api_controller import api_bp
    from app.controllers.community_controller import community_bp
    from app.controllers.onboarding_controller import onboarding_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(sme_bp)
    app.register_blueprint(lgu_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(community_bp)
    app.register_blueprint(onboarding_bp)

    from app.ml.constants import short_industry_label

    app.jinja_env.globals["short_industry"] = short_industry_label

    from app.utils.privacy import mask_email, mask_phone

    app.jinja_env.filters["mask_email"] = mask_email
    app.jinja_env.filters["mask_phone"] = mask_phone

    from datetime import timedelta as _timedelta

    def _pht(value, fmt="%b %d, %Y %I:%M %p"):
        """A stored UTC time shown in Philippine time."""
        return (value + _timedelta(hours=8)).strftime(fmt) + " PHT" if value else "—"

    app.jinja_env.filters["pht"] = _pht

    def _staging_waiting(status):
        """How many upload batches are waiting at a step (sidebar badge)."""
        from app.services.staging_service import count_waiting

        try:
            return count_waiting(status)
        except Exception:
            return 0

    app.jinja_env.globals["staging_waiting"] = _staging_waiting

    from app.services.account_params import PASSWORD_HINT, PASSWORD_MIN_LENGTH, PASSWORD_RULES

    app.jinja_env.globals["PASSWORD_RULES"] = [(key, label) for key, label, _test in PASSWORD_RULES]
    app.jinja_env.globals["PASSWORD_HINT"] = PASSWORD_HINT
    app.jinja_env.globals["PASSWORD_MIN_LENGTH"] = PASSWORD_MIN_LENGTH
    app.jinja_env.globals["MIN_CAPITAL"] = int(app.config.get("MIN_PLAN_CAPITAL", 5000))
    def score_tone(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "fair"
        return "good" if number >= 6.5 else ("fair" if number >= 4 else "low")

    app.jinja_env.globals["score_tone"] = score_tone
    app.jinja_env.globals["SCORE_COLORS"] = {"good": "#16a34a", "fair": "#d97706", "low": "#dc2626"}

    from app.ml.seed_data import BARANGAY_NAMES

    app.jinja_env.globals["BARANGAY_LIST"] = sorted(BARANGAY_NAMES)

    app.jinja_env.globals["DPO"] = {
        "name": app.config.get("DPO_NAME", ""),
        "office": app.config.get("DPO_OFFICE", ""),
        "email": app.config.get("DPO_EMAIL", ""),
        "phone": app.config.get("DPO_PHONE", ""),
    }

    def _retention_days():
        from app.services.retention_service import DEFAULT_AUDIT_DAYS, DEFAULT_UPLOAD_DAYS, retention_days

        try:
            return retention_days()
        except Exception:
            return DEFAULT_UPLOAD_DAYS, DEFAULT_AUDIT_DAYS

    app.jinja_env.globals["retention_days"] = _retention_days

    from app.utils.captcha import client_config as _captcha_config, warn_if_disabled

    app.jinja_env.globals["captcha_config"] = _captcha_config
    app.jinja_env.globals["TERMS_UPDATED"] = app.config.get("TERMS_UPDATED", "")
    warn_if_disabled(app)

    @app.context_processor
    def inject_globals():
        from flask_login import current_user
        from app.models import Notification
        from app.ml.constants import BUSINESS_TYPES
        from app.ml.subcategories import as_client_payload as plan_subcategories

        unread_count = 0
        if current_user.is_authenticated:
            unread_count = Notification.unread_count_for(current_user.user_id)
        return {
            "GOOGLE_MAPS_JS_API_KEY": app.config.get("GOOGLE_MAPS_JS_API_KEY", ""),
            "unread_notification_count": unread_count,
            "ALL_BUSINESS_TYPES": BUSINESS_TYPES,
            "PLAN_SUBCATEGORIES": plan_subcategories(),
            "SUPPORT_EMAIL": app.config.get("SUPPORT_EMAIL", ""),
        }

    from app.services.startup_migrations import run_startup_migrations

    run_startup_migrations(app)

    from app.services.retention_service import install as install_retention

    install_retention(app)

    from sqlalchemy.orm import Session as _OrmSession

    from app.utils.http_perf import install_api_etags, install_compression, install_static_versioning
    from app.utils.request_memo import install_flush_invalidation

    install_static_versioning(app)
    if app.config.get("COMPRESS_RESPONSES", True):
        install_compression(app)
    install_api_etags(app)
    install_flush_invalidation(_OrmSession)

    if app.config.get("WARM_ON_BOOT"):
        from app.services.warmup import warm_up

        warm_up(app)

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(_e):
        return render_template("errors/500.html"), 500

    return app
