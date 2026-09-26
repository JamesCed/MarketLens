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
"""

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
    page = client.get("/home").get_data(as_text=True)

    assert "illustrative projection" not in page, (
        "the caption still calls the later quarters illustrative, which is now false"
    )
    assert "separate AI model run" in page
    assert "held at today&#39;s values" in page or "held at today's values" in page
