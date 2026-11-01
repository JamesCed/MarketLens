"""
tests/test_custom_template.py
-------------------------------
V3.9: the Custom Template on Gov't Data Upload.

Every column MarketLens reads -- including the inputs of the AI model and
of the Market Saturation Index (Equation 10) -- is listed as a suggestion
with what it feeds and where MarketLens gets it today. The LGU office
ticks only what it can really provide; the required columns are always
in, so a custom template always imports. A new standard template carries
every model input as already-counted figures per industry per barangay.
"""

import csv
import io
import json
import os
import tempfile

import pytest
from sqlalchemy import text

from app import create_app
from app.extensions import db
from app.ml.constants import BUSINESS_TYPES
from app.ml.seed_data import BARANGAY_NAMES
from app.models import AuditLog, MarketData, SystemSetting, User

UPLOAD = "/lgu/government-data-upload"
FOOD = "Food and Beverage"


@pytest.fixture
def app(tmp_path):
    app = create_app("testing")
    app.config.update(UPLOAD_FOLDER=str(tmp_path / "uploads"))

    @app.before_request
    def _forget_cached_login():
        from flask import g

        g.pop("_login_user", None)

    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()
        with db.engine.begin() as conn:
            for table in ("upload_batches", "lgu_access_requests"):
                conn.execute(text(f"DROP TABLE IF EXISTS {table}"))


@pytest.fixture
def officer(app):
    user = User(name="LGU Officer", email="officer@template.test", role="LGU")
    user.set_password("password123")
    user.approve_upload()
    db.session.add(user)
    db.session.commit()
    return user


def _login(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.user_id)
        sess["_fresh"] = True
    return client


def _rows(response):
    return list(csv.reader(io.StringIO(response.get_data(as_text=True))))


def _import(app, body, user_id, dataset_type="MARKET_DATA", source="DTI"):
    from app.services.data_import_service import process_upload

    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="")
    handle.write(body)
    handle.close()
    try:
        return process_upload(dataset_type, handle.name, source, user_id, run_followups=False)
    finally:
        os.unlink(handle.name)


def test_the_page_lists_every_column_as_a_suggestion_with_what_it_feeds(app, officer):
    from app.controllers.lgu_controller import UPLOAD_COLUMNS

    page = _login(app, officer).get(UPLOAD).get_data(as_text=True)
    assert 'id="custom-template"' in page and "Save as my template" in page
    for key, columns in UPLOAD_COLUMNS.items():
        for name, *_rest in columns:
            assert f'name="col-{key}" value="{name}"' in page, (key, name)
    for feeds in ("MSI Competitor density (w1 = 0.45)", "MSI Demand trend (w2 = 0.35)",
                  "MSI Socio-demographic (w3 = 0.20)", "AI saturation model input"):
        assert feeds in page, feeds
    assert "Estimated (no public barangay figure)" in page
    assert "Real for 73 of 76 barangays" in page
    assert "Optional" not in page, "columns are Required, Important or For records"
    assert "Market counts &amp; area figures template (CSV)" in page


def test_the_counts_template_carries_every_model_input_and_imports(app, officer):
    from app.ml.constants import FEATURE_NAMES

    response = _login(app, officer).get("/lgu/dataset-template/MARKET_COUNTS")
    assert response.status_code == 200
    assert "marketlens_market_counts_template.csv" in response.headers["Content-Disposition"]
    header, example = _rows(response)
    model_inputs = set(FEATURE_NAMES) - {"business_density", "years_in_operation", "industry_type_encoded"}
    assert model_inputs <= set(header), "every market-level model input is in the counts template"

    count, status, message = _import(app, ",".join(header) + "\n" + ",".join(example) + "\n", officer.user_id)
    assert status == "success", message
    row = MarketData.query.filter_by(industry_type=FOOD, location="San Roque", source="DTI").one()
    assert row.competitor_count == 18 and float(row.average_rent) == 15000.0
    assert float(row.historical_success_rate) == 0.85 and float(row.foot_traffic_index) == 55.0
    assert row.date_recorded.isoformat() == "2026-09-30"


def test_a_custom_template_keeps_the_required_columns_and_template_order(app, officer):
    client = _login(app, officer)
    response = client.get("/lgu/dataset-template/MARKET_COUNTS?columns=average_rent,bogus,competitor_count")
    assert "marketlens_market_counts_custom_template.csv" in response.headers["Content-Disposition"]
    header, example = _rows(response)
    assert header == ["barangay", "industry_type", "competitor_count", "average_rent"]
    assert example == ["San Roque", "Food and Beverage", "18", "15000"]
    header, _example = _rows(client.get("/lgu/dataset-template/LGU_DATA?columns="))
    assert header == ["barangay", "zoning_info", "business_density"], "the zoning required columns stay"


def test_saving_the_custom_template_remembers_the_ticks(app, officer):
    client = _login(app, officer)
    response = client.post("/lgu/custom-template", data={
        "col-LGU_DATA": ["permit_count"],
        "col-MARKET_DATA": ["status"],
        "col-MARKET_COUNTS": ["competitor_count", "average_rent"],
    })
    assert response.status_code == 302 and response.headers["Location"].endswith("#custom-template")
    saved = json.loads(db.session.get(User, officer.user_id).upload_template)
    assert saved == {"LGU_DATA": ["barangay", "zoning_info", "business_density", "permit_count"],
                     "MARKET_DATA": ["barangay", "industry_type", "status"],
                     "MARKET_COUNTS": ["barangay", "industry_type", "competitor_count", "average_rent"]}
    assert AuditLog.query.filter_by(action="save_custom_template").count() == 1

    header, _example = _rows(client.get("/lgu/dataset-template/MARKET_DATA?mine=1"))
    assert header == ["barangay", "industry_type", "status"]
    page = client.get(UPLOAD).get_data(as_text=True)
    assert 'value="average_rent" id="ct-MARKET_COUNTS-average_rent" checked' in page
    assert 'value="foot_traffic_index" id="ct-MARKET_COUNTS-foot_traffic_index" >' in page
    assert "Saved on your account" in page


def test_a_new_custom_template_starts_with_the_suggested_columns(app, officer):
    from app.services.upload_templates import saved_selections

    selections = saved_selections(officer)
    assert selections["MARKET_COUNTS"] == ["barangay", "industry_type", "competitor_count", "population_density",
                                           "average_rent", "foot_traffic_index", "historical_success_rate",
                                           "date_recorded"]
    assert "permit_count" not in selections["LGU_DATA"], "records-only columns are not suggested"
    page = _login(app, officer).get(UPLOAD).get_data(as_text=True)
    assert "<strong data-ct-count>5</strong> of 6" in page
    assert "<strong data-ct-count>1</strong> of 6" in page


def test_unticking_everything_keeps_the_required_columns(app, officer):
    _login(app, officer).post("/lgu/custom-template", data={})
    saved = json.loads(db.session.get(User, officer.user_id).upload_template)
    assert saved["MARKET_COUNTS"] == ["barangay", "industry_type", "competitor_count"], (
        "competitor_count stays: it is what makes the importer read the file as counts")
    assert saved["LGU_DATA"] == ["barangay", "zoning_info", "business_density"]


def test_a_custom_counts_file_keeps_the_figures_it_leaves_out(app, officer):
    pair = (FOOD, BARANGAY_NAMES[0])
    from app.services.data_import_service import area_features_for

    before = area_features_for([pair])[pair]
    body = f"barangay,industry_type,competitor_count,average_rent\n{pair[1]},{pair[0]},9,22000\n"
    _count, status, message = _import(app, body, officer.user_id)
    assert status == "success", message
    row = MarketData.query.filter_by(industry_type=FOOD, location=pair[1], source="DTI").one()
    assert row.competitor_count == 9 and float(row.average_rent) == 22000.0
    assert float(row.population_density) == pytest.approx(float(before["population_density"]))
    assert float(row.historical_success_rate) == pytest.approx(float(before["historical_success_rate"]))


def test_a_locked_account_cannot_use_the_custom_template(app):
    user = User(name="New LGU", email="new@template.test", role="LGU")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    client = _login(app, user)
    assert client.post("/lgu/custom-template", data={"col-MARKET_COUNTS": ["competitor_count"]}).status_code == 403
    assert client.get("/lgu/dataset-template/MARKET_COUNTS?columns=average_rent").status_code == 403
    assert db.session.get(User, user.user_id).upload_template is None


def test_every_column_has_its_facts(app):
    from app.controllers.lgu_controller import DATASET_GUIDES, UPLOAD_COLUMNS
    from app.services.upload_templates import FACTS, TEMPLATE_TARGETS

    assert set(UPLOAD_COLUMNS) == set(TEMPLATE_TARGETS) == set(DATASET_GUIDES)
    for columns in UPLOAD_COLUMNS.values():
        for name, *_rest in columns:
            assert name in FACTS, name
    assert len(BUSINESS_TYPES) and FOOD in BUSINESS_TYPES
