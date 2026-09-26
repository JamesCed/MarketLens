"""
tests/test_trends_and_roi.py
-------------------------------
Covers this round's three changes:

  1. The Saturation Map's hover tooltip no longer carries the "Est.
     Household Demand" block (it stayed on the detail panel).
  2. The Recommendations page: summary cards counting every barangay the
     AI scored CITY-WIDE rather than the SME's own saved plans, the
     AI-derived "ROI Timeframe" that replaced "Est. Break-even", and the
     "Explore All Recommendations" view.
  3. Trend Reports: ONE city-wide dashboard for every role (no more
     per-plan "My Trend Report"), and the "Select Period" control
     actually changing how far back the charts look.
"""

import os
from datetime import date

import pytest

from app import create_app
from app.extensions import db
from app.models import User, SmeProfile, MarketData, SystemSetting, PlanSave, ForecastResult
from app.services.location_opportunity_service import (
    estimate_roi_timeframe,
    rank_location_opportunities,
    ASSUMED_NET_MARGIN,
    MINIMUM_RAMP_MONTHS,
    RECOMMENDABLE_TIERS,
    DEFAULT_LIMIT,
)
from app.services import trend_analytics_service as trends


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _user(email, role):
    user = User(name=role, email=email, role=role)
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def _client_for(app, email):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": "password123"})
    return client


def _url(app, endpoint, **kwargs):
    from flask import url_for
    with app.test_request_context():
        return url_for(endpoint, **kwargs)


# ---------------------------------------------------------------------------
# 1. Saturation map tooltip
# ---------------------------------------------------------------------------


def _map_js():
    path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "js", "map.js")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_hover_tooltip_no_longer_carries_the_demand_block():
    """The FIES demand block made the hover tooltip tall enough to cover
    a good part of the map. It was removed from the tooltip only."""
    contents = _map_js()
    assert "demandTooltipHtml" not in contents


def test_detail_panel_still_shows_the_demand_figures():
    """Removing it from the tooltip must not take it off the detail
    panel, where it is a tidy labelled row rather than an overlay."""
    assert "demandDetailHtml" in _map_js()


# ---------------------------------------------------------------------------
# 2. ROI timeframe
# ---------------------------------------------------------------------------


def test_roi_uses_margin_not_raw_revenue():
    """Capital / monthly REVENUE treats every peso of sales as profit,
    which put a PHP 20,000 plan at "1 month". Recovery runs on margin."""
    roi = estimate_roi_timeframe(
        viability_score=8.0, saturation_index=50, residents_per_business=None,
        city_median_depth=None, startup_capital=20000, monthly_revenue_est=25000,
    )
    naive_months = 20000 / 25000  # < 1 month, the old behaviour
    assert roi["low_months"] > naive_months * 2
    # With a 15% margin the midpoint is ~5.3 months, so the window
    # should straddle that rather than sitting at 1.
    expected_mid = 20000 / (25000 * ASSUMED_NET_MARGIN)
    assert roi["low_months"] <= expected_mid <= roi["high_months"]


def test_roi_is_slower_in_a_saturated_market():
    """The whole point of calling it AI-derived: the model's saturation
    prediction has to actually move the number."""
    common = dict(viability_score=5.0, residents_per_business=None, city_median_depth=None,
                  startup_capital=500000, monthly_revenue_est=100000)
    uncontested = estimate_roi_timeframe(saturation_index=15, **common)
    saturated = estimate_roi_timeframe(saturation_index=90, **common)
    assert saturated["low_months"] > uncontested["low_months"]


def test_roi_falls_back_to_the_model_when_no_revenue_is_on_file():
    roi = estimate_roi_timeframe(
        viability_score=9.0, saturation_index=20, residents_per_business=None,
        city_median_depth=None, startup_capital=0, monthly_revenue_est=0,
    )
    assert roi["basis"] == "model"
    assert roi["low_months"] >= 1
    # A strong market should still come back faster than a weak one.
    weak = estimate_roi_timeframe(1.0, 80, None, None, 0, 0)
    assert roi["high_months"] < weak["high_months"]


def test_roi_always_returns_a_range_not_a_point():
    roi = estimate_roi_timeframe(6.0, 40, 500, 300, 300000, 60000)
    assert roi["high_months"] > roi["low_months"]
    assert roi["label"] == f"{roi['low_months']}-{roi['high_months']} months"


# ---------------------------------------------------------------------------
# 3. Recommendations page
# ---------------------------------------------------------------------------


@pytest.fixture
def sme_with_plan(app):
    with app.app_context():
        user = _user("sme@example.com", "SME")
        db.session.add(SmeProfile(
            user_id=user.user_id, business_name="Kapepe", industry_type="Food and Beverage",
            location="Santo Domingo", startup_capital=20000, monthly_revenue_est=25000,
            business_stage="startup",
        ))
        db.session.commit()
        yield user


def test_summary_counts_cover_every_barangay_scored_not_the_saved_plans(app):
    """The cards used to count the SME's own plans, which is why they
    read "1 / 1 / 2" no matter how much of the city the AI had scored."""
    with app.app_context():
        locations = ["Barangay %02d" % i for i in range(1, 41)]
        result = rank_location_opportunities("Food and Beverage", locations, use_llm=False)

        assert result["total_scored"] == len(locations)
        assert result["showing_count"] <= result["total_scored"]
        tier_total = (
            result["high_opportunity_count"] + result["moderate_opportunity_count"]
            + result["low_opportunity_count"] + result["saturated_count"]
        )
        assert tier_total == len(locations), "every scored barangay lands in exactly one tier"
        # The counts must describe the whole city, not just the page.
        assert result["high_opportunity_count"] >= len(
            [o for o in result["opportunities"] if o["opportunity_type"] == "High Opportunity"]
        )


def test_every_card_carries_an_roi_timeframe(app):
    with app.app_context():
        result = rank_location_opportunities(
            "Food and Beverage", ["Poblacion", "Tibag", "Aguso"], use_llm=False
        )
        for opp in result["opportunities"]:
            assert opp["roi_timeframe"].endswith("months")
            assert opp["roi_basis"] in ("plan", "model")


def test_explore_more_returns_every_recommended_location(app):
    """"Explore more" lifts the display cap -- but only over locations
    the AI actually recommends. A saturated barangay is never added."""
    with app.app_context():
        locations = ["Barangay %02d" % i for i in range(1, 41)]
        capped = rank_location_opportunities("Food and Beverage", locations, use_llm=False)
        every = rank_location_opportunities("Food and Beverage", locations, limit=None, use_llm=False)

        assert len(capped["opportunities"]) == DEFAULT_LIMIT
        assert len(every["opportunities"]) == every["recommended_count"]
        assert every["recommended_count"] <= every["total_scored"]


def test_only_enterable_tiers_are_ever_recommended(app):
    """The page is headed "Recommendations" -- offering a market the
    same model just called saturated would be advice that contradicts
    itself."""
    with app.app_context():
        locations = ["Barangay %02d" % i for i in range(1, 41)]
        result = rank_location_opportunities(
            "Food and Beverage", locations, limit=None, use_llm=False
        )
        offered = {o["opportunity_type"] for o in result["opportunities"]}
        assert offered <= set(RECOMMENDABLE_TIERS)
        # And the count matches the two recommendable tiers exactly.
        assert result["recommended_count"] == (
            result["high_opportunity_count"] + result["moderate_opportunity_count"]
        )


def test_recommendations_page_shows_roi_and_explore_all(app, sme_with_plan):
    with app.app_context():
        client = _client_for(app, "sme@example.com")
        url = _url(app, "sme.recommendations")

        body = client.get(url).get_data(as_text=True)
        assert "ROI Timeframe" in body
        assert "Est. Break-even" not in body, "the old label should be gone everywhere"
        # No explanatory caption under the ROI value any more.
        assert "assumed net margin, adjusted" not in body
        assert "Explore More Recommendations" in body
        assert "AI-Recommended Locations" in body
        # At least five cards, per the page's own guarantee.
        assert body.count("Ranked on") >= 5

        expanded = client.get(url + "?all=1").get_data(as_text=True)
        assert "Showing all" in expanded
        assert expanded.count("Ranked on") > body.count("Ranked on")


def test_an_ai_recommendation_can_be_saved_as_a_real_plan(app, sme_with_plan):
    """Saving a recommendation CREATES the plan it describes -- there is
    no forecast_result behind an ephemeral card to bookmark."""
    with app.app_context():
        client = _client_for(app, "sme@example.com")
        save_url = _url(app, "sme.save_recommended_location")
        before = SmeProfile.query.count()

        client.post(save_url, data={
            "industry_type": "Food and Beverage", "location": "Matatalaib",
        }, follow_redirects=True)

        assert SmeProfile.query.count() == before + 1
        created = SmeProfile.query.filter_by(location="Matatalaib").one()
        assert created.industry_type == "Food and Beverage"
        # Parameters carry over from the plan it was scored against.
        assert float(created.startup_capital or 0) == 20000
        # And it is forecast + bookmarked, so it behaves like any plan.
        assert ForecastResult.query.filter_by(sme_id=created.sme_id).count() >= 1
        assert PlanSave.query.count() >= 1


def test_saving_the_same_recommendation_twice_does_not_duplicate(app, sme_with_plan):
    with app.app_context():
        client = _client_for(app, "sme@example.com")
        save_url = _url(app, "sme.save_recommended_location")
        for _ in range(2):
            client.post(save_url, data={
                "industry_type": "Food and Beverage", "location": "Matatalaib",
            }, follow_redirects=True)
        assert SmeProfile.query.filter_by(location="Matatalaib").count() == 1


def test_saving_rejects_a_location_the_system_does_not_know(app, sme_with_plan):
    """This endpoint creates a real plan, so it validates rather than
    trusting whatever was posted."""
    with app.app_context():
        client = _client_for(app, "sme@example.com")
        before = SmeProfile.query.count()
        client.post(_url(app, "sme.save_recommended_location"), data={
            "industry_type": "Food and Beverage", "location": "${p.location}",
        }, follow_redirects=True)
        assert SmeProfile.query.count() == before


# ---------------------------------------------------------------------------
# 4. Trend Reports -- one dashboard, and the period control
# ---------------------------------------------------------------------------


def test_sme_and_lgu_get_the_same_city_wide_dashboard(app):
    """The per-plan "My Trend Report" is gone: a chart built from one
    user's saved plans is a restatement of their own inputs, not a
    market trend."""
    with app.app_context():
        _user("sme@example.com", "SME")
        _user("lgu@example.com", "LGU")
        url = _url(app, "sme.trend_reports")

        for email in ("sme@example.com", "lgu@example.com"):
            body = _client_for(app, email).get(url).get_data(as_text=True)
            assert "Trend Reports &amp; Analytics" in body or "Trend Reports & Analytics" in body
            assert "My Trend Report" not in body
            assert "Select Period" in body


def test_personal_trend_endpoint_is_gone(app):
    with app.app_context():
        _user("sme@example.com", "SME")
        assert _client_for(app, "sme@example.com").get("/api/trend-data/personal").status_code == 404


def test_the_month_picker_moves_the_whole_window(app):
    """Picking a month reviews the market AS OF that month -- the
    series end there instead of at today."""
    with app.app_context():
        _user("lgu@example.com", "LGU")
        client = _client_for(app, "lgu@example.com")
        api = _url(app, "api.trend_data")

        march = client.get(f"{api}?as_of=2026-03").get_json()
        august = client.get(f"{api}?as_of=2026-08").get_json()

        assert march["period"]["as_of"] == "2026-03"
        assert august["period"]["as_of"] == "2026-08"
        # Labels carry the YEAR now -- the picker reaches back to 2020,
        # so a bare "Mar" would not say WHICH March.
        assert march["monthly_trends"]["labels"][-1] == "Mar 2026"
        assert august["monthly_trends"]["labels"][-1] == "Aug 2026"
        assert march["market_quarterly"]["labels"] != august["market_quarterly"]["labels"]


def test_the_picker_refuses_months_that_would_be_pure_projection(app):
    """A future month has no history at all, and a month before this
    system existed has none either."""
    with app.app_context():
        this_month = date.today().strftime("%Y-%m")
        assert trends.resolve_as_of_month("2099-01").strftime("%Y-%m") == this_month
        assert trends.resolve_as_of_month("not-a-month").strftime("%Y-%m") == this_month
        assert trends.resolve_as_of_month("").strftime("%Y-%m") == this_month
        assert trends.resolve_as_of_month("1999-05") == trends.EARLIEST_TREND_MONTH


def test_kpi_cards_report_a_real_month_over_month_change(app):
    """The four Figure 4 indicators, as of a chosen month. The delta has
    to come from two dated snapshots, not from today's number."""
    with app.app_context():
        for recorded, count in [(date(2026, 7, 15), 100), (date(2026, 8, 15), 130)]:
            db.session.add(MarketData(
                industry_type="Food and Beverage", location="Poblacion", competitor_count=count,
                population_density=1000, historical_success_rate=0.5, foot_traffic_index=50,
                average_rent=20000, source="Google Places API", date_recorded=recorded,
            ))
        db.session.commit()

        overview = trends.get_period_overview(date(2026, 8, 1))
        card = overview["cards"]["total_businesses"]

        assert overview["as_of_label"] == "August 2026"
        assert overview["previous_label"] == "July 2026"
        assert card["value"] == 130
        assert card["delta_percent"] == 30.0  # 100 -> 130
        assert card["measured"] is True


def test_a_month_with_no_data_says_so_instead_of_showing_zero_change(app):
    """A fabricated "+0.0% from last month" reads like a measurement.
    An empty month must report no comparison instead.

    Months from before this app existed are now back-projected from the
    published national establishment series, so most of them DO get a
    comparison (see tests/test_historical_trends.py). This is the case
    that must not: a database with nothing measured anywhere has nothing
    to scale, so every card is honestly blank.
    """
    with app.app_context():
        overview = trends.get_period_overview(date(2026, 5, 1))
        for name in ("total_businesses", "new_startups"):
            card = overview["cards"][name]
            delta = card.get("delta_percent", card.get("delta_points"))
            assert delta is None, f"{name} invented a comparison out of an empty database"
            assert card["projected"] is False
        assert overview["cards"]["market_saturation"]["measured"] is False


def test_trend_page_uses_a_real_month_picker_not_a_dropdown(app):
    """Month and year only, no day grid -- which is what
    <input type="month"> gives, and what "as of this month" means."""
    with app.app_context():
        _user("lgu@example.com", "LGU")
        body = _client_for(app, "lgu@example.com").get(_url(app, "sme.trend_reports")).get_data(as_text=True)
        assert 'type="month"' in body
        assert 'id="trendMonth"' in body
        assert 'id="trendPeriod"' not in body, "the old dropdown should be gone"
        # Bounded so nobody can ask for a month that hasn't happened.
        assert 'max="' in body and 'min="' in body


def test_a_longer_window_does_not_invent_history(app):
    """Widening the window must add BACK-PROJECTED quarters, clearly
    flagged -- not extra quarters presented as measured."""
    with app.app_context():
        db.session.add(MarketData(
            industry_type="Food and Beverage", location="Poblacion", competitor_count=12,
            population_density=1000, historical_success_rate=0.5, foot_traffic_index=50,
            average_rent=20000, source="Google Places API", date_recorded=date.today(),
        ))
        db.session.commit()

        short = trends.get_market_quarterly_performance("Food and Beverage", quarters=5)
        long = trends.get_market_quarterly_performance("Food and Beverage", quarters=13)

        assert len(long["labels"]) > len(short["labels"])
        # The extra depth is projection, not newly-discovered measurement.
        assert long["measured_quarters"] == short["measured_quarters"]
        assert sum(long["projected"]) > sum(short["projected"])


def test_quarterly_endpoint_honours_the_chosen_month(app):
    with app.app_context():
        _user("lgu@example.com", "LGU")
        client = _client_for(app, "lgu@example.com")
        url = _url(app, "api.quarterly_performance")

        march = client.get(f"{url}?as_of=2026-03").get_json()
        august = client.get(f"{url}?as_of=2026-08").get_json()

        assert march["labels"] != august["labels"]
        assert march["labels"][-1] == "Q1 2026"
        assert august["labels"][-1] == "Q3 2026"
        # Both series the combined Figure 4.1 chart draws.
        for payload in (march, august):
            assert "businesses" in payload and "growth_rate_percent" in payload
