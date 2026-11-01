"""
tests/test_system_downgrade.py
--------------------------------
V3.8 system downgrade: MarketLens is back to how the manuscript describes
it, before the LGU Encoder / Data Steward workflow and the LGU access
verification (V3.4-V3.6).

  1. THE DATABASE IS REVERTED at start-up: the upload_batches and
     lgu_access_requests tables and the user.lgu_role, lgu_status,
     lgu_verified_by and lgu_verified_at columns are dropped, and the
     retired System Settings rows deleted. The LGU context columns stay.
  2. ONE PLAIN LGU ROLE. LGU registration asks for the account details
     and the Tarlac City LGU context only; the account is active at once
     and uploads straight to the live dataset.
  3. THE ADMIN AND LGU PAGES lose the removed cards, panels and pages.
"""

from datetime import datetime

import pytest
from sqlalchemy import inspect, text

from tests.account_fixtures import LGU_CONTEXT, account_fields

from app import create_app
from app.extensions import db
from app.models import Notification, SystemSetting, User

RETIRED_PATHS = (
    "/admin/lgu-access",
    "/admin/publish-data",
    "/lgu/access",
    "/lgu/data-review",
    "/lgu/data-review/1",
    "/lgu/uploads/1/edit",
    "/lgu/uploads/1/archive",
    "/admin/users/1/lgu-role",
    "/admin/lgu-access/1/approve",
    "/admin/publish-data/1/publish",
    "/lgu-access-guide",
)


@pytest.fixture
def app():
    app = create_app("testing")

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
        with db.engine.begin() as conn:
            for table in ("upload_batches", "lgu_access_requests"):
                conn.execute(text(f"DROP TABLE IF EXISTS {table}"))


def _user(email, role="LGU", **fields):
    user = User(name=f"{role} Person", email=email, role=role, **fields)
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def _login(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.user_id)
        sess["_fresh"] = True
    return client


def _make_v37_schema():
    """What a V3.7 database had on top of the current models."""
    with db.engine.begin() as conn:
        for column, kind in (("lgu_role", "VARCHAR(20)"), ("lgu_status", "VARCHAR(20)"),
                             ("lgu_verified_by", "INTEGER"), ("lgu_verified_at", "DATETIME")):
            conn.execute(text(f'ALTER TABLE "user" ADD COLUMN {column} {kind} NULL'))
        conn.execute(text("CREATE TABLE upload_batches (id INTEGER PRIMARY KEY, user_id INTEGER "
                          "REFERENCES user(user_id), status VARCHAR(20), payload TEXT)"))
        conn.execute(text("CREATE TABLE lgu_access_requests (id INTEGER PRIMARY KEY, user_id INTEGER "
                          "REFERENCES user(user_id), status VARCHAR(20), reference_code VARCHAR(20))"))
        conn.execute(text("INSERT INTO upload_batches (user_id, status, payload) VALUES (1, 'approved', '[]')"))
        conn.execute(text("INSERT INTO lgu_access_requests (user_id, status) VALUES (1, 'pending_verification')"))


def _user_columns():
    return {col["name"] for col in inspect(db.engine).get_columns("user")}


def test_the_v37_staging_and_access_schema_is_dropped_at_start_up(app):
    from app.services.startup_migrations import RETIRED_SETTINGS, _retire_lgu_staging_schema

    rejected = _user("rejected@lgu.test")
    pending = _user("pending@lgu.test", lgu_office="City Planning and Development Office (CPDO)",
                    lgu_position="Planning Officer", lgu_scope="barangay", lgu_barangay="Poblacion")
    steward = _user("steward@lgu.test")
    sme = _user("owner@sme.test", role="SME")
    _make_v37_schema()
    db.session.execute(text("UPDATE \"user\" SET lgu_status = 'rejected' WHERE email = 'rejected@lgu.test'"))
    db.session.execute(text("UPDATE \"user\" SET lgu_status = 'pending_verification' WHERE email = 'pending@lgu.test'"))
    db.session.execute(text("UPDATE \"user\" SET lgu_status = 'verified', lgu_role = 'steward' "
                            "WHERE email = 'steward@lgu.test'"))
    for key in RETIRED_SETTINGS:
        db.session.add(SystemSetting(setting_key=key, setting_value="1"))
    db.session.add_all([
        Notification(user_id=steward.user_id, type="system",
                     message="LGU Person uploaded 'permits.xlsx' (Market data, batch #4). It is waiting for your review."),
        Notification(user_id=pending.user_id, type="system", message="Your LGU access request was approved."),
        Notification(user_id=sme.user_id, type="system", message="Batch #4 ('permits.xlsx') is now live."),
        Notification(user_id=sme.user_id, type="early_warning", message="Saturation in Poblacion reached 81."),
        Notification(user_id=sme.user_id, type="system", message="Welcome to MarketLens."),
    ])
    db.session.commit()

    done = _retire_lgu_staging_schema()

    db.session.expire_all()
    tables = set(inspect(db.engine).get_table_names())
    assert "upload_batches" not in tables and "lgu_access_requests" not in tables
    columns = _user_columns()
    assert not columns & {"lgu_role", "lgu_status", "lgu_verified_by", "lgu_verified_at"}
    assert {"lgu_office", "lgu_position", "lgu_scope", "lgu_barangay"} <= columns
    assert set(done["tables_dropped"]) == {"upload_batches", "lgu_access_requests"}
    assert done["settings_deleted"] == len(RETIRED_SETTINGS)

    # The admin's rejection still stands; everyone else is an ordinary LGU account.
    assert db.session.get(User, rejected.user_id).status == "inactive"
    kept = db.session.get(User, pending.user_id)
    assert kept.status == "active" and kept.lgu_scope_label == "Barangay Poblacion"
    assert kept.lgu_office == "City Planning and Development Office (CPDO)"
    assert db.session.get(User, steward.user_id).status == "active"
    assert db.session.get(User, sme.user_id).status == "active"

    left = sorted(n.message for n in Notification.query.all())
    assert left == ["Saturation in Poblacion reached 81.", "Welcome to MarketLens."]
    assert SystemSetting.query.filter(SystemSetting.setting_key.in_(RETIRED_SETTINGS)).count() == 0
    for key in ("msi_weight_competitor_density", "msi_weight_demand_trend", "msi_weight_sociodemographic",
                "saturation_alert_threshold"):
        assert SystemSetting.get(key) is not None, key

    assert _retire_lgu_staging_schema() == {}, "a second start changes nothing"


def test_a_database_that_never_had_the_workflow_is_left_alone(app):
    from app.services.startup_migrations import _retire_lgu_staging_schema

    _user("plain@lgu.test")
    assert _retire_lgu_staging_schema() == {}
    assert User.query.filter_by(email="plain@lgu.test").one().status == "active"


def test_the_models_no_longer_define_the_retired_tables_or_columns(app):
    assert not {"upload_batches", "lgu_access_requests"} & set(db.metadata.tables)
    for name in ("lgu_role", "lgu_status", "lgu_verified_by", "lgu_verified_at"):
        assert name not in User.__table__.columns
    for name in ("is_lgu_verified", "is_lgu_pending", "is_data_steward", "is_lgu_encoder", "LGU_ROLES"):
        assert not hasattr(User, name), name
    tables = set(inspect(db.engine).get_table_names())
    assert not {"upload_batches", "lgu_access_requests"} & tables


def test_the_retired_pages_are_gone(app):
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    for path in RETIRED_PATHS:
        assert path not in rules, path
    for rule in rules:
        assert "lgu-access" not in rule and "publish" not in rule and "review" not in rule, rule
    lgu = _login(app, _user("officer@lgu.test"))
    admin = _login(app, _user("admin@x.test", role="Admin"))
    for client in (lgu, admin):
        for path in RETIRED_PATHS:
            assert client.get(path).status_code == 404, path
            assert client.post(path).status_code == 404, path


def test_lgu_registration_asks_for_account_details_and_lgu_context_only(app):
    page = app.test_client().get("/register/lgu").get_data(as_text=True)
    assert "Account details" in page and "Tarlac City LGU context" in page
    for field in ('name="lgu_scope"', 'name="lgu_barangay"', 'name="lgu_office"', 'name="lgu_position"'):
        assert field in page
    for gone in ("Role selection", "requested_role", "Encoder", "Steward", "access request", "pending"):
        assert gone not in page, gone


def test_a_registered_lgu_account_is_active_and_can_upload_at_once(app):
    form = {**account_fields("maria@tarlaccity.gov.ph", first_name="Maria", last_name="Santos"), **LGU_CONTEXT}
    response = app.test_client().post("/register/lgu", data=form, follow_redirects=True)
    assert response.status_code == 200

    user = User.query.filter_by(email="maria@tarlaccity.gov.ph").one()
    assert user.role == "LGU" and user.status == "active"
    assert user.lgu_office == LGU_CONTEXT["lgu_office"] and user.lgu_position == LGU_CONTEXT["lgu_position"]
    assert user.lgu_scope_label == "City-wide"

    client = _login(app, user)
    page = client.get("/lgu/government-data-upload").get_data(as_text=True)
    assert 'type="file"' in page
    assert "How an upload goes live" not in page
    assert "Privacy notice for uploads" not in page
    menu = client.get("/lgu/dashboard").get_data(as_text=True)
    for item in ("LGU Dashboard", "Diversification Plan", "Saturation Map", "Trend Reports", "Gov&#39;t Data Upload"):
        assert item in menu or item.replace("&#39;", "'") in menu, item
    assert "Gov't Data Review" not in menu and "Request Access" not in menu


def test_system_settings_keep_the_msi_weights_and_drop_the_removed_cards(app):
    admin = _login(app, _user("admin@x.test", role="Admin"))
    page = admin.get("/admin/settings").get_data(as_text=True)
    for field in ("msi_weight_competitor_density", "msi_weight_demand_trend", "msi_weight_sociodemographic",
                  "saturation_alert_threshold"):
        assert f'name="{field}"' in page, field
    for gone in ("kmeans_n_clusters", "K-Means clusters", "Data retention", "upload_retention_days",
                 "LGU access verification", "lgu_email_domains", "lgu_access_review_days"):
        assert gone not in page, gone

    admin.post("/admin/settings", data={"kmeans_n_clusters": "6", "msi_weight_competitor_density": "0.5",
                                        "msi_weight_demand_trend": "0.3", "msi_weight_sociodemographic": "0.2"})
    assert SystemSetting.get("kmeans_n_clusters") is None, "the removed field cannot be saved any more"


def test_manage_users_and_datasets_lose_the_access_and_publish_panels(app):
    _user("officer@lgu.test", lgu_office="Business Permits and Licensing Office (BPLO)", lgu_position="Inspector")
    admin = _login(app, _user("admin@x.test", role="Admin"))
    users = admin.get("/admin/users").get_data(as_text=True)
    assert "LGU access requests" not in users
    assert "Encoder" not in users and "Steward" not in users
    datasets = admin.get("/admin/datasets").get_data(as_text=True)
    assert "Publish approved government data" not in datasets


def test_retention_still_runs_with_the_fixed_periods(app):
    from app.services import retention_service

    assert retention_service.retention_days() == (1095, 365)
    counts = retention_service.apply_retention(today=datetime.utcnow().date())
    assert set(counts) >= {"lgu_data", "market_data", "audit_logs"}
    assert "upload_batches" not in counts and "lgu_access_requests" not in counts
