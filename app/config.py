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
    #
    # WHEN THERE IS NO DATABASE_URL the five parts above are assembled
    # into one, and the user and password are PERCENT-ENCODED on the way
    # in. A generated database password can contain '@', '/', ':', '#'
    # or '?', every one of which means something structural inside a
    # URL -- an unencoded '@' in particular makes the parser read the
    # rest of the password as part of the hostname, so the app then
    # tries to log in somewhere else with half a password. Encoding here
    # means the five DB_* variables are the SAFE way to configure a
    # host: you paste the password exactly as the provider shows it and
    # never think about URL syntax at all.
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

    # ---------------- AI-written forecast transcript and recommendation text ----------------
    # Per the paper's 1.2 Purpose and Description (SME Module: "AI-driven
    # business recommendations and alternative industry suggestions"),
    # this turns the forecasting engine's numbers into natural language.
    # Its main job is the FORECAST TRANSCRIPT: Gemini is handed the
    # trained models' whole computation for a plan and transcribes it --
    # what they forecast, how the numbers led there, what it means for
    # the owner -- quoting only the models' own figures (checked number
    # by number; see llm_service.transcribe_forecast).
    #
    # LLM_PROVIDER picks which API is asked first for everything else
    # (alerts, location cards) -- "gemini" (the default, and the AI this
    # system names for the transcript), "openai" (GPT, or OpenRouter via
    # OPENAI_BASE_URL) or "anthropic" (Claude). The transcript itself
    # always asks Gemini first. If a provider has no usable key the others
    # are tried, and if none answers the built-in rule-based text is used
    # (see recommendation_service.py) -- the page never breaks because of
    # this. With LLM_PROVIDER=gemini and only an sk- key (OpenAI /
    # OpenRouter) configured, Gemini is skipped -- Google would refuse
    # that key -- and OpenAI is asked first instead.
    USE_LLM_RECOMMENDATIONS = _bool(os.environ.get("USE_LLM_RECOMMENDATIONS"), False)
    LLM_PROVIDER = (os.environ.get("LLM_PROVIDER") or "gemini").strip().lower()

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

    # ---- Google Gemini, spoken natively rather than through the
    #      OpenAI-compatible shim ----
    #
    # WHY A THIRD PROVIDER RATHER THAN JUST OPENAI_BASE_URL. Gemini does
    # expose an OpenAI-compatible endpoint, and pointing OPENAI_BASE_URL
    # at it used to be the whole trick. That stopped working for new
    # accounts: Google AI Studio changed key issuance on 28 May 2026 and
    # keys are now "auth keys" in the AQ.Ab... format, bound to a Cloud
    # service account. An auth key sent the way the OpenAI SDK sends
    # one -- `Authorization: Bearer <key>` -- is rejected by that
    # compatibility layer, either with 400 "Multiple authentication
    # credentials received" or, more confusingly, a 401 that reads like
    # a bad key. The same key works against the NATIVE endpoint, which
    # takes it as `x-goog-api-key`.
    #
    # The older AIza Standard keys, which did work over Bearer, are
    # being retired -- unrestricted ones started being refused on
    # 19 June 2026 -- so "use an AIza key instead" is not a fix, it is a
    # shorter runway.
    #
    # Hence llm_service._generate_with_gemini(), which posts directly to
    # models/<model>:generateContent using `requests` (already a
    # dependency, so this path does not touch the openai SDK at all) and
    # asks for responseMimeType=application/json -- a real JSON
    # guarantee from the API rather than a polite request in the prompt.
    #
    # GEMINI_API_KEY falls back to OPENAI_API_KEY so a deployment that
    # already has the key in the OpenAI slot keeps working after
    # changing nothing but LLM_PROVIDER.
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    # THE MODEL ID HAS TO BE AN EXACT, CURRENT ONE. An unknown name is a
    # 404 from the API, which looks in the logs exactly like a bad key
    # and sends you off regenerating a credential that was never the
    # problem.
    #
    # This default was "gemini-3-flash", which is NOT a model ID Google
    # serves: `gemini-3-flash-preview` existed as a preview, and the
    # stable line is numbered in tenths -- 3.5, 3.6, 3.7, 3.8. Checked
    # against ai.google.dev/gemini-api/docs/models on 27 September 2026;
    # the current stable Flash models are gemini-3.8-flash (newest),
    # 3.7/3.6/3.5-flash, and gemini-3.5-flash-lite / 3.1-flash-lite.
    # Everything on 2.0 is shut down and 2.5 is limited to accounts that
    # already used it.
    #
    # 3.8 Flash is the default because the calls here are small and
    # infrequent -- a few hundred tokens per recommendation or alert --
    # so the quality of the writing is worth more than the difference in
    # price. Set GEMINI_MODEL=gemini-3.5-flash-lite for the cheaper one.
    #
    # `or`, not a .get() default: render.yaml declares GEMINI_MODEL, and a
    # variable declared but left empty on a host is "" -- which a .get()
    # default does not replace, and an empty model id is a 404.
    GEMINI_MODEL = (os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash").strip()
    GEMINI_BASE_URL = os.environ.get(
        "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
    ).strip().rstrip("/")

    # The address shown in the Contact Us dialog on every dashboard.
    #
    # Kept here rather than typed into the templates because it appears
    # in three places (the contact dialog, the Support dialog's "getting
    # help" note, and the mailto: link) and an address that is right in
    # two of them is worse than one that is wrong in all three: nobody
    # notices the stale one. Overridable per deployment, so a different
    # city standing this system up does not have to edit templates.
    SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "smesystem2026@gmail.com")

    # ------- Email verification code (SME/LGU self-registration) -------
    # Sent via Gmail SMTP using an "App Password" (see README "Getting a
    # Gmail App Password") -- NOT the account's normal password; Google
    # only issues app passwords once 2-Step Verification is on.
    #
    # PASTE THE APP PASSWORD WITH THE SPACES REMOVED. Google shows it in
    # four groups of four for readability. smtplib does not always
    # tolerate them, and the resulting 535 looks identical to a wrong
    # password, so _clean() below strips whitespace rather than leaving
    # that to whoever fills in the deployment panel.
    # ---- Sending over HTTPS instead of SMTP ----
    #
    # On 26 September 2026 Render began blocking outbound traffic to
    # SMTP ports 25, 465 and 587 on FREE web services. The connection
    # never leaves the host, so no app password, timeout or retry gets
    # a message out -- verification codes simply stop arriving on a
    # deployment where nothing about the code changed.
    #
    # Set BREVO_API_KEY and email goes over HTTPS on port 443 instead,
    # which is not blocked. Leave it unset and Gmail SMTP is used
    # exactly as before, so local development and any paid host need no
    # configuration change at all.
    #
    # Brevo's free tier is 300 emails a day. The SENDING ADDRESS must
    # be verified with Brevo first (Senders, Domains & Dedicated IPs)
    # or every send is refused -- that is the one failure here that
    # looks like a code problem and is not.
    BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()

    # Who the mail comes from -- verification codes, password resets and
    # the market alerts from market_alert_service.
    #
    # Resolution order, and the reason for each step:
    #   1. MAIL_FROM_ADDRESS, when a deployment sets it. The verified
    #      Brevo sender is sometimes a different address from the Gmail
    #      account, and that case needs an explicit answer.
    #   2. GMAIL_ADDRESS, so an install that predates the Brevo
    #      transport keeps sending from exactly the address it always
    #      did, with nothing to change.
    #   3. SUPPORT_EMAIL -- the same address the Contact Us panel and
    #      the footer already give out.
    #
    # Step 3 is the one that is new, and it is there because a
    # BREVO_API_KEY with no GMAIL_ADDRESS used to resolve to no sender at
    # all: every send was refused for a missing "from", which reads in
    # the logs like a credential problem and is not one. Falling back to
    # the support address also means alerts arrive from somewhere a
    # reader can actually reply to, rather than from a no-reply nobody
    # reads.
    MAIL_FROM_ADDRESS = (
        os.environ.get("MAIL_FROM_ADDRESS", "").strip()
        or os.environ.get("GMAIL_ADDRESS", "").strip()
        or SUPPORT_EMAIL
    )

    # How long a password-reset code lasts. Longer than the sign-up
    # code because resetting a password often means going to find
    # another device to read the email on.
    PASSWORD_RESET_CODE_TTL_MINUTES = int(os.environ.get("PASSWORD_RESET_CODE_TTL_MINUTES", 15))

    GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
    GMAIL_APP_PASSWORD = "".join(os.environ.get("GMAIL_APP_PASSWORD", "").split())
    GMAIL_SENDER_NAME = os.environ.get("GMAIL_SENDER_NAME", "SME Market Saturation DSS")
    EMAIL_VERIFICATION_CODE_TTL_MINUTES = int(os.environ.get("EMAIL_VERIFICATION_CODE_TTL_MINUTES", 10))

    # IS VERIFICATION OPTIONAL OR MANDATORY?
    #
    # Historically it was always optional: if Gmail was unconfigured OR
    # the send failed for any reason, the account was created anyway.
    # That is the right default for a machine with no internet, and the
    # WRONG behaviour on a public deployment that means to verify
    # people -- because the failure mode is silent. A mistyped app
    # password does not produce "verification is broken", it produces
    # "verification quietly stopped happening", and nothing on screen
    # distinguishes the two.
    #
    # true  -> a send failure blocks registration and says so. Nobody
    #          gets an account without proving they own the inbox.
    # false -> the old behaviour: fall back to creating the account.
    #
    # Defaults to ON whenever Gmail is configured, because configuring
    # it is the act of saying you want verification. An installation
    # with no Gmail settings is unaffected either way.
    REQUIRE_EMAIL_VERIFICATION = _bool(
        os.environ.get("REQUIRE_EMAIL_VERIFICATION"),
        bool(
            os.environ.get("BREVO_API_KEY", "").strip()
            or (os.environ.get("GMAIL_ADDRESS", "").strip()
                and os.environ.get("GMAIL_APP_PASSWORD", "").strip())
        ),
    )

    # How many wrong codes before the pending registration is thrown
    # away. A six-digit code is one in a million per guess, which is
    # only protection if the number of guesses is bounded -- without a
    # cap, an attacker can post guesses as fast as HTTP allows for the
    # whole TTL window. Five is enough for a person mistyping and
    # nowhere near enough to search the space.
    EMAIL_VERIFICATION_MAX_ATTEMPTS = int(os.environ.get("EMAIL_VERIFICATION_MAX_ATTEMPTS", 5))

    # Minimum seconds between "resend code" requests. A free Gmail
    # account can send roughly 500 messages a day, and an unthrottled
    # resend button spends that quota -- or floods a stranger's inbox
    # using your address -- at whatever rate someone can click.
    EMAIL_VERIFICATION_RESEND_SECONDS = int(os.environ.get("EMAIL_VERIFICATION_RESEND_SECONDS", 60))

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
