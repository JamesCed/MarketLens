"""
tests/test_lgu_upload_flow.py
-------------------------------
The Gov't Data Upload, end to end.

  1. A LARGE FILE IS WRITTEN IN A FEW STATEMENTS. The importer used to
     add one ORM object per row -- about fifteen thousand INSERTs for
     the sample register, one network round trip each against the
     hosted MySQL. The request outlived the server's time limit, the
     worker was killed, the transaction rolled back and the upload
     silently never happened.

  2. ONE GOV'T / ZONING ROW PER BARANGAY. A permit register is totalled
     per barangay instead of stored one row per permit.

  3. TWO TEMPLATES. Zoning data and market data (the permit register)
     are separate datasets with their own required columns, and a file
     sent to the wrong one is refused with a pointer to the right one.

  4. THE OUTPUT APPEARS. After a market or zoning upload the city-wide
     recommendations switch on and rank barangays.

  5. A LONG UPLOAD IS A BACKGROUND JOB with a status endpoint, so the
     page cannot hang.
"""

import csv
import os
import tempfile
import time
from datetime import date

import pytest
from sqlalchemy import event

from app import create_app
from app.extensions import db
from app.ml.constants import BUSINESS_TYPES
from app.ml.seed_data import BARANGAY_NAMES
from app.models import LguData, MarketData, SystemSetting
from app.models.user import User

FOOD = "Food and Beverage"
RETAIL = "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"
SAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample_data")
SAMPLE_XLSX = os.path.join(SAMPLE_DIR, "MarketLens_LGU_Sample_Upload_Tarlac_City.xlsx")


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
        user = User(name="LGU Officer", email="officer@flow.test", role="LGU")
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
        return user.user_id


def _csv(rows, header):
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="")
    writer = csv.writer(handle)
    writer.writerow(header)
    writer.writerows(rows)
    handle.close()
    return handle.name


def _register_rows(n_per_barangay=30, barangays=BARANGAY_NAMES[:20]):
    rows, number = [], 0
    trades = [(FOOD, "Bakery"), (FOOD, "Milk tea shop"), (RETAIL, "Hardware"), (RETAIL, "Sari-sari store")]
    for barangay in barangays:
        for index in range(n_per_barangay):
            number += 1
            industry, line = trades[index % len(trades)]
            status = "expired" if index % 10 == 0 else "active"
            rows.append((f"P-{number:05d}", barangay, industry, line, f"Shop {number}", status))
    return rows


REGISTER_HEADER = ["permit_no", "barangay", "industry_type", "line_of_business", "business_name", "status"]


def _count_inserts(app):
    counter = {"insert": 0}

    def before(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("INSERT"):
            counter["insert"] += 1

    event.listen(db.engine, "before_cursor_execute", before)
    return counter, lambda: event.remove(db.engine, "before_cursor_execute", before)


def test_a_large_register_is_written_in_a_few_statements(app, lgu_user):
    from app.services.data_import_service import process_upload

    rows = _register_rows(n_per_barangay=100)
    path = _csv(rows, REGISTER_HEADER)
    try:
        with app.app_context():
            counter, stop = _count_inserts(app)
            try:
                count, status, message = process_upload("MARKET_DATA", path, "DTI", lgu_user)
            finally:
                stop()
    finally:
        os.unlink(path)

    assert status == "success", message
    assert len(rows) == 2000
    assert counter["insert"] <= 10, (
        f"{counter['insert']} INSERT statements for one upload -- each is a network round trip "
        f"to the hosted database, and one per row outlives the request limit"
    )


def test_a_register_becomes_one_zoning_row_per_barangay(app, lgu_user):
    from app.services.data_import_service import process_upload

    barangays = BARANGAY_NAMES[:5]
    rows = _register_rows(n_per_barangay=20, barangays=barangays)
    path = _csv(rows, REGISTER_HEADER)
    try:
        with app.app_context():
            _count, status, message = process_upload("MARKET_DATA", path, "DTI", lgu_user)
            uploaded = LguData.query.filter_by(uploaded_by=lgu_user).all()
            food = MarketData.query.filter_by(location=barangays[0], industry_type=FOOD, source="DTI").one()
    finally:
        os.unlink(path)

    assert status == "success", message
    assert sorted(row.barangay for row in uploaded) == sorted(barangays)
    for row in uploaded:
        assert row.permit_count == 18, "20 permits, 2 expired: 18 in force"
        assert row.closure_records == 2
        assert row.business_density is not None, "density is carried, never blanked"
    assert food.competitor_count == 9


def test_a_permit_list_sent_as_zoning_data_is_pointed_to_market_data(app, lgu_user):
    from app.services.data_import_service import process_upload

    path = _csv(_register_rows(n_per_barangay=3, barangays=BARANGAY_NAMES[:2]), REGISTER_HEADER)
    try:
        with app.app_context():
            count, status, message = process_upload("LGU_DATA", path, "DTI", lgu_user)
            saved = LguData.query.filter_by(uploaded_by=lgu_user).count()
    finally:
        os.unlink(path)

    assert status == "failed" and count == 0 and saved == 0
    assert "zoning_info" in message and "business_density" in message
    assert "Market Data" in message


def test_zoning_data_requires_its_values_on_every_row(app, lgu_user):
    from app.services.data_import_service import process_upload

    path = _csv([(BARANGAY_NAMES[0], "Commercial", 1.2), (BARANGAY_NAMES[1], "", 1.0),
                 (BARANGAY_NAMES[2], "Residential", "")],
                ["barangay", "zoning_info", "business_density"])
    try:
        with app.app_context():
            _count, status, message = process_upload("LGU_DATA", path, "CLUP", lgu_user)
    finally:
        os.unlink(path)

    assert status == "failed"
    assert "Row 3, column 'zoning_info'" in message
    assert "Row 4, column 'business_density'" in message


def test_zoning_data_lands_and_switches_on_the_city_wide_recommendations(app, lgu_user):
    from app.services.data_import_service import has_active_lgu_data, process_upload

    path = _csv([(name, "Commercial", 1.0 + index / 10, 50 + index)
                 for index, name in enumerate(BARANGAY_NAMES[:8])],
                ["barangay", "zoning_info", "business_density", "permit_count"])
    try:
        with app.app_context():
            for name in BARANGAY_NAMES[:8]:
                db.session.add(MarketData(industry_type=BUSINESS_TYPES[0], location=name,
                                          competitor_count=BARANGAY_NAMES.index(name) + 1,
                                          population_density=900, historical_success_rate=0.55,
                                          foot_traffic_index=45, average_rent=19000,
                                          source="Google Places API", date_recorded=date.today()))
            db.session.commit()
            assert has_active_lgu_data() is False
            _count, status, message = process_upload("LGU_DATA", path, "CLUP", lgu_user)
            assert status == "success", message
            assert has_active_lgu_data() is True
            row = LguData.query.filter_by(uploaded_by=lgu_user, barangay=BARANGAY_NAMES[3]).one()
            assert float(row.business_density) == pytest.approx(1.3)
            assert row.zoning_info == "Commercial" and row.permit_count == 53
    finally:
        os.unlink(path)

    client = app.test_client()
    client.post("/login", data={"email": "officer@flow.test", "password": "password123"})
    page = client.get("/lgu/dashboard").get_data(as_text=True)
    assert "No official LGU dataset is active" not in page
    payload = client.get(f"/api/lgu-recommendations?industry_type={BUSINESS_TYPES[0]}").get_json()
    assert payload["has_lgu_data"] is True and len(payload["top_opportunity"]) == 3


def test_a_market_upload_switches_on_the_city_wide_recommendations(app, lgu_user):
    from app.services.data_import_service import active_lgu_dataset_summary, has_active_lgu_data, process_upload

    path = _csv(_register_rows(n_per_barangay=8, barangays=BARANGAY_NAMES[:6]), REGISTER_HEADER)
    try:
        with app.app_context():
            _count, status, message = process_upload("MARKET_DATA", path, "DTI", lgu_user)
            assert status == "success", message
            assert has_active_lgu_data() is True
            assert active_lgu_dataset_summary()["barangays_covered"] == 6
    finally:
        os.unlink(path)

    client = app.test_client()
    client.post("/login", data={"email": "officer@flow.test", "password": "password123"})
    payload = client.get(f"/api/lgu-recommendations?industry_type={FOOD}").get_json()
    assert payload["has_lgu_data"] is True
    assert payload["top_opportunity"] and payload["top_saturated"]


def test_a_combined_file_fills_both_tables_under_either_target(app, lgu_user):
    from app.services.data_import_service import process_upload

    header = REGISTER_HEADER + ["business_density", "zoning_info", "effective_date"]
    rows = [row + (2.5, "Commercial", "2026-09-30") for row in
            _register_rows(n_per_barangay=10, barangays=BARANGAY_NAMES[:3])]
    for target in ("LGU_DATA", "MARKET_DATA"):
        path = _csv(rows, header)
        try:
            with app.app_context():
                _count, status, message = process_upload(target, path, "DTI", lgu_user)
                assert status == "success", (target, message)
                newest = LguData.query.filter_by(uploaded_by=lgu_user).order_by(LguData.lgu_id.desc()).first()
                assert newest.zoning_info == "Commercial"
                assert float(newest.business_density) == pytest.approx(2.5)
                assert newest.effective_date == date(2026, 9, 30)
                assert newest.permit_count == 9
        finally:
            os.unlink(path)
    with app.app_context():
        assert LguData.query.filter_by(uploaded_by=lgu_user).count() == 6
        assert MarketData.query.filter_by(source="DTI").count() == 12


def test_duplicate_permits_count_once_and_unknown_trades_are_reported(app, lgu_user):
    from app.services.data_import_service import process_upload

    rows = [("P-1", BARANGAY_NAMES[0], FOOD, "Bakery", "A", "active"),
            ("P-1", BARANGAY_NAMES[0], FOOD, "Bakery", "A", "active"),
            ("P-2", BARANGAY_NAMES[0], FOOD, "Bakery", "B", "active"),
            ("P-3", BARANGAY_NAMES[0], "Space tourism", "Rockets", "C", "active")]
    path = _csv(rows, REGISTER_HEADER)
    try:
        with app.app_context():
            _count, status, message = process_upload("MARKET_DATA", path, "DTI", lgu_user)
            food = MarketData.query.filter_by(location=BARANGAY_NAMES[0], industry_type=FOOD).one()
    finally:
        os.unlink(path)

    assert status == "success", message
    assert food.competitor_count == 2
    assert "counted once" in message
    assert "matches no PSIC section" in message and "row 5" in message


def test_a_row_with_an_unquoted_comma_is_reported(app, lgu_user):
    from app.services.data_import_service import process_upload

    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    handle.write("barangay,industry_type,status\n")
    handle.write(f"{BARANGAY_NAMES[0]},Agriculture, Forestry, and Fishing,active\n")
    handle.close()
    try:
        with app.app_context():
            _count, status, message = process_upload("MARKET_DATA", handle.name, "DTI", lgu_user)
    finally:
        os.unlink(handle.name)

    assert status == "failed"
    assert "must be in quotes" in message


def test_the_newest_zoning_row_wins_however_many_uploads_exist(app, lgu_user):
    from app.services.forecasting_service import _lgu_rows

    with app.app_context():
        for day in range(1, 6):
            db.session.add(LguData(source="DTI", barangay=BARANGAY_NAMES[0], business_density=day,
                                   upload_date=date(2026, 9, day), uploaded_by=lgu_user))
        newest = LguData(source="DTI", barangay=BARANGAY_NAMES[0], business_density=9,
                         upload_date=date(2026, 9, 10), uploaded_by=lgu_user)
        db.session.add(newest)
        db.session.commit()
        newest.archive(lgu_user, "test")
        db.session.commit()

        rows = _lgu_rows([BARANGAY_NAMES[0], BARANGAY_NAMES[1]])

    assert list(rows) == [BARANGAY_NAMES[0]]
    assert float(rows[BARANGAY_NAMES[0]].business_density) == 5.0, "an archived row is never read"


@pytest.mark.skipif(not os.path.exists(SAMPLE_XLSX), reason="sample workbook not bundled")
def test_the_sample_workbook_imports_as_zoning_data(app, lgu_user):
    from app.services.data_import_service import has_active_lgu_data, process_upload

    with app.app_context():
        counter, stop = _count_inserts(app)
        try:
            _count, status, message = process_upload("LGU_DATA", SAMPLE_XLSX, "DTI", lgu_user)
        finally:
            stop()
        assert status == "success", message
        assert has_active_lgu_data()
        assert LguData.query.filter_by(uploaded_by=lgu_user).count() == len(BARANGAY_NAMES)
        assert MarketData.query.filter_by(source="DTI").count() == len(BARANGAY_NAMES) * len(BUSINESS_TYPES)
    assert counter["insert"] <= 10


def _login(app, email="officer@flow.test"):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": "password123"})
    return client


def test_the_upload_page_marks_every_column_and_never_says_optional(app, lgu_user):
    from app.controllers.lgu_controller import IMPORTANCE, UPLOAD_COLUMNS

    page = _login(app).get("/lgu/government-data-upload").get_data(as_text=True)
    assert "Optional" not in page
    for columns in UPLOAD_COLUMNS.values():
        for _name, level, _meaning, _example in columns:
            assert level in IMPORTANCE
    zoning = {name for name, *_ in UPLOAD_COLUMNS["LGU_DATA"]}
    market = {name for name, *_ in UPLOAD_COLUMNS["MARKET_DATA"]}
    assert {"barangay", "zoning_info", "business_density"} <= zoning
    assert {"barangay", "industry_type", "status", "line_of_business"} <= market
    assert not ({"industry_type", "status", "business_name"} & zoning), "the zoning template carries no permit columns"
    assert not ({"zoning_info", "business_density"} & market), "the market template carries no zoning columns"
    assert "Zoning template (CSV)" in page and "Market template (CSV)" in page


def test_the_market_template_actually_imports(app, lgu_user):
    client = _login(app)
    response = client.get("/lgu/dataset-template/MARKET_DATA")
    assert "marketlens_market_data_template.csv" in response.headers["Content-Disposition"]
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    handle.write(response.get_data(as_text=True))
    handle.close()
    try:
        from app.services.data_import_service import process_upload

        with app.app_context():
            _count, status, message = process_upload("MARKET_DATA", handle.name, "DTI", lgu_user)
    finally:
        os.unlink(handle.name)
    assert status == "success", message


def test_the_upload_route_stages_the_file_and_changes_nothing_live(app, lgu_user):
    from app.models import UploadBatch

    client = _login(app)
    path = _csv(_register_rows(n_per_barangay=4, barangays=BARANGAY_NAMES[:3]), REGISTER_HEADER)
    try:
        with open(path, "rb") as handle:
            response = client.post("/lgu/government-data-upload", data={
                "dataset_type": "MARKET_DATA", "source": "DTI", "dataset_file": (handle, "permits.csv"),
            }, content_type="multipart/form-data", follow_redirects=True)
    finally:
        os.unlink(path)
    page = response.get_data(as_text=True)
    assert "Saved to the staging area as batch #" in page
    assert "City-wide recommendations now use this data" not in page
    assert "Your uploads" in page and "Waiting for review" in page
    with app.app_context():
        batch = UploadBatch.query.one()
        assert batch.status == "pending_review" and batch.uploaded_by == lgu_user
        assert batch.market_rows > 0 and batch.payload
        assert MarketData.query.filter_by(source="DTI").count() == 0, "nothing is live before it is published"


@pytest.fixture
def background_app(tmp_path):
    os.environ["TEST_DATABASE_URL"] = "sqlite:///" + str(tmp_path / "jobs.db")
    try:
        app = create_app("testing")
    finally:
        os.environ.pop("TEST_DATABASE_URL", None)
    app.config.update(UPLOAD_IN_BACKGROUND=True, UPLOAD_WAIT_SECONDS=0, WARM_AFTER_UPLOAD=False,
                      UPLOAD_FOLDER=str(tmp_path / "uploads"))
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        for email in ("officer@flow.test", "other@flow.test"):
            user = User(name="LGU Officer", email=email, role="LGU")
            user.set_password("password123")
            db.session.add(user)
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def test_a_long_upload_runs_in_the_background_and_reports_back(background_app):
    import re

    from app.services import upload_jobs

    client = _login(background_app)
    path = _csv(_register_rows(n_per_barangay=5, barangays=BARANGAY_NAMES[:4]), REGISTER_HEADER)
    upload_jobs._RUN_LOCK.acquire()
    try:
        with open(path, "rb") as handle:
            response = client.post("/lgu/government-data-upload", data={
                "dataset_type": "MARKET_DATA", "source": "DTI", "dataset_file": (handle, "permits.csv"),
            }, content_type="multipart/form-data")
        assert response.status_code == 302
        job_id = re.search(r"job=([0-9a-f]+)", response.headers["Location"]).group(1)
        waiting = client.get(response.headers["Location"]).get_data(as_text=True)
        assert 'id="uploadJobCard"' in waiting, "the page shows a progress card, not a hang"
        assert client.get(f"/lgu/upload-status/{job_id}").get_json()["state"] == "queued"
    finally:
        upload_jobs._RUN_LOCK.release()
        os.unlink(path)

    assert _login(background_app, "other@flow.test").get(
        f"/lgu/upload-status/{job_id}").status_code == 404, "a job is visible only to its owner"
    for _ in range(200):
        state = client.get(f"/lgu/upload-status/{job_id}").get_json()
        if state["state"] == "done":
            break
        time.sleep(0.05)
    assert state["status"] == "success", state

    page = client.get("/lgu/government-data-upload").get_data(as_text=True)
    assert "Saved to the staging area" in page
    assert "Saved to the staging area" not in client.get("/lgu/government-data-upload").get_data(as_text=True), (
        "the result is shown once"
    )
    from app.models import UploadBatch

    with background_app.app_context():
        batch = UploadBatch.query.one()
        assert state["batch_id"] == batch.id and batch.market_rows == 8
        assert MarketData.query.filter_by(source="DTI").count() == 0, "staged, not live"


ZONING_SAMPLE = os.path.join(SAMPLE_DIR, "MarketLens_LGU_Zoning_Sample_Tarlac_City.xlsx")
MARKET_SAMPLE = os.path.join(SAMPLE_DIR, "MarketLens_LGU_Market_Sample_Tarlac_City.xlsx")


@pytest.mark.skipif(not (os.path.exists(ZONING_SAMPLE) and os.path.exists(MARKET_SAMPLE)),
                    reason="split samples not bundled")
def test_the_split_samples_import_under_their_own_targets_only(app, lgu_user):
    from app.services.data_import_service import process_upload

    with app.app_context():
        _c, status, message = process_upload("MARKET_DATA", ZONING_SAMPLE, "DTI", lgu_user)
        assert status == "failed" and "Gov't / Zoning Data" in message
        _c, status, message = process_upload("LGU_DATA", MARKET_SAMPLE, "DTI", lgu_user)
        assert status == "failed" and "Market Data" in message

        _c, status, message = process_upload("LGU_DATA", ZONING_SAMPLE, "CLUP", lgu_user)
        assert status == "success", message
        _c, status, message = process_upload("MARKET_DATA", MARKET_SAMPLE, "DTI", lgu_user)
        assert status == "success", message

        assert MarketData.query.filter_by(source="DTI").count() == len(BARANGAY_NAMES) * len(BUSINESS_TYPES)
        newest = {row.barangay: row for row in
                  LguData.query.filter_by(uploaded_by=lgu_user).order_by(LguData.lgu_id).all()}
        assert len(newest) == len(BARANGAY_NAMES)
        assert sum(row.permit_count for row in newest.values()) == 9799
        assert all(row.zoning_info and row.business_density for row in newest.values()), (
            "the market upload kept the zoning figures the zoning upload set"
        )
