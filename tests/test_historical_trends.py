"""
tests/test_historical_trends.py
----------------------------------
Covers the round that stopped the Trend Reports KPI cards saying "no
comparison available" for every month anyone picked, and made picking a
month fast.

Three things are being pinned down here:

  1. The grounded history itself. The back-projection has to carry the
     REAL shape of the published national MSME establishment series --
     above all the 2020 contraction, which is the one feature a
     plausible-looking invented curve would smooth away. A test that only
     checked "the numbers go up" would pass on a fabricated exponential,
     so these check the direction of specific months.
  2. The cards. Every month from 2020 on must produce a comparison when
     there is something real to project from -- and must still refuse to
     when there is not. Removing the FALSE blanks without removing the
     honest ones is the whole point.
  3. The speed. Changing the month must not re-run the city-wide AI
     sweep, and must not have to rebuild the month-independent half of
     the page at all.
"""

from datetime import date

import pytest

from app import create_app
from app.extensions import db
from app.models import User, SmeProfile, MarketData, SystemSetting, ForecastResult
from app.ml.seed_data import BARANGAY_NAMES
from app.services import historical_baseline_service as history
from app.services import trend_analytics_service as trends


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        trends.clear_trend_caches()
        yield app
        db.session.remove()
        db.drop_all()
        trends.clear_trend_caches()


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


def _url(app, endpoint):
    return app.url_map.iter_rules() and [r.rule for r in app.url_map.iter_rules() if r.endpoint == endpoint][0]


def _seed_market(rows=12, recorded=None, count=150):
    """A database that looks like a real deployment: market_data rows,
    all recorded on one day, because that is when the app first ran."""
    recorded = recorded or date.today()
    for i, barangay in enumerate(BARANGAY_NAMES[:rows]):
        db.session.add(MarketData(
            industry_type="Food and Beverage", location=barangay, competitor_count=count + i,
            population_density=1000, historical_success_rate=0.5, foot_traffic_index=50,
            average_rent=20000, source="Google Places API", date_recorded=recorded,
        ))
    db.session.commit()


# ---------------------------------------------------------------------
# 1. The history is the real published shape, not a smooth invention
# ---------------------------------------------------------------------

def test_the_projection_carries_the_2020_contraction():
    """The single most important assertion in this file.

    Philippine MSME establishments FELL in 2020 (995,741 -> 952,969) and
    rebounded hard in 2021. Any invented growth curve -- including the
    flat 8%-per-quarter one this replaced -- rises smoothly through 2020,
    which is both wrong and the giveaway that nothing real is behind it.
    """
    reference = date(2026, 9, 1)
    jan_2020 = history.project_businesses(10_000, date(2020, 1, 1), reference=reference)
    jun_2020 = history.project_businesses(10_000, date(2020, 6, 1), reference=reference)
    jun_2021 = history.project_businesses(10_000, date(2021, 6, 1), reference=reference)

    assert jun_2020 < jan_2020, "the first half of 2020 must fall, not rise"
    assert jun_2021 > jan_2020, "2021 must rebound past where 2020 started"


def test_history_is_below_the_present_and_rises_toward_it():
    reference = date(2026, 9, 1)
    series = [
        history.project_businesses(10_000, date(year, 6, 1), reference=reference)
        for year in range(2021, 2027)
    ]
    assert all(value < 10_000 for value in series[:-1])
    assert series == sorted(series), "2021 onward should climb toward today"


def test_the_projection_is_deterministic_never_random():
    """Two identical calls, and two page loads, must agree -- a chart
    that re-rolls its own history on refresh is not history."""
    args = (10_000, date(2022, 4, 1))
    assert history.project_businesses(*args) == history.project_businesses(*args)
    keyed = history.project_businesses(*args, series_key="Retail Trade")
    assert keyed == history.project_businesses(*args, series_key="Retail Trade")
    assert keyed != history.project_businesses(*args, series_key="Manufacturing")


def test_nothing_is_written_to_the_database(app):
    """market_data holds real measurements. The back-projection is
    computed on the fly, every time, precisely so invented rows never
    contaminate it: thousands of fabricated snapshots would inflate Total
    Businesses, would be indistinguishable from a real PSA/DTI upload
    afterwards, and would be very hard to undo.

    The AI sweep DOES write -- compute_scores() caches its Places lookups
    into market_data -- so it runs first and the count is taken after it.
    What is pinned here is that the PROJECTION adds nothing.
    """
    with app.app_context():
        _seed_market(rows=5)
        baseline = trends._sweep_baseline("Food and Beverage")
        before = MarketData.query.count()

        for anchor in (date(2020, 6, 1), date(2021, 3, 1), date(2024, 12, 1)):
            trends.get_period_overview(anchor, baseline=baseline, industry_type="Food and Beverage")
            trends.get_market_quarterly_performance("Food and Beverage", as_of=anchor)
            trends.get_monthly_industry_trends(baseline, [], ["Food and Beverage"], as_of=anchor)

        assert MarketData.query.count() == before


# ---------------------------------------------------------------------
# 2. The KPI cards: no more false blanks, and no lost honest ones
# ---------------------------------------------------------------------

def test_every_card_gets_a_comparison_for_a_month_before_the_app_existed(app):
    """This is the reported bug. Picking any month of 2020-2025 left all
    four cards reading "no comparison available", because market_data
    only goes back to the day the app was first run."""
    with app.app_context():
        _seed_market()
        baseline = trends._sweep_baseline()
        for anchor in (date(2020, 3, 1), date(2021, 6, 1), date(2023, 11, 1), date(2025, 8, 1)):
            overview = trends.get_period_overview(anchor, baseline=baseline)
            for name, card in overview["cards"].items():
                delta = card.get("delta_percent", card.get("delta_points"))
                assert delta is not None, f"{name} still has no comparison in {overview['as_of_label']}"


def test_a_projected_card_says_it_is_projected(app):
    """The blanks are gone, so the flag is now the only thing standing
    between a modelled month and a measured one."""
    with app.app_context():
        _seed_market()
        overview = trends.get_period_overview(date(2021, 6, 1), baseline=trends._sweep_baseline())
        card = overview["cards"]["total_businesses"]
        assert card["measured"] is False
        assert card["projected"] is True
        # ...and it names what it was projected from, on screen.
        assert overview["basis"]["source"].startswith("DTI")
        assert overview["basis"]["national_establishments"] > 0


def test_a_measured_month_is_still_measured_and_still_wins(app):
    """Real dated rows must keep beating the projection outright. This
    is the guard against the fix quietly replacing real data."""
    with app.app_context():
        for recorded, count in [(date(2026, 7, 15), 100), (date(2026, 8, 15), 130)]:
            db.session.add(MarketData(
                industry_type="Food and Beverage", location="Poblacion", competitor_count=count,
                population_density=1000, historical_success_rate=0.5, foot_traffic_index=50,
                average_rent=20000, source="Google Places API", date_recorded=recorded,
            ))
        db.session.commit()

        card = trends.get_period_overview(date(2026, 8, 1))["cards"]["total_businesses"]
        assert card["value"] == 130
        assert card["delta_percent"] == 30.0
        assert card["measured"] is True and card["projected"] is False


def test_an_empty_database_still_says_no_comparison(app):
    """The honest blank must survive. With nothing measured anywhere,
    there is nothing to scale, and a projection from zero would be pure
    invention -- so the card must go back to saying so."""
    with app.app_context():
        overview = trends.get_period_overview(date(2021, 5, 1))
        card = overview["cards"]["total_businesses"]
        assert card["delta_percent"] is None
        assert card["projected"] is False


def test_new_startups_still_compares_when_the_market_is_shrinking(app):
    """Net new businesses hits zero for months on end in the first half
    of 2020, because the country was losing establishments. A percentage
    change has no answer from a base of zero, so this card reports an
    absolute change -- otherwise the bug came straight back for exactly
    the months worth looking at."""
    with app.app_context():
        _seed_market()
        card = trends.get_period_overview(date(2020, 4, 1), baseline=trends._sweep_baseline())["cards"]["new_startups"]
        assert card["value"] == 0, "a contracting month has no net new businesses"
        assert card["delta_points"] == 0, "but 0 vs 0 is a known change, not a missing one"


def test_viability_delta_is_not_rounded_into_nothing(app):
    """Viability is a 0-10 index against saturation's 0-100, so a real
    month-over-month move lands in the hundredths. Rounding it to one
    decimal first would report every month as flat."""
    with app.app_context():
        _seed_market()
        baseline = trends._sweep_baseline()
        deltas = [
            trends.get_period_overview(date(2021, month, 1), baseline=baseline)["cards"]["avg_viability"]["delta_points"]
            for month in (3, 6, 9)
        ]
        assert any(abs(d) > 0 for d in deltas), f"every viability delta rounded to zero: {deltas}"


# ---------------------------------------------------------------------
# 3. The picker offers 2020 -> now, and the charts follow it
# ---------------------------------------------------------------------

def test_the_picker_reaches_back_to_2020(app):
    with app.app_context():
        assert trends.EARLIEST_TREND_MONTH == date(2020, 1, 1)
        assert trends.resolve_as_of_month("2020-01") == date(2020, 1, 1)
        assert trends.resolve_as_of_month("2021-07") == date(2021, 7, 1)
        # Still bounded at both ends.
        assert trends.resolve_as_of_month("2019-12") == date(2020, 1, 1)
        assert trends.resolve_as_of_month("2099-01").strftime("%Y-%m") == date.today().strftime("%Y-%m")


def test_the_page_offers_2020_as_the_earliest_month(app):
    with app.app_context():
        _user("sme@example.com", "SME")
        body = _client_for(app, "sme@example.com").get("/trend-reports").get_data(as_text=True)
        assert 'min="2020-01"' in body


def test_quarterly_chart_is_not_a_flat_line_of_zeros_for_a_2020_month(app):
    """Picking 2021 used to draw the demand line flat on the floor with
    0% growth in every quarter, because every quarter in the window
    predated the first snapshot and the code returned zeros."""
    with app.app_context():
        _seed_market()
        q = trends.get_market_quarterly_performance(as_of=date(2021, 6, 1))
        assert all(value > 0 for value in q["businesses"])
        assert all(q["projected"]), "and every one of them is flagged as projected"
        assert any(rate != 0 for rate in q["growth_rate_percent"])


def test_quarterly_growth_is_negative_through_the_2020_contraction(app):
    """The flat 8%-per-quarter projection this replaced reported +8%
    growth for the worst quarters of the pandemic."""
    with app.app_context():
        _seed_market()
        q = trends.get_market_quarterly_performance(as_of=date(2020, 6, 1))
        assert any(rate < 0 for rate in q["growth_rate_percent"]), q["growth_rate_percent"]


def test_monthly_trend_lines_move_with_the_year_not_just_the_month_name(app):
    """Two different years must not draw the same six values -- and the
    labels have to say which year, now that the picker spans seven."""
    with app.app_context():
        _seed_market()
        baseline = trends._sweep_baseline()
        early = trends.get_monthly_industry_trends(baseline, [], ["Food and Beverage"], as_of=date(2020, 6, 1))
        late = trends.get_monthly_industry_trends(baseline, [], ["Food and Beverage"], as_of=date(2025, 6, 1))

        assert early["labels"][-1] == "Jun 2020"
        assert late["labels"][-1] == "Jun 2025"
        assert early["series"]["Food and Beverage"] != late["series"]["Food and Beverage"]
        # A less crowded market back then, which is the point of the
        # projection -- not a flat line with a sine wiggle on it.
        assert early["series"]["Food and Beverage"][-1] < late["series"]["Food and Beverage"][-1]


def test_real_forecasts_are_matched_by_year_not_by_month_name(app):
    """`forecast_date.strftime("%b") == label` matched March 2021
    against March 2026. Harmless when the picker only went back to 2024;
    wrong now that it reaches 2020."""
    with app.app_context():
        _seed_market()
        profile = SmeProfile(
            user_id=_user("sme@example.com", "SME").user_id, business_name="X",
            industry_type="Food and Beverage", location="Poblacion",
            startup_capital=50000, business_stage="startup",
        )
        db.session.add(profile)
        db.session.commit()
        # forecast_result.market_id / lgu_id are NOT NULL, and
        # cluster_label is a derived read-only property -- so let the
        # engine produce a real scored row and date it by hand.
        scores = trends.compute_scores("Food and Beverage", "Poblacion")
        db.session.add(ForecastResult(
            sme_id=profile.sme_id,
            market_id=scores["market_id"],
            lgu_id=scores["lgu_id"],
            input_industry_type="Food and Beverage", input_location="Poblacion",
            saturation_index=91.0, viability_score=0.9, forecast_date=date(2026, 6, 15),
        ))
        db.session.commit()

        baseline = trends._sweep_baseline()
        forecasts = ForecastResult.query.all()
        hit = trends.get_monthly_industry_trends(baseline, forecasts, ["Food and Beverage"], as_of=date(2026, 6, 1))
        miss = trends.get_monthly_industry_trends(baseline, forecasts, ["Food and Beverage"], as_of=date(2021, 6, 1))

        assert hit["series"]["Food and Beverage"][-1] == 91.0, "June 2026 should use its own real forecast"
        assert miss["series"]["Food and Beverage"][-1] != 91.0, "June 2021 must not borrow June 2026's forecast"


# ---------------------------------------------------------------------
# 4. Changing the month is fast
# ---------------------------------------------------------------------

def test_changing_the_month_does_not_re_run_the_city_wide_sweep(app, monkeypatch):
    """The sweep is 600 AI scores and is what made the page take seconds
    to answer a click. It describes the market as it stands NOW, so the
    chosen month cannot change it -- it must be computed once and reused.
    """
    with app.app_context():
        _seed_market()
        trends.clear_trend_caches()

        calls = {"n": 0}
        real = trends.compute_scores

        def counting(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(trends, "compute_scores", counting)

        trends.build_trend_period(as_of=date(2026, 1, 1))
        after_first = calls["n"]
        assert after_first > 0, "the first build should actually sweep"

        for month in (date(2020, 3, 1), date(2021, 6, 1), date(2024, 9, 1)):
            trends.build_trend_period(as_of=month)
        assert calls["n"] == after_first, "later months re-ran the sweep"


def test_the_cache_lets_go_the_moment_market_data_changes(app):
    """Speed must never cost correctness. The memo is keyed on a
    fingerprint of market_data, not on a timer, so there is no window in
    which the page can show a total the database no longer agrees with --
    for an INSERT or for an in-place edit, which leaves the row count
    untouched and would fool a count-only fingerprint.
    """
    with app.app_context():
        this_month = date.today().replace(day=1)
        db.session.add(MarketData(
            industry_type="Food and Beverage", location="Poblacion", competitor_count=10,
            population_density=1000, historical_success_rate=0.5, foot_traffic_index=50,
            average_rent=20000, source="Google Places API", date_recorded=date.today(),
        ))
        db.session.commit()
        assert trends.businesses_as_of(this_month)["value"] == 10

        db.session.add(MarketData(
            industry_type="Retail Trade", location="San Nicolas", competitor_count=999,
            population_density=1000, historical_success_rate=0.5, foot_traffic_index=50,
            average_rent=20000, source="Google Places API", date_recorded=date.today(),
        ))
        db.session.commit()
        assert trends.businesses_as_of(this_month)["value"] == 1009

        edited = MarketData.query.filter_by(location="San Nicolas").one()
        edited.competitor_count = 1
        db.session.commit()
        assert trends.businesses_as_of(this_month)["value"] == 11


def test_the_period_endpoint_skips_the_month_independent_half(app):
    """It returns what the month changes and nothing else -- the pie
    chart and the Places verification table do not move with the date,
    so re-sending them on every click was waste."""
    with app.app_context():
        _seed_market()
        _user("sme@example.com", "SME")
        client = _client_for(app, "sme@example.com")

        payload = client.get("/api/trend-period?as_of=2021-06").get_json()

        assert payload["period"]["as_of"] == "2021-06"
        for key in ("period_overview", "monthly_trends", "market_quarterly", "top_industries"):
            assert key in payload
        for key in ("industry_distribution", "places_api_rows", "overview"):
            assert key not in payload, f"{key} does not depend on the month"
        # Internal handles must never be serialised.
        assert "_baseline" not in payload and "_real_forecasts" not in payload


def test_the_full_report_and_the_period_endpoint_agree(app):
    """Two entry points, one answer -- otherwise the numbers would shift
    the moment a user touched the calendar."""
    with app.app_context():
        _seed_market()
        full = trends.build_trend_report(as_of=date(2022, 5, 1))
        period = trends.build_trend_period(as_of=date(2022, 5, 1))
        period.pop("_baseline", None)
        period.pop("_real_forecasts", None)
        for key in period:
            assert full[key] == period[key], key


def test_the_page_changes_the_month_without_reloading(app):
    """A full page reload re-downloads every asset, rebuilds both charts
    and re-runs the Places verification table, to move four numbers."""
    with app.app_context():
        _user("sme@example.com", "SME")
        body = _client_for(app, "sme@example.com").get("/trend-reports").get_data(as_text=True)
        assert "window.location.assign" not in body, "the month picker should no longer reload the page"
        assert "/api/trend-period" in body
        assert "history.replaceState" in body, "but ?as_of= should stay in the address bar"
