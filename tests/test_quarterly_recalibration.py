"""
tests/test_quarterly_recalibration.py
----------------------------------------
The Home page's "Current vs. Projected Demand & Viability" chart.

WHAT IT USED TO BE. Q1 was a real score and Q2-Q4 were that score
multiplied by three hardcoded constants -- 1.08, 1.04 and 1.06 per
quarter. The shape lived in the multipliers, not in the data, so the
chart sloped exactly the same way for every barangay, every industry
and every plan. It could not tell an SME anything the Q1 bar had not
already told them, and it had one incoherence built in: saturation and
viability both rose, which cannot happen, because viability IS
(100 - saturation) / 10.

WHAT IT IS NOW. Four separate Random Forest runs. Each quarter's
feature vector is recalibrated -- the competitor count projected
forward from that barangay's own recorded history where it has one,
from the PSA/DTI national establishment series where it does not --
and the model is re-run on it.

The tests below are mostly about the DIFFERENCE between barangays,
because that is the thing a constant multiplier cannot produce and the
thing a real recalibration must: a barangay filling up and a barangay
emptying out have to come back with opposite slopes.

WITH A PLAN (section 3b). "viability = (100 - saturation) / 10" is the
MARKET's viability, and it is still what the chart draws when it is
called without a plan. A plan's stored viability is now the trained
Plan Viability Model's (stage 2), so the Home page passes the plan and
each quarter re-runs that model too, with every plan input held. Q1
must then reproduce the stored plan forecast, and two plans in one
market must share a saturation line but not a viability line.
"""

import json
from datetime import date, timedelta

import pytest

from app import create_app
from app.extensions import db
from app.ml.constants import BUSINESS_TYPES
from app.ml.seed_data import BARANGAY_NAMES
from app.models import LguData, MarketData, SystemSetting
from app.models.user import User

INDUSTRY = BUSINESS_TYPES[0]


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _history(location, counts, spacing_days=80, user_id=None):
    """A market_data history for one combo: `counts` oldest-first."""
    for index, count in enumerate(counts):
        db.session.add(MarketData(
            industry_type=INDUSTRY, location=location, competitor_count=count,
            population_density=2500, historical_success_rate=0.55,
            foot_traffic_index=48, average_rent=19000,
            source="Google Places API",
            date_recorded=date.today() - timedelta(days=(len(counts) - 1 - index) * spacing_days),
        ))
    db.session.add(LguData(
        source="DTI", barangay=location, permit_count=5, business_density=1.3,
        upload_date=date.today(), uploaded_by=user_id,
    ))
    db.session.commit()


@pytest.fixture
def user(app):
    with app.app_context():
        row = User(name="LGU", email="officer@recal.test", role="LGU")
        row.set_password("password123")
        db.session.add(row)
        db.session.commit()
        return row.user_id


def _outlook(location):
    from app.services.trend_analytics_service import project_quarterly_outlook

    return project_quarterly_outlook(50.0, 5.0, location, industry_type=INDUSTRY)


# ---------------------------------------------------------------------
# 1. Every quarter is a real model run
# ---------------------------------------------------------------------

def test_all_four_quarters_are_model_runs(app, user):
    with app.app_context():
        _history(BARANGAY_NAMES[0], [4, 6, 9, 13], user_id=user)
        outlook = _outlook(BARANGAY_NAMES[0])

    assert outlook["basis"] == ["model"] * 4
    assert outlook["recalibrated"] is True
    assert all(value is not None for value in outlook["competitors"])


def test_confidence_falls_with_the_forecast_horizon(app, user):
    """The forest's own confidence describes how much its trees agree
    about ONE vector. It says nothing about whether that vector will
    still describe the market in nine months, so the horizon cost is
    subtracted rather than left for a reader to guess at."""
    with app.app_context():
        _history(BARANGAY_NAMES[0], [4, 6, 9, 13], user_id=user)
        outlook = _outlook(BARANGAY_NAMES[0])

    confidence = outlook["confidence"]
    assert all(c is not None for c in confidence)
    assert confidence == sorted(confidence, reverse=True), confidence
    assert confidence[0] > confidence[-1]


# ---------------------------------------------------------------------
# 2. The slope comes from the barangay, not from a constant
# ---------------------------------------------------------------------

def test_a_barangay_filling_up_and_one_emptying_out_slope_opposite_ways(app, user):
    """The test the old chart could never have passed. Three hardcoded
    multipliers produce one shape for the whole city; a recalibration
    has to produce the shape each barangay's own history implies."""
    with app.app_context():
        _history(BARANGAY_NAMES[0], [4, 6, 9, 13], user_id=user)     # filling up
        _history(BARANGAY_NAMES[1], [18, 15, 12, 10], user_id=user)  # emptying out

        filling = _outlook(BARANGAY_NAMES[0])
        emptying = _outlook(BARANGAY_NAMES[1])

    assert filling["competitors"][-1] > filling["competitors"][0]
    assert emptying["competitors"][-1] < emptying["competitors"][0]

    assert filling["saturation"][-1] > filling["saturation"][0], (
        "a barangay gaining competitors must not be projected as less saturated"
    )
    assert emptying["saturation"][-1] < emptying["saturation"][0]


def test_a_static_barangay_is_projected_static(app, user):
    """A flat line is a real answer. The old chart could not give one:
    every series rose because the multipliers rose."""
    with app.app_context():
        _history(BARANGAY_NAMES[1], [20, 20, 20, 20], user_id=user)
        outlook = _outlook(BARANGAY_NAMES[1])

    assert len(set(outlook["competitors"])) == 1
    assert len(set(outlook["saturation"])) == 1


def test_viability_moves_opposite_to_saturation(app, user):
    """viability = (100 - saturation) / 10, so they cannot both rise.
    The old chart had them both rising, which was arithmetically
    impossible and went unnoticed because neither came from the
    model."""
    with app.app_context():
        _history(BARANGAY_NAMES[0], [4, 6, 9, 13], user_id=user)
        outlook = _outlook(BARANGAY_NAMES[0])

    saturation, viability = outlook["saturation"], outlook["viability"]
    assert saturation[-1] > saturation[0]
    assert viability[-1] < viability[0], (
        f"saturation rose {saturation[0]}->{saturation[-1]} while viability also rose "
        f"{viability[0]}->{viability[-1]}; they are two views of one number"
    )


def test_demand_does_not_wobble(app, user):
    """The national series carries a small deterministic per-series
    variation so that dozens of back-projected history lines do not sit
    on top of each other. Across four forward points it made demand
    fall in Q2 and rise again in Q3 -- an artefact that reads as a
    finding. The forward path does not pass a series key."""
    with app.app_context():
        # No history: forces the national-series fallback, which is
        # where the wobble came from.
        db.session.add(MarketData(
            industry_type=INDUSTRY, location=BARANGAY_NAMES[3], competitor_count=8,
            population_density=2500, historical_success_rate=0.55,
            foot_traffic_index=48, average_rent=19000,
            source="Google Places API", date_recorded=date.today(),
        ))
        db.session.add(LguData(
            source="DTI", barangay=BARANGAY_NAMES[3], permit_count=5,
            business_density=1.3, upload_date=date.today(), uploaded_by=user,
        ))
        db.session.commit()
        outlook = _outlook(BARANGAY_NAMES[3])

    demand = outlook["demand"]
    assert demand == sorted(demand), f"demand is not monotonic: {demand}"


# ---------------------------------------------------------------------
# 3. Reading a rate from history, carefully
# ---------------------------------------------------------------------

def test_a_short_history_is_not_read_as_a_trend(app, user):
    """Two counts a fortnight apart say almost nothing about a year."""
    from app.services.trend_analytics_service import observed_competitor_growth

    with app.app_context():
        _history(BARANGAY_NAMES[4], [5, 40], spacing_days=10, user_id=user)
        assert observed_competitor_growth(INDUSTRY, BARANGAY_NAMES[4]) is None


def test_a_wild_jump_is_clamped(app, user):
    """A Places lookup that hit the 60-result ceiling, or a permit
    register covering half the city, can move a count for reasons that
    are not a trend. Unclamped and compounded over four quarters, one
    such row becomes a hockey stick."""
    from app.services.trend_analytics_service import (
        MAX_OBSERVED_QUARTERLY_GROWTH, observed_competitor_growth,
    )

    with app.app_context():
        _history(BARANGAY_NAMES[5], [1, 60], spacing_days=100, user_id=user)
        rate = observed_competitor_growth(INDUSTRY, BARANGAY_NAMES[5])

    assert rate == pytest.approx(MAX_OBSERVED_QUARTERLY_GROWTH)


def test_no_history_falls_back_to_the_national_series(app, user):
    with app.app_context():
        db.session.add(MarketData(
            industry_type=INDUSTRY, location=BARANGAY_NAMES[6], competitor_count=8,
            population_density=2500, historical_success_rate=0.55,
            foot_traffic_index=48, average_rent=19000,
            source="Google Places API", date_recorded=date.today(),
        ))
        db.session.add(LguData(
            source="DTI", barangay=BARANGAY_NAMES[6], permit_count=5,
            business_density=1.3, upload_date=date.today(), uploaded_by=user,
        ))
        db.session.commit()
        outlook = _outlook(BARANGAY_NAMES[6])

    assert outlook["assumptions"]["source"] == "national"
    assert "national MSME establishment series" in outlook["assumptions"]["series"]
    assert outlook["basis"] == ["model"] * 4


def test_an_unscored_combo_says_so_instead_of_inventing_a_trend(app):
    """No market row at all. Repeating Q1 is the honest answer; the
    caption reads differently for it, so `basis` has to say which
    happened."""
    with app.app_context():
        outlook = _outlook(BARANGAY_NAMES[7])

    assert outlook["recalibrated"] is False
    assert outlook["basis"] == ["scaled"] * 4
    assert len(set(outlook["saturation"])) == 1
    assert all(value is None for value in outlook["confidence"])


# ---------------------------------------------------------------------
# 3b. With a plan, the viability line is the PLAN's
# ---------------------------------------------------------------------
# A plan's stored viability is now the Plan Viability Model's (stage 2,
# app/services/plan_forecast_service.py), which also weighs the plan's
# capital, staff, price list, offering and idea -- not (100 - saturation)
# / 10. So with sme_profile= the chart re-runs BOTH stages per quarter,
# holding the plan's inputs. The market-only tests above still describe
# the call without a plan, which is unchanged.

FOOD = "Food and Beverage"


def _offline(app, monkeypatch):
    """No LLM and no live Places call for the plan-forecast tests."""
    monkeypatch.setenv("USE_LLM_RECOMMENDATIONS", "false")
    app.config["PLACES_LIVE_FETCH"] = False
    app.config["GOOGLE_PLACES_API_KEY"] = ""


def _food_history(location, counts, user_id, spacing_days=80):
    for index, count in enumerate(counts):
        db.session.add(MarketData(
            industry_type=FOOD, location=location, competitor_count=count,
            population_density=2500, historical_success_rate=0.62,
            foot_traffic_index=48, average_rent=19000, source="Google Places API",
            date_recorded=date.today() - timedelta(days=(len(counts) - 1 - index) * spacing_days),
        ))
    db.session.add(LguData(
        source="DTI", barangay=location, permit_count=5, business_density=2.6,
        upload_date=date.today(), uploaded_by=user_id,
    ))
    db.session.commit()


def _sme_plan(**fields):
    from app.models import SmeProfile

    owner = User(name="Plan owner", email=f"owner{User.query.count()}@recal.test", role="SME")
    owner.set_password("password123")
    db.session.add(owner)
    db.session.commit()
    base = dict(user_id=owner.user_id, business_name="Pan", industry_type=FOOD,
                location=BARANGAY_NAMES[0], business_stage="startup", startup_capital=400000,
                employee_count=2, product_offering="Bread")
    base.update(fields)
    profile = SmeProfile(**base)
    db.session.add(profile)
    db.session.commit()
    return profile


def _plan_outlook(forecast, profile):
    from app.services.trend_analytics_service import project_quarterly_outlook

    return project_quarterly_outlook(
        forecast.saturation_index, forecast.viability_score, forecast.input_location,
        industry_type=forecast.input_industry_type, sme_profile=profile,
    )


def test_q1_of_a_plan_outlook_reproduces_the_stored_plan_forecast(app, user, monkeypatch):
    from app.services.forecasting_service import generate_forecast_for_profile

    _offline(app, monkeypatch)
    with app.app_context():
        _food_history(BARANGAY_NAMES[0], [6, 8, 11, 14], user_id=user)
        profile = _sme_plan(innovation_idea="Ube pandesal")
        forecast = generate_forecast_for_profile(profile)
        outlook = _plan_outlook(forecast, profile)

    assert outlook["viability_model"] == "plan"
    assert outlook["basis"] == ["model"] * 4
    # Q1 is today's market with today's plan: the stored forecast.
    assert outlook["viability"][0] == pytest.approx(round(min(60.0, float(forecast.viability_score) * 6.0), 1))
    assert outlook["saturation"][0] == pytest.approx(round(min(60.0, float(forecast.saturation_index) * 0.6), 1))
    assert "held as entered" in outlook["assumptions"]["viability"]
    assert outlook["assumptions"]["viability"].startswith("the Plan Viability Model")


def test_q1_still_matches_the_stored_forecast_after_the_admin_changes_the_assumptions(app, user, monkeypatch):
    """A stored forecast keeps the wage and gross margin it was computed
    with until the plan is re-forecast (Admin > System Settings says so).
    The chart drawn beside it must hold the same ones -- it used to pick
    up the new settings at once, so Q1 drifted off the gauge."""
    from app.services import plan_forecast_service as pfs
    from app.services.forecasting_service import generate_forecast_for_profile

    _offline(app, monkeypatch)
    with app.app_context():
        _food_history(BARANGAY_NAMES[0], [6, 8, 11, 14], user_id=user)
        # Payroll-heavy and priced, so both assumptions actually move it.
        # The price matters: at P600 the plan needs fewer sales a day than
        # the barangay can supply at the default 40% margin and more than
        # it can at 15%, so the margin moves price coverage. (It used to
        # be one P40 item, which needed three times the ceiling at ANY
        # margin -- coverage was 0 either way, only the wage moved the
        # scorecard, by 0.6 points, and whether the 1-dp score changed
        # was down to where the forest's steps happened to fall.)
        profile = _sme_plan(startup_capital=150000, employee_count=6,
                            offering_details=json.dumps([{"item": "Celebration cake", "price": 600}]))
        forecast = generate_forecast_for_profile(profile)
        before = _plan_outlook(forecast, profile)

        SystemSetting.set(pfs.WAGE_SETTING_KEY, "900")
        SystemSetting.set(pfs.MARGIN_SETTING_KEY, "0.15")
        after = _plan_outlook(forecast, profile)

        stored = float(forecast.viability_score)
        stored_payload = json.loads(forecast.recommendation)["forecast"]
        # The new settings DO change a forecast made now -- so the test
        # would catch the chart reading them.
        fresh = json.loads(generate_forecast_for_profile(profile).recommendation)["forecast"]

    stored_bar = round(min(60.0, stored * 6.0), 1)
    assert before["viability"][0] == pytest.approx(stored_bar)
    assert after["viability"][0] == pytest.approx(stored_bar)
    assert after["viability"] == before["viability"]
    assert fresh["financials"]["daily_wage"] == 900.0 and fresh["financials"]["gross_margin"] == 0.15
    assert fresh["plan"]["scorecard_index"] < stored_payload["plan"]["scorecard_index"] - 3.0
    assert fresh["plan"]["viability_index"] < stored_payload["plan"]["viability_index"]


def test_q1_matches_the_stored_forecast_with_a_sub_category_adjustment_too(app, user, monkeypatch):
    from app.models import SubcategoryMarketData
    from app.services.forecasting_service import generate_forecast_for_profile

    _offline(app, monkeypatch)
    with app.app_context():
        _food_history(BARANGAY_NAMES[2], [20, 24, 28, 32], user_id=user)
        # Far fewer bakeries than the industry count implies -> the
        # direct-competition ratio pulls the plan's competition down.
        db.session.add(SubcategoryMarketData(
            industry_type=FOOD, subcategory="bakery", location=BARANGAY_NAMES[2],
            competitor_count=1, source="Google Places API", date_recorded=date.today(),
        ))
        db.session.commit()
        profile = _sme_plan(location=BARANGAY_NAMES[2], subcategory="bakery")
        forecast = generate_forecast_for_profile(profile)
        stored = json.loads(forecast.recommendation)
        outlook = _plan_outlook(forecast, profile)

    assert stored["subcategory_analysis"]["adjusts_score"] is True
    assert outlook["saturation"][0] == pytest.approx(round(min(60.0, float(forecast.saturation_index) * 0.6), 1))
    assert outlook["viability"][0] == pytest.approx(round(min(60.0, float(forecast.viability_score) * 6.0), 1))


def test_the_plan_outlook_reflects_the_plan_not_only_the_market(app, user, monkeypatch):
    """Two plans, one market: the saturation line is the same, the
    viability line is not -- because one of them can afford its
    ramp-up and the other cannot."""
    from app.services.forecasting_service import generate_forecast_for_profile

    _offline(app, monkeypatch)
    with app.app_context():
        _food_history(BARANGAY_NAMES[0], [6, 8, 11, 14], user_id=user)
        thin = _sme_plan(startup_capital=20000, employee_count=5, product_offering=None)
        solid = _sme_plan(startup_capital=3000000, employee_count=3, innovation_idea="Delivery")
        thin_outlook = _plan_outlook(generate_forecast_for_profile(thin), thin)
        solid_outlook = _plan_outlook(generate_forecast_for_profile(solid), solid)

    assert thin_outlook["saturation"] == solid_outlook["saturation"]
    assert all(s > t for s, t in zip(solid_outlook["viability"], thin_outlook["viability"]))


def test_without_a_plan_the_outlook_is_the_market_formula(app, user):
    with app.app_context():
        _history(BARANGAY_NAMES[0], [4, 6, 9, 13], user_id=user)
        outlook = _outlook(BARANGAY_NAMES[0])

    assert outlook["viability_model"] == "market"
    assert outlook["assumptions"]["viability"].startswith("(100 - saturation) / 10")


# ---------------------------------------------------------------------
# 4. The page says which of those happened
# ---------------------------------------------------------------------

def test_the_caption_no_longer_calls_it_illustrative(app, user):
    with app.app_context():
        sme = User(name="Juan", email="sme@recal.test", role="SME")
        sme.set_password("password123")
        db.session.add(sme)
        db.session.commit()

        from app.models import SmeProfile

        db.session.add(SmeProfile(
            user_id=sme.user_id, business_name="Juan's Coffee",
            industry_type=INDUSTRY, location=BARANGAY_NAMES[0],
            business_stage="startup", startup_capital=250000,
            employee_count=2, monthly_revenue_est=60000,
        ))
        db.session.commit()
        _history(BARANGAY_NAMES[0], [4, 6, 9, 13], user_id=user)

    client = app.test_client()
    client.post("/login", data={"email": "sme@recal.test", "password": "password123"},
                follow_redirects=True)
    page = client.get("/planning").get_data(as_text=True)

    assert "illustrative projection" not in page, (
        "the caption still calls the later quarters illustrative, which is now false"
    )
    # Worded "a separate AI model run" before the Plan Viability Model;
    # with two models per quarter it now says "a separate run of the
    # trained models". Either way it must say every quarter is a run.
    assert "separate run of the trained models" in page or "separate AI model run" in page
    assert "held at today&#39;s values" in page or "held at today's values" in page
