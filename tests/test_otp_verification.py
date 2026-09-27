"""
tests/test_otp_verification.py
---------------------------------
Email OTP verification for self-registration.

The flow itself already existed -- a 6-digit code from `secrets`, a
ten-minute expiry, a verify route and a resend route -- and was dormant
only because no Gmail credentials were configured. What it did not have
was any of the things that make an OTP an actual control rather than a
formality:

  1. A SEND FAILURE CREATED THE ACCOUNT ANYWAY. On a deployment that
     means to verify people, that is the worst available outcome,
     because it is silent: a mistyped app password does not look like
     "verification is broken", it looks like "verification quietly is
     not happening".
  2. UNLIMITED GUESSES. One chance in a million per guess is only
     protection if the guesses are bounded. Unbounded, an attacker can
     post them as fast as HTTP allows for the whole ten minutes.
  3. AN UNTHROTTLED RESEND. A free Gmail account sends about 500
     messages a day, and the recipient is whoever filled in the form --
     so the button is both a quota drain and a way to flood a
     stranger's inbox from your address.

These tests never touch the network: email_service.send_verification_code
is replaced so the code can be read directly.
"""

import re
from datetime import datetime, timedelta

import pytest

from app import create_app
from app.extensions import db
from app.models import SystemSetting
from app.models.user import User

from app.ml.constants import BUSINESS_TYPES

# An SME sign-up also creates a first business plan, so the three
# fields the forecasting engine cannot work without have to be here or
# the form never reaches the code-sending branch.
REGISTRATION = {
    "full_name": "Juan Dela Cruz",
    "email": "newuser@otp.test",
    "password": "password123",
    "confirm_password": "password123",
    "role": "sme",
    "business_name": "Juan's Coffee",
    "industry_type": BUSINESS_TYPES[0],
    "location": "Poblacion",
    "business_stage": "startup",
}


@pytest.fixture
def app():
    app = create_app("testing")
    app.config["GMAIL_ADDRESS"] = "smesystem2026@gmail.com"
    app.config["GMAIL_APP_PASSWORD"] = "x" * 16
    app.config["REQUIRE_EMAIL_VERIFICATION"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def sent(monkeypatch):
    """Captures the codes that would have been emailed."""
    from app.services import email_service

    codes = []

    def fake_send(to_email, to_name, code):
        codes.append({"to": to_email, "name": to_name, "code": code})
        return True

    monkeypatch.setattr(email_service, "send_verification_code", fake_send)
    return codes


def _register(client, **overrides):
    data = dict(REGISTRATION)
    data.update(overrides)
    return client.post("/register", data=data, follow_redirects=True)


# ---------------------------------------------------------------------
# 1. The happy path
# ---------------------------------------------------------------------

def test_registering_sends_a_six_digit_code_and_creates_nothing_yet(app, sent):
    client = app.test_client()
    page = _register(client)

    assert len(sent) == 1
    assert sent[0]["to"] == REGISTRATION["email"]
    assert re.fullmatch(r"\d{6}", sent[0]["code"]), sent[0]["code"]
    assert "verification code" in page.get_data(as_text=True)

    with app.app_context():
        assert User.query.filter_by(email=REGISTRATION["email"]).first() is None, (
            "the account was created before the code was confirmed"
        )


def test_the_right_code_creates_the_account(app, sent):
    client = app.test_client()
    _register(client)

    page = client.post("/register/verify", data={"code": sent[0]["code"]},
                       follow_redirects=True).get_data(as_text=True)

    assert "Email verified" in page
    with app.app_context():
        user = User.query.filter_by(email=REGISTRATION["email"]).first()
        assert user is not None
        assert user.role == "SME"


def test_an_expired_code_is_refused(app, sent):
    from app.controllers.auth_controller import PENDING_SESSION_KEY

    client = app.test_client()
    _register(client)

    with client.session_transaction() as session:
        pending = session[PENDING_SESSION_KEY]
        pending["expires_at"] = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
        session[PENDING_SESSION_KEY] = pending

    page = client.post("/register/verify", data={"code": sent[0]["code"]},
                       follow_redirects=True).get_data(as_text=True)

    assert "expired" in page.lower()
    with app.app_context():
        assert User.query.filter_by(email=REGISTRATION["email"]).first() is None


# ---------------------------------------------------------------------
# 2. Guesses are bounded
# ---------------------------------------------------------------------

def test_wrong_codes_are_counted_and_run_out(app, sent):
    client = app.test_client()
    _register(client)
    app.config["EMAIL_VERIFICATION_MAX_ATTEMPTS"] = 3

    wrong = "000000" if sent[0]["code"] != "000000" else "111111"

    first = client.post("/register/verify", data={"code": wrong},
                        follow_redirects=True).get_data(as_text=True)
    assert "2 attempts left" in first, first[:0] or "expected a remaining-attempts count"

    client.post("/register/verify", data={"code": wrong}, follow_redirects=True)
    third = client.post("/register/verify", data={"code": wrong},
                        follow_redirects=True).get_data(as_text=True)

    assert "Too many incorrect codes" in third
    with app.app_context():
        assert User.query.filter_by(email=REGISTRATION["email"]).first() is None


def test_the_real_code_is_useless_after_the_attempts_run_out(app, sent):
    """Burning the pending registration has to actually burn it --
    otherwise the cap is a message rather than a control."""
    client = app.test_client()
    _register(client)
    app.config["EMAIL_VERIFICATION_MAX_ATTEMPTS"] = 2

    wrong = "000000" if sent[0]["code"] != "000000" else "111111"
    client.post("/register/verify", data={"code": wrong}, follow_redirects=True)
    client.post("/register/verify", data={"code": wrong}, follow_redirects=True)

    page = client.post("/register/verify", data={"code": sent[0]["code"]},
                       follow_redirects=True).get_data(as_text=True)

    assert "Start registration again" in page or "Register" in page
    with app.app_context():
        assert User.query.filter_by(email=REGISTRATION["email"]).first() is None


def test_a_resend_gives_a_new_code_and_a_fresh_allowance(app, sent):
    """A new code deserves new attempts -- but resending must not buy
    more guesses at the OLD code, so the old one must stop working."""
    client = app.test_client()
    _register(client)
    app.config["EMAIL_VERIFICATION_MAX_ATTEMPTS"] = 3
    app.config["EMAIL_VERIFICATION_RESEND_SECONDS"] = 0

    wrong = "000000" if sent[0]["code"] != "000000" else "111111"
    client.post("/register/verify", data={"code": wrong}, follow_redirects=True)
    client.post("/register/verify", data={"code": wrong}, follow_redirects=True)

    first_code = sent[0]["code"]
    client.post("/register/resend-code", follow_redirects=True)
    assert len(sent) == 2
    assert sent[1]["code"] != first_code, "the resend reissued the same code"

    stale = client.post("/register/verify", data={"code": first_code},
                        follow_redirects=True).get_data(as_text=True)
    assert "attempts left" in stale, "the superseded code still worked"

    page = client.post("/register/verify", data={"code": sent[1]["code"]},
                       follow_redirects=True).get_data(as_text=True)
    assert "Email verified" in page


# ---------------------------------------------------------------------
# 3. Resend is throttled
# ---------------------------------------------------------------------

def test_resending_immediately_is_refused(app, sent):
    client = app.test_client()
    _register(client)
    app.config["EMAIL_VERIFICATION_RESEND_SECONDS"] = 60

    page = client.post("/register/resend-code", follow_redirects=True).get_data(as_text=True)

    assert len(sent) == 1, "the throttle let a second message straight through"
    assert "wait" in page.lower()


def test_resending_after_the_cooldown_works(app, sent):
    from app.controllers.auth_controller import PENDING_SESSION_KEY

    client = app.test_client()
    _register(client)
    app.config["EMAIL_VERIFICATION_RESEND_SECONDS"] = 60

    with client.session_transaction() as session:
        pending = session[PENDING_SESSION_KEY]
        pending["last_sent_at"] = (datetime.utcnow() - timedelta(seconds=90)).isoformat()
        session[PENDING_SESSION_KEY] = pending

    client.post("/register/resend-code", follow_redirects=True)
    assert len(sent) == 2


# ---------------------------------------------------------------------
# 4. A send failure does not quietly skip verification
# ---------------------------------------------------------------------

def test_a_failed_send_blocks_registration_when_verification_is_required(app, monkeypatch):
    from app.services import email_service

    monkeypatch.setattr(email_service, "send_verification_code",
                        lambda *_args, **_kwargs: False)
    app.config["REQUIRE_EMAIL_VERIFICATION"] = True

    client = app.test_client()
    page = _register(client).get_data(as_text=True)

    assert "account was not created" in page
    with app.app_context():
        assert User.query.filter_by(email=REGISTRATION["email"]).first() is None, (
            "a wrong app password silently produced an unverified account"
        )


def test_a_failed_send_still_falls_back_when_verification_is_optional(app, monkeypatch):
    """The old behaviour is still available, because on a machine with
    no internet it is the right one -- it just is not the default any
    more once Gmail is configured."""
    from app.services import email_service

    monkeypatch.setattr(email_service, "send_verification_code",
                        lambda *_args, **_kwargs: False)
    app.config["REQUIRE_EMAIL_VERIFICATION"] = False

    client = app.test_client()
    _register(client)

    with app.app_context():
        assert User.query.filter_by(email=REGISTRATION["email"]).first() is not None


def test_verification_is_required_by_default_once_gmail_is_configured(monkeypatch):
    """Configuring Gmail IS the act of saying you want verification, so
    it should not also need a second switch nobody knows about."""
    import importlib

    monkeypatch.setenv("GMAIL_ADDRESS", "smesystem2026@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.delenv("REQUIRE_EMAIL_VERIFICATION", raising=False)

    from app import config as config_module

    importlib.reload(config_module)
    try:
        assert config_module.Config.REQUIRE_EMAIL_VERIFICATION is True
        # ...and the spaces Google displays are stripped, because
        # smtplib does not reliably tolerate them and the resulting 535
        # is indistinguishable from a wrong password.
        assert config_module.Config.GMAIL_APP_PASSWORD == "abcdefghijklmnop"
    finally:
        monkeypatch.undo()
        importlib.reload(config_module)


# ---------------------------------------------------------------------
# 5. Diagnostics, and what they must never print
# ---------------------------------------------------------------------

def test_the_status_never_returns_the_app_password(app):
    from app.services import email_service

    secret = "abcdefghijklmnop"
    app.config["GMAIL_APP_PASSWORD"] = secret
    with app.app_context():
        status = email_service.status()

    assert secret not in repr(status)
    assert status["app_password"] == {"set": True, "length": 16, "looks_right": True}


def test_a_password_with_spaces_left_in_is_visibly_the_wrong_length(app):
    """The single most common setup mistake, made obvious rather than
    left as an unexplained 535."""
    from app.services import email_service

    app.config["GMAIL_APP_PASSWORD"] = "abcd efgh ijkl mnop"
    with app.app_context():
        status = email_service.status()

    assert status["app_password"]["length"] == 19
    assert status["app_password"]["looks_right"] is False


def test_an_smtp_error_is_recorded_with_a_usable_hint(app):
    from app.services import email_service

    with app.app_context():
        email_service._record_failure(
            Exception("(535, b'5.7.8 Username and Password not accepted')")
        )
        failure = email_service.last_failure()

    assert "535" in failure["detail"]
    assert "app password" in failure["hint"].lower()


def test_an_error_message_cannot_leak_the_password(app):
    from app.services import email_service

    secret = "abcdefghijklmnop"
    app.config["GMAIL_APP_PASSWORD"] = secret
    with app.app_context():
        email_service._record_failure(Exception(f"535 rejected {secret}"))
        assert secret not in email_service.last_failure()["detail"]


def test_the_email_status_endpoint_is_admin_only(app):
    with app.app_context():
        user = User(name="Juan", email="sme@diag.test", role="SME")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()

    client = app.test_client()
    client.post("/login", data={"email": "sme@diag.test", "password": "password123"},
                follow_redirects=True)
    response = client.get("/admin/email-status", follow_redirects=False)

    assert response.status_code in (302, 403)


def test_an_admin_sees_the_configuration(app):
    with app.app_context():
        admin = User(name="Admin", email="admin@diag.test", role="Admin")
        admin.set_password("password123")
        db.session.add(admin)
        db.session.commit()

    client = app.test_client()
    client.post("/login", data={"email": "admin@diag.test", "password": "password123"},
                follow_redirects=True)
    payload = client.get("/admin/email-status").get_json()

    assert payload["status"]["gmail_address"] == "smesystem2026@gmail.com"
    assert payload["status"]["require_verification"] is True
    assert "x" * 16 not in repr(payload)
