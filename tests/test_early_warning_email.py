"""
tests/test_early_warning_email.py
-----------------------------------
V3.7: an early warning is e-mailed as well as shown in the bell, so SME
and LGU users hear about it with MarketLens closed.

Pinned down here:

  1. A plan's own early warning (forecasting_service) e-mails its owner,
     with a link to that plan on the Planning page -- and only when the
     switch is on, mail is configured and the plan was not e-mailed
     about inside the cooldown.
  2. The market sweep's early warnings carry a link by role: Planning
     for an SME, the LGU Dashboard for LGU staff.
  3. Every alert e-mail has a text and an HTML copy; the HTML escapes
     what accounts typed in.
  4. send_in_background() really returns before the mail is sent, and
     the mail is still sent, inside an app context.
  5. app_link() never points at localhost from a background thread.
  6. The Settings "Send me a test e-mail" button, with its cooldown.
"""

import threading
from datetime import date, datetime, timedelta

import pytest

from app import create_app
from app.extensions import db
from app.models import ForecastResult, MarketData, Notification, SmeProfile, SystemSetting, User
from app.services import email_service
from app.services import market_alert_service as alerts
from tests.test_market_alerts import _market_history, _plan

BASE = "https://marketlens.example"


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def early(monkeypatch):
    """Capture send_early_warning calls instead of sending them."""
    sent = []
    monkeypatch.setattr(email_service, "is_configured", lambda: True)
    monkeypatch.setattr(
        email_service, "send_early_warning",
        lambda to, name, subject, body, link=None: sent.append(
            {"to": to, "name": name, "subject": subject, "body": body, "link": link}) or True)
    return sent


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    from app.services import recommendation_service

    monkeypatch.setattr(recommendation_service, "llm_recommendations_enabled", lambda: False)


def _user(email="sme@example.com", role="SME", **prefs):
    user = User(name="Ana Cruz", email=email, role=role)
    user.set_password("password123")
    for field, value in prefs.items():
        setattr(user, field, value)
    db.session.add(user)
    db.session.commit()
    return user


def _forecast(user, saturation=92.0, location="Poblacion", profile=None):
    from app.services.forecasting_service import _maybe_fire_early_warning, find_or_create_lgu_data

    market = MarketData(
        industry_type="Food and Beverage", location=location, competitor_count=90,
        population_density=8000, historical_success_rate=0.5, foot_traffic_index=50,
        average_rent=20000, source="Google Places API", date_recorded=date.today(),
    )
    db.session.add(market)
    if profile is None:
        profile = SmeProfile(
            user_id=user.user_id, business_name="Kape", industry_type="Food and Beverage",
            location=location, startup_capital=1000, business_stage="startup",
        )
        db.session.add(profile)
    db.session.commit()
    lgu = find_or_create_lgu_data(location)
    forecast = ForecastResult(
        sme_id=profile.sme_id, market_id=market.market_id, lgu_id=lgu.lgu_id,
        input_industry_type="Food and Beverage", input_location=location,
        saturation_index=saturation, viability_score=1.0, forecast_date=date.today(),
    )
    db.session.add(forecast)
    db.session.commit()
    _maybe_fire_early_warning(forecast, profile)
    return forecast, profile


def test_a_plan_early_warning_is_emailed_to_its_owner_with_a_link_to_the_plan(app, early):
    with app.app_context():
        app.config["PUBLIC_BASE_URL"] = BASE
        user = _user()
        forecast, profile = _forecast(user, saturation=92.0)

        assert Notification.query.filter_by(user_id=user.user_id, type="early_warning").count() == 1
        assert len(early) == 1
        mail = early[0]
        assert mail["to"] == "sme@example.com" and mail["name"] == "Ana Cruz"
        assert mail["subject"] == (f"MarketLens early warning: Food and Beverage in Poblacion is "
                                   f"{forecast.saturation_percent}% saturated")
        assert "Early warning: Food and Beverage in Poblacion" in mail["body"]
        assert "75% saturation alert threshold" in mail["body"]
        assert mail["link"] == f"{BASE}/planning?plan={profile.sme_id}"


def test_below_the_threshold_nothing_is_sent(app, early):
    with app.app_context():
        user = _user()
        _forecast(user, saturation=40.0)
        assert Notification.query.filter_by(user_id=user.user_id).count() == 0
        assert early == []


def test_with_the_switch_off_neither_the_alert_nor_the_email_is_sent(app, early):
    with app.app_context():
        user = _user(notify_saturation_change=False)
        _forecast(user)
        assert Notification.query.filter_by(user_id=user.user_id).count() == 0
        assert early == []


def test_without_mail_configured_the_in_app_alert_still_appears(app, monkeypatch):
    with app.app_context():
        app.config.update(BREVO_API_KEY="", GMAIL_ADDRESS="", GMAIL_APP_PASSWORD="")
        attempted = []
        monkeypatch.setattr(email_service, "_send", lambda *a, **k: attempted.append(a) or True)
        user = _user()
        _forecast(user)
        assert Notification.query.filter_by(user_id=user.user_id, type="early_warning").count() == 1
        assert attempted == []


def test_an_archived_account_is_not_emailed(app, early):
    with app.app_context():
        user = _user()
        user.archived_at = datetime.utcnow()
        db.session.commit()
        _forecast(user)
        assert early == []


def test_the_same_plan_is_emailed_once_per_cooldown_but_every_alert_is_kept(app, early):
    with app.app_context():
        user = _user()
        _first, profile = _forecast(user)
        _forecast(user, profile=profile)
        assert Notification.query.filter_by(user_id=user.user_id, type="early_warning").count() == 2
        assert len(early) == 1

        _forecast(user, location="San Roque")
        assert len(early) == 2, "a different plan is a different warning"


def test_the_cooldown_runs_out(app, early):
    with app.app_context():
        user = _user()
        _first, profile = _forecast(user)
        for row in Notification.query.all():
            row.created_at = datetime.utcnow() - timedelta(hours=25)
        db.session.commit()
        _forecast(user, profile=profile)
        assert len(early) == 2


def test_a_zero_cooldown_emails_every_time(app, early):
    with app.app_context():
        app.config["EARLY_WARNING_EMAIL_COOLDOWN_HOURS"] = 0
        user = _user()
        _first, profile = _forecast(user)
        _forecast(user, profile=profile)
        assert len(early) == 2


def test_a_mail_failure_never_breaks_the_forecast(app, monkeypatch):
    with app.app_context():
        monkeypatch.setattr(email_service, "is_configured", lambda: True)

        def _boom(*a, **k):
            raise RuntimeError("mail server down")

        monkeypatch.setattr(email_service, "send_early_warning", _boom)
        user = _user()
        _forecast(user)
        assert Notification.query.filter_by(user_id=user.user_id, type="early_warning").count() == 1


@pytest.fixture
def market_sent(monkeypatch):
    sent = []

    def _capture(to_email, to_name, subject, body, link=None):
        sent.append({"to": to_email, "subject": subject, "link": link})
        return True

    monkeypatch.setattr(email_service, "send_market_alert", _capture)
    monkeypatch.setattr(email_service, "send_recommendation_alert", lambda *a, **k: True)
    return sent


def test_sweep_alerts_link_each_role_to_its_own_page(app, market_sent):
    with app.app_context():
        app.config["PUBLIC_BASE_URL"] = BASE
        sme = _user("owner@example.com", "SME")
        _user("planner@lgu.gov.ph", "LGU")
        _plan(sme, "Food and Beverage", "San Roque")
        _market_history("Food and Beverage", "San Roque", [4, 40])
        db.session.commit()

        result = alerts.run_market_alert_sweep(force=True)

        assert result["emailed"] == 2
        by_to = {row["to"]: row for row in market_sent}
        assert by_to["owner@example.com"]["link"] == f"{BASE}/planning"
        assert by_to["planner@lgu.gov.ph"]["link"] == f"{BASE}/lgu/dashboard"
        assert by_to["owner@example.com"]["subject"].startswith("MarketLens early warning")
        assert by_to["planner@lgu.gov.ph"]["subject"].startswith("MarketLens early warning")


def test_without_a_known_address_the_sweep_sends_no_link_rather_than_localhost(app, market_sent):
    with app.app_context():
        _user("planner@lgu.gov.ph", "LGU")
        _market_history("Food and Beverage", "San Roque", [4, 40])
        db.session.commit()

        alerts.run_market_alert_sweep(force=True)

        assert market_sent and market_sent[0]["link"] is None


def test_an_alert_email_has_a_text_and_an_escaped_html_copy(app, monkeypatch):
    with app.app_context():
        app.config["BREVO_API_KEY"] = "xkeysib-test"
        captured = {}

        def _fake_send(to_email, to_name, subject, body, html=None):
            captured.update(to=to_email, subject=subject, body=body, html=html)
            return True

        monkeypatch.setattr(email_service, "_send", _fake_send)
        link = f"{BASE}/planning?plan=7"
        assert email_service.send_early_warning(
            "a@example.com", "Ana <b>", "MarketLens early warning: test",
            "Early warning: <script>x</script> in Poblacion.", link=link) is True

        assert f"Open your plan: {link}" in captured["body"]
        assert "Early warning alerts" in captured["body"] and "Settings > Notifications" in captured["body"]
        html = captured["html"]
        assert html.startswith("<!doctype html>")
        assert "&lt;script&gt;" in html and "<script>" not in html
        assert "Ana &lt;b&gt;" in html
        assert f'href="{BASE}/planning?plan=7"' in html and "Open your plan" in html
        assert "MarketLens early warning" in html


def test_brevo_receives_the_html_copy(app, monkeypatch):
    with app.app_context():
        app.config.update(BREVO_API_KEY="xkeysib-test", MAIL_FROM_ADDRESS="alerts@example.com")
        posted = {}

        class _Response:
            status_code = 201
            text = ""

        import requests

        monkeypatch.setattr(requests, "post",
                            lambda url, headers=None, json=None, timeout=None: posted.update(json) or _Response())
        assert email_service.send_market_alert("a@example.com", "Ana", "s", "The market moved.",
                                               link=f"{BASE}/planning") is True
        assert "The market moved." in posted["textContent"]
        assert "htmlContent" in posted and f"{BASE}/planning" in posted["htmlContent"]


def test_gmail_sends_text_and_html_as_alternatives(app, monkeypatch):
    with app.app_context():
        app.config.update(BREVO_API_KEY="", GMAIL_ADDRESS="me@gmail.com",
                          GMAIL_APP_PASSWORD="abcdabcdabcdabcd")
        messages = []

        class _SMTP:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self):
                pass

            def login(self, *a):
                pass

            def sendmail(self, sender, to, raw):
                messages.append(raw)

        monkeypatch.setattr(email_service.smtplib, "SMTP", _SMTP)
        assert email_service.send_early_warning("a@example.com", "Ana", "s", "Body text.") is True
        raw = messages[0]
        assert "multipart/alternative" in raw and "text/plain" in raw and "text/html" in raw


def test_send_in_background_returns_first_and_sends_after(app, monkeypatch):
    with app.app_context():
        app.config["EMAIL_IN_BACKGROUND"] = True
        monkeypatch.setattr(email_service, "is_configured", lambda: True)
        release = threading.Event()
        done = []

        def _slow_send(to, name, subject, body, link=None):
            from flask import current_app

            release.wait(5)
            done.append((to, link, current_app.name, threading.current_thread().name))
            return True

        assert email_service.send_in_background(_slow_send, "a@example.com", "Ana", "s", "b",
                                                link="x") is True
        assert done == [], "the caller must not wait for the mail"
        release.set()
        assert email_service.wait_for_background_emails(5)
        assert done and done[0][0] == "a@example.com" and done[0][3] == "email-sender"


def test_send_in_background_queues_nothing_without_a_transport(app, monkeypatch):
    with app.app_context():
        app.config["EMAIL_IN_BACKGROUND"] = True
        monkeypatch.setattr(email_service, "is_configured", lambda: False)
        called = []
        assert email_service.send_in_background(lambda *a, **k: called.append(a)) is False
        assert email_service.wait_for_background_emails(1) and called == []


def test_app_link_uses_the_public_address_and_never_localhost_off_request(app):
    with app.app_context():
        assert email_service.app_link("lgu.dashboard") == ""
        app.config["PUBLIC_BASE_URL"] = BASE + "/"
        assert email_service.app_link("lgu.dashboard") == f"{BASE}/lgu/dashboard"
        assert email_service.app_link("sme.planning", plan=3) == f"{BASE}/planning?plan=3"
        assert email_service.app_link("no.such_endpoint") == ""
    with app.test_request_context("/", base_url="https://other.example"):
        app.config["PUBLIC_BASE_URL"] = ""
        assert email_service.app_link("lgu.dashboard") == "https://other.example/lgu/dashboard"


def _client(app, email="sme@example.com"):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": "password123"})
    return client


def test_the_settings_page_offers_a_test_email_when_mail_is_configured(app, early):
    with app.app_context():
        _user()
        body = _client(app).get("/settings?section=notifications").get_data(as_text=True)
        assert "Early warning alerts" in body
        assert "Send me a test e-mail" in body and "sme@example.com" in body


def test_the_test_email_button_sends_once_per_cooldown(app, early):
    with app.app_context():
        app.config["PUBLIC_BASE_URL"] = BASE
        _user()
        client = _client(app)
        page = client.post("/settings", data={"form_type": "test_alert_email"},
                           follow_redirects=True).get_data(as_text=True)
        assert len(early) == 1 and "Test early-warning e-mail sent to sme@example.com" in page
        assert early[0]["subject"] == "MarketLens early warning: test e-mail"
        assert early[0]["link"] == f"{BASE}/planning"

        page = client.post("/settings", data={"form_type": "test_alert_email"},
                           follow_redirects=True).get_data(as_text=True)
        assert len(early) == 1 and "You can send another in" in page


def test_the_test_email_needs_the_switch_on(app, early):
    with app.app_context():
        _user(notify_saturation_change=False)
        page = _client(app).post("/settings", data={"form_type": "test_alert_email"},
                                 follow_redirects=True).get_data(as_text=True)
        assert early == [] and "save your preferences first" in page


def test_an_lgu_test_email_links_to_the_lgu_dashboard(app, early):
    with app.app_context():
        app.config["PUBLIC_BASE_URL"] = BASE
        _user("planner@lgu.gov.ph", "LGU")
        _client(app, "planner@lgu.gov.ph").post("/settings", data={"form_type": "test_alert_email"})
        assert early and early[0]["link"] == f"{BASE}/lgu/dashboard"
