"""
app/config.py
--------------
All environment-dependent settings live here and NOWHERE else in the
codebase. Every value is read from environment variables (loaded from
.env by python-dotenv in app.py), with sane local-development defaults
so the app still boots even if a value was forgotten.

This is the ONLY file you need to touch to point the app at a
different database or to plug in real API keys.
"""

import os
from datetime import timedelta


def _bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# What SECRET_KEY falls back to when nothing is set. Fine on localhost,
# REFUSED in production (see ProductionConfig.validate below): Flask
# signs the login session cookie with this key, so anyone who knows it
# can forge a cookie for any account, Admin included. A public URL
# running on a key printed in a public repo is not "a bit insecure", it
# is an open front door.
DEV_SECRET_KEY = "dev-secret-key-change-me"


# The TLS certificate authority a managed MySQL host is verified
# against. It is a PUBLIC certificate -- it identifies the server, it
# does not authenticate you -- so it is committed to the repo on
# purpose. The password is the secret; this is not.
DEFAULT_DB_SSL_CA = os.path.join(BASE_DIR, "certs", "aiven-ca.pem")

# Query-string keys a managed host puts in its connection URL that
# PyMySQL's connect() does not accept. SQLAlchemy forwards every unknown
# query parameter to the driver as a keyword argument, so leaving these
# in place is not "ignored", it is a TypeError at the first connection --
# and `ssl-mode` is not even a legal Python identifier.
_NOT_PYMYSQL_KWARGS = ("ssl-mode", "sslmode", "ssl_mode")


def normalise_database_url(url):
    """Makes a managed host's connection string usable by this app.

    Aiven (and most managed MySQL providers) hand you something like:

        mysql://avnadmin:PASSWORD@host.aivencloud.com:23964/defaultdb?ssl-mode=REQUIRED

    Three things are wrong with that for SQLAlchemy + PyMySQL, and all
    three fail at different times, which is what makes them annoying:

      1. `mysql://` names no driver, so SQLAlchemy picks its default
         (MySQLdb) and fails at import with
         "Can't load plugin: sqlalchemy.dialects:mysql".
      2. `ssl-mode=REQUIRED` is passed straight to PyMySQL's connect()
         as a keyword argument. PyMySQL has no such parameter, so the
         FIRST QUERY dies with an unexpected-keyword TypeError -- after
         the app has already booted and looked healthy.
      3. Dropping ssl-mode on its own would silently downgrade the
         connection, and Aiven refuses unencrypted ones anyway. The
         PyMySQL spelling is `ssl_ca=<path to the CA certificate>`,
         which both encrypts AND verifies that the server is really
         Aiven's.

    So this rewrites the scheme, strips the keys PyMySQL cannot take,
    guarantees utf8mb4, and attaches the CA certificate for any non-local
    host. Set DB_SSL_CA to point somewhere else, or to "" to switch the
    certificate off (you almost never want to).

    Anything that is not a MySQL URL -- the SQLite URLs the tests use,
    for instance -- is returned untouched.
    """
    if not url:
        return url
    if not url.startswith(("mysql://", "mysql+")):
        return url

    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    if url.startswith("mysql://"):
        url = "mysql+pymysql://" + url[len("mysql://"):]

    parts = urlsplit(url)
    # keep_blank_values, so a deliberately empty value is not silently
    # dropped and then re-added with a default below.
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in _NOT_PYMYSQL_KWARGS]
    keys = {k.lower() for k, _ in query}

    if "charset" not in keys:
        query.append(("charset", "utf8mb4"))

    if "ssl_ca" not in keys and "ssl" not in keys and "ssl_disabled" not in keys:
        ca_path = os.environ.get("DB_SSL_CA")
        if ca_path is None:
            ca_path = DEFAULT_DB_SSL_CA if os.path.exists(DEFAULT_DB_SSL_CA) else ""
        host = (parts.hostname or "").lower()
        is_local = host in ("", "localhost", "127.0.0.1", "::1") or host.endswith(".local")
        if ca_path and not is_local:
            query.append(("ssl_ca", ca_path))

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class Config:
    # ---------------- Flask core ----------------
    SECRET_KEY = os.environ.get("SECRET_KEY", DEV_SECRET_KEY)
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    # ---------------- Database (MySQL) ----------------
    # Given project credentials (see .env / .env):
    #   host=127.0.0.1 port=3306 user=root password=root database=dss_db
    DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
    DB_PORT = os.environ.get("DB_PORT", "3306")
    DB_USER = os.environ.get("DB_USER", "root")
    DB_PASSWORD = os.environ.get("DB_PASSWORD", "root")
    DB_NAME = os.environ.get("DB_NAME", "dss_db")

    # PyMySQL is the pure-python MySQL driver -- easiest to install for
    # beginners (no system build tools required), which is why it's the
    # one listed in requirements.txt.
    #
    # DATABASE_URL, when set, overrides all five DB_* values above. It is
    # passed through normalise_database_url() so that the connection
    # string a managed host hands you can be pasted in unedited -- see
    # that function for what it repairs and why.
    SQLALCHEMY_DATABASE_URI = normalise_database_url(
        os.environ.get("DATABASE_URL")
        or f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # ---------------- File uploads (Gov't Data Upload page) ----------------
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "instance", "uploads")
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50 MB, matches the storyboard's stated limit
    ALLOWED_UPLOAD_EXTENSIONS = {"csv", "xlsx", "xls"}

    # ---------------- Google Places API (server-side, competitor data) ----------------
    GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")

    # ---------------- Google Maps JavaScript API (front-end satellite map) ----------------
    GOOGLE_MAPS_JS_API_KEY = os.environ.get("GOOGLE_MAPS_JS_API_KEY", "")

    # ---------------- Google Geocoding API (barangay name -> real lat/lng) ----------------
    # Used by app/services/geocoding_service.py to put each barangay's
    # saturation zone on its true location. Optional: if this is blank,
    # geocoding falls back to GOOGLE_PLACES_API_KEY (Places API (New)
    # Text Search can return coordinates too), so the map still works
    # with a single key.
    GOOGLE_GEOCODING_API_KEY = os.environ.get("GOOGLE_GEOCODING_API_KEY", "")

    # ---------------- Optional LLM-generated recommendation text ----------------
    # Per the paper's 1.2 Purpose and Description (SME Module: "AI-driven
    # business recommendations and alternative industry suggestions"),
    # this turns the forecasting engine's numbers into a natural-language
    # paragraph. LLM_PROVIDER picks which API writes it -- "openai" (GPT)
    # or "anthropic" (Claude). If the chosen provider has no API key set,
    # app/services/llm_service.py automatically tries the other provider,
    # and if NEITHER has a key, it silently falls back to the built-in
    # rule-based text (see recommendation_service.py) -- the page never
    # breaks because of this.
    USE_LLM_RECOMMENDATIONS = _bool(os.environ.get("USE_LLM_RECOMMENDATIONS"), False)
    LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai").strip().lower()

    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    # Leave blank to talk to OpenAI directly. Set to
    # "https://openrouter.ai/api/v1" (with OPENAI_MODEL="openai/gpt-4o-mini"
    # and an OpenRouter key, which looks like "sk-or-v1-...") to route the
    # SAME `openai` Python SDK call through OpenRouter instead -- OpenRouter
    # is intentionally OpenAI-SDK-compatible, so no other code changes.
    OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "").strip()

    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

    # ---------------- Optional: email verification code (SME/LGU self-registration) ----------------
    # Sent via Gmail SMTP using an "App Password" (see README "Getting a
    # Gmail App Password"). If GMAIL_ADDRESS/GMAIL_APP_PASSWORD are left
    # blank, app/services/email_service.py can't send anything, so
    # auth_controller.py automatically skips verification and registers
    # the account immediately (same as before this feature existed) --
    # a missing/broken email setup never blocks anyone from registering.
    GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
    GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
    GMAIL_SENDER_NAME = os.environ.get("GMAIL_SENDER_NAME", "SME Market Saturation DSS")
    EMAIL_VERIFICATION_CODE_TTL_MINUTES = int(os.environ.get("EMAIL_VERIFICATION_CODE_TTL_MINUTES", 10))

    # ---------------- AI forecasting engine defaults ----------------
    # These are also stored in the system_settings table so an Admin can
    # tune them at runtime without redeploying; these are just the
    # fallback values used the very first time the app runs.
    KMEANS_N_CLUSTERS = int(os.environ.get("KMEANS_N_CLUSTERS", 4))
    MSI_WEIGHT_COMPETITOR_DENSITY = float(os.environ.get("MSI_W1", 0.45))
    MSI_WEIGHT_DEMAND_TREND = float(os.environ.get("MSI_W2", 0.35))
    MSI_WEIGHT_SOCIODEMOGRAPHIC = float(os.environ.get("MSI_W3", 0.20))
    # 0-100 scale (matches forecast_result.saturation_index / the
    # system_settings default) -- NOT 0-1 like an earlier draft.
    SATURATION_ALERT_THRESHOLD = float(os.environ.get("SATURATION_ALERT_THRESHOLD", 75.0))
    # No lat/lng radius search anymore -- Places Text Search is keyed on
    # a free-text location string (see app/services/places_service.py).
    # 0 = UNLIMITED: keep paging until Google runs out of results, so a
    # barangay's competitor_count reflects the businesses actually there
    # instead of stopping at the first page. (Google itself still caps
    # Text Search at 60 results per query -- 20 per page, 3 pages.)
    # Set a positive number here to re-impose a cap.
    PLACES_MAX_RESULTS = int(os.environ.get("PLACES_MAX_RESULTS", 0))

    # ---------------- Live Google Places fetching on/off ----------------
    # Every outbound call to the Places API funnels through
    # places_service.search_competitors_detailed(), and this switch is
    # what that function checks. Set PLACES_LIVE_FETCH=false and the app
    # stops calling Google entirely: it serves the competitor counts
    # already stored in `market_data` and falls back to a clearly-flagged
    # estimate for any combo it has never seen.
    #
    # WHY THIS EXISTS. A full city sweep is 20 industries x 76 barangays
    # = 1,520 lookups, and with result paging each lookup can cost up to
    # 3 Places requests -- roughly 4,560 billable calls, on whichever card is
    # attached to the key. On localhost that is your own deliberate
    # click. On a PUBLIC URL the "Fetch remaining" button is reachable by
    # anyone who can log in, including a curious panelist, so a deployed
    # copy should fetch nothing and read what has already been collected.
    # See DEPLOYMENT.md.
    PLACES_LIVE_FETCH = _bool(os.environ.get("PLACES_LIVE_FETCH"), True)

    MODEL_DIR = os.path.join(BASE_DIR, "app", "ml", "model_store")

    @classmethod
    def validate(cls, app):
        """Called once from create_app(). Base config checks nothing."""
        return


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False

    # ---------------- Cookies over HTTPS ----------------
    # Render (and any other host worth using) terminates TLS for you, so
    # the session cookie can and should be marked Secure -- without it
    # the cookie would also be sent over plain HTTP, where it can be read
    # off the wire. HttpOnly keeps JavaScript from reading it; SameSite
    # =Lax is CSRF defence-in-depth behind Flask-WTF's own tokens.
    #
    # Override SESSION_COOKIE_SECURE=false ONLY if you are deliberately
    # running the production config over plain http (e.g. on a LAN for a
    # dry run) -- with it left on, logging in over http silently fails,
    # because the browser accepts the cookie and then refuses to send it
    # back.
    SESSION_COOKIE_SECURE = _bool(os.environ.get("SESSION_COOKIE_SECURE"), True)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PREFERRED_URL_SCHEME = "https"

    # ---------------- CSRF: the HTTPS referrer check ----------------
    # READ THIS BEFORE CHANGING IT -- it is the one setting here that
    # turns something OFF, and the reason is specific.
    #
    # Once Flask sees a request as https (which it does now, correctly,
    # via ProxyFix in app/__init__.py), Flask-WTF adds a third CSRF check
    # on top of the token: it requires a Referer header and requires it to
    # match the site. Any POST without one is rejected with
    # "400 Bad Request: The referrer header is missing."
    #
    # That breaks logins on the live site for reasons that have nothing to
    # do with an attack. Browsers legitimately omit Referer -- a strict
    # privacy setting, a Referrer-Policy: no-referrer, a privacy
    # extension, some mobile in-app browsers. The failure shows up ONLY on
    # the deployed HTTPS copy, never on http://localhost, so it is exactly
    # the kind of fault that surfaces in front of an audience.
    #
    # WHAT IS STILL PROTECTING YOU, and why this is a considered trade
    # rather than a shortcut:
    #   1. The CSRF token itself. Every form still carries one and it is
    #      still verified -- this is the primary defence and it is
    #      untouched.
    #   2. SESSION_COOKIE_SAMESITE = "Lax" above. A cross-site POST does
    #      not carry the session cookie at all in any current browser, so
    #      the request the referrer check is meant to catch arrives
    #      unauthenticated and does nothing.
    # The referrer check dates from before SameSite existed; with SameSite
    # set it is a third layer over two that already hold.
    #
    # Set WTF_CSRF_SSL_STRICT=true in the environment to restore it.
    WTF_CSRF_SSL_STRICT = _bool(os.environ.get("WTF_CSRF_SSL_STRICT"), False)

    @classmethod
    def validate(cls, app):
        """Refuse to start a public deployment in a state that is unsafe
        rather than merely inconvenient.

        This is deliberately a hard failure, not a warning in a log
        nobody reads. A deployment that boots with the development secret
        key looks completely healthy right up to the moment somebody
        forges an Admin session with a key they read in your public
        GitHub repo.
        """
        if app.config.get("SECRET_KEY") in (None, "", DEV_SECRET_KEY):
            raise RuntimeError(
                "SECRET_KEY is unset or still the development default. Set a long "
                "random SECRET_KEY environment variable before serving this app "
                "publicly -- Flask signs the login session cookie with it, so the "
                "default value lets anyone forge an Admin login. On Render, "
                "render.yaml generates one for you; otherwise run "
                "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"` "
                "and paste the result into the host's environment variables."
            )


class TestingConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:")


config_by_name = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}
