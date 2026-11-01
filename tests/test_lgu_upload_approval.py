"""
tests/test_lgu_upload_approval.py
-----------------------------------
V3.9: Gov't Data Upload is locked for an LGU account until an
Administrator approves it in Admin > Manage Users.

The uploaded permit and zoning figures feed every saturation score, so
someone who only CLAIMS to be LGU staff at sign-up must not be able to
change them. A new LGU account opens the LGU Dashboard, Diversification
Plan, Saturation Map and Trend Reports right away; Gov't Data Upload is
grayed out in the menu and the page itself (and its POST) is refused on
the server until an Administrator approves the account.
"""

import io

import pytest
from sqlalchemy import inspect, text

from tests.account_fixtures import LGU_CONTEXT, account_fields

from app import create_app
from app.extensions import db
from app.models import AuditLog, LguData, MarketData, Notification, SystemSetting, User

UPLOAD = "/lgu/government-data-upload"


@pytest.fixture
def app(tmp_path):
    app = create_app("testing")
    app.config.update(UPLOAD_FOLDER=str(tmp_path / "uploads"), UPLOAD_IN_BACKGROUND=False, WARM_AFTER_UPLOAD=False)

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


def _register_lgu(app, email="maria@tarlaccity.gov.ph"):
    form = {**account_fields(email, first_name="Maria", last_name="Santos"), **LGU_CONTEXT}
    response = app.test_client().post("/register/lgu", data=form, follow_redirects=True)
    assert response.status_code == 200
    return User.query.filter_by(email=email).one(), response.get_data(as_text=True)


def _zoning_csv():
    body = "barangay,zoning_info,business_density\nSan Vicente,Commercial,2.4\nPoblacion,Mixed-use,3.1\n"
    return io.BytesIO(body.encode("utf-8"))


def test_a_new_lgu_sign_up_cannot_open_or_post_the_upload_page(app):
    user, page = _register_lgu(app)
    assert user.upload_approved_at is None and not user.can_upload_gov_data
    assert "Data Upload stays locked until an Administrator approves your account" in page

    client = _login(app, user)
    locked = client.get(UPLOAD)
    assert locked.status_code == 403
    body = locked.get_data(as_text=True)
    assert "Gov't Data Upload is locked" in body and 'id="uploadForm"' not in body

    rows_before = LguData.query.count() + MarketData.query.count()
    refused = client.post(UPLOAD, data={"dataset_type": "LGU_DATA", "source": "CLUP",
                                        "dataset_file": (_zoning_csv(), "zoning.csv")},
                          content_type="multipart/form-data")
    assert refused.status_code == 403
    assert LguData.query.count() + MarketData.query.count() == rows_before, "nothing may be imported"
    assert AuditLog.query.filter_by(action="lgu_upload_blocked", user_id=user.user_id).count() == 1
    assert client.get("/lgu/dataset-template/LGU_DATA").status_code == 403


def test_the_menu_grays_out_the_upload_and_the_dashboard_says_why(app):
    user, _page = _register_lgu(app)
    client = _login(app, user)
    dashboard = client.get("/lgu/dashboard").get_data(as_text=True)
    assert 'class="dss-nav-link is-locked"' in dashboard
    assert f'href="{UPLOAD}"' not in dashboard
    assert "Gov't Data Upload is locked</strong> until an Administrator approves" in dashboard
    for page in ("/lgu/dashboard", "/lgu/diversification", "/saturation-map", "/trend-reports"):
        assert client.get(page).status_code == 200, page


def test_admins_are_notified_of_a_new_lgu_account(app):
    admin = _user("admin@x.test", role="Admin")
    _register_lgu(app)
    notes = [n.message for n in Notification.query.filter_by(user_id=admin.user_id).all()]
    assert len(notes) == 1
    assert "waiting for Gov't Data Upload approval" in notes[0] and "Manage Users" in notes[0]
    assert LGU_CONTEXT["lgu_office"] in notes[0]


def test_manage_users_shows_the_waiting_account_with_an_approve_button(app):
    admin = _user("admin@x.test", role="Admin")
    waiting, _page = _register_lgu(app)
    _user("approved@lgu.test", lgu_office="City Treasurer's Office", lgu_position="Clerk").approve_upload()
    db.session.commit()
    client = _login(app, admin)

    page = client.get("/admin/users").get_data(as_text=True)
    assert "Upload locked &middot; waiting for approval" in page
    assert f'/admin/users/{waiting.user_id}/approve-upload' in page
    assert "Upload approved" in page
    assert "1 LGU account" in page
    assert 'title="LGU accounts waiting for Gov\'t Data Upload approval">1<' in page

    filtered = client.get("/admin/users?status=lgu_waiting").get_data(as_text=True)
    assert f'/admin/users/{waiting.user_id}/approve-upload' in filtered and "Upload approved" not in filtered
    assert "1 account match" in filtered


def test_approving_opens_the_upload_and_tells_the_lgu_user(app):
    admin = _user("admin@x.test", role="Admin")
    user, _page = _register_lgu(app)
    Notification.query.delete()
    db.session.commit()

    response = _login(app, admin).post(f"/admin/users/{user.user_id}/approve-upload")
    assert response.status_code == 302
    db.session.expire_all()
    user = db.session.get(User, user.user_id)
    assert user.can_upload_gov_data and user.upload_approved_by == admin.user_id
    assert [n.message for n in Notification.query.filter_by(user_id=user.user_id)] == [
        "An Administrator approved your LGU account: Gov't Data Upload is now open in your menu."]
    entry = AuditLog.query.filter_by(action="admin_approve_lgu_upload").one()
    assert str(entry.target_id) == str(user.user_id)

    client = _login(app, user)
    page = client.get(UPLOAD)
    assert page.status_code == 200 and 'id="uploadForm"' in page.get_data(as_text=True)
    assert f'href="{UPLOAD}"' in client.get("/lgu/dashboard").get_data(as_text=True)

    client.post(UPLOAD, data={"dataset_type": "LGU_DATA", "source": "CLUP",
                              "dataset_file": (_zoning_csv(), "zoning.csv")}, content_type="multipart/form-data")
    assert LguData.query.filter_by(uploaded_by=user.user_id).count() == 2


def test_locking_again_needs_a_reason_and_closes_the_page(app):
    admin = _user("admin@x.test", role="Admin")
    user = _user("officer@lgu.test")
    user.approve_upload(admin.user_id)
    db.session.commit()
    client = _login(app, admin)

    client.post(f"/admin/users/{user.user_id}/lock-upload", data={"reason": ""})
    db.session.expire_all()
    assert db.session.get(User, user.user_id).can_upload_gov_data, "no reason, no change"

    client.post(f"/admin/users/{user.user_id}/lock-upload", data={"reason": "Moved to another office"})
    db.session.expire_all()
    locked = db.session.get(User, user.user_id)
    assert not locked.can_upload_gov_data and locked.upload_approved_by is None
    assert AuditLog.query.filter_by(action="admin_lock_lgu_upload").one().reason == "Moved to another office"
    assert _login(app, locked).get(UPLOAD).status_code == 403


def test_only_an_administrator_can_approve_and_only_lgu_accounts(app):
    lgu = _user("officer@lgu.test")
    other = _user("other@lgu.test")
    sme = _user("owner@sme.test", role="SME")
    lgu_client = _login(app, lgu)
    assert lgu_client.post(f"/admin/users/{other.user_id}/approve-upload").status_code == 403
    assert lgu_client.post(f"/admin/users/{lgu.user_id}/approve-upload").status_code == 403

    admin = _login(app, _user("admin@x.test", role="Admin"))
    admin.post(f"/admin/users/{sme.user_id}/approve-upload")
    db.session.expire_all()
    assert db.session.get(User, sme.user_id).upload_approved_at is None
    assert not db.session.get(User, lgu.user_id).can_upload_gov_data


def test_an_lgu_account_created_by_an_administrator_is_approved(app):
    admin = _user("admin@x.test", role="Admin")
    _login(app, admin).post("/admin/users/create", data={
        "first_name": "Ana", "last_name": "Reyes", "email": "ana@tarlaccity.gov.ph",
        "password": "Secret#2026", "role": "LGU"})
    user = User.query.filter_by(email="ana@tarlaccity.gov.ph").one()
    assert user.can_upload_gov_data and user.upload_approved_by == admin.user_id


def test_the_demo_lgu_account_from_seed_is_approved(app):
    import seed

    assert seed._create_account_if_missing("Maria Santos", "lgu@dss.local", "lgu12345", "LGU")
    db.session.commit()
    assert User.query.filter_by(email="lgu@dss.local").one().can_upload_gov_data


def test_the_approval_columns_are_added_to_an_existing_database(app):
    from app.services.startup_migrations import _add_missing_columns

    with db.engine.begin() as conn:
        conn.execute(text('ALTER TABLE "user" DROP COLUMN upload_approved_at'))
        conn.execute(text('ALTER TABLE "user" DROP COLUMN upload_approved_by'))
    added = _add_missing_columns()
    assert {"upload_approved_at", "upload_approved_by"} <= set(added.get("user", []))
    columns = {col["name"] for col in inspect(db.engine).get_columns("user")}
    assert {"upload_approved_at", "upload_approved_by"} <= columns


def test_lgu_accounts_the_admin_had_verified_in_v37_keep_the_upload(app):
    """Upgrading straight from V3.7: an LGU account the System Admin had
    verified (or one older than verification, lgu_status blank) keeps Gov't
    Data Upload; a pending one waits for approval."""
    from app.services.startup_migrations import _retire_lgu_staging_schema

    verified = _user("verified@lgu.test")
    legacy = _user("legacy@lgu.test")
    pending = _user("pending@lgu.test")
    with db.engine.begin() as conn:
        for column, kind in (("lgu_role", "VARCHAR(20)"), ("lgu_status", "VARCHAR(20)"),
                             ("lgu_verified_by", "INTEGER"), ("lgu_verified_at", "DATETIME")):
            conn.execute(text(f'ALTER TABLE "user" ADD COLUMN {column} {kind} NULL'))
        conn.execute(text("UPDATE \"user\" SET lgu_status = 'verified', lgu_verified_by = 7, "
                          "lgu_verified_at = '2026-10-20 08:00:00' WHERE email = 'verified@lgu.test'"))
        conn.execute(text("UPDATE \"user\" SET lgu_status = 'pending_verification' WHERE email = 'pending@lgu.test'"))

    done = _retire_lgu_staging_schema()
    assert done["lgu_upload_approvals_kept"] == 2
    db.session.expire_all()
    kept = db.session.get(User, verified.user_id)
    assert kept.can_upload_gov_data and kept.upload_approved_by == 7
    assert kept.upload_approved_at.strftime("%Y-%m-%d") == "2026-10-20"
    assert db.session.get(User, legacy.user_id).can_upload_gov_data
    assert not db.session.get(User, pending.user_id).can_upload_gov_data


def test_the_tour_of_a_locked_account_skips_the_upload_page(app):
    import os
    import re

    user, _page = _register_lgu(app)
    page = _login(app, user).get("/lgu/dashboard").get_data(as_text=True)
    assert '"uploadLocked": true' in page

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "static", "js",
                        "tour_steps.js")
    source = open(path, encoding="utf-8").read()
    steps = re.findall(r'id: "([^"]+)",\n\s+page: "([^"]+)",\n(\s+needsUpload: true,\n)?', source)
    upload_steps = [(sid, flag) for sid, page_path, flag in steps if page_path == UPLOAD]
    assert upload_steps and all(flag for _sid, flag in upload_steps), "every upload-page step is skipped when locked"
    for _sid, page_path, flag in steps:
        if page_path.startswith("/lgu/") and not flag:
            assert _login(app, user).get(page_path).status_code == 200, page_path
