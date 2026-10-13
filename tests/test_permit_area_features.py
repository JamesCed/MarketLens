"""
tests/test_permit_area_features.py
------------------------------------
A permit register only says how many businesses hold a permit. The
market rows derived from it become the newest row for their (industry,
barangay), and every scoring path reads the newest row's population
density, foot traffic, rent and success rate, so those rows must carry
them -- left empty, they reached the model as zeros.

Also pins the bundled sample register in sample_data/: it must import
cleanly and give every barangay a full Diversification Plan.
"""

import os
import tempfile
from datetime import date, timedelta

import pytest

from app import create_app
from app.extensions import db
from app.ml.seed_data import BARANGAY_NAMES, get_barangay_profile
from app.models import MarketData, SystemSetting
from app.models.user import User

FOOD = "Food and Beverage"
RETAIL = "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"
FIELDS = ("population_density", "foot_traffic_index", "average_rent", "historical_success_rate")
SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "sample_data", "MarketLens_LGU_Sample_Upload_Tarlac_City.csv")


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
        user = User(name="LGU Officer", email="officer@area.test", role="LGU")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
        return user.user_id


def _csv(rows, header):
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    handle.write(",".join(header) + "\n")
    for row in rows:
        handle.write(",".join(str(cell) for cell in row) + "\n")
    handle.close()
    return handle.name


def _upload(app, lgu_user, rows):
    from app.services.data_import_service import process_upload

    path = _csv(rows, ["barangay", "industry_type", "line_of_business", "status"])
    try:
        with app.app_context():
            return process_upload("LGU_DATA", path, "DTI", lgu_user)
    finally:
        os.unlink(path)


def test_a_permit_row_keeps_the_figures_of_the_row_it_replaces(app, lgu_user):
    tibag = "Tibag"
    with app.app_context():
        db.session.add(MarketData(industry_type=FOOD, location=tibag, competitor_count=9,
                                  population_density=6100.0, foot_traffic_index=61.0, average_rent=17500.0,
                                  historical_success_rate=0.62, source="Google Places API",
                                  date_recorded=date.today() - timedelta(days=3)))
        db.session.commit()

    _count, status, message = _upload(app, lgu_user, [(tibag, FOOD, "Bakery", "active")] * 3)
    assert status == "success", message

    with app.app_context():
        permit = MarketData.query.filter_by(industry_type=FOOD, location=tibag, source="DTI").one()
        assert permit.competitor_count == 3
        assert tuple(float(getattr(permit, field)) for field in FIELDS) == (6100.0, 61.0, 17500.0, 0.62)


def test_a_permit_row_with_nothing_before_it_uses_the_barangay_profile(app, lgu_user):
    location = BARANGAY_NAMES[5]
    _count, status, message = _upload(app, lgu_user, [(location, RETAIL, "Hardware", "active")] * 2)
    assert status == "success", message

    with app.app_context():
        permit = MarketData.query.filter_by(industry_type=RETAIL, location=location, source="DTI").one()
        profile = get_barangay_profile(location)
        for field in FIELDS:
            assert float(getattr(permit, field)) == pytest.approx(profile[field]), field


def test_the_model_never_sees_zeros_after_an_upload(app, lgu_user):
    from app.services.forecasting_service import build_feature_vector, find_or_create_lgu_data
    from app.services.trend_analytics_service import latest_market_data_by_key

    location = BARANGAY_NAMES[0]
    _count, status, message = _upload(app, lgu_user, [(location, FOOD, "Coffee shop", "active")] * 4)
    assert status == "success", message

    with app.app_context():
        market = latest_market_data_by_key()[(FOOD, location)]
        assert market.source == "DTI"
        vector = build_feature_vector(market, find_or_create_lgu_data(location), 0, FOOD)
        assert all(value > 0 for value in vector[1:5]), vector


def test_permit_rows_saved_before_the_fix_are_filled_in_on_start(app):
    from app.services.startup_migrations import _backfill_permit_row_features

    location = BARANGAY_NAMES[2]
    with app.app_context():
        db.session.add(MarketData(industry_type=FOOD, location=location, competitor_count=5,
                                  source="DTI", date_recorded=date.today()))
        db.session.commit()
        assert _backfill_permit_row_features() == 1
        row = MarketData.query.filter_by(industry_type=FOOD, location=location, source="DTI").one()
        profile = get_barangay_profile(location)
        for field in FIELDS:
            assert float(getattr(row, field)) == pytest.approx(profile[field]), field
        assert _backfill_permit_row_features() == 0, "nothing left to fill on the next start"


def test_the_upload_reads_the_file_once(app, lgu_user, monkeypatch):
    from app.services import data_import_service

    reads = []
    real = data_import_service._pandas().read_csv
    monkeypatch.setattr(data_import_service._pandas(), "read_csv",
                        lambda *a, **k: reads.append(a[0]) or real(*a, **k))
    _count, status, message = _upload(app, lgu_user, [(BARANGAY_NAMES[0], FOOD, "Bakery", "active")])
    assert status == "success", message
    assert len(reads) == 1
    assert data_import_service._READ_CACHE == {}, "the parsed file is not kept after the upload"


@pytest.mark.skipif(not os.path.exists(SAMPLE), reason="sample register not bundled")
def test_the_sample_register_fills_the_diversification_plan(app, lgu_user):
    from app.ml.constants import BUSINESS_TYPES
    from app.services.data_import_service import has_active_lgu_data, process_upload
    from app.services.diversification_service import build_diversification_plan

    with app.app_context():
        _count, status, message = process_upload("LGU_DATA", SAMPLE, "DTI", lgu_user)
        assert status == "success", message
        assert has_active_lgu_data()

        permit_rows = MarketData.query.filter_by(source="DTI").all()
        assert len(permit_rows) == len(BARANGAY_NAMES) * len(BUSINESS_TYPES)
        assert all(getattr(row, field) is not None for row in permit_rows for field in FIELDS)

        plan = build_diversification_plan()
        assert len(plan["barangays"]) == len(BARANGAY_NAMES)
        assert all(b["diversity"] is not None for b in plan["barangays"])
        assert plan["priority"], "the plan names priority barangays"
        assert any(b["promote"] for b in plan["barangays"])
