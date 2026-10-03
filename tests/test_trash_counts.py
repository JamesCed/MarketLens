"""
tests/test_trash_counts.py
-----------------------------
A plan in Trash must not be counted.

Moving a plan to Trash archives the sme_profile row and keeps every
forecast it produced, so Restore can bring it all back. forecast_result
carries no archive stamp of its own, so the global archive filter
(app/models/archive.py) only reaches a trashed plan's forecasts where
SmeProfile is part of the query. Three places counted forecast_result
on its own and so went on counting a plan its owner had removed:

  * the LGU dashboard's "forecasts generated", saturated-zone and
    opportunity-zone tallies (lgu_controller.dashboard);
  * the trend pages' real-forecast series and month averages
    (trend_analytics_service._real_forecasts,
    _forecast_averages_in_month);
  * the quarterly market chart's saturation average
    (trend_analytics_service.get_market_quarterly_performance).

All three now go through trend_analytics_service.live_plan_forecasts(),
which joins SmeProfile. The tests check each figure three ways: with
nothing in Trash it is exactly what counting every forecast gives (the
join drops no row), with a plan in Trash that plan's forecasts are gone,
and after Restore they are back.
"""

from datetime import date

import pytest

from app import create_app
from app.extensions import db
from app.models import ForecastResult, LguData, MarketData, SmeProfile, SystemSetting, User
from app.services import trend_analytics_service as tas

FOOD = "Food and Beverage"
RETAIL = "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"


@pytest.fixture
def app():
    app = create_app("testing")
    app.config["PLACES_LIVE_FETCH"] = False
    app.config["GOOGLE_PLACES_API_KEY"] = ""
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _user(email, role="SME"):
    user = User(name=email.split("@")[0], email=email, role=role)
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def _forecast(profile, market, lgu, saturation, industry=None):
    row = ForecastResult(
        sme_id=profile.sme_id, market_id=market.market_id, lgu_id=lgu.lgu_id,
        viability_score=round((100 - saturation) / 10, 2), saturation_index=saturation,
        input_industry_type=industry or profile.industry_type, input_location=profile.location,
        confidence_level=80, model_version="rf_v1+plan_rf_v1", forecast_date=date.today(),
    )
    db.session.add(row)
    return row


@pytest.fixture
def plans(app):
    """Two owners' plans with forecasts dated today:

        kept     (Food)   saturation 30 and 20  -- one opportunity zone (<= 25)
        trashed  (Food)   saturation 90         -- a saturated zone (> 75)
        retail   (Retail) saturation 60

    'trashed' is the one the tests move to Trash."""
    owner = _user("owner@counts.test")
    other = _user("other@counts.test")
    market = MarketData(industry_type=FOOD, location="Tibag", competitor_count=8,
                        population_density=2300.0, historical_success_rate=0.7, foot_traffic_index=40,
                        average_rent=18000, source="Manual", date_recorded=date.today())
    lgu = LguData(source="DTI", barangay="Tibag", permit_count=4, business_density=2.0,
                  upload_date=date.today(), uploaded_by=owner.user_id)
    db.session.add_all([market, lgu])
    db.session.commit()

    kept = SmeProfile(user_id=owner.user_id, business_name="Kept", industry_type=FOOD, location="Tibag",
                      startup_capital=300000, business_stage="startup")
    trashed = SmeProfile(user_id=owner.user_id, business_name="Binned", industry_type=FOOD,
                         location="Tibag", startup_capital=300000, business_stage="startup")
    retail = SmeProfile(user_id=other.user_id, business_name="Shop", industry_type=RETAIL,
                        location="Tibag", startup_capital=300000, business_stage="startup")
    db.session.add_all([kept, trashed, retail])
    db.session.commit()

    _forecast(kept, market, lgu, 30.0)
    _forecast(kept, market, lgu, 20.0)
    _forecast(trashed, market, lgu, 90.0)
    _forecast(retail, market, lgu, 60.0)
    db.session.commit()
    return {"owner": owner, "kept": kept.sme_id, "trashed": trashed.sme_id, "retail": retail.sme_id}


def _trash(sme_id, by_user):
    profile = db.session.get(SmeProfile, sme_id)
    profile.archive(by_user.user_id, "moved to Trash")
    db.session.commit()
    db.session.expire_all()


def _restore(sme_id):
    from app.models.archive import get_including_archived

    get_including_archived(SmeProfile, sme_id).restore()
    db.session.commit()
    db.session.expire_all()


# ---------------------------------------------------------------------
# The shared query
# ---------------------------------------------------------------------

def test_live_plan_forecasts_is_every_forecast_until_a_plan_is_trashed(app, plans):
    assert tas.live_plan_forecasts().count() == ForecastResult.query.count() == 4

    _trash(plans["trashed"], plans["owner"])
    live = tas.live_plan_forecasts().all()
    assert len(live) == 3
    assert plans["trashed"] not in {f.sme_id for f in live}
    # The rows themselves are kept -- that is what Restore relies on.
    assert ForecastResult.query.count() == 4

    _restore(plans["trashed"])
    assert tas.live_plan_forecasts().count() == 4


# ---------------------------------------------------------------------
# LGU dashboard tallies
# ---------------------------------------------------------------------

def _dashboard_tallies(app, monkeypatch):
    """The dashboard's three forecast tallies, read from the template
    context. Scoring is stubbed: the industry cards are not under test
    here and need no model."""
    from app.controllers import lgu_controller
    from app.services import forecasting_service

    monkeypatch.setattr(forecasting_service, "compute_scores_batch",
                        lambda pairs: [{"viability_score": 5.0, "saturation_index": 50.0} for _ in pairs])
    captured = {}

    def capture(template, **context):
        captured.update(context)
        return ""

    monkeypatch.setattr(lgu_controller, "render_template", capture)
    _user("lgu@counts.test", role="LGU")
    client = app.test_client()
    client.post("/login", data={"email": "lgu@counts.test", "password": "password123"})
    assert client.get("/lgu/dashboard").status_code == 200
    return captured["total_forecasts"], captured["saturated_count"], captured["opportunity_count"]


def test_the_lgu_dashboard_tallies_are_unchanged_with_nothing_in_trash(app, plans, monkeypatch):
    every = ForecastResult.query
    expected = (every.count(),
                every.filter(ForecastResult.saturation_index > 75).count(),
                every.filter(ForecastResult.saturation_index <= 25).count())
    assert expected == (4, 1, 1)
    assert _dashboard_tallies(app, monkeypatch) == expected


def test_the_lgu_dashboard_does_not_count_a_trashed_plan(app, plans, monkeypatch):
    _trash(plans["trashed"], plans["owner"])
    # The binned plan was the only saturated zone.
    assert _dashboard_tallies(app, monkeypatch) == (3, 0, 1)


def test_a_restored_plan_counts_on_the_lgu_dashboard_again(app, plans, monkeypatch):
    _trash(plans["trashed"], plans["owner"])
    _restore(plans["trashed"])
    assert _dashboard_tallies(app, monkeypatch) == (4, 1, 1)


# ---------------------------------------------------------------------
# Trend pages
# ---------------------------------------------------------------------

def test_the_trend_forecast_series_drops_a_trashed_plan(app, plans):
    assert len(tas._real_forecasts()) == 4
    assert {f.sme_id for f in tas._real_forecasts(FOOD)} == {plans["kept"], plans["trashed"]}

    _trash(plans["trashed"], plans["owner"])
    assert len(tas._real_forecasts()) == 3
    # The industry filter still filters on the FORECAST's industry after
    # the join (filter_by would have read it off SmeProfile).
    assert {f.sme_id for f in tas._real_forecasts(FOOD)} == {plans["kept"]}
    assert {f.sme_id for f in tas._real_forecasts(RETAIL)} == {plans["retail"]}


def test_the_month_averages_drop_a_trashed_plan(app, plans):
    # All four: saturation (30 + 20 + 90 + 60) / 4 = 50.0.
    assert tas._forecast_averages_in_month(date.today()) == (50.0, 5.0, 4)
    # Food only: (30 + 20 + 90) / 3 = 46.7.
    assert tas._forecast_averages_in_month(date.today(), FOOD)[2] == 3

    _trash(plans["trashed"], plans["owner"])
    saturation, viability, n = tas._forecast_averages_in_month(date.today())
    assert (saturation, n) == (round((30 + 20 + 60) / 3, 1), 3)
    assert viability == round(((7.0 + 8.0 + 4.0) / 3), 1)
    assert tas._forecast_averages_in_month(date.today(), FOOD) == (25.0, 7.5, 2)


def test_the_quarterly_saturation_average_drops_a_trashed_plan(app, plans):
    before = tas.get_market_quarterly_performance(FOOD)
    assert before["saturation_percent"][-1] == round((30 + 20 + 90) / 3, 1)

    _trash(plans["trashed"], plans["owner"])
    after = tas.get_market_quarterly_performance(FOOD)
    assert after["saturation_percent"][-1] == 25.0
    assert after["labels"] == before["labels"]
