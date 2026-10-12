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


def create_app(config_name=None):
    config_name = config_name or os.environ.get("FLASK_CONFIG", "development")

    app = Flask(__name__, instance_relative_config=True)
    selected = config_by_name[config_name]
    app.config.from_object(selected)

    # Fail fast on a deployment configured in a way that is unsafe rather
    # than merely inconvenient -- see ProductionConfig.validate() in
    # app/config.py. The development and testing configs check nothing.
    selected.validate(app)

    # ---- running behind a reverse proxy (Render, Railway, nginx) ----
    # The host terminates HTTPS and forwards the request on to this app
    # over plain http inside its own network. Without this, Flask believes
    # every request arrived over http: url_for(..., _external=True)
    # builds http:// links, the redirect after login drops to http, and a
    # Secure session cookie never gets sent back. ProxyFix tells Flask to
    # trust the X-Forwarded-* headers the proxy sets.
    #
    # Trusting those headers is only safe when a proxy really is in
    # front, because otherwise a client could set them itself -- so this
    # is applied for the production config ONLY, never for a local dev
    # server.
    if not app.config.get("DEBUG") and not app.config.get("TESTING"):
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["MODEL_DIR"], exist_ok=True)

    # ---- extensions ----
    db.init_app(app)

    # Response cache for the Trend Reports endpoints.
    #
    # FileSystemCache, not SimpleCache: SimpleCache keeps every cached
    # payload in the worker's own heap, and a trend report is a few
    # hundred KB. On a 512 MB instance that competes with the model and
    # the request itself for exactly the memory we are trying to
    # protect. The filesystem copy costs nothing resident and survives
    # for the life of the deploy.
    #
    # cache is None when Flask-Caching is not installed (see
    # app/extensions.py) -- the app runs without it, just slower.
    if cache is not None:
        cache.init_app(app, config={
            "CACHE_TYPE": "FileSystemCache",
            "CACHE_DIR": os.path.join(app.instance_path, "response_cache"),
            "CACHE_DEFAULT_TIMEOUT": 900,
            # Bounded so a long-running instance cannot fill its disk;
            # Flask-Caching prunes the oldest entries past this.
            "CACHE_THRESHOLD": 200,
        })
    login_manager.init_app(app)
    csrf.init_app(app)

    # ---- models must be imported before create_all()/migrations run ----
    from app import models  # noqa: F401

    # Archived market_data / lgu_data rows are invisible to every
    # ordinary query from here on -- see app/models/archive.py.
    from app.models.archive import install_archive_filter

    install_archive_filter()

    # LAST SEEN (Admin > Manage Users). Written at most once every
    # User.LAST_SEEN_RESOLUTION per account -- the account row is already
    # loaded for the request, so the check itself costs nothing, and the
    # write happens a few times an hour at most, not on every page.
    @app.before_request
    def _record_last_seen():
        from flask_login import current_user as _user

        try:
            if _user.is_authenticated and _user.touch_last_seen():
                from app.extensions import db as _db

                _db.session.commit()
        except Exception:  # noqa: BLE001 -- monitoring must never break a page
            from app.extensions import db as _db

            _db.session.rollback()

    # ---- blueprints (controllers) ----
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
    # The community is on Discord: /community redirects to the invite.
    app.register_blueprint(community_bp)
    app.register_blueprint(onboarding_bp)

    # ---- template globals ----
    # "Wholesale and Retail Trade; Repair of Motor Vehicles and
    # Motorcycles" does not fit on a plan chip; templates shorten it the
    # same way the rest of the app does.
    from app.ml.constants import short_industry_label

    app.jinja_env.globals["short_industry"] = short_industry_label

    # Confidential values are shown masked wherever a screen only needs
    # to tell records apart (app/utils/privacy.py).
    from app.utils.privacy import mask_email, mask_phone

    app.jinja_env.filters["mask_email"] = mask_email
    app.jinja_env.filters["mask_phone"] = mask_phone

    # The password rule, shown as a live checklist next to every "new
    # password" field (shared/_password_rules.html).
    from app.services.account_params import PASSWORD_HINT, PASSWORD_MIN_LENGTH, PASSWORD_RULES

    app.jinja_env.globals["PASSWORD_RULES"] = [(key, label) for key, label, _test in PASSWORD_RULES]
    app.jinja_env.globals["PASSWORD_HINT"] = PASSWORD_HINT
    app.jinja_env.globals["PASSWORD_MIN_LENGTH"] = PASSWORD_MIN_LENGTH
    app.jinja_env.globals["MIN_CAPITAL"] = int(app.config.get("MIN_PLAN_CAPITAL", 5000))
    # One colour rule for every score out of 10 (IT expert review: a 3.3/10
    # was shown in green). good >= 6.5 (green), fair >= 4 (amber), low < 4
    # (red) -- the same cuts as the forecast's HIGH / MODERATE / LOW word.
    def score_tone(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "fair"
        return "good" if number >= 6.5 else ("fair" if number >= 4 else "low")

    app.jinja_env.globals["score_tone"] = score_tone
    app.jinja_env.globals["SCORE_COLORS"] = {"good": "#16a34a", "fair": "#d97706", "low": "#dc2626"}

    # Tarlac City's 76 barangays, alphabetical -- every barangay dropdown.
    from app.ml.seed_data import BARANGAY_NAMES

    app.jinja_env.globals["BARANGAY_LIST"] = sorted(BARANGAY_NAMES)

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
            # Available on EVERY page. Settings > Business Preferences
            # needs the full industry list for its per-plan Edit form,
            # and profile_controller doesn't otherwise pass
            # business_types to the template.
            "ALL_BUSINESS_TYPES": BUSINESS_TYPES,
            # The sub-category table for the plan forms (sign-up, Home's
            # Add New Plan, Settings' Edit) -- see shared/_plan_fields.html.
            "PLAN_SUBCATEGORIES": plan_subcategories(),
            # The Contact Us dialog is reachable from every dashboard,
            # so its address has to be available on every page.
            "SUPPORT_EMAIL": app.config.get("SUPPORT_EMAIL", ""),
        }

    # ---- one-time data migrations ----
    # Applies defaults whose MEANING changed between versions. Without
    # this, ensure_defaults() only ever ran from seed.py, so an existing
    # database kept stale values forever -- which is exactly why every
    # Google Places lookup stayed capped at 20 results long after the
    # shipped default became 0 (unlimited). Never raises; see the module.
    from app.services.startup_migrations import run_startup_migrations

    run_startup_migrations(app)

    # ---- speed: cache-busted static files, gzip, per-request memo ----
    # See app/utils/http_perf.py and app/utils/request_memo.py.
    from sqlalchemy.orm import Session as _OrmSession

    from app.utils.http_perf import install_compression, install_static_versioning
    from app.utils.request_memo import install_flush_invalidation

    install_static_versioning(app)
    if app.config.get("COMPRESS_RESPONSES", True):
        install_compression(app)
    install_flush_invalidation(_OrmSession)

    # ---- warm-up: models, templates and reference data loaded now ----
    # Under gunicorn --preload this runs before the port opens, so no
    # visitor is served by a half-loaded process. See warmup.py.
    if app.config.get("WARM_ON_BOOT"):
        from app.services.warmup import warm_up

        warm_up(app)

    # ---- error pages ----
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
