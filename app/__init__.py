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

    # ---- blueprints (controllers) ----
    from app.controllers.auth_controller import auth_bp
    from app.controllers.sme_controller import sme_bp
    from app.controllers.lgu_controller import lgu_bp
    from app.controllers.admin_controller import admin_bp
    from app.controllers.profile_controller import profile_bp
    from app.controllers.api_controller import api_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(sme_bp)
    app.register_blueprint(lgu_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    # ---- template globals ----
    @app.context_processor
    def inject_globals():
        from flask_login import current_user
        from app.models import Notification
        from app.ml.constants import BUSINESS_TYPES

        unread_count = 0
        if current_user.is_authenticated:
            unread_count = Notification.query.filter_by(user_id=current_user.user_id, is_read=False).count()
        return {
            "GOOGLE_MAPS_JS_API_KEY": app.config.get("GOOGLE_MAPS_JS_API_KEY", ""),
            "unread_notification_count": unread_count,
            # Available on EVERY page. Settings > Business Preferences
            # needs the full industry list for its per-plan Edit form,
            # and profile_controller doesn't otherwise pass
            # business_types to the template.
            "ALL_BUSINESS_TYPES": BUSINESS_TYPES,
        }

    # ---- one-time data migrations ----
    # Applies defaults whose MEANING changed between versions. Without
    # this, ensure_defaults() only ever ran from seed.py, so an existing
    # database kept stale values forever -- which is exactly why every
    # Google Places lookup stayed capped at 20 results long after the
    # shipped default became 0 (unlimited). Never raises; see the module.
    from app.services.startup_migrations import run_startup_migrations

    run_startup_migrations(app)

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
