"""
tests/test_app.py
-------------------
A minimal smoke test suite so you have something to run (and extend)
for the paper's "Functionality Testing" chapter. Uses an in-memory
SQLite database (TestingConfig in app/config.py) so it never touches
your real MySQL data.

Run with:
    pytest
"""

import pytest

from app import create_app
from app.extensions import db
from app.models import User, SystemSetting


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
def client(app):
    return app.test_client()


def test_login_page_loads(client):
    response = client.get("/login")
    assert response.status_code == 200


def test_register_and_login_sme(client, app):
    # SME sign-up now carries the first business plan -- business name,
    # industry and location are required. See tests/test_signup_flow.py.
    response = client.post(
        "/register",
        data={
            "full_name": "Test SME",
            "email": "test.sme@example.com",
            "password": "password123",
            "confirm_password": "password123",
            "role": "sme",
            "business_name": "Test Store",
            "industry_type": "Food and Beverage",
            "location": "Poblacion",
            "business_stage": "startup",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    with app.app_context():
        user = User.query.filter_by(email="test.sme@example.com").first()
        assert user is not None
        assert user.role == "SME"

    response = client.post(
        "/login",
        data={"email": "test.sme@example.com", "password": "password123"},
        follow_redirects=True,
    )
    assert response.status_code == 200


def test_sme_cannot_access_government_upload(client, app):
    with app.app_context():
        user = User(name="Blocked SME", email="blocked@example.com", role="SME")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()

    client.post("/login", data={"email": "blocked@example.com", "password": "password123"})
    response = client.get("/lgu/government-data-upload")
    assert response.status_code == 403


def test_forecasting_service_compute_scores(app):
    with app.app_context():
        from app.services.forecasting_service import compute_scores

        result = compute_scores("Retail", "Test Barangay")
        assert 0.0 <= result["saturation_index"] <= 100.0
        assert 0.0 <= result["viability_score"] <= 10.0
        assert result["cluster_label"] in ("Low", "Moderate", "High", "Saturated")


def test_forecasting_service_generate_forecast_for_profile(app):
    with app.app_context():
        from app.models import SmeProfile
        from app.services.forecasting_service import generate_forecast_for_profile

        user = User(name="Forecast Owner", email="forecastowner@example.com", role="SME")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()

        profile = SmeProfile(
            user_id=user.user_id,
            business_name="Test Cafe",
            industry_type="Food & Beverage",
            location="Test Barangay",
            startup_capital=100000,
        )
        db.session.add(profile)
        db.session.commit()

        forecast = generate_forecast_for_profile(profile)
        assert forecast.forecast_id is not None
        assert forecast.sme_id == profile.sme_id
        assert 0.0 <= float(forecast.saturation_index) <= 100.0
