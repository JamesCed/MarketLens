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
from urllib.parse import quote


def _bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


DEV_SECRET_KEY = "dev-secret-key-change-me"


DEFAULT_DB_SSL_CA = os.path.join(BASE_DIR, "certs", "aiven-ca.pem")

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
    SECRET_KEY = os.environ.get("SECRET_KEY", DEV_SECRET_KEY)
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    PII_ENCRYPTION_KEY = os.environ.get("PII_ENCRYPTION_KEY", "")
    PII_ENCRYPTION_PREVIOUS_KEYS = os.environ.get("PII_ENCRYPTION_PREVIOUS_KEYS", "")

    DPO_NAME = os.environ.get("DPO_NAME", "").strip()
    DPO_OFFICE = os.environ.get("DPO_OFFICE", "").strip()
    DPO_EMAIL = os.environ.get("DPO_EMAIL", "").strip()
    DPO_PHONE = os.environ.get("DPO_PHONE", "").strip()

    DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
    DB_PORT = os.environ.get("DB_PORT", "3306")
    DB_USER = os.environ.get("DB_USER", "root")
    DB_PASSWORD = os.environ.get("DB_PASSWORD", "root")
    DB_NAME = os.environ.get("DB_NAME", "dss_db")

    SQLALCHEMY_DATABASE_URI = normalise_database_url(
        os.environ.get("DATABASE_URL")
        or "mysql+pymysql://{user}:{password}@{host}:{port}/{name}?charset=utf8mb4".format(
            user=quote(DB_USER, safe=""),
            password=quote(DB_PASSWORD, safe=""),
            host=DB_HOST,
            port=DB_PORT,
            name=DB_NAME,
        )
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    UPLOAD_FOLDER = os.path.join(BASE_DIR, "instance", "uploads")
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024
    ALLOWED_UPLOAD_EXTENSIONS = {"csv", "xlsx", "xls"}
    UPLOAD_IN_BACKGROUND = _bool(os.environ.get("UPLOAD_IN_BACKGROUND"), True)
    UPLOAD_WAIT_SECONDS = float(os.environ.get("UPLOAD_WAIT_SECONDS", "15") or 15)
    WARM_AFTER_UPLOAD = _bool(os.environ.get("WARM_AFTER_UPLOAD"), True)
    DATA_RETENTION_ENABLED = _bool(os.environ.get("DATA_RETENTION_ENABLED"), True)

    CAPTCHA_PROVIDER = (os.environ.get("CAPTCHA_PROVIDER") or "turnstile").strip().lower()
    CAPTCHA_SITE_KEY = os.environ.get("CAPTCHA_SITE_KEY", "").strip()
    CAPTCHA_SECRET_KEY = os.environ.get("CAPTCHA_SECRET_KEY", "").strip()
    CAPTCHA_MIN_SCORE = float(os.environ.get("CAPTCHA_MIN_SCORE", "0.5") or 0.5)
    CAPTCHA_PASS_MINUTES = float(os.environ.get("CAPTCHA_PASS_MINUTES", "30") or 30)
    CAPTCHA_FAIL_OPEN = _bool(os.environ.get("CAPTCHA_FAIL_OPEN"), False)
    CAPTCHA_EXPECTED_HOSTNAME = os.environ.get("CAPTCHA_EXPECTED_HOSTNAME", "").strip()
    RATE_LIMIT_ENABLED = _bool(os.environ.get("RATE_LIMIT_ENABLED"), True)
    TERMS_UPDATED = "October 26, 2026"

    GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")

    GOOGLE_MAPS_JS_API_KEY = os.environ.get("GOOGLE_MAPS_JS_API_KEY", "")

    GOOGLE_GEOCODING_API_KEY = os.environ.get("GOOGLE_GEOCODING_API_KEY", "")

    USE_LLM_RECOMMENDATIONS = _bool(os.environ.get("USE_LLM_RECOMMENDATIONS"), False)
    LLM_PROVIDER = (os.environ.get("LLM_PROVIDER") or "gemini").strip().lower()

    WARM_ON_BOOT = _bool(os.environ.get("WARM_ON_BOOT"), True)

    MIN_PLAN_CAPITAL = float(os.environ.get("MIN_PLAN_CAPITAL", "5000") or 5000)
    COMPRESS_RESPONSES = _bool(os.environ.get("COMPRESS_RESPONSES"), True)

    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "").strip()

    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    GEMINI_MODEL = (os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash").strip()
    GEMINI_BASE_URL = os.environ.get(
        "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
    ).strip().rstrip("/")

    SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "smesystem2026@gmail.com")

    BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()

    MAIL_FROM_ADDRESS = (
        os.environ.get("MAIL_FROM_ADDRESS", "").strip()
        or os.environ.get("GMAIL_ADDRESS", "").strip()
        or SUPPORT_EMAIL
    )

    PASSWORD_RESET_CODE_TTL_MINUTES = int(os.environ.get("PASSWORD_RESET_CODE_TTL_MINUTES", 15))

    GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
    GMAIL_APP_PASSWORD = "".join(os.environ.get("GMAIL_APP_PASSWORD", "").split())
    GMAIL_SENDER_NAME = os.environ.get("GMAIL_SENDER_NAME", "SME Market Saturation DSS")
    EMAIL_VERIFICATION_CODE_TTL_MINUTES = int(os.environ.get("EMAIL_VERIFICATION_CODE_TTL_MINUTES", 10))

    REQUIRE_EMAIL_VERIFICATION = _bool(
        os.environ.get("REQUIRE_EMAIL_VERIFICATION"),
        bool(
            os.environ.get("BREVO_API_KEY", "").strip()
            or (os.environ.get("GMAIL_ADDRESS", "").strip()
                and os.environ.get("GMAIL_APP_PASSWORD", "").strip())
        ),
    )

    EMAIL_VERIFICATION_MAX_ATTEMPTS = int(os.environ.get("EMAIL_VERIFICATION_MAX_ATTEMPTS", 5))

    EMAIL_VERIFICATION_RESEND_SECONDS = int(os.environ.get("EMAIL_VERIFICATION_RESEND_SECONDS", 60))

    KMEANS_N_CLUSTERS = int(os.environ.get("KMEANS_N_CLUSTERS", 4))
    MSI_WEIGHT_COMPETITOR_DENSITY = float(os.environ.get("MSI_W1", 0.45))
    MSI_WEIGHT_DEMAND_TREND = float(os.environ.get("MSI_W2", 0.35))
    MSI_WEIGHT_SOCIODEMOGRAPHIC = float(os.environ.get("MSI_W3", 0.20))
    SATURATION_ALERT_THRESHOLD = float(os.environ.get("SATURATION_ALERT_THRESHOLD", 75.0))
    PLACES_MAX_RESULTS = int(os.environ.get("PLACES_MAX_RESULTS", 0))

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

    SESSION_COOKIE_SECURE = _bool(os.environ.get("SESSION_COOKIE_SECURE"), True)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PREFERRED_URL_SCHEME = "https"

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
    WARM_ON_BOOT = False
    UPLOAD_IN_BACKGROUND = False
    WARM_AFTER_UPLOAD = False
    CAPTCHA_SITE_KEY = ""
    CAPTCHA_SECRET_KEY = ""
    RATE_LIMIT_ENABLED = False
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:")
    GEMINI_RETRY_BACKOFF_SECONDS = (0, 0)


config_by_name = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}
