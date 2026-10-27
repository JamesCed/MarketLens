"""
tests/test_lgu_access_verification.py
---------------------------------------
LGU roles are given only after the System Admin approves the person
(app/services/lgu_access_service.py):

  * LGU staff register on /register/lgu (account details, Tarlac City LGU
    context, role and reason); the account is "pending_verification";
  * while pending it can open the LGU Dashboard, Diversification Plan,
    Saturation Map and Trend Reports, but Gov't Data Upload / Review show
    as "Pending Request Access..." and cannot be opened;
  * the System Admin decides on Manage Users; the decision is e-mailed
    with a link to the page the role opens;
  * re-checks, removing access, retention, the guide and the settings.
"""

import datetime

import pytest

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


@pytest.fixture
def outbox(monkeypatch):
    """Capture account e-mails instead of sending them."""
    from app.services import email_service

    sent = []
    monkeypatch.setattr(email_service, "is_configured", lambda: True)
    monkeypatch.setattr(email_service, "send_account_notice",
                        lambda to, name, subject, body: sent.append((to, subject, body)) or True)
    return sent


def _login(app, email, password="password123"):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": password})
    return client


def _register(app, email="maria@lgu.gov.ph", **overrides):
    data = {**LGU_REQUEST, **account_fields(email, first_name="Maria", last_name="Santos")}
    data.update(overrides)
    return app.test_client().post("/register/lgu", data=data, follow_redirects=True)


def _user(email="maria@lgu.gov.ph"):
    return User.query.filter_by(email=email).one()


def _approve(app, role="encoder", email="maria@lgu.gov.ph", note=""):
    row = LguAccessRequest.query.filter_by(user_id=_user(email).user_id, status="pending_verification").one()
    _login(app, "admin@lgu.test").post(f"/admin/lgu-access/{row.id}/approve",
                                       data={"granted_role": role, "verified_offline": "1", "note": note})
    db.session.expire_all()
    return row


# ---------------------------------------------------------- registration

def test_the_lgu_form_has_the_three_parts(app):
    page = app.test_client().get("/register/lgu").get_data(as_text=True)
    for text in ("Account details", "Tarlac City LGU context", "Role selection &amp; system scope",
                 'name="lgu_scope"', 'name="lgu_barangay"', 'name="lgu_office"', 'name="lgu_position"',
                 'name="lgu_requested_role"', 'name="lgu_reason"', "Barangay Executive Office",
                 "Official work e-mail address"):
        assert text in page, text
    assert page.count('<option value="') >= 76 + 6, "76 barangays and the offices"
    assert 'name="gender"' not in page and 'name="birthdate"' not in page, "only what is needed"
    assert 'id="lgu-barangay" name="lgu_barangay" class="form-select" disabled' in page, \
        "Target Barangay is off until 'Specific barangay' is chosen"
    sign_up = app.test_client().get("/register").get_data(as_text=True)
    assert "/register/lgu" in sign_up, "the LGU choice on the main sign-up opens this form"


def test_registration_creates_a_pending_verification_account(app):
    page = _register(app, lgu_scope="barangay", lgu_barangay="San Roque",
                     lgu_office="Barangay Executive Office", lgu_position="Barangay Secretary",
                     lgu_requested_role="steward").get_data(as_text=True)
    user = _user()
    row = LguAccessRequest.query.filter_by(user_id=user.user_id).one()
    assert user.role == "LGU" and user.lgu_status == "pending_verification" and user.is_lgu_pending
    assert user.lgu_role_key is None and not user.is_lgu_encoder() and not user.is_data_steward()
    assert user.lgu_scope_label == "Barangay San Roque" and user.lgu_office == "Barangay Executive Office"
    assert row.status == "pending_verification" and row.requested_role == "steward"
    assert row.scope_label == "Barangay San Roque" and row.reason == LGU_REQUEST["lgu_reason"]
    assert user.gender is None and user.contact_number is None
    assert "pending verification" in page and "24 hours" in page
    told = {n.user_id for n in Notification.query.filter(Notification.message.contains("New LGU access request"))}
    assert told == {_user("admin@lgu.test").user_id, _user("admin2@lgu.test").user_id}
    assert AuditLog.query.filter_by(action="lgu_access_requested").count() == 1


@pytest.mark.parametrize("field, value, message", [
    ("lgu_scope", "", "jurisdiction scope"),
    ("lgu_office", "Planet Office", "department or office"),
    ("lgu_position", "", "official job title"),
    ("lgu_requested_role", "admin", "Choose the LGU role you need"),
    ("lgu_reason", "data", "which datasets your office will manage"),
    ("first_name", "", "First name is required"),
])
def test_an_incomplete_registration_creates_no_account(app, field, value, message):
    page = _register(app, **{field: value}).get_data(as_text=True)
    assert message in page
    assert User.query.filter_by(email="maria@lgu.gov.ph").first() is None


def test_barangay_scope_needs_a_real_barangay(app):
    page = _register(app, lgu_scope="barangay", lgu_barangay="Atlantis").get_data(as_text=True)
    assert "76 barangays" in page and User.query.filter_by(email="maria@lgu.gov.ph").first() is None
    _register(app, lgu_scope="city", lgu_barangay="San Roque")
    assert _user().lgu_barangay is None and _user().lgu_scope_label == "City-wide", "a city-wide scope keeps no barangay"


def test_the_official_domain_is_enforced_when_set(app):
    SystemSetting.set("lgu_email_domains", "tarlaccity.gov.ph")
    db.session.commit()
    page = _register(app, email="maria@gmail.com").get_data(as_text=True)
    assert "official LGU work e-mail address (@tarlaccity.gov.ph)" in page
    assert User.query.filter_by(email="maria@gmail.com").first() is None
    _register(app, email="maria@tarlaccity.gov.ph")
    assert _user("maria@tarlaccity.gov.ph").is_lgu_pending


# ---------------------------------------------------------- while pending

def test_a_pending_account_plans_but_cannot_upload_or_review(app):
    _register(app)
    client = _login(app, "maria@lgu.gov.ph", STRONG_PASSWORD)
    for path in ("/lgu/dashboard", "/lgu/diversification", "/saturation-map", "/trend-reports"):
        assert client.get(path).status_code == 200, path
    for path in ("/lgu/government-data-upload", "/lgu/data-review"):
        response = client.get(path)
        assert response.status_code == 302 and response.headers["Location"].endswith("/lgu/access"), path
    page = client.get("/lgu/access").get_data(as_text=True).replace("&#39;", "'")
    assert "Pending" in page and 'class="dss-dots"' in page and "24 hours" in page
    assert "Pending Request Access" in page, "the sidebar shows the role page as pending"
    assert 'href="/lgu/government-data-upload"' not in page and 'href="/lgu/data-review"' not in page
    assert 'href="/lgu/dashboard"' in page and 'href="/lgu/diversification"' in page
    from app.models import UploadBatch

    client.post("/lgu/government-data-upload", data={"dataset_type": "MARKET_DATA"})
    assert UploadBatch.query.count() == 0


def test_signing_in_while_pending_lands_on_the_pending_page(app):
    _register(app)
    response = app.test_client().post("/login", data={"email": "maria@lgu.gov.ph", "password": STRONG_PASSWORD})
    assert response.headers["Location"].endswith("/lgu/access")


def test_a_pending_account_can_update_its_request(app):
    _register(app)
    client = _login(app, "maria@lgu.gov.ph", STRONG_PASSWORD)
    client.post("/lgu/access", data={**LGU_REQUEST, "lgu_requested_role": "steward"})
    db.session.expire_all()
    rows = LguAccessRequest.query.order_by(LguAccessRequest.id).all()
    assert [r.status for r in rows] == ["cancelled", "pending_verification"] and rows[1].requested_role == "steward"


def test_requests_wait_until_the_admin_decides(app):
    from app.services.retention_service import apply_retention

    _register(app)
    row = LguAccessRequest.query.one()
    row.created_at = datetime.datetime.utcnow() - datetime.timedelta(days=4000)
    db.session.commit()
    apply_retention()
    db.session.expire_all()
    assert LguAccessRequest.query.one().status == "pending_verification" and _user().is_lgu_pending


# ---------------------------------------------------------- the decision

def test_only_admins_decide(app):
    _register(app)
    row = LguAccessRequest.query.one()
    page = _login(app, "admin@lgu.test").get("/admin/users").get_data(as_text=True)
    assert 'id="lgu-access"' in page and row.reference_code in page and LGU_REQUEST["lgu_reason"] in page
    assert page.index('role="search"') < page.index('id="lgu-access"') < page.index('class="table align-middle dss-admin-table"'), \
        "the panel sits between the search bar and the accounts table"
    assert _login(app, "admin@lgu.test").get("/admin/lgu-access").headers["Location"].endswith("/admin/users#lgu-access")
    _login(app, "maria@lgu.gov.ph", STRONG_PASSWORD).post(f"/admin/lgu-access/{row.id}/approve",
                                                          data={"granted_role": "steward", "verified_offline": "1"})
    assert not _user().is_lgu_verified, "an applicant cannot approve themselves"


def test_approving_needs_the_confirmation(app):
    _register(app)
    row = LguAccessRequest.query.one()
    page = _login(app, "admin@lgu.test").post(f"/admin/lgu-access/{row.id}/approve", data={"granted_role": "encoder"},
                                              follow_redirects=True).get_data(as_text=True)
    assert "works in the office they named" in page and not _user().is_lgu_verified


def test_approval_opens_the_role_page_and_emails_its_link(app, outbox):
    _register(app, lgu_scope="barangay", lgu_barangay="San Roque")
    _approve(app, role="steward", note="Confirmed with the barangay captain.")
    user = _user()
    assert user.is_lgu_verified and user.is_data_steward() and user.lgu_scope_label == "Barangay San Roque"
    to, subject, body = outbox[-1]
    assert to == "maria@lgu.gov.ph" and "LGU Data Steward access was approved" in subject
    assert "http://localhost/lgu/data-review" in body and "Confirmed with the barangay captain." in body
    client = _login(app, "maria@lgu.gov.ph", STRONG_PASSWORD)
    page = client.get("/lgu/data-review").get_data(as_text=True).replace("&#39;", "'")
    assert "Gov't Data Review" in page and "Pending Request Access" not in page
    assert AuditLog.query.filter_by(action="admin_approve_lgu_access").count() == 1


def test_an_encoder_approval_links_to_the_upload_page(app, outbox):
    _register(app)
    _approve(app, role="encoder")
    assert "http://localhost/lgu/government-data-upload" in outbox[-1][2]
    assert _login(app, "maria@lgu.gov.ph", STRONG_PASSWORD).get("/lgu/government-data-upload").status_code == 200


def test_rejection_needs_a_reason_is_emailed_and_can_be_resent(app, outbox):
    _register(app)
    row = LguAccessRequest.query.one()
    admin = _login(app, "admin@lgu.test")
    admin.post(f"/admin/lgu-access/{row.id}/reject", data={"reason": "no"})
    db.session.expire_all()
    assert LguAccessRequest.query.one().status == "pending_verification", "a reason is required"
    admin.post(f"/admin/lgu-access/{row.id}/reject", data={"reason": "Please register with your LGU work e-mail."})
    db.session.expire_all()
    assert _user().lgu_status == "rejected"
    to, subject, body = outbox[-1]
    assert "not approved" in subject and "LGU work e-mail" in body and "http://localhost/lgu/access" in body
    client = _login(app, "maria@lgu.gov.ph", STRONG_PASSWORD)
    page = client.get("/lgu/access").get_data(as_text=True)
    assert "LGU work e-mail" in page and "Send a new request" in page
    assert "Access request rejected" in page
    client.post("/lgu/access", data=LGU_REQUEST)
    db.session.expire_all()
    assert _user().is_lgu_pending


# ---------------------------------------------------------- upkeep

def test_removing_access_closes_the_role_page_only(app):
    _register(app)
    _approve(app)
    user_id = _user().user_id
    _login(app, "admin@lgu.test").post(f"/admin/lgu-access/user/{user_id}/revoke",
                                       data={"reason": "Transferred to the Assessor's Office."})
    db.session.expire_all()
    assert _user().lgu_status == "revoked" and _user().lgu_role_key is None
    client = _login(app, "maria@lgu.gov.ph", STRONG_PASSWORD)
    assert client.get("/lgu/government-data-upload").headers["Location"].endswith("/lgu/access")
    assert client.get("/lgu/dashboard").status_code == 200


def test_review_due_and_re_verify(app):
    from app.services import lgu_access_service as access

    legacy = User(name="Old LGU", email="old@lgu.test", role="LGU")
    legacy.set_password("password123")
    db.session.add(legacy)
    db.session.commit()
    assert legacy.is_lgu_verified and access.is_review_due(legacy)
    page = _login(app, "admin@lgu.test").get("/admin/users").get_data(as_text=True)
    assert "Review due" in page
    _login(app, "admin@lgu.test").post(f"/admin/lgu-access/user/{legacy.user_id}/reverify")
    db.session.expire_all()
    assert not access.is_review_due(_user("old@lgu.test"))


def test_an_admin_created_lgu_account_is_verified(app):
    _login(app, "admin@lgu.test").post("/admin/users/create", data={
        "first_name": "Rosa", "last_name": "Cruz", "email": "rosa@lgu.test", "password": STRONG_PASSWORD,
        "role": "LGU_STEWARD", "lgu_office": "BPLO", "lgu_position": "Supervisor",
    })
    user = _user("rosa@lgu.test")
    assert user.is_lgu_verified and user.is_data_steward() and user.lgu_verified_by == _user("admin@lgu.test").user_id


def test_old_pending_status_is_renamed_at_start_up(app):
    from app.services.startup_migrations import _rename_pending_lgu_status

    _register(app)
    user = _user()
    user.lgu_status = "pending"
    LguAccessRequest.query.one().status = "pending"
    db.session.commit()
    assert _user().is_lgu_pending, "the old value still reads as pending"
    _rename_pending_lgu_status()
    db.session.expire_all()
    assert _user().lgu_status == "pending_verification" and LguAccessRequest.query.one().status == "pending_verification"


# --------------------------------------------------- guide & settings

def test_the_guide_describes_the_new_flow(app):
    page = app.test_client().get("/lgu-access-guide").get_data(as_text=True)
    for text in ("Register as LGU staff", "Pending&hellip;", "24 hours", "Gov&rsquo;t Data Upload", "Specific barangay"):
        assert text in page, text
    assert "#get-roles" not in page and "head of office" not in page, "Discord and endorsements are gone"


def test_lgu_access_settings_are_validated(app):
    admin = _login(app, "admin@lgu.test")
    admin.post("/admin/settings", data={"lgu_email_domains": "tarlaccity.gov.ph, @Example.ORG",
                                        "lgu_access_review_days": "90"})
    assert SystemSetting.get("lgu_email_domains") == "tarlaccity.gov.ph, example.org"
    page = admin.post("/admin/settings", data={"lgu_email_domains": "not a domain", "lgu_access_review_days": "5"},
                      follow_redirects=True).get_data(as_text=True)
    assert "not a valid e-mail domain" in page and "30-730 days" in page


def test_system_health_checks_admins_and_lgu_roles(app):
    from app.controllers.admin_controller import _system_health

    with app.test_request_context():
        health = {h["name"]: h for h in _system_health()}
    assert health["Administrators"]["ok"]
    assert not health["LGU access"]["ok"] and "no verified" in health["LGU access"]["note"]


def test_every_lgu_access_action_has_an_audit_label():
    from app.utils.audit_labels import category_for, importance_for, label_for

    for action in ("lgu_access_requested", "admin_approve_lgu_access", "admin_reject_lgu_access",
                   "admin_revoke_lgu_access", "admin_reverify_lgu_access"):
        assert label_for(action) != action and category_for(action) == "accounts" and importance_for(action) == "high"
