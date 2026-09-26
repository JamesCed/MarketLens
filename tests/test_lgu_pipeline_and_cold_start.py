"""
tests/test_lgu_pipeline_and_cold_start.py
--------------------------------------------
The LGU "data highway": an official upload lands, and the city-wide
output changes to match it.

Three things are pinned here, and they are the three ways this feature
can fail quietly rather than loudly.

  1. AN UPLOAD IS ALL OR NOTHING. The importer used to commit inside
     itself, so a file that failed on row 60 left rows 1-59 in the
     database AND reported an error. The uploader was then holding a
     half-imported register with no way to tell how far it got, and a
     re-upload would double every row that had already landed.

  2. AN UNREADABLE CELL NAMES ITSELF. "invalid literal for int() with
     base 10: 'N/A'" is not an error message for someone with a
     spreadsheet open, it is a scavenger hunt. Row, column, value.

  3. RECOMMENDATIONS WAIT FOR REAL DATA. lgu_data is never empty --
     the scoring engine writes placeholder rows for itself -- so
     "are there rows?" is the wrong question and would answer True on
     a database nobody has uploaded to. City-wide rankings stay
     switched off until a real person uploads something.
"""

import os
import tempfile
from datetime import date, timedelta

import pytest

from app import create_app
from app.extensions import db
from app.ml.constants import BUSINESS_TYPES
from app.ml.seed_data import BARANGAY_NAMES
from app.models import LguData, MarketData, SmeProfile, SystemSetting
from app.models.user import User


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
def lgu_user(app):
    with app.app_context():
        user = User(name="LGU Officer", email="officer@tarlac.test", role="LGU")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
        return user.user_id


def _csv(rows, header):
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    handle.write(",".join(header) + "\n")
    for row in rows:
        handle.write(",".join("" if cell is None else str(cell) for cell in row) + "\n")
    handle.close()
    return handle.name


# ---------------------------------------------------------------------
# 1. All or nothing
# ---------------------------------------------------------------------

def test_one_bad_row_leaves_the_database_untouched(app, lgu_user):
    from app.services.data_import_service import process_upload

    # Deliberately NOT "N/A": pandas reads that as a missing-value
    # marker, and imputing a missing figure is the documented
    # behaviour this project wants. A typo is a different thing.
    path = _csv(
        [
            (BARANGAY_NAMES[0], 12, 1.4),
            (BARANGAY_NAMES[1], 8, 1.1),
            (BARANGAY_NAMES[2], "12o", 0.9),      # row 4: a typed digit-o
        ],
        ["barangay", "permit_count", "business_density"],
    )
    try:
        with app.app_context():
            before = LguData.query.count()
            count, status, message = process_upload("LGU_DATA", path, "DTI", lgu_user)
            after = LguData.query.count()
    finally:
        os.unlink(path)

    assert status == "failed"
    assert count == 0
    assert after == before, (
        f"{after - before} row(s) survived a failed import -- the uploader now has a "
        f"half-imported register and no way to know it"
    )
    assert "nothing was imported" in message


def test_a_good_file_lands_completely(app, lgu_user):
    from app.services.data_import_service import process_upload

    path = _csv(
        [(name, 10 + index, 1.2) for index, name in enumerate(BARANGAY_NAMES[:5])],
        ["barangay", "permit_count", "business_density"],
    )
    try:
        with app.app_context():
            count, status, message = process_upload("LGU_DATA", path, "DTI", lgu_user)
            rows = LguData.query.filter_by(uploaded_by=lgu_user).count()
    finally:
        os.unlink(path)

    assert status == "success", message
    assert count == 5
    assert rows == 5


# ---------------------------------------------------------------------
# 2. Errors that name themselves
# ---------------------------------------------------------------------

def test_a_bad_cell_is_reported_with_its_row_and_column(app, lgu_user):
    from app.services.data_import_service import process_upload

    path = _csv(
        [(BARANGAY_NAMES[0], 5, 1.0), (BARANGAY_NAMES[1], "twelve", 1.0)],
        ["barangay", "permit_count", "business_density"],
    )
    try:
        with app.app_context():
            _count, status, message = process_upload("LGU_DATA", path, "DTI", lgu_user)
    finally:
        os.unlink(path)

    assert status == "failed"
    # Row 3 -- the header is row 1, so the second data row is row 3 in
    # the spreadsheet the uploader is looking at.
    assert "Row 3" in message, message
    assert "permit_count" in message
    assert "twelve" in message


def test_an_unknown_barangay_is_rejected_with_a_suggestion(app, lgu_user):
    """A misspelt barangay used to be inserted verbatim, producing a
    row every lookup in the app would miss -- the upload reported
    success and the barangay's figures never changed."""
    from app.services.data_import_service import process_upload

    target = BARANGAY_NAMES[0]
    typo = target[:-1] if len(target) > 4 else target + "x"
    path = _csv([(typo, 5, 1.0)], ["barangay", "permit_count", "business_density"])
    try:
        with app.app_context():
            _count, status, message = process_upload("LGU_DATA", path, "DTI", lgu_user)
    finally:
        os.unlink(path)

    assert status == "failed"
    assert "not a recognised Tarlac City barangay" in message
    assert target in message, f"expected a 'did you mean' hint naming {target}: {message}"


def test_a_percentage_success_rate_is_caught(app, lgu_user):
    """historical_success_rate is a 0-1 fraction. 85 instead of 0.85
    moves the feature by two orders of magnitude and poisons every
    score for that barangay, silently."""
    from app.services.data_import_service import process_upload

    path = _csv(
        [(BUSINESS_TYPES[0], BARANGAY_NAMES[0], 85)],
        ["industry_type", "location", "historical_success_rate"],
    )
    try:
        with app.app_context():
            _count, status, message = process_upload("MARKET_DATA", path, "DTI", lgu_user)
    finally:
        os.unlink(path)

    assert status == "failed"
    assert "fraction between 0 and 1" in message


# ---------------------------------------------------------------------
# 3. Permits become per-industry competitor counts
# ---------------------------------------------------------------------

def test_a_permit_register_with_psic_codes_produces_competitor_counts(app, lgu_user):
    """The substance of the merge. A register is one row per business;
    grouped by trade and barangay it is an official competitor count,
    which is the same shape as a Google Places count and can be
    reconciled with one."""
    from app.services.data_import_service import process_upload

    rows = (
        [(BARANGAY_NAMES[0], "56101", "active") for _ in range(4)]     # food service
        + [(BARANGAY_NAMES[0], "47211", "active") for _ in range(7)]   # retail
        + [(BARANGAY_NAMES[1], "56101", "active") for _ in range(2)]
        + [(BARANGAY_NAMES[0], "56101", "expired")]                    # must not count
    )
    path = _csv(rows, ["barangay", "psic_code", "status"])
    try:
        with app.app_context():
            _count, status, message = process_upload("LGU_DATA", path, "DTI", lgu_user)

            food = MarketData.query.filter_by(
                location=BARANGAY_NAMES[0],
                industry_type="Accommodation and Food Service Activities",
                source="DTI",
            ).one()
            retail = MarketData.query.filter_by(
                location=BARANGAY_NAMES[0],
                industry_type="Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
                source="DTI",
            ).one()
    finally:
        os.unlink(path)

    assert status == "success", message
    assert food.competitor_count == 4, "an expired permit is not a competitor"
    assert retail.competitor_count == 7


def test_a_register_without_a_trade_column_still_imports(app, lgu_user):
    """Plenty of registers are barangay summaries with no per-business
    detail. That is not an error -- they just yield no per-industry
    counts."""
    from app.services.data_import_service import process_upload

    path = _csv([(BARANGAY_NAMES[0], 30)], ["barangay", "permit_count"])
    try:
        with app.app_context():
            count, status, _message = process_upload("LGU_DATA", path, "DTI", lgu_user)
            derived = MarketData.query.filter_by(source="DTI").count()
    finally:
        os.unlink(path)

    assert status == "success"
    assert count == 1
    assert derived == 0


# ---------------------------------------------------------------------
# 4. Reconciling the two competitor sources
# ---------------------------------------------------------------------

def test_the_larger_of_the_two_sources_wins(app):
    """Both sources undercount, in different directions, and neither
    contains the other: Google misses unlisted micro-enterprises, a
    permit register misses informal operators. A business seen by
    either exists, so the supported figure is the larger -- never the
    sum (double-counting everything both see) and never the newest
    (discarding evidence because a file was uploaded)."""
    from app.services.forecasting_service import reconciled_competitor_counts

    industry, location = BUSINESS_TYPES[0], BARANGAY_NAMES[0]
    with app.app_context():
        db.session.add(MarketData(
            industry_type=industry, location=location, competitor_count=12,
            source="Google Places API", date_recorded=date.today() - timedelta(days=5),
        ))
        db.session.add(MarketData(
            industry_type=industry, location=location, competitor_count=3,
            source="DTI", date_recorded=date.today(),   # newer, but smaller
        ))
        db.session.commit()

        merged = reconciled_competitor_counts([industry], [location])

    assert merged[(industry, location)] == 12, (
        "a newer permit count erased a larger Google count -- newest-wins is "
        "the merge this reconciliation exists to replace"
    )


def test_permits_raise_a_count_google_could_not_see(app):
    from app.services.forecasting_service import reconciled_competitor_counts

    industry, location = BUSINESS_TYPES[0], BARANGAY_NAMES[1]
    with app.app_context():
        db.session.add(MarketData(
            industry_type=industry, location=location, competitor_count=2,
            source="Google Places API", date_recorded=date.today(),
        ))
        db.session.add(MarketData(
            industry_type=industry, location=location, competitor_count=19,
            source="DTI", date_recorded=date.today() - timedelta(days=30),
        ))
        db.session.commit()

        merged = reconciled_competitor_counts([industry], [location])

    assert merged[(industry, location)] == 19


def test_the_reconciled_count_is_what_the_model_scores(app):
    """Not merely reported. If the card says 19 competitors and the
    model scored 2, the page is showing two different answers."""
    from app.services.forecasting_service import compute_scores

    industry, location = BUSINESS_TYPES[0], BARANGAY_NAMES[2]
    with app.app_context():
        db.session.add(MarketData(
            industry_type=industry, location=location, competitor_count=1,
            population_density=900, historical_success_rate=0.55,
            foot_traffic_index=45, average_rent=19000,
            source="Google Places API", date_recorded=date.today(),
        ))
        db.session.add(MarketData(
            industry_type=industry, location=location, competitor_count=25,
            source="DTI", date_recorded=date.today(),
        ))
        db.session.commit()

        scores = compute_scores(industry, location)

    assert scores["competitor_count"] == 25


# ---------------------------------------------------------------------
# 5. Cold start
# ---------------------------------------------------------------------

def test_placeholder_rows_do_not_count_as_an_active_dataset(app):
    """The trap this function exists for. lgu_data is never empty --
    the scoring engine writes placeholder rows for itself -- so
    "are there rows?" would answer True on a database nobody has
    uploaded anything to."""
    from app.services.data_import_service import has_active_lgu_data
    from app.services.forecasting_service import find_or_create_lgu_data

    with app.app_context():
        assert has_active_lgu_data() is False

        find_or_create_lgu_data(BARANGAY_NAMES[0])      # engine talking to itself
        assert LguData.query.count() == 1
        assert has_active_lgu_data() is False, (
            "an auto-generated placeholder was mistaken for an official upload"
        )


def test_a_real_upload_switches_it_on(app, lgu_user):
    from app.services.data_import_service import (
        active_lgu_dataset_summary, has_active_lgu_data, process_upload,
    )

    path = _csv([(name, 10) for name in BARANGAY_NAMES[:4]], ["barangay", "permit_count"])
    try:
        with app.app_context():
            assert has_active_lgu_data() is False
            _count, status, message = process_upload("LGU_DATA", path, "DTI", lgu_user)
            assert status == "success", message

            assert has_active_lgu_data() is True
            summary = active_lgu_dataset_summary()
    finally:
        os.unlink(path)

    assert summary["barangays_covered"] == 4
    assert summary["source"] == "DTI"


def test_the_home_panel_shows_the_empty_state_before_any_upload(app):
    with app.app_context():
        user = User(name="Juan", email="sme@cold.test", role="SME")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
        db.session.add(SmeProfile(
            user_id=user.user_id, business_name="Juan's Coffee",
            industry_type=BUSINESS_TYPES[0], location=BARANGAY_NAMES[0],
            business_stage="startup", startup_capital=250000,
            employee_count=2, monthly_revenue_est=60000,
        ))
        db.session.commit()

    client = app.test_client()
    client.post("/login", data={"email": "sme@cold.test", "password": "password123"},
                follow_redirects=True)
    page = client.get("/home").get_data(as_text=True)

    assert "No record yet" in page
    assert "No official LGU dataset is active" in page
    # The container itself must be absent, so the script's
    # `if (!lguBox) return` short-circuits and the ranking endpoint is
    # never called. (The script still MENTIONS the id, which is why
    # this looks for the element rather than the bare string.)
    assert 'id="lguRecommendationList"' not in page, (
        "the ranking container rendered anyway, so the browser will still call the "
        "endpoint and rank barangays off placeholder rows"
    )


def test_an_sme_is_not_offered_an_upload_button_they_cannot_use(app):
    """Offering an SME a button that leads to a page their role cannot
    open is a dead end. Say who can instead."""
    with app.app_context():
        user = User(name="Juan", email="sme2@cold.test", role="SME")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()

    client = app.test_client()
    client.post("/login", data={"email": "sme2@cold.test", "password": "password123"},
                follow_redirects=True)
    page = client.get("/home").get_data(as_text=True)

    assert "Upload Dataset" not in page
    assert "LGU account can upload one" in page


def test_the_ranking_endpoint_refuses_before_any_upload(app):
    """The gate is not only cosmetic: the endpoint itself declines, so
    the panel cannot be routed around from the browser."""
    with app.app_context():
        user = User(name="Juan", email="sme3@cold.test", role="SME")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()

    client = app.test_client()
    client.post("/login", data={"email": "sme3@cold.test", "password": "password123"},
                follow_redirects=True)
    payload = client.get("/api/lgu-recommendations").get_json()

    assert payload["has_lgu_data"] is False
    assert payload["top_opportunity"] == []
    assert payload["top_saturated"] == []


def test_the_saturation_map_is_not_gated(app):
    """Deliberately NOT switched off by the cold start. The map answers
    "what does the data we have say", which is a fair question with or
    without a permit register; only the city-wide investment ranking
    is a claim that needs the city's own records behind it."""
    with app.app_context():
        user = User(name="Juan", email="sme4@cold.test", role="SME")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
        db.session.add(MarketData(
            industry_type=BUSINESS_TYPES[0], location=BARANGAY_NAMES[0],
            competitor_count=4, population_density=900, historical_success_rate=0.5,
            foot_traffic_index=45, average_rent=19000, source="Manual",
            date_recorded=date.today(),
        ))
        db.session.commit()

    client = app.test_client()
    client.post("/login", data={"email": "sme4@cold.test", "password": "password123"},
                follow_redirects=True)
    response = client.get("/api/locations-forecast")

    assert response.status_code == 200
    assert isinstance(response.get_json(), list)


def test_after_an_upload_the_panel_ranks_barangays(app, lgu_user):
    from app.services.data_import_service import process_upload

    path = _csv(
        [(name, 10) for name in BARANGAY_NAMES[:6]],
        ["barangay", "permit_count"],
    )
    try:
        with app.app_context():
            for name in BARANGAY_NAMES[:6]:
                db.session.add(MarketData(
                    industry_type=BUSINESS_TYPES[0], location=name,
                    competitor_count=BARANGAY_NAMES.index(name) + 1,
                    population_density=900, historical_success_rate=0.55,
                    foot_traffic_index=45, average_rent=19000,
                    source="Google Places API", date_recorded=date.today(),
                ))
            db.session.commit()
            _count, status, message = process_upload("LGU_DATA", path, "DTI", lgu_user)
            assert status == "success", message

            user = User(name="Juan", email="sme5@cold.test", role="SME")
            user.set_password("password123")
            db.session.add(user)
            db.session.commit()
    finally:
        os.unlink(path)

    client = app.test_client()
    client.post("/login", data={"email": "sme5@cold.test", "password": "password123"},
                follow_redirects=True)
    payload = client.get(
        f"/api/lgu-recommendations?industry_type={BUSINESS_TYPES[0]}"
    ).get_json()

    assert payload["has_lgu_data"] is True
    assert len(payload["top_opportunity"]) == 3
    assert len(payload["top_saturated"]) == 3
    # Least saturated first in the opportunity list, most saturated
    # first in the other -- the two lists must not be the same order.
    opportunity = [row["saturation_index"] for row in payload["top_opportunity"]]
    saturated = [row["saturation_index"] for row in payload["top_saturated"]]
    assert opportunity == sorted(opportunity)
    assert saturated == sorted(saturated, reverse=True)


# ---------------------------------------------------------------------
# 6. The day budget on live Places lookups
# ---------------------------------------------------------------------

def test_the_day_budget_stops_spending_once_it_is_reached(app):
    """PLACES_LIVE_FETCH is true on a public URL and Places API (New)
    bills per call, so the per-request cap bounds the wrong quantity on
    its own."""
    from app.services import forecasting_service as fs

    with app.app_context():
        SystemSetting.set(fs.PLACES_DAILY_BUDGET_KEY, "3")
        fs._DAY_SPEND["day"] = None          # fresh process

        allowed = [fs._spend_day_budget() for _ in range(5)]

    assert allowed == [True, True, True, False, False], allowed


def test_a_zero_budget_means_unlimited(app):
    from app.services import forecasting_service as fs

    with app.app_context():
        SystemSetting.set(fs.PLACES_DAILY_BUDGET_KEY, "0")
        fs._DAY_SPEND["day"] = None
        assert all(fs._spend_day_budget() for _ in range(25))


def test_the_budget_counter_cannot_commit_someone_elses_transaction(app, lgu_user):
    """SystemSetting.set() commits. If the counter wrote from inside
    scoring, it would commit whatever the caller had staged -- and the
    upload pipeline's whole promise is that a failed file leaves
    nothing behind."""
    from app.services import forecasting_service as fs

    with app.app_context():
        fs._DAY_SPEND["day"] = None
        before = LguData.query.count()

        db.session.add(LguData(
            source="DTI", barangay=BARANGAY_NAMES[0], permit_count=1,
            business_density=1.0, upload_date=date.today(), uploaded_by=lgu_user,
        ))
        assert fs._spend_day_budget() is True     # staged row still pending
        db.session.rollback()

        after = LguData.query.count()

    assert after == before, (
        "the day-budget counter committed a row that was staged by someone else"
    )
