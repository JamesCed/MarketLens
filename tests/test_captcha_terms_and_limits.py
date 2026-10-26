"""
tests/test_captcha_terms_and_limits.py
----------------------------------------
Invisible human verification, rate limits, and the Terms / Privacy pages.

  * Sign-in, sign-up and "forgot password" need a fresh CAPTCHA token on
    every submission once keys are configured.
  * The AI / Google Places routes need a human pass (one verified token
    per CAPTCHA_PASS_MINUTES) and are rate limited per account.
  * Failed sign-ins are throttled per network and e-mail.
  * The Terms carry a disclaimer of warranty, acceptable-use rules and
    the third-party (Google Places) acknowledgement; the Privacy Policy
    explains hashed passwords, session cookies (no JWTs) and location
    queries; both are linked from every page and agreed to at sign-up.
"""

import pytest

from app import create_app
from app.extensions import db
from app.ml.constants import BUSINESS_TYPES
from app.ml.seed_data import BARANGAY_NAMES
from app.models import SystemSetting
from app.models.user import User
from tests.account_fixtures import LGU_REQUEST, STRONG_PASSWORD, account_fields

GOOD_TOKEN = "XXXX.DUMMY.TOKEN.XXXX"


@pytest.fixture
def app(monkeypatch):
    app = create_app("testing")
    app.config.update(CAPTCHA_PROVIDER="turnstile", CAPTCHA_SITE_KEY="1x00000000000000000000BB",
                      CAPTCHA_SECRET_KEY="1x0000000000000000000000000000000AA", RATE_LIMIT_ENABLED=True)
    calls = []

    def fake_verify(url, data):
        calls.append((url, dict(data)))
        return {"success": data.get("response") == GOOD_TOKEN, "action": ""}

    monkeypatch.setattr("app.utils.captcha._post_verify", fake_verify)
    app.captcha_calls = calls
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _user(email, role="SME", password="password123"):
    user = User(name="Test User", email=email, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def test_the_pages_carry_the_site_key_but_never_the_secret(app):
    page = app.test_client().get("/login").get_data(as_text=True)
    assert "1x00000000000000000000BB" in page and 'data-captcha="login"' in page
    assert "1x0000000000000000000000000000000AA" not in page
    assert "js/captcha.js" in page


def test_sign_in_needs_a_captcha_token(app):
    _user("a@captcha.test")
    client = app.test_client()
    page = client.post("/login", data={"email": "a@captcha.test", "password": "password123"}).get_data(as_text=True)
    assert "human verification" in page
    response = client.post("/login", data={"email": "a@captcha.test", "password": "password123",
                                           "captcha_token": GOOD_TOKEN})
    assert response.status_code == 302
    url, sent = app.captcha_calls[-1]
    assert "challenges.cloudflare.com/turnstile/v0/siteverify" in url
    assert sent["secret"] == "1x0000000000000000000000000000000AA"


def test_a_bad_token_is_refused(app):
    _user("b@captcha.test")
    page = app.test_client().post("/login", data={"email": "b@captcha.test", "password": "password123",
                                                  "captcha_token": "forged"}).get_data(as_text=True)
    assert "human verification" in page


def test_sign_up_needs_a_token_and_the_terms(app):
    client = app.test_client()
    data = {"role": "lgu", **LGU_REQUEST, **account_fields("new@captcha.test")}
    client.post("/register", data=data)
    assert User.query.filter_by(email="new@captcha.test").first() is None, "no token, no account"
    no_terms = {k: v for k, v in data.items() if k != "accept_terms"}
    page = client.post("/register", data={**no_terms, "captcha_token": GOOD_TOKEN}).get_data(as_text=True)
    assert "accept the Terms and Conditions and the Privacy Policy" in page
    client.post("/register", data={**data, "captcha_token": GOOD_TOKEN})
    assert User.query.filter_by(email="new@captcha.test").first() is not None


def test_forgot_password_needs_a_token(app):
    page = app.test_client().post("/forgot-password", data={"email": "x@captcha.test"}).get_data(as_text=True)
    assert "human verification" in page


def test_repeated_failed_sign_ins_are_throttled(app):
    _user("c@captcha.test")
    client = app.test_client()
    for _ in range(5):
        client.post("/login", data={"email": "c@captcha.test", "password": "wrong", "captcha_token": GOOD_TOKEN})
    response = client.post("/login", data={"email": "c@captcha.test", "password": "password123",
                                           "captcha_token": GOOD_TOKEN})
    assert response.status_code == 429
    assert "Too many sign-in attempts" in response.get_data(as_text=True)
    other = client.post("/login", data={"email": "d@captcha.test", "password": "wrong", "captcha_token": GOOD_TOKEN})
    assert other.status_code == 200, "another e-mail is not locked out"


def _signed_in_sme(app):
    _user("sme@captcha.test")
    client = app.test_client()
    client.post("/login", data={"email": "sme@captcha.test", "password": "password123", "captcha_token": GOOD_TOKEN})
    return client


def test_the_ai_routes_ask_for_a_human_pass_then_remember_it(app):
    client = _signed_in_sme(app)
    url = f"/api/forecast?industry_type={BUSINESS_TYPES[0]}&location={BARANGAY_NAMES[0]}"
    refused = client.get(url)
    assert refused.status_code == 403
    assert refused.headers["X-Captcha-Required"] == "1"
    assert refused.get_json()["captcha_required"] is True

    assert client.post("/api/captcha/verify", json={"token": "forged", "action": "analysis"}).status_code == 400
    assert client.post("/api/captcha/verify", json={"token": GOOD_TOKEN, "action": "analysis"}).get_json()["ok"]
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 200, "the pass lasts; no second challenge"


def test_the_ai_routes_are_rate_limited(app):
    from app.utils import rate_limit

    client = _signed_in_sme(app)
    client.post("/api/captcha/verify", json={"token": GOOD_TOKEN, "action": "analysis"})
    original = rate_limit.LIMITS["places_nearby"]
    rate_limit.LIMITS["places_nearby"] = (2, 600)
    try:
        url = f"/api/places/nearby?industry_type={BUSINESS_TYPES[0]}&location={BARANGAY_NAMES[0]}"
        assert client.get(url).status_code == 200
        assert client.get(url).status_code == 200
        limited = client.get(url)
        assert limited.status_code == 429 and int(limited.headers["Retry-After"]) > 0
    finally:
        rate_limit.LIMITS["places_nearby"] = original


def test_creating_a_plan_needs_verification(app):
    client = _signed_in_sme(app)
    response = client.post("/home/analyze", data={"business_name": "Kape", "industry_type": BUSINESS_TYPES[0],
                                                  "location": BARANGAY_NAMES[0]}, follow_redirects=True)
    assert "human verification" in response.get_data(as_text=True)


def test_verification_is_off_without_keys():
    app = create_app("testing")
    with app.app_context():
        from app.utils import captcha

        assert captcha.enabled() is False
        assert captcha.is_human("login", fresh=True) is True
        assert captcha.client_config() == {"enabled": False}


def test_recaptcha_v3_scores_are_checked(app, monkeypatch):
    from app.utils import captcha

    app.config.update(CAPTCHA_PROVIDER="recaptcha", CAPTCHA_MIN_SCORE=0.5)
    monkeypatch.setattr(captcha, "_post_verify",
                        lambda url, data: {"success": True, "score": 0.2, "action": "login"})
    with app.test_request_context("/login", method="POST", data={"captcha_token": "t"}):
        assert captcha.verify_token("t", "login") is False
    monkeypatch.setattr(captcha, "_post_verify",
                        lambda url, data: {"success": True, "score": 0.9, "action": "register"})
    with app.test_request_context("/login", method="POST"):
        assert captcha.verify_token("t", "login") is False, "a token for another action is refused"


def test_the_terms_cover_warranty_acceptable_use_and_google_places(app):
    page = app.test_client().get("/terms").get_data(as_text=True)
    assert "Disclaimer of warranty" in page and "analytical estimates" in page
    assert "not</strong> financial, investment, legal" in page
    assert "Acceptable use" in page
    for banned in ("scrape, crawl or harvest", "reverse-engineer", "spam or flood"):
        assert banned in page, banned
    assert "Google Places API" in page and "maps.google.com/help/terms_maps" in page


def test_the_privacy_policy_explains_credentials_and_location_queries(app):
    page = app.test_client().get("/privacy").get_data(as_text=True)
    assert "Data Privacy Act of 2012" in page and "10173" in page
    assert "Passwords are hashed" in page and "scrypt" in page
    assert "not a JSON Web Token" in page and "HttpOnly" in page
    assert 'id="location"' in page and "never your name, account or location" in page


def test_every_page_links_terms_and_privacy(app):
    client = app.test_client()
    for path in ("/login", "/register", "/terms", "/privacy", "/forgot-password"):
        page = client.get(path).get_data(as_text=True)
        assert 'href="/terms"' in page and 'href="/privacy"' in page, path
    client = _signed_in_sme(app)
    page = client.get("/planning").get_data(as_text=True)
    assert 'class="dss-legal-footer"' in page


def test_the_sign_up_checkbox_names_both_documents(app):
    page = app.test_client().get("/register").get_data(as_text=True)
    assert 'name="accept_terms"' in page
    assert 'data-bs-target="#termsModal"' in page and 'data-bs-target="#privacyModal"' in page
    assert 'id="privacyModal"' in page
