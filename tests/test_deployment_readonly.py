"""
tests/test_deployment_readonly.py
-----------------------------------
Covers the settings that make a PUBLIC deployment of this app safe, as
opposed to one that merely works. Three groups:

  1. Read-only Google mode (PLACES_LIVE_FETCH=false). A deployed copy
     must never call the Places API -- a full city sweep is thousands of
     billable calls and, on a public URL, the button that starts one is
     reachable by anyone who can log in. The important test here is not
     "does it skip the call" but "does it leave the real data alone":
     without a guard, an old-but-real competitor count gets REPLACED by a
     fresh simulated one, so a live site looks correct for a month and
     then quietly degrades.

  2. Production hardening. The app must refuse to boot with the
     development SECRET_KEY, because that key is in a public repo and
     Flask signs the login cookie with it.

  3. Development is unchanged. None of the above may alter local
     behaviour.
"""

from datetime import date, timedelta

import pytest

from app import create_app
from app.config import DEV_SECRET_KEY, ProductionConfig
from app.extensions import db
from app.models import MarketData, SystemSetting
from app.services import places_service
from app.services.forecasting_service import find_or_create_market_data


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _real_google_row(days_old=0, count=4242):
    row = MarketData(
        industry_type="Food and Beverage",
        location="Poblacion",
        competitor_count=count,
        population_density=1000,
        historical_success_rate=0.5,
        foot_traffic_index=50,
        average_rent=20000,
        source="Google Places API",
        date_recorded=date.today() - timedelta(days=days_old),
    )
    db.session.add(row)
    db.session.commit()
    return row


# ---------------------------------------------------------------------
# 1. Read-only Google mode
# ---------------------------------------------------------------------

def test_read_only_mode_makes_no_places_call_even_with_a_key(app):
    """The gate is the config flag, NOT the absence of a key. A key left
    in the environment by accident must still not be spendable."""
    app.config["PLACES_LIVE_FETCH"] = False
    assert places_service.live_fetch_enabled() is False

    result = places_service.search_competitors_detailed(
        "Poblacion", "Food and Beverage", api_key="A-KEY-THAT-WOULD-BILL"
    )
    assert result["simulated"] is True


def test_live_mode_is_still_the_local_default(app):
    """Development must be untouched by any of this."""
    assert app.config["PLACES_LIVE_FETCH"] is True
    assert places_service.live_fetch_enabled() is True


def test_an_old_real_count_is_never_replaced_by_a_fresh_estimate(app):
    """THE regression this guard exists for.

    market_data rows go stale after 30 days, at which point
    find_or_create_market_data() fetches a replacement. With live
    fetching off, that "replacement" is a SIMULATED number -- so a
    deployment would serve real Google counts for its first month and
    then overwrite them, barangay by barangay, with estimates. An old
    real measurement beats a fresh invented one.
    """
    app.config["PLACES_LIVE_FETCH"] = False
    _real_google_row(days_old=400, count=4242)
    rows_before = MarketData.query.count()

    served = find_or_create_market_data("Food and Beverage", "Poblacion")

    assert served.competitor_count == 4242, "the real count was overwritten"
    assert served.source == "Google Places API"
    assert MarketData.query.count() == rows_before, "a replacement snapshot was written"


def test_live_mode_still_refreshes_a_stale_row(app):
    """The guard must not break the local refresh path it sits in front
    of: with fetching ON, a stale row is still replaced as before."""
    app.config["PLACES_LIVE_FETCH"] = True
    _real_google_row(days_old=400, count=4242)
    rows_before = MarketData.query.count()

    find_or_create_market_data("Food and Beverage", "Poblacion")

    assert MarketData.query.count() > rows_before, "a stale row should still be refreshed locally"


def test_geocoding_also_stops_calling_google(app):
    """Geocoding is a second billable Google API and honours the same
    switch. Nothing is lost -- every barangay coordinate is precomputed."""
    app.config["PLACES_LIVE_FETCH"] = False
    from app.services.geocoding_service import geocode_barangay_verbose

    coords, source, status = geocode_barangay_verbose("Poblacion", api_key="A-KEY-THAT-WOULD-BILL")
    assert coords is None and source is None
    assert "disabled" in status


def test_the_bulk_fetch_endpoint_is_refused_server_side(app):
    """Hiding the button stops an honest user; it does not stop a POST.
    This endpoint spends real money, so the gate has to be server-side."""
    from app.models import User

    user = User(name="LGU", email="lgu@example.com", role="LGU")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()

    app.config["PLACES_LIVE_FETCH"] = False
    client = app.test_client()
    client.post("/login", data={"email": "lgu@example.com", "password": "password123"})

    response = client.post("/api/places-refresh?limit=25")
    assert response.status_code == 403
    assert response.get_json()["live_fetch_disabled"] is True


def test_the_page_can_tell_disabled_apart_from_unconfigured(app):
    """A read-only deployment has no API key BY DESIGN, and the page
    must not read that as "every count is simulated" -- the imported
    rows are real Google data, collected locally. The two states look
    identical from the key alone, so the progress endpoint reports them
    separately and the page branches on `live_fetch_disabled` FIRST.

    (This used to be asserted against /api/places-fetched, which powered
    a temporary verification table. That table and its route are gone;
    the distinction still has to hold on the endpoint the page actually
    reads.)"""
    from app.models import User

    user = User(name="LGU", email="lgu2@example.com", role="LGU")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    _real_google_row(count=77)

    app.config["PLACES_LIVE_FETCH"] = False
    app.config["GOOGLE_PLACES_API_KEY"] = ""
    client = app.test_client()
    client.post("/login", data={"email": "lgu2@example.com", "password": "password123"})

    payload = client.get("/api/places-refresh/progress").get_json()
    assert payload["configured"] is False, "no key, correctly reported"
    assert payload["live_fetch_disabled"] is True, "and that is on purpose, not a misconfiguration"


def test_the_fetch_card_is_removed_rather_than_left_disabled(app):
    """On a read-only deployment there is nothing to fetch, so the card
    goes entirely. Pinned because the template branches on a flag the
    server has to keep sending."""
    import os

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    with open(os.path.join(root, "app", "templates", "sme", "trend_reports.html"), encoding="utf-8") as f:
        page = f.read()

    assert 'id="placesRefreshBox"' in page, "the card needs an id to be removable"
    assert "p.live_fetch_disabled" in page
    assert page.index("p.live_fetch_disabled") < page.index("!p.configured"), \
        "the disabled case must be handled before the missing-key case"


# ---------------------------------------------------------------------
# 2. Production hardening
# ---------------------------------------------------------------------

def test_production_refuses_the_development_secret_key():
    """Flask signs the login session cookie with SECRET_KEY, and the
    development default is printed in app/config.py -- which is in the
    public repo. Anyone who reads it can forge an Admin session, so this
    is a hard failure at boot, not a warning in a log nobody reads."""

    class _FakeApp:
        config = {"SECRET_KEY": DEV_SECRET_KEY}

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        ProductionConfig.validate(_FakeApp())

    for missing in (None, ""):
        class _Missing:
            config = {"SECRET_KEY": missing}

        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            ProductionConfig.validate(_Missing())


def test_production_accepts_a_real_secret_key():
    class _FakeApp:
        config = {"SECRET_KEY": "a-long-random-value-from-the-host-environment"}

    assert ProductionConfig.validate(_FakeApp()) is None


def test_production_does_not_require_a_referrer_header():
    """Found by exercising the production config, not by reading it.

    Once ProxyFix correctly reports https, Flask-WTF adds a strict Referer
    check on top of the CSRF token and rejects any POST without one:
    "400 Bad Request: The referrer header is missing." Browsers omit
    Referer for entirely ordinary reasons (a privacy setting, a
    Referrer-Policy, an in-app browser), and the failure appears ONLY on
    the deployed HTTPS copy -- localhost over http never sees it. So the
    first thing that would break live is logging in.

    Turning it off is a considered trade, not a shortcut: the CSRF token
    is still verified, and SameSite=Lax means a cross-site POST carries no
    session cookie at all. Both of those are asserted here, because
    switching this off is only defensible while they hold.
    """
    assert ProductionConfig.WTF_CSRF_SSL_STRICT is False
    assert ProductionConfig.SESSION_COOKIE_SAMESITE == "Lax"
    assert getattr(ProductionConfig, "WTF_CSRF_ENABLED", True) is True


def test_a_user_can_actually_log_in_over_https(app):
    """End-to-end proof of the above, through the real WSGI stack with the
    X-Forwarded-Proto header a host sets."""
    from app.models import User

    user = User(name="SME", email="live@example.com", role="SME")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()

    app.config["WTF_CSRF_ENABLED"] = True
    app.config["WTF_CSRF_SSL_STRICT"] = ProductionConfig.WTF_CSRF_SSL_STRICT
    app.config["SESSION_COOKIE_SECURE"] = False  # the test client is on http

    import re

    client = app.test_client()
    forwarded = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "example.onrender.com"}

    page = client.get("/login", headers=forwarded).get_data(as_text=True)
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)

    response = client.post(
        "/login",
        data={"email": "live@example.com", "password": "password123", "csrf_token": token},
        headers=forwarded,          # note: deliberately NO Referer header
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "referrer header is missing" not in response.get_data(as_text=True).lower()


def test_production_marks_the_session_cookie_secure():
    """Behind Render's TLS the cookie must not also be sent over plain
    http, where it can be read off the wire."""
    assert ProductionConfig.SESSION_COOKIE_SECURE is True
    assert ProductionConfig.SESSION_COOKIE_HTTPONLY is True
    assert ProductionConfig.PREFERRED_URL_SCHEME == "https"


def test_development_and_testing_validate_without_complaint(app):
    """The check is production-only; local runs must be unaffected."""
    from app.config import DevelopmentConfig, TestingConfig

    assert DevelopmentConfig.validate(app) is None
    assert TestingConfig.validate(app) is None


def test_the_dev_server_is_not_the_deployment_entry_point():
    """gunicorn imports wsgi.py, which defaults to the production config.
    app.py stays the local entry point."""
    import os

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    with open(os.path.join(root, "wsgi.py"), encoding="utf-8") as f:
        wsgi_source = f.read()
    with open(os.path.join(root, "Procfile"), encoding="utf-8") as f:
        procfile = f.read()

    assert '"production"' in wsgi_source
    # load_dotenv() MUST run before `from app import create_app`:
    # app/config.py reads os.environ at class-definition time, so an
    # import-then-load order would silently ignore every setting.
    assert wsgi_source.index("load_dotenv()") < wsgi_source.index("from app import create_app")
    assert "wsgi:app" in procfile
    # NOT app:app. `app` is the package (app/), and Python resolves that
    # name to the package every time -- a file called app.py next to it
    # can never be imported by name, so `gunicorn app:app` dies with
    # "Failed to find attribute 'app' in 'app'". Cheap to get wrong when
    # editing a dashboard field, and it fails only once deployed.
    assert "app:app" not in procfile
    # One worker only -- a free 512 MB instance cannot hold two copies of
    # numpy/pandas/scikit-learn plus the Random Forest.
    assert "--workers 1" in procfile


def test_the_app_package_shadows_app_py_so_nothing_should_import_it():
    """The rename of run.py to app.py put a module next to a package of
    the same name. This asserts the resolution order rather than
    trusting it: if `import app` ever stopped meaning the package, every
    import in the project would change meaning at once."""
    import importlib.util
    import os

    spec = importlib.util.find_spec("app")
    assert spec.submodule_search_locations is not None, "`app` must resolve to the PACKAGE"
    assert os.path.basename(spec.origin) == "__init__.py"

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    with open(os.path.join(root, "app.py"), encoding="utf-8") as f:
        entry = f.read()
    # The banner exists so the next person does not lose an afternoon to
    # "Failed to find attribute 'app' in 'app'".
    assert "gunicorn wsgi:app" in entry
    assert "app/ folder" in entry or "app/ PACKAGE" in entry


# ---------------------------------------------------------------------
# 3. The managed-database connection string
# ---------------------------------------------------------------------
# Aiven hands you a URL that PyMySQL cannot use. Each of the three
# faults fails at a DIFFERENT moment -- one at import, one at the first
# query, one not at all (it just silently drops TLS) -- which is exactly
# why they are repaired in code instead of in a README step someone
# skips.

AIVEN_URL = ("mysql://avnadmin:s3cret@mysql-38bdb7d4-project-dss.h.aivencloud.com"
             ":23964/defaultdb?ssl-mode=REQUIRED")


def test_the_driver_is_named_so_sqlalchemy_can_load_it():
    """`mysql://` makes SQLAlchemy reach for MySQLdb, which is not
    installed -- "Can't load plugin: sqlalchemy.dialects:mysql" at
    import time."""
    from app.config import normalise_database_url

    assert normalise_database_url(AIVEN_URL).startswith("mysql+pymysql://")


def test_ssl_mode_is_stripped_because_pymysql_has_no_such_argument():
    """The nastiest of the three. SQLAlchemy forwards unknown query
    parameters to the driver as keyword arguments, so this one does not
    get ignored -- it raises a TypeError on the first real query, long
    after the app has booted and looked fine. `ssl-mode` is not even a
    legal Python identifier."""
    import inspect

    import pymysql
    from sqlalchemy.engine import make_url

    from app.config import normalise_database_url

    query = dict(make_url(normalise_database_url(AIVEN_URL)).query)
    accepted = inspect.signature(pymysql.connect).parameters
    rejected = [key for key in query if key not in accepted]
    assert rejected == [], f"PyMySQL would refuse these connect() kwargs: {rejected}"


def test_dropping_ssl_mode_does_not_silently_drop_tls():
    """Removing ssl-mode on its own would downgrade a connection the
    host requires to be encrypted. The PyMySQL spelling is ssl_ca, which
    both encrypts AND verifies the server is really the managed host."""
    import os

    from sqlalchemy.engine import make_url

    from app.config import DEFAULT_DB_SSL_CA, normalise_database_url

    query = dict(make_url(normalise_database_url(AIVEN_URL)).query)
    assert "ssl_ca" in query, "a remote MySQL host must be reached over verified TLS"
    assert os.path.exists(query["ssl_ca"]), "the CA certificate has to actually be in the repo"


def test_the_committed_ca_is_a_certificate_authority():
    """A CA certificate is public -- it identifies the server, it does
    not authenticate us -- so committing it is correct. Committing the
    wrong file, or an expired one, is not."""
    import datetime
    import os

    from cryptography import x509

    from app.config import DEFAULT_DB_SSL_CA

    with open(DEFAULT_DB_SSL_CA, "rb") as handle:
        cert = x509.load_pem_x509_certificate(handle.read())

    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert cert.not_valid_after_utc > datetime.datetime.now(datetime.timezone.utc), \
        "the committed CA has expired -- download the current one from the host"


def test_a_local_database_is_left_alone():
    """No certificate is attached to localhost: a local MySQL has no TLS
    set up, and forcing it would break every developer's machine."""
    from sqlalchemy.engine import make_url

    from app.config import normalise_database_url

    local = "mysql+pymysql://root:root@127.0.0.1:3306/dss_db?charset=utf8mb4"
    assert "ssl_ca" not in dict(make_url(normalise_database_url(local)).query)


def test_a_url_that_is_already_correct_is_not_second_guessed():
    """Someone who has set ssl_ca themselves has made a decision."""
    from sqlalchemy.engine import make_url

    from app.config import normalise_database_url

    given = "mysql+pymysql://u:p@h.aivencloud.com:1/d?charset=utf8mb4&ssl_ca=/my/own/ca.pem"
    assert dict(make_url(normalise_database_url(given)).query)["ssl_ca"] == "/my/own/ca.pem"


def test_sqlite_urls_pass_through_untouched():
    """The whole test suite runs on SQLite -- this function must not
    have an opinion about it."""
    from app.config import normalise_database_url

    for url in ("sqlite:///:memory:", "sqlite:////tmp/x.db", ""):
        assert normalise_database_url(url) == url


def test_no_real_password_is_committed_anywhere():
    """The live database password was pasted into a chat, not into the
    repo, and it must stay that way. This looks for the shape of an
    Aiven password rather than one specific value, so it keeps working
    after a rotation."""
    import os
    import re

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    skip_dirs = {"venv", ".git", "__pycache__", "node_modules", "instance", "dumps", ".pytest_cache"}
    pattern = re.compile(r"AVNS_[A-Za-z0-9_-]{8,}")

    offenders = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            if not name.endswith((".py", ".md", ".yaml", ".yml", ".html", ".txt", ".sample", ".cfg", ".pem")):
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8", errors="ignore") as handle:
                    if pattern.search(handle.read()):
                        offenders.append(os.path.relpath(path, root))
            except OSError:
                continue

    assert offenders == [], f"a live database password appears in: {offenders}"


def test_every_package_folder_has_an_init_file():
    """The deploy that failed did so because app/__init__.py was not in
    the repository. Without it app/ is a PEP 420 namespace package,
    which Python ranks BELOW a module of the same name -- so `app`
    started meaning app.py, app.py imported itself, and the error
    message said "circular import" rather than "a file is missing".

    Checking every package folder rather than just app/, because the
    same omission anywhere under it fails the same confusing way."""
    import os

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    app_dir = os.path.join(root, "app")

    missing = []
    for folder, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", "static", "templates", "model_store"}]
        if not os.path.exists(os.path.join(folder, "__init__.py")):
            missing.append(os.path.relpath(folder, root))

    assert missing == [], f"these package folders have no __init__.py: {missing}"


def test_the_entry_point_refuses_to_run_as_a_shadowed_import():
    """app.py checks that `app` means the package before it imports
    anything, so the failure above can never again present itself as a
    circular import with no explanation."""
    import os

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    with open(os.path.join(root, "app.py"), encoding="utf-8") as handle:
        source = handle.read()

    assert "_assert_app_means_the_package" in source
    assert "namespace package" in source
    # The guard has to run BEFORE the import it is guarding, or it
    # guards nothing.
    assert source.index("_assert_app_means_the_package()") < source.index("from app import create_app")


def test_db_password_special_characters_survive_url_assembly():
    """A generated database password can contain '@', '/', ':', '#' or
    '?'. Every one of those means something structural in a URL, and an
    unencoded '@' is the dangerous one: the parser reads everything
    after it as the hostname, so the app tries to authenticate against
    a different server with half a password -- and the error it gets
    back blames DNS or the credentials, never the URL.

    Composing from the five DB_* variables must therefore encode them,
    which is what makes DB_* the safe way to configure a managed host:
    paste the password exactly as the provider prints it."""
    import importlib
    import os

    from sqlalchemy.engine import make_url

    from app import config as config_module

    saved = {k: os.environ.get(k) for k in
             ("DATABASE_URL", "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME")}
    try:
        os.environ.pop("DATABASE_URL", None)
        os.environ.update({
            "DB_HOST": "myhost.aivencloud.com", "DB_PORT": "23964",
            "DB_USER": "avnadmin", "DB_NAME": "defaultdb",
        })
        for password in ("plainpw123", "pa@ss", "p:a/s#s?w@rd", "with spaces"):
            os.environ["DB_PASSWORD"] = password
            importlib.reload(config_module)
            url = make_url(config_module.Config.SQLALCHEMY_DATABASE_URI)
            assert url.host == "myhost.aivencloud.com", f"{password!r} corrupted the host"
            assert url.port == 23964, f"{password!r} corrupted the port"
            assert url.password == password, f"{password!r} did not round-trip"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(config_module)
