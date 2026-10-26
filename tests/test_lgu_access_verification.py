"""
tests/test_lgu_access_verification.py
---------------------------------------
LGU roles are given only after the System Admin verifies the person
(app/services/lgu_access_service.py):

  * an LGU sign-up is created "waiting for verification" with a
    reference code, and sees only its access-request page;
  * the System Admin approves (after confirming the endorsement was
    checked through an official channel) or declines with a reason;
  * verified accounts are re-checked, and access can be removed;
  * the public guide, the settings, retention and the health checks.
"""

import datetime

import pytest
from sqlalchemy import text

from app import create_app
from app.extensions import db
from app.models import AuditLog, LguAccessRequest, Notification, SystemSetting
from app.models.user import User
from tests.account_fixtures import LGU_REQUEST, STRONG_PASSWORD, account_fields


@pytest.fixture
def app():
    app = create_app("testing")

    def _fresh_login_cache():
        from flask import g

        g.pop("_login_user", None)

    app.before_request_funcs.setdefault(None, []).insert(0, _fresh_login_cache)
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        for name, email, role in (("Admin One", "admin@lgu.test", "Admin"), ("Admin Two", "admin2@lgu.test", "Admin"),
                                  ("Owner One", "sme@lgu.test", "SME")):
            user = User(name=name, email=email, role=role)
            user.set_password("password123")
            db.session.add(user)
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


def _login(app, email, password="password123"):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": password})
    return client


def _sign_up(app, email="maria@lgu.gov.ph", **overrides):
    data = {"role": "lgu", **LGU_REQUEST, **account_fields(email, first_name="Maria", last_name="Santos")}
    data.update(overrides)
    return app.test_client().post("/register", data=data, follow_redirects=True)


def _user(email="maria@lgu.gov.ph"):
    return User.query.filter_by(email=email).one()


def _approve(app, role="encoder", email="maria@lgu.gov.ph"):
    row = LguAccessRequest.query.filter_by(user_id=_user(email).user_id, status="pending").one()
    _login(app, "admin@lgu.test").post(f"/admin/lgu-access/{row.id}/approve",
                                       data={"granted_role": role, "verified_offline": "1"})
    db.session.expire_all()
    return row


# ------------------------------------------------------------- sign-up

def test_an_lgu_sign_up_waits_for_verification_with_a_reference_code(app):
    page = _sign_up(app).get_data(as_text=True)
    user = _user()
    row = LguAccessRequest.query.filter_by(user_id=user.user_id).one()
    assert user.role == "LGU" and user.lgu_status == "pending" and not user.is_lgu_verified
    assert user.lgu_role_key is None and not user.is_lgu_encoder() and not user.is_data_steward()
    assert row.reference_code.startswith("LGU-") and len(row.reference_code) == 10
    assert row.status == "pending" and row.requested_role == "encoder"
    assert row.endorser_email == "head.bplo@lgu.gov.ph" and user.lgu_office == LGU_REQUEST["lgu_office"]
    assert row.reference_code in page and "waiting for verification" in page
    told = {n.user_id for n in Notification.query.filter(Notification.message.contains(row.reference_code))}
    assert told == {_user("admin@lgu.test").user_id, _user("admin2@lgu.test").user_id}
    assert AuditLog.query.filter_by(action="lgu_access_requested").count() == 1


def test_the_endorser_details_are_encrypted_at_rest(app):
    _sign_up(app, lgu_discord="maria.santos")
    raw = db.session.execute(text("SELECT endorser_email, discord_username FROM lgu_access_requests")).one()
    assert "head.bplo" not in raw[0] and "maria" not in raw[1]
    assert LguAccessRequest.query.one().discord_username == "maria.santos"


@pytest.mark.parametrize("field, value, message", [
    ("lgu_office", "", "Choose your LGU office"),
    ("lgu_position", "", "Enter your position"),
    ("lgu_requested_role", "admin", "Choose the LGU role you need"),
    ("lgu_endorser_name", "", "full name of your head of office"),
    ("lgu_endorser_email", "maria@lgu.gov.ph", "must be someone else"),
    ("lgu_discord", "Not A Username!", "Discord username"),
    ("lgu_confirm", "", "Confirm that you work in the LGU office"),
])
def test_an_incomplete_lgu_request_creates_no_account(app, field, value, message):
    page = _sign_up(app, **{field: value}).get_data(as_text=True)
    assert message in page
    assert User.query.filter_by(email="maria@lgu.gov.ph").first() is None


def test_official_domains_are_enforced_when_set(app):
    SystemSetting.set("lgu_email_domains", "tarlaccity.gov.ph")
    db.session.commit()
    page = _sign_up(app, email="maria@gmail.com").get_data(as_text=True)
    assert "official LGU e-mail address (@tarlaccity.gov.ph)" in page
    assert "head of office's e-mail must be an official LGU address" in page.replace("&#39;", "'")
    assert User.query.filter_by(email="maria@gmail.com").first() is None
    _sign_up(app, email="maria@tarlaccity.gov.ph", lgu_endorser_email="bplo.head@tarlaccity.gov.ph")
    assert _user("maria@tarlaccity.gov.ph").lgu_status == "pending"


# ------------------------------------------------------ while pending

def test_a_pending_account_sees_only_its_access_page(app):
    _sign_up(app)
    client = _login(app, "maria@lgu.gov.ph", STRONG_PASSWORD)
    for path in ("/lgu/dashboard", "/lgu/government-data-upload", "/lgu/data-review", "/lgu/diversification"):
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"].endswith("/lgu/access"), path
    page = client.get("/lgu/access").get_data(as_text=True)
    row = LguAccessRequest.query.one()
    assert row.reference_code in page and "What happens next" in page and "MarketLens Admin" in page
    assert f"Reference code: {row.reference_code}" in page and "#get-roles" in page, "the Discord message to paste"
    assert "head.bplo@lgu.gov.ph" not in page.split('id="lguDiscordMessage"')[1].split("</pre>")[0]
    assert "LGU Access Request" in page and 'href="/lgu/dashboard"' not in page
    response = client.post("/lgu/government-data-upload", data={"dataset_type": "MARKET_DATA"})
    assert response.headers["Location"].endswith("/lgu/access")
    from app.models import UploadBatch

    assert UploadBatch.query.count() == 0


def test_a_pending_account_can_replace_its_request(app):
    _sign_up(app)
    first = LguAccessRequest.query.one()
    client = _login(app, "maria@lgu.gov.ph", STRONG_PASSWORD)
    client.post("/lgu/access", data={**LGU_REQUEST, "lgu_requested_role": "steward"})
    db.session.expire_all()
    rows = LguAccessRequest.query.order_by(LguAccessRequest.id).all()
    assert [r.status for r in rows] == ["cancelled", "pending"]
    assert rows[1].reference_code != first.reference_code and rows[1].requested_role == "steward"


# ---------------------------------------------------------- approving

def test_only_admins_open_lgu_access(app):
    _sign_up(app)
    assert _login(app, "admin@lgu.test").get("/admin/lgu-access").status_code == 200
    for email, password in (("sme@lgu.test", "password123"), ("maria@lgu.gov.ph", STRONG_PASSWORD)):
        assert _login(app, email, password).get("/admin/lgu-access").status_code in (302, 403), email
    row = LguAccessRequest.query.one()
    _login(app, "maria@lgu.gov.ph", STRONG_PASSWORD).post(f"/admin/lgu-access/{row.id}/approve",
                                                          data={"granted_role": "steward", "verified_offline": "1"})
    assert not _user().is_lgu_verified, "an applicant cannot approve themselves"


def test_approving_needs_the_official_channel_confirmation(app):
    _sign_up(app)
    row = LguAccessRequest.query.one()
    admin = _login(app, "admin@lgu.test")
    page = admin.get("/admin/lgu-access").get_data(as_text=True)
    assert row.reference_code in page and "head.bplo@lgu.gov.ph" in page and "Rosa Garcia" in page
    page = admin.post(f"/admin/lgu-access/{row.id}/approve", data={"granted_role": "encoder"},
                      follow_redirects=True).get_data(as_text=True)
    assert "official channel" in page and not _user().is_lgu_verified


def test_an_approved_applicant_gets_the_role_the_admin_chose(app):
    _sign_up(app)
    _approve(app, role="steward")
    user = _user()
    assert user.is_lgu_verified and user.is_data_steward() and user.lgu_verified_by == _user("admin@lgu.test").user_id
    assert LguAccessRequest.query.one().status == "approved"
    assert Notification.query.filter_by(user_id=user.user_id).filter(Notification.message.contains("approved")).count() == 1
    assert AuditLog.query.filter_by(action="admin_approve_lgu_access").count() == 1
    client = _login(app, "maria@lgu.gov.ph", STRONG_PASSWORD)
    assert client.get("/lgu/dashboard").status_code == 200
    assert client.get("/lgu/data-review").status_code == 200, "a Data Steward reviews"


def test_declining_needs_a_reason_and_the_applicant_can_resend(app):
    _sign_up(app)
    row = LguAccessRequest.query.one()
    admin = _login(app, "admin@lgu.test")
    admin.post(f"/admin/lgu-access/{row.id}/reject", data={"reason": "no"})
    db.session.expire_all()
    assert LguAccessRequest.query.one().status == "pending", "a reason is required"
    admin.post(f"/admin/lgu-access/{row.id}/reject",
               data={"reason": "Your head of office has not sent the endorsement e-mail."})
    db.session.expire_all()
    assert _user().lgu_status == "rejected" and LguAccessRequest.query.one().status == "rejected"
    client = _login(app, "maria@lgu.gov.ph", STRONG_PASSWORD)
    page = client.get("/lgu/access").get_data(as_text=True)
    assert "has not sent the endorsement" in page and "Send a new request" in page
    client.post("/lgu/access", data=LGU_REQUEST)
    db.session.expire_all()
    assert _user().lgu_status == "pending" and LguAccessRequest.query.filter_by(status="pending").count() == 1


# ----------------------------------------------------------- upkeep

def test_removing_access_closes_every_lgu_page(app):
    _sign_up(app)
    _approve(app)
    user_id = _user().user_id
    admin = _login(app, "admin@lgu.test")
    admin.post(f"/admin/lgu-access/user/{user_id}/revoke", data={"reason": ""})
    assert _user().is_lgu_verified, "a reason is required"
    admin.post(f"/admin/lgu-access/user/{user_id}/revoke", data={"reason": "Transferred to the Assessor's Office."})
    db.session.expire_all()
    assert _user().lgu_status == "revoked" and _user().lgu_role_key is None
    response = _login(app, "maria@lgu.gov.ph", STRONG_PASSWORD).get("/lgu/dashboard")
    assert response.headers["Location"].endswith("/lgu/access")
    assert AuditLog.query.filter_by(action="admin_revoke_lgu_access").count() == 1


def test_review_due_and_re_verify(app):
    from app.services import lgu_access_service as access

    legacy = User(name="Old LGU", email="old@lgu.test", role="LGU")
    legacy.set_password("password123")
    db.session.add(legacy)
    db.session.commit()
    assert legacy.is_lgu_verified and access.is_review_due(legacy), "pre-existing accounts are re-checked once"
    page = _login(app, "admin@lgu.test").get("/admin/lgu-access").get_data(as_text=True)
    assert "Review due" in page and "before verification was recorded" in page
    _login(app, "admin@lgu.test").post(f"/admin/lgu-access/user/{legacy.user_id}/reverify")
    db.session.expire_all()
    legacy = _user("old@lgu.test")
    assert not access.is_review_due(legacy)
    legacy.lgu_verified_at = datetime.datetime.utcnow() - datetime.timedelta(days=access.review_days() + 1)
    db.session.commit()
    assert access.is_review_due(legacy)


def test_an_admin_created_lgu_account_is_verified_by_that_admin(app):
    _login(app, "admin@lgu.test").post("/admin/users/create", data={
        "first_name": "Rosa", "last_name": "Cruz", "email": "rosa@lgu.test", "password": STRONG_PASSWORD,
        "role": "LGU_STEWARD", "lgu_office": "BPLO", "lgu_position": "Supervisor",
    })
    user = _user("rosa@lgu.test")
    assert user.is_lgu_verified and user.is_data_steward() and user.lgu_office == "BPLO"
    assert user.lgu_verified_by == _user("admin@lgu.test").user_id and user.lgu_verified_at is not None


def test_the_role_dropdown_refuses_an_unverified_account(app):
    _sign_up(app)
    _login(app, "admin@lgu.test").post(f"/admin/users/{_user().user_id}/lgu-role", data={"lgu_role": "steward"})
    db.session.expire_all()
    assert _user().lgu_role is None and not _user().is_lgu_verified


# ------------------------------------------------- retention & privacy

def test_lgu_requests_follow_retention(app):
    from app.services.retention_service import LGU_REQUEST_EXPIRY_DAYS, apply_retention

    _sign_up(app)
    _sign_up(app, email="ana@lgu.gov.ph")
    _approve(app, email="ana@lgu.gov.ph")
    stale = LguAccessRequest.query.filter_by(status="pending").one()
    stale.created_at = datetime.datetime.utcnow() - datetime.timedelta(days=LGU_REQUEST_EXPIRY_DAYS + 1)
    decided = LguAccessRequest.query.filter_by(status="approved").one()
    decided.created_at = datetime.datetime.utcnow() - datetime.timedelta(days=4000)
    db.session.commit()

    counts = apply_retention()
    assert counts["lgu_requests_expired"] == 1 and counts["lgu_access_requests"] == 1
    db.session.expire_all()
    stale = LguAccessRequest.query.one()
    assert stale.status == "expired" and stale.endorser_email is None and stale.discord_username is None
    assert _user().lgu_status == "rejected"
    assert _user("ana@lgu.gov.ph").is_lgu_verified, "deleting the evidence does not undo a verification"


# --------------------------------------------------- guide & settings

def test_the_guide_is_public_and_names_the_verifying_office(app):
    SystemSetting.set("lgu_verification_office", "City ICT Office")
    SystemSetting.set("lgu_verification_email", "ict@lgu.test")
    db.session.commit()
    page = app.test_client().get("/lgu-access-guide").get_data(as_text=True)
    for text_ in ("City ICT Office", "ict@lgu.test", "#get-roles", "Private Thread", "MarketLens Admin", "LGU staff &ndash; applicant",
                  "Endorsement e-mail template", "two different people", "RA 10175"):
        assert text_ in page, text_
    assert 'href="/lgu-access-guide"' in app.test_client().get("/register").get_data(as_text=True)


def test_lgu_access_settings_are_validated(app):
    admin = _login(app, "admin@lgu.test")
    admin.post("/admin/settings", data={"lgu_email_domains": "tarlaccity.gov.ph, @Example.ORG",
                                        "lgu_verification_email": "ict@tarlaccity.gov.ph",
                                        "lgu_verification_office": "City ICT Office", "lgu_access_review_days": "90"})
    assert SystemSetting.get("lgu_email_domains") == "tarlaccity.gov.ph, example.org"
    assert SystemSetting.get("lgu_access_review_days") == "90"
    page = admin.post("/admin/settings", data={"lgu_email_domains": "not a domain", "lgu_access_review_days": "5"},
                      follow_redirects=True).get_data(as_text=True)
    assert "not a valid e-mail domain" in page and "30-730 days" in page
    assert SystemSetting.get("lgu_email_domains") == "tarlaccity.gov.ph, example.org"


def test_system_health_checks_admins_and_lgu_roles(app):
    from app.controllers.admin_controller import _system_health

    with app.test_request_context():
        health = {h["name"]: h for h in _system_health()}
    assert health["Administrators"]["ok"], health["Administrators"]
    assert not health["LGU access"]["ok"] and "no verified" in health["LGU access"]["note"]
    _user("admin2@lgu.test").status = "inactive"
    db.session.commit()
    with app.test_request_context():
        health = {h["name"]: h for h in _system_health()}
    assert not health["Administrators"]["ok"] and "backup" in health["Administrators"]["note"]


def test_every_lgu_access_action_has_an_audit_label():
    from app.utils.audit_labels import category_for, importance_for, label_for

    for action in ("lgu_access_requested", "admin_approve_lgu_access", "admin_reject_lgu_access",
                   "admin_revoke_lgu_access", "admin_reverify_lgu_access"):
        assert label_for(action) != action and category_for(action) == "accounts" and importance_for(action) == "high"
