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


# ---------------------------------------------------------------------
# 6. Forgotten password
# ---------------------------------------------------------------------

@pytest.fixture
def reset_sent(monkeypatch):
    from app.services import email_service

    codes = []

    def fake_send(to_email, to_name, code):
        codes.append({"to": to_email, "name": to_name, "code": code})
        return True

    monkeypatch.setattr(email_service, "send_password_reset_code", fake_send)
    return codes


@pytest.fixture
def existing_user(app):
    with app.app_context():
        user = User(name="Maria Santos", email="maria@otp.test", role="SME")
        user.set_password("oldpassword1")
        db.session.add(user)
        db.session.commit()
        return user.email


def test_the_login_page_offers_a_way_out(app):
    page = app.test_client().get("/login").get_data(as_text=True)
    assert "Forgot password?" in page
    assert "/forgot-password" in page


def test_a_known_address_gets_a_code_and_can_reset(app, existing_user, reset_sent):
    client = app.test_client()
    client.post("/forgot-password", data={"email": existing_user}, follow_redirects=True)

    assert len(reset_sent) == 1
    assert re.fullmatch(r"\d{6}", reset_sent[0]["code"])

    page = client.post("/reset-password", data={
        "code": reset_sent[0]["code"],
        "password": "brandnewpass",
        "confirm_password": "brandnewpass",
    }, follow_redirects=True).get_data(as_text=True)

    assert "password has been changed" in page
    with app.app_context():
        user = User.query.filter_by(email=existing_user).first()
        assert user.check_password("brandnewpass")
        assert not user.check_password("oldpassword1")


def test_an_unknown_address_looks_exactly_like_a_known_one(app, existing_user, reset_sent):
    """The important one. A different answer for an unknown address
    turns this form into a membership oracle: submit a list, and every
    'sent' is a confirmed user of the system. For a system whose users
    are named business owners and city officials, that is not a
    harmless disclosure."""
    client = app.test_client()

    known = client.post("/forgot-password", data={"email": existing_user},
                        follow_redirects=True).get_data(as_text=True)
    unknown = client.post("/forgot-password", data={"email": "nobody@nowhere.test"},
                          follow_redirects=True).get_data(as_text=True)

    assert existing_user in known
    assert "nobody@nowhere.test" in unknown
    # Same sentence, only the address differs.
    assert known.replace(existing_user, "X") == unknown.replace("nobody@nowhere.test", "X")
    assert len(reset_sent) == 1, "a code was emailed for an address with no account"


def test_a_wrong_reset_code_does_not_change_the_password(app, existing_user, reset_sent):
    client = app.test_client()
    client.post("/forgot-password", data={"email": existing_user}, follow_redirects=True)

    wrong = "000000" if reset_sent[0]["code"] != "000000" else "111111"
    client.post("/reset-password", data={
        "code": wrong, "password": "hackedpass", "confirm_password": "hackedpass",
    }, follow_redirects=True)

    with app.app_context():
        assert User.query.filter_by(email=existing_user).first().check_password("oldpassword1")


def test_reset_codes_run_out_too(app, existing_user, reset_sent):
    client = app.test_client()
    app.config["EMAIL_VERIFICATION_MAX_ATTEMPTS"] = 2
    client.post("/forgot-password", data={"email": existing_user}, follow_redirects=True)

    wrong = "000000" if reset_sent[0]["code"] != "000000" else "111111"
    payload = {"code": wrong, "password": "hackedpass", "confirm_password": "hackedpass"}
    client.post("/reset-password", data=payload, follow_redirects=True)
    page = client.post("/reset-password", data=payload, follow_redirects=True).get_data(as_text=True)

    assert "Too many incorrect codes" in page

    # ...and the real code is dead with it.
    client.post("/reset-password", data={
        "code": reset_sent[0]["code"], "password": "hackedpass",
        "confirm_password": "hackedpass",
    }, follow_redirects=True)
    with app.app_context():
        assert User.query.filter_by(email=existing_user).first().check_password("oldpassword1")


def test_an_expired_reset_code_is_refused(app, existing_user, reset_sent):
    from app.controllers.auth_controller import PASSWORD_RESET_SESSION_KEY

    client = app.test_client()
    client.post("/forgot-password", data={"email": existing_user}, follow_redirects=True)

    with client.session_transaction() as session:
        pending = session[PASSWORD_RESET_SESSION_KEY]
        pending["expires_at"] = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
        session[PASSWORD_RESET_SESSION_KEY] = pending

    page = client.post("/reset-password", data={
        "code": reset_sent[0]["code"], "password": "brandnewpass",
        "confirm_password": "brandnewpass",
    }, follow_redirects=True).get_data(as_text=True)

    assert "expired" in page.lower()
    with app.app_context():
        assert User.query.filter_by(email=existing_user).first().check_password("oldpassword1")


def test_mismatched_new_passwords_are_refused(app, existing_user, reset_sent):
    client = app.test_client()
    client.post("/forgot-password", data={"email": existing_user}, follow_redirects=True)

    page = client.post("/reset-password", data={
        "code": reset_sent[0]["code"], "password": "brandnewpass",
        "confirm_password": "differentpass",
    }, follow_redirects=True).get_data(as_text=True)

    assert "do not match" in page
    with app.app_context():
        assert User.query.filter_by(email=existing_user).first().check_password("oldpassword1")


def test_a_deactivated_account_gets_no_reset_code(app, reset_sent):
    with app.app_context():
        user = User(name="Gone", email="gone@otp.test", role="SME")
        user.set_password("oldpassword1")
        user.status = "inactive"
        db.session.add(user)
        db.session.commit()

    client = app.test_client()
    client.post("/forgot-password", data={"email": "gone@otp.test"}, follow_redirects=True)

    assert reset_sent == [], "a deactivated account could still reset its password"


def test_reset_resend_is_throttled(app, existing_user, reset_sent):
    client = app.test_client()
    app.config["EMAIL_VERIFICATION_RESEND_SECONDS"] = 60
    client.post("/forgot-password", data={"email": existing_user}, follow_redirects=True)

    client.post("/reset-password/resend", follow_redirects=True)
    assert len(reset_sent) == 1


# ---------------------------------------------------------------------
# 7. The transport that works on a host with no outbound SMTP
# ---------------------------------------------------------------------

def test_brevo_is_used_when_its_key_is_set(app, monkeypatch):
    """Render blocks outbound SMTP on free web services (ports 25, 465,
    587) as of 26 September 2026, so smtplib cannot deliver there at
    all. This path goes over HTTPS on 443 instead."""
    import requests

    from app.services import email_service

    captured = {}

    class _Response:
        status_code = 201
        text = '{"messageId":"<x@brevo>"}'

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, body=json)
        return _Response()

    monkeypatch.setattr(requests, "post", fake_post)
    app.config["BREVO_API_KEY"] = "xkeysib-test"
    app.config["MAIL_FROM_ADDRESS"] = "smesystem2026@gmail.com"

    with app.app_context():
        assert email_service.send_verification_code("new@user.test", "New User", "123456") is True

    assert captured["url"] == "https://api.brevo.com/v3/smtp/email"
    assert captured["headers"]["api-key"] == "xkeysib-test"
    assert captured["body"]["to"] == [{"email": "new@user.test", "name": "New User"}]
    assert "123456" in captured["body"]["textContent"]


def test_smtp_is_still_used_when_brevo_is_not_configured(app, monkeypatch):
    """Local development and any paid host must keep working with no
    configuration change at all."""
    from app.services import email_service

    called = {"smtp": False}

    def fake_smtp(*_args, **_kwargs):
        called["smtp"] = True
        return True

    monkeypatch.setattr(email_service, "_send_via_gmail_smtp", fake_smtp)
    app.config["BREVO_API_KEY"] = ""

    with app.app_context():
        email_service.send_verification_code("new@user.test", "New User", "123456")

    assert called["smtp"] is True


def test_a_blocked_smtp_port_is_explained_not_just_reported(app):
    """The failure that actually happened. "Connection timed out" is
    true and useless; the hint has to name the cause and the way
    out."""
    from app.services import email_service

    with app.app_context():
        email_service._record_failure(TimeoutError("timed out"))
        hint = email_service.last_failure()["hint"]

    assert "Render blocks outbound SMTP" in hint
    assert "BREVO_API_KEY" in hint


def test_the_code_is_a_random_six_digit_integer(app, sent):
    """Generated with `secrets`, not `random`: a Mersenne Twister's
    internal state can be reconstructed from a modest number of
    observed outputs, and these codes guard account creation and
    password resets."""
    import inspect

    from app.controllers import auth_controller

    source = inspect.getsource(auth_controller)
    assert "secrets.randbelow(1_000_000)" in source
    assert "random.randint" not in source, (
        "the OTP generator was switched to `random`, which is predictable"
    )

    client = app.test_client()
    _register(client)
    assert re.fullmatch(r"\d{6}", sent[0]["code"])
    assert 0 <= int(sent[0]["code"]) <= 999_999


def test_an_unexpected_transport_error_does_not_500_the_form(app, monkeypatch):
    """The guard that turns a crash into a message.

    Each transport catches what it expects -- an SMTP error, an HTTP
    error. This covers what they do not: anything raised before their
    own try block, or by a library behaving differently on the host
    than it does in development. Registration and password reset both
    call this mid-form, and a mail problem becoming a 500 loses the
    visitor's typing and tells them nothing.
    """
    from app.services import email_service

    def exploding(*_args, **_kwargs):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(email_service, "_send_via_brevo", exploding)
    app.config["BREVO_API_KEY"] = "xkeysib-test"

    with app.app_context():
        assert email_service.send_verification_code("a@b.test", "A", "123456") is False
        failure = email_service.last_failure()

    assert "something nobody anticipated" in failure["detail"]
    assert "traceback" in failure, (
        "an unexpected error must carry its traceback into the diagnostics -- the "
        "type and message alone rarely say what to change"
    )


def test_the_registration_form_survives_an_exploding_transport(app, monkeypatch):
    """End to end: the visitor gets a message, not a 500."""
    from app.services import email_service

    monkeypatch.setattr(email_service, "_send_via_brevo",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    app.config["BREVO_API_KEY"] = "xkeysib-test"
    app.config["REQUIRE_EMAIL_VERIFICATION"] = True

    client = app.test_client()
    response = _register(client)

    assert response.status_code == 200
    assert "account was not created" in response.get_data(as_text=True)


def test_the_forgot_password_form_survives_an_exploding_transport(app, monkeypatch):
    from app.services import email_service

    with app.app_context():
        user = User(name="Maria", email="maria2@otp.test", role="SME")
        user.set_password("oldpassword1")
        db.session.add(user)
        db.session.commit()

    monkeypatch.setattr(email_service, "_send_via_brevo",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    app.config["BREVO_API_KEY"] = "xkeysib-test"

    client = app.test_client()
    response = client.post("/forgot-password", data={"email": "maria2@otp.test"},
                           follow_redirects=True)

    assert response.status_code == 200


def test_the_brevo_key_is_redacted_from_a_traceback(app):
    """A traceback can carry a local variable holding the API key."""
    from app.services import email_service

    app.config["BREVO_API_KEY"] = "xkeysib-SECRET-value-here"
    with app.app_context():
        email_service._record_failure(
            RuntimeError("failed with xkeysib-SECRET-value-here"), include_traceback=True
        )
        failure = email_service.last_failure()

    assert "xkeysib-SECRET-value-here" not in repr(failure)
