"""
tests/test_admin_archive_and_audit.py
----------------------------------------
The client's three Admin-module revisions:

  1. User management has no delete -- accounts are ARCHIVED.
  2. Dataset management has no delete -- rows are ARCHIVED.
  3. The audit trail answers the five W's: who, what, when, where, why.

What is worth pinning down here, beyond "the buttons work":

  * THE DELETE ROUTES ARE GONE, not merely hidden. A button removed from
    a template while its route survives is still one crafted POST away.

  * WHY IS REQUIRED. Every archive, restore and suspend/activate is
    refused without a stated reason -- and a reason made of whitespace
    or a stray "ok" is not one.

  * ARCHIVED MEANS OUT OF THE ANALYSIS. An archived market_data row must
    stop influencing the scoring engine's lookups, and an archived real
    LGU upload must switch the "real data on file" check back off --
    through the same functions the pages use, not a re-implementation.

  * ARCHIVED DOES NOT MEAN DESTROYED. The forecast built on an archived
    row is still there and still shows the snapshot it was built from.
    That was the whole point of replacing delete: deleting cascaded
    through forecast_result and removed SMEs' saved analyses.

  * THE AUDIT ROW CARRIES ALL FIVE W's, captured from a real request
    (a real User-Agent header, a real route) -- and a row written with
    no request at all still gets written.
"""

import csv
import io
import re
from datetime import date, datetime, timedelta

import pytest

from app import create_app
from app.extensions import db
from app.models import (
    AuditLog, ForecastResult, LguData, MarketData, SmeProfile, SystemSetting, User,
)
from app.models.archive import get_including_archived
from app.services.forecasting_service import SYSTEM_USER_EMAIL

CHROME_ON_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@pytest.fixture
def app():
    app = create_app("testing")

    # ONE CLIENT'S SIGN-IN MUST NOT LEAK INTO ANOTHER'S REQUEST.
    # The fixture keeps a single app context pushed for the whole test,
    # and Flask reuses an already-pushed app context for every request
    # instead of creating one per request as it does in production. So
    # flask.g -- where Flask-Login caches the signed-in user -- survives
    # from one request to the next, and the admin's identity would
    # carry into the SME's request (and vice versa). These tests sign
    # in as several people, so each request starts from its own cookie,
    # exactly as it would on a real server.
    @app.before_request
    def _forget_cached_login():
        from flask import g

        g.pop("_login_user", None)

    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _user(email, role="SME", name=None):
    user = User(name=name or f"{role} Person", email=email, role=role)
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def _login(app, email):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": "password123"})
    return client


@pytest.fixture
def admin(app):
    return _user("admin@audit.test", role="Admin", name="Ada Admin")


@pytest.fixture
def sme(app):
    return _user("sme@audit.test", role="SME", name="Sam Sari-Sari")


@pytest.fixture
def admin_client(app, admin):
    return _login(app, admin.email)


def _market(industry="Food and Beverage", location="Poblacion", count=10, days_ago=0,
            source="Google Places API"):
    row = MarketData(
        industry_type=industry, location=location, competitor_count=count,
        population_density=900, historical_success_rate=0.5, foot_traffic_index=40,
        average_rent=15000, source=source, date_recorded=date.today() - timedelta(days=days_ago),
    )
    db.session.add(row)
    db.session.commit()
    return row


def _audit_rows(action):
    return AuditLog.query.filter_by(action=action).all()


def _is_archived(model, pk):
    db.session.expire_all()
    return get_including_archived(model, pk).is_archived


# =====================================================================
# 1. The delete routes are gone
# =====================================================================

def test_no_delete_endpoint_is_registered(app):
    endpoints = set(app.view_functions)
    for gone in ("admin.delete_user", "admin.delete_lgu_data", "admin.delete_market_data"):
        assert gone not in endpoints, f"{gone} still exists -- a hidden button is not a removed route"


def test_posting_to_the_old_delete_urls_deletes_nothing(app, admin_client, sme):
    market = _market()
    lgu = LguData(source="DTI", barangay="Poblacion", permit_count=4, uploaded_by=sme.user_id)
    db.session.add(lgu)
    db.session.commit()

    for url in (
        f"/admin/users/{sme.user_id}/delete",
        f"/admin/datasets/lgu/{lgu.lgu_id}/delete",
        f"/admin/datasets/market/{market.market_id}/delete",
    ):
        assert admin_client.post(url).status_code in (404, 405), url

    db.session.expire_all()
    assert db.session.get(User, sme.user_id) is not None
    assert db.session.get(LguData, lgu.lgu_id) is not None
    assert db.session.get(MarketData, market.market_id) is not None


def test_no_page_offers_a_delete_button(app, admin_client, sme):
    _market()
    for url in ("/admin/users", "/admin/datasets"):
        body = admin_client.get(url).get_data(as_text=True)
        assert ">Delete<" not in body, url
        assert "confirm(" not in body, f"{url} still uses a browser confirm() dialog"


# =====================================================================
# 2. WHY is required
# =====================================================================

# Each of these must be refused. "a     b" collapses to "a b" -- two
# letters padded out with spaces are not a five-character reason.
NOT_A_REASON = ["", "    ", "ok", "abcd", "a     b", "  abcd  ", "\n\n\t  \n"]


@pytest.mark.parametrize("reason", NOT_A_REASON)
def test_archiving_an_account_requires_a_reason(app, admin_client, sme, reason):
    page = admin_client.post(
        f"/admin/users/{sme.user_id}/archive", data={"reason": reason}, follow_redirects=True
    ).get_data(as_text=True)

    assert not _is_archived(User, sme.user_id), f"archived with reason {reason!r}"
    assert "Please give a reason" in page
    assert _audit_rows("admin_archive_user") == []


def test_restoring_an_account_requires_a_reason(app, admin_client, sme, admin):
    sme.archive(admin.user_id, "Left the programme")
    db.session.commit()

    admin_client.post(f"/admin/users/{sme.user_id}/restore", data={"reason": "ok"})
    assert _is_archived(User, sme.user_id)

    admin_client.post(f"/admin/users/{sme.user_id}/restore", data={"reason": "Owner asked to come back"})
    assert not _is_archived(User, sme.user_id)


@pytest.mark.parametrize("reason", ["", "  ", "nope"])
def test_suspending_requires_a_reason(app, admin_client, sme, reason):
    admin_client.post(f"/admin/users/{sme.user_id}/toggle-active", data={"reason": reason})
    db.session.expire_all()
    assert db.session.get(User, sme.user_id).status == "active"
    assert _audit_rows("admin_toggle_active") == []


def test_suspend_and_activate_record_the_reason(app, admin_client, sme):
    admin_client.post(f"/admin/users/{sme.user_id}/toggle-active", data={"reason": "Reported for spam posts"})
    db.session.expire_all()
    assert db.session.get(User, sme.user_id).status == "inactive"

    admin_client.post(f"/admin/users/{sme.user_id}/toggle-active", data={"reason": "Appeal accepted"})
    db.session.expire_all()
    assert db.session.get(User, sme.user_id).status == "active"

    reasons = [row.reason for row in AuditLog.query.filter_by(action="admin_toggle_active").order_by(AuditLog.id)]
    assert reasons == ["Reported for spam posts", "Appeal accepted"]


@pytest.mark.parametrize("kind", ["lgu", "market"])
def test_archiving_and_restoring_a_data_row_require_a_reason(app, admin_client, sme, kind):
    if kind == "lgu":
        row = LguData(source="DTI", barangay="Poblacion", permit_count=4, uploaded_by=sme.user_id)
        db.session.add(row)
        db.session.commit()
        model, pk, base = LguData, row.lgu_id, f"/admin/datasets/lgu/{row.lgu_id}"
    else:
        row = _market()
        model, pk, base = MarketData, row.market_id, f"/admin/datasets/market/{row.market_id}"

    admin_client.post(f"{base}/archive", data={"reason": "bad"})
    assert not _is_archived(model, pk)

    admin_client.post(f"{base}/archive", data={"reason": "Duplicate of a newer upload"})
    assert _is_archived(model, pk)

    admin_client.post(f"{base}/restore", data={"reason": ""})
    assert _is_archived(model, pk)

    admin_client.post(f"{base}/restore", data={"reason": "Archived by mistake"})
    assert not _is_archived(model, pk)


# =====================================================================
# 3. Archived accounts
# =====================================================================

def test_an_archived_account_cannot_sign_in_and_can_after_restore(app, admin_client, sme):
    admin_client.post(f"/admin/users/{sme.user_id}/archive", data={"reason": "Duplicate account"})

    page = app.test_client().post(
        "/login", data={"email": sme.email, "password": "password123"}, follow_redirects=True
    ).get_data(as_text=True)
    assert "deactivated" in page
    assert all(r.user_id != sme.user_id for r in _audit_rows("login"))

    admin_client.post(f"/admin/users/{sme.user_id}/restore", data={"reason": "Owner asked to come back"})

    response = app.test_client().post("/login", data={"email": sme.email, "password": "password123"})
    assert response.status_code == 302, "a restored account still could not sign in"
    assert any(r.user_id == sme.user_id for r in _audit_rows("login"))


def test_archiving_ends_a_session_already_in_progress(app, admin_client, sme):
    """Deleting an account used to end its session for free (the loader
    found no user). Archiving must be at least as final."""
    sme_client = _login(app, sme.email)
    assert sme_client.get("/settings").status_code == 200

    admin_client.post(f"/admin/users/{sme.user_id}/archive", data={"reason": "Duplicate account"})

    response = sme_client.get("/settings")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_you_cannot_archive_yourself(app, admin_client, admin):
    admin_client.post(f"/admin/users/{admin.user_id}/archive", data={"reason": "Testing self-archive"})
    assert not _is_archived(User, admin.user_id)


def test_the_system_account_cannot_be_archived(app, admin_client):
    system = _user(SYSTEM_USER_EMAIL, role="Admin", name="System")
    page = admin_client.post(
        f"/admin/users/{system.user_id}/archive", data={"reason": "Tidying up accounts"}, follow_redirects=True
    ).get_data(as_text=True)
    assert not _is_archived(User, system.user_id)
    assert "system account" in page.lower()


def test_the_users_page_splits_current_and_archived(app, admin_client, sme, admin):
    other = _user("other@audit.test", name="Olive Other")
    admin_client.post(f"/admin/users/{sme.user_id}/archive", data={"reason": "Closed the business"})

    body = admin_client.get("/admin/users").get_data(as_text=True)
    current = body[body.index('id="pane-current"'):body.index('id="pane-archived"')]
    archived = body[body.index('id="pane-archived"'):]

    assert other.email in current
    assert sme.email not in current
    assert sme.email in archived
    assert "Closed the business" in archived
    assert admin.email in archived, "the Archived tab must say who archived the account"
    assert f"/admin/users/{sme.user_id}/restore" in archived


def test_an_archived_account_still_resolves_where_it_is_named(app, admin_client, sme):
    """The reason User is not globally hidden: its uploads and its audit
    rows must still name it."""
    lgu = LguData(source="DTI", barangay="Poblacion", permit_count=4, uploaded_by=sme.user_id)
    db.session.add(lgu)
    db.session.commit()
    admin_client.post(f"/admin/users/{sme.user_id}/archive", data={"reason": "Duplicate account"})

    db.session.expire_all()
    assert db.session.get(LguData, lgu.lgu_id).uploader.email == sme.email
    assert sme.email in admin_client.get("/admin/datasets").get_data(as_text=True)


def test_the_dashboard_counts_active_and_archived_users_separately(app, admin_client, sme, admin):
    _user("two@audit.test")
    sme.archive(admin.user_id, "Closed the business")
    db.session.commit()

    body = admin_client.get("/admin/dashboard").get_data(as_text=True)
    # The Accounts card counts accounts in circulation; the archived one is
    # counted on the Suspended card's "N archived" line instead.
    card = re.sub(r"\s+", " ", body[body.index("> Accounts</div>"):body.index("Online now")])
    assert re.search(r'dss-stat-value">\s*2\s*<', card), card     # admin + two@, not the archived one
    assert "1 archived" in re.sub(r"\s+", " ", body)


# =====================================================================
# 4. Archived data rows leave the analysis -- and nothing is destroyed
# =====================================================================

def test_an_archived_market_row_leaves_the_scoring_lookups(app, admin_client):
    from app.services.forecasting_service import _latest_market_rows, reconciled_competitor_counts

    industry, location = "Food and Beverage", "Poblacion"
    _market(industry, location, count=7, days_ago=10)
    newest = _market(industry, location, count=42, days_ago=0)
    key = (industry, location)

    assert _latest_market_rows([industry], [location])[key].market_id == newest.market_id
    assert reconciled_competitor_counts([industry], [location])[key] == 42

    admin_client.post(f"/admin/datasets/market/{newest.market_id}/archive", data={"reason": "Bad Places result"})
    db.session.expire_all()

    assert _latest_market_rows([industry], [location])[key].competitor_count == 7, (
        "the scoring engine still reads an archived market_data row"
    )
    assert reconciled_competitor_counts([industry], [location])[key] == 7

    admin_client.post(f"/admin/datasets/market/{newest.market_id}/restore", data={"reason": "Checked, it was right"})
    db.session.expire_all()

    assert _latest_market_rows([industry], [location])[key].market_id == newest.market_id
    assert reconciled_competitor_counts([industry], [location])[key] == 42


def test_archiving_the_only_real_lgu_upload_switches_the_cold_start_back_on(app, admin_client):
    from app.services.data_import_service import has_active_lgu_data

    officer = _user("officer@audit.test", role="LGU")
    upload = LguData(source="DTI", barangay="Poblacion", permit_count=12, uploaded_by=officer.user_id)
    db.session.add(upload)
    db.session.commit()
    assert has_active_lgu_data() is True

    admin_client.post(f"/admin/datasets/lgu/{upload.lgu_id}/archive", data={"reason": "Wrong year uploaded"})
    db.session.expire_all()
    assert has_active_lgu_data() is False, "an archived upload still counts as real LGU data"

    admin_client.post(f"/admin/datasets/lgu/{upload.lgu_id}/restore", data={"reason": "Right year after all"})
    db.session.expire_all()
    assert has_active_lgu_data() is True


def test_a_forecast_survives_its_market_and_lgu_rows_being_archived(app, admin_client, sme):
    """The reason delete was replaced: forecast_result cascades on
    delete, so deleting a data row used to delete SMEs' forecasts."""
    from app.services.forecasting_service import find_or_create_lgu_data

    market = _market(count=33)
    lgu = find_or_create_lgu_data("Poblacion")
    profile = SmeProfile(
        user_id=sme.user_id, business_name="Sam's Eatery", industry_type="Food and Beverage",
        location="Poblacion", startup_capital=50000, business_stage="startup",
    )
    db.session.add(profile)
    db.session.commit()
    forecast = ForecastResult(
        sme_id=profile.sme_id, market_id=market.market_id, lgu_id=lgu.lgu_id,
        input_industry_type="Food and Beverage", input_location="Poblacion",
        saturation_index=55.0, viability_score=6.0, forecast_date=date.today(),
    )
    db.session.add(forecast)
    db.session.commit()
    forecast_id, market_id, lgu_id = forecast.forecast_id, market.market_id, lgu.lgu_id

    admin_client.post(f"/admin/datasets/market/{market_id}/archive", data={"reason": "Superseded snapshot"})
    admin_client.post(f"/admin/datasets/lgu/{lgu_id}/archive", data={"reason": "Superseded placeholder"})

    db.session.remove()
    kept = db.session.get(ForecastResult, forecast_id)
    assert kept is not None, "archiving a data row removed the forecast built on it"
    assert kept.market_data is not None and kept.market_data.market_id == market_id
    assert kept.market_data.competitor_count == 33
    assert kept.lgu_data is not None and kept.lgu_data.lgu_id == lgu_id
    # ...while the row itself really is out of circulation. A query, not
    # session.get(): the lazy load above put the row in the identity
    # map, and get() would hand that back without asking the database.
    assert MarketData.query.filter_by(market_id=market_id).first() is None


def test_archive_and_restore_clear_the_analytics_caches(app, admin_client, monkeypatch):
    from app.services import data_import_service

    calls = []
    monkeypatch.setattr(data_import_service, "_clear_analytics_caches", lambda: calls.append(1))
    row = _market()

    admin_client.post(f"/admin/datasets/market/{row.market_id}/archive", data={"reason": "Bad Places result"})
    assert len(calls) == 1
    admin_client.post(f"/admin/datasets/market/{row.market_id}/restore", data={"reason": "Checked, it was right"})
    assert len(calls) == 2


def test_the_datasets_page_lists_archived_records_with_restore(app, admin_client, admin):
    live = _market("Retail", "San Vicente", count=5)
    gone = _market("Food and Beverage", "Poblacion", count=9)
    admin_client.post(f"/admin/datasets/market/{gone.market_id}/archive", data={"reason": "Duplicate snapshot"})

    body = admin_client.get("/admin/datasets").get_data(as_text=True)
    archived = body[body.index('id="archived-records"'):]
    live_part = body[:body.index('id="archived-records"')]

    assert "Duplicate snapshot" in archived
    assert admin.email in archived
    assert f"/admin/datasets/market/{gone.market_id}/restore" in archived
    assert f"/admin/datasets/market/{gone.market_id}/archive" not in live_part
    assert f"/admin/datasets/market/{live.market_id}/archive" in live_part
    assert "cascade" not in body.lower(), "the old deletion copy is still on the page"


# =====================================================================
# 5. The five W's
# =====================================================================

def test_an_admin_archive_writes_all_five_ws(app, admin_client, admin, sme):
    before = datetime.utcnow() - timedelta(seconds=5)
    admin_client.post(
        f"/admin/users/{sme.user_id}/archive",
        data={"reason": "Duplicate account, owner confirmed by phone"},
        headers={"User-Agent": CHROME_ON_WINDOWS},
    )

    rows = _audit_rows("admin_archive_user")
    assert len(rows) == 1
    row = rows[0]

    # WHO
    assert row.user_id == admin.user_id
    assert row.actor_name == "Ada Admin"
    assert row.actor_role == "Admin"
    # WHAT
    assert row.action == "admin_archive_user"
    assert row.target_type == "User"
    assert row.target_id == str(sme.user_id)
    assert row.target_label == sme.email
    # WHEN
    assert row.created_at is not None and row.created_at >= before
    # WHERE
    assert row.ip_address == "127.0.0.1"
    assert row.http_method == "POST"
    assert row.route == f"/admin/users/{sme.user_id}/archive"
    assert row.user_agent == CHROME_ON_WINDOWS
    # WHY -- exactly what the admin typed
    assert row.reason == "Duplicate account, owner confirmed by phone"


def test_a_dataset_archive_names_the_row_it_touched(app, admin_client):
    row = _market("Retail", "San Vicente")
    admin_client.post(f"/admin/datasets/market/{row.market_id}/archive", data={"reason": "Wrong barangay"})

    entry = _audit_rows("admin_archive_market_data")[0]
    assert entry.target_type == "MarketData"
    assert entry.target_id == str(row.market_id)
    assert "Retail @ San Vicente" in entry.target_label
    assert entry.reason == "Wrong barangay"


def test_log_action_outside_a_request_still_writes_and_invents_no_where(app, sme):
    from app.utils.audit import log_action

    log_action("nightly_sweep", details="background job")   # must not raise
    log_action("register", details="from a script", user_id=sme.user_id)

    job = _audit_rows("nightly_sweep")[0]
    assert (job.ip_address, job.http_method, job.route, job.user_agent) == (None, None, None, None)
    assert job.user_id is None and job.actor_name is None
    assert job.reason, "WHY should fall back to the action's purpose"

    scripted = _audit_rows("register")[0]
    assert scripted.user_id == sme.user_id
    assert scripted.actor_name == sme.name and scripted.actor_role == "SME"
    assert scripted.route is None


def test_a_target_is_logged_with_its_own_primary_key(app, admin, sme):
    """SmeProfile carries its owner's user_id; the id logged must be the
    plan's own sme_id, not the owner's."""
    from app.utils.audit import log_action

    profile = SmeProfile(
        user_id=sme.user_id, business_name="Sam's Eatery", industry_type="Food and Beverage",
        location="Poblacion", startup_capital=1000, business_stage="startup",
    )
    db.session.add(profile)
    db.session.commit()
    assert profile.sme_id != sme.user_id    # otherwise this test proves nothing

    log_action("update_plan", target=profile)
    entry = _audit_rows("update_plan")[0]
    assert entry.target_type == "SmeProfile"
    assert entry.target_id == str(profile.sme_id)
    assert entry.target_label == "Sam's Eatery"


def test_describe_device():
    from app.utils.audit_labels import describe_device

    assert describe_device(CHROME_ON_WINDOWS) == "Chrome on Windows"
    assert describe_device(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    ) == "Safari on iPhone"
    assert describe_device(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
    ) == "Edge on Windows"
    assert describe_device(
        "Mozilla/5.0 (Linux; Android 14; SM-A546E) AppleWebKit/537.36 (KHTML, like Gecko) "
        "SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36"
    ) == "Samsung Internet on Android"
    assert describe_device(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:127.0) Gecko/20100101 Firefox/127.0"
    ) == "Firefox on macOS"
    assert describe_device(
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Googlebot/2.1; "
        "+http://www.google.com/bot.html) Chrome/126.0.0.0 Safari/537.36"
    ) == "Automated bot"
    assert describe_device("curl/8.5.0") == "curl (command line)"
    assert describe_device(None) is None
    assert describe_device("") is None


# =====================================================================
# 6. The audit page: filters, legacy rows, export
# =====================================================================

def _entry(action, details, *, user=None, role=None, when=None, **extra):
    row = AuditLog(
        action=action, details=details, user_id=user.user_id if user else None,
        actor_name=user.name if (user and role) else None, actor_role=role,
        created_at=when or datetime.utcnow(), **extra,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _audit_body(client, **params):
    body = client.get("/admin/audit-log", query_string=params).get_data(as_text=True)
    return body[body.index("<tbody>"):body.index("</tbody>")]


def test_audit_filters_narrow_the_results(app, admin_client, admin, sme):
    lgu = _user("lgu@audit.test", role="LGU", name="Lara LGU")
    _entry("run_forecast", "MARK-sme-forecast", user=sme, role="SME")
    _entry("dataset_upload", "MARK-lgu-upload", user=lgu, role="LGU")
    _entry("admin_update_settings", "MARK-admin-settings", user=admin, role="Admin")
    _entry("login_failed", "MARK-anonymous")

    everything = _audit_body(admin_client)
    for marker in ("sme-forecast", "lgu-upload", "admin-settings", "anonymous"):
        assert f"MARK-{marker}" in everything

    by_role = _audit_body(admin_client, role="LGU")
    assert "MARK-lgu-upload" in by_role and "MARK-sme-forecast" not in by_role

    no_account = _audit_body(admin_client, role="none")
    assert "MARK-anonymous" in no_account and "MARK-admin-settings" not in no_account

    by_action = _audit_body(admin_client, action="run_forecast")
    assert "MARK-sme-forecast" in by_action and "MARK-lgu-upload" not in by_action

    by_email = _audit_body(admin_client, q="lgu@audit")
    assert "MARK-lgu-upload" in by_email and "MARK-sme-forecast" not in by_email

    by_name = _audit_body(admin_client, q="Sam Sari")
    assert "MARK-sme-forecast" in by_name and "MARK-lgu-upload" not in by_name

    by_label = _audit_body(admin_client, q="uploaded a government")
    assert "MARK-lgu-upload" in by_label, "searching the words on screen should find the row"


def test_a_legacy_row_takes_its_role_from_the_account(app, admin_client, sme):
    """Written before the actor_role column existed: role filter and
    badge both fall back to the joined account."""
    _entry("login", "MARK-legacy", user=sme, role=None, ip_address="10.0.0.9")

    body = _audit_body(admin_client, role="SME")
    assert "MARK-legacy" in body
    assert sme.name in body and "SME</span>" in body
    assert "Signed in" in body
    assert "10.0.0.9" in body


def test_an_unknown_action_still_renders(app, admin_client):
    _entry("some_future_thing", "MARK-future")
    body = _audit_body(admin_client)
    assert "Some future thing" in body


def test_the_date_filter_uses_philippine_dates(app, admin_client, sme):
    # 17:00 UTC on Sep 30 is 01:00 on Oct 1 in Tarlac.
    _entry("run_forecast", "MARK-late-night", user=sme, role="SME", when=datetime(2026, 9, 30, 17, 0))

    assert "MARK-late-night" in _audit_body(admin_client, date_from="2026-10-01", date_to="2026-10-01")
    assert "MARK-late-night" not in _audit_body(admin_client, date_to="2026-09-30")
    assert "MARK-late-night" not in _audit_body(admin_client, date_from="2026-10-02")


def test_pagination_keeps_the_filters(app, admin_client, sme):
    for i in range(35):
        _entry("run_forecast", f"MARK-page-{i}", user=sme, role="SME")
    _entry("login", "MARK-not-this", user=sme, role="SME")

    body = admin_client.get("/admin/audit-log?action=run_forecast&role=SME").get_data(as_text=True)
    links = re.findall(r'href="([^"]*page=2[^"]*)"', body)
    assert links, "no link to page 2"
    assert all("action=run_forecast" in link and "role=SME" in link for link in links)

    page_two = admin_client.get(links[0].replace("&amp;", "&")).get_data(as_text=True)
    assert "MARK-page-" in page_two and "MARK-not-this" not in page_two


def test_the_csv_export_is_the_filtered_set(app, admin_client, sme):
    _entry("run_forecast", "MARK-in", user=sme, role="SME")
    _entry("login", "MARK-out", user=sme, role="SME")
    _entry("run_forecast", "=HYPERLINK(\"http://evil\")", user=sme, role="SME")

    response = admin_client.get("/admin/audit-log/export.csv?action=run_forecast")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]

    rows = list(csv.reader(io.StringIO(response.get_data().decode("utf-8-sig"))))
    header = rows[0]
    for column in ("Who", "What", "Why", "IP address", "Device"):
        assert column in header
    details = [row[header.index("Details")] for row in rows[1:]]
    assert "MARK-in" in details and "MARK-out" not in details
    assert "'=HYPERLINK(\"http://evil\")" in details, "a formula-looking cell was not neutralised"

    exported = _audit_rows("export_audit_log")
    assert len(exported) == 1 and "action=run_forecast" in exported[0].details


# =====================================================================
# 7. Admin only
# =====================================================================

@pytest.mark.parametrize("role", ["SME", "LGU"])
def test_every_new_admin_route_refuses_non_admins(app, role, sme):
    market = _market()
    lgu = LguData(source="DTI", barangay="Poblacion", permit_count=4, uploaded_by=sme.user_id)
    db.session.add(lgu)
    db.session.commit()
    outsider = _user(f"{role.lower()}-outsider@audit.test", role=role)
    client = _login(app, outsider.email)
    reason = {"reason": "Trying my luck here"}

    assert client.get("/admin/audit-log").status_code == 403
    assert client.get("/admin/audit-log/export.csv").status_code == 403
    for url in (
        f"/admin/users/{sme.user_id}/archive",
        f"/admin/users/{sme.user_id}/restore",
        f"/admin/users/{sme.user_id}/toggle-active",
        f"/admin/datasets/lgu/{lgu.lgu_id}/archive",
        f"/admin/datasets/lgu/{lgu.lgu_id}/restore",
        f"/admin/datasets/market/{market.market_id}/archive",
        f"/admin/datasets/market/{market.market_id}/restore",
    ):
        assert client.post(url, data=reason).status_code == 403, url

    assert not _is_archived(User, sme.user_id)
    assert not _is_archived(MarketData, market.market_id)
    assert not _is_archived(LguData, lgu.lgu_id)
