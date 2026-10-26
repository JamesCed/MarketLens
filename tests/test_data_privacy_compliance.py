"""
tests/test_data_privacy_compliance.py
---------------------------------------
The Data Privacy Act of 2012 (RA 10173) safeguards around LGU uploads.

  * Transparency: the upload page says what is collected, why, who can
    see it and how long it is kept, and names who to contact.
  * Proportionality: a file with personal information (owner names,
    contact details, ID numbers) is refused; only totals are stored.
  * Retention: uploaded rows and audit entries are deleted after their
    configured periods -- except rows a saved forecast still uses.
  * Security: a file's contents are checked, not only its extension,
    and the PII encryption key can be rotated without losing data.
"""

import csv
import io
import os
import tempfile
import zipfile
from datetime import date, datetime, timedelta

import pytest

from app import create_app
from app.extensions import db
from app.ml.seed_data import BARANGAY_NAMES
from app.models import AuditLog, ForecastResult, LguData, MarketData, SmeProfile, SubcategoryMarketData, SystemSetting
from app.models.user import User

FOOD = "Food and Beverage"


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
        user = User(name="LGU Officer", email="officer@privacy.test", role="LGU")
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


def _upload(app, lgu_user, path, target="MARKET_DATA"):
    from app.services.data_import_service import process_upload

    try:
        with app.app_context():
            return process_upload(target, path, "DTI", lgu_user)
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_the_upload_page_carries_a_privacy_notice(app, lgu_user):
    client = app.test_client()
    client.post("/login", data={"email": "officer@privacy.test", "password": "password123"})
    page = client.get("/lgu/government-data-upload").get_data(as_text=True)
    assert 'id="uploadPrivacyNotice"' in page
    for heading in ("What is collected", "What is kept", "Purpose", "Who can see it", "How long",
                    "remove personal information"):
        assert heading in page, heading
    assert "1095 days after upload" in page and "365 days" in page


def test_the_public_notice_covers_uploads_and_the_dpo(app):
    app.jinja_env.globals["DPO"] = {"name": "Juana Cruz", "office": "City Legal Office",
                                    "email": "dpo@tarlaccity.test", "phone": ""}
    page = app.test_client().get("/privacy").get_data(as_text=True)
    assert 'id="lgu-data"' in page
    assert "Juana Cruz" in page and "dpo@tarlaccity.test" in page
    assert "National Privacy Commission" in page


@pytest.mark.parametrize("column", ["owner_name", "Owner Name", "proprietor", "contact_no", "Mobile Number",
                                    "email", "TIN", "birthdate", "home_address", "first_name"])
def test_a_file_with_personal_columns_is_refused(app, lgu_user, column):
    path = _csv([(BARANGAY_NAMES[0], FOOD, "active", "x")], ["barangay", "industry_type", "status", column])
    count, status, message = _upload(app, lgu_user, path)
    assert status == "failed" and count == 0
    assert "personal information" in message
    with app.app_context():
        assert MarketData.query.count() == 0 and LguData.query.count() == 0


def test_contact_details_hidden_in_another_column_are_refused(app, lgu_user):
    path = _csv([(BARANGAY_NAMES[0], FOOD, "active", "Bakery - call 0917 123 4567")],
                ["barangay", "industry_type", "status", "line_of_business"])
    _count, status, message = _upload(app, lgu_user, path)
    assert status == "failed"
    assert "Row 2, column 'line_of_business'" in message and "mobile number" in message


def test_business_names_and_permit_numbers_are_never_stored(app, lgu_user):
    path = _csv([("TC-0001", BARANGAY_NAMES[0], FOOD, "Bakery", "Aling Nena Bakeshop", "active")],
                ["permit_no", "barangay", "industry_type", "line_of_business", "business_name", "status"])
    _count, status, message = _upload(app, lgu_user, path)
    assert status == "success", message
    with app.app_context():
        stored = " ".join(str(value) for model in (LguData, MarketData, SubcategoryMarketData)
                          for row in model.query.all() for value in vars(row).values())
    assert "Aling Nena" not in stored and "TC-0001" not in stored


def _write(path, data, mode="wb"):
    with open(path, mode) as handle:
        handle.write(data)
    return path


@pytest.mark.parametrize("payload, expected", [
    (b"MZ\x90\x00 this is a program", "a Windows program"),
    (b"%PDF-1.7 not a csv", "a PDF document"),
    (b"<!DOCTYPE html><html></html>", "web page"),
    (b"barangay,industry_type\x00\x01\x02", "binary data"),
])
def test_a_renamed_file_is_refused_by_its_contents(app, lgu_user, tmp_path, payload, expected):
    path = _write(str(tmp_path / "register.csv"), payload)
    _count, status, message = _upload(app, lgu_user, path)
    assert status == "failed" and expected in message


def test_a_fake_xlsx_is_refused(app, lgu_user, tmp_path):
    path = _write(str(tmp_path / "register.xlsx"), b"barangay,industry_type\nTibag,Food and Beverage\n")
    _count, status, message = _upload(app, lgu_user, path)
    assert status == "failed" and "not an Excel workbook" in message


def test_a_workbook_with_macros_is_refused(app, lgu_user, tmp_path):
    path = str(tmp_path / "register.xlsx")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/vbaProject.bin", b"\x00" * 64)
    _count, status, message = _upload(app, lgu_user, path)
    assert status == "failed" and "macros" in message


def test_a_zip_bomb_is_refused(app, lgu_user, tmp_path):
    path = str(tmp_path / "register.xlsx")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/worksheets/sheet1.xml", b"0" * (40 * 1024 * 1024))
    _count, status, message = _upload(app, lgu_user, path)
    assert status == "failed" and "far more data" in message


def test_a_real_workbook_passes_the_content_check(tmp_path):
    from openpyxl import Workbook

    from app.utils.upload_checks import check_upload_content

    path = str(tmp_path / "ok.xlsx")
    workbook = Workbook()
    workbook.active.append(["barangay", "industry_type"])
    workbook.save(path)
    assert check_upload_content(path) is None


def _seed_old_upload(app, lgu_user, days_old):
    with app.app_context():
        old = date.today() - timedelta(days=days_old)
        lgu = LguData(source="DTI", barangay=BARANGAY_NAMES[0], business_density=1.2, zoning_info="Commercial",
                      upload_date=old, uploaded_by=lgu_user)
        market = MarketData(industry_type=FOOD, location=BARANGAY_NAMES[0], competitor_count=4,
                            source="DTI", date_recorded=old)
        places = MarketData(industry_type=FOOD, location=BARANGAY_NAMES[1], competitor_count=4,
                            source="Google Places API", date_recorded=old)
        sub = SubcategoryMarketData(industry_type=FOOD, subcategory="bakery", location=BARANGAY_NAMES[0],
                                    competitor_count=2, source="DTI", date_recorded=old)
        log = AuditLog(action="login", details="x", created_at=datetime.utcnow() - timedelta(days=days_old))
        db.session.add_all([lgu, market, places, sub, log])
        db.session.commit()
        return lgu.lgu_id, market.market_id


def test_retention_deletes_expired_uploads_and_audit_entries(app, lgu_user):
    from app.services.retention_service import apply_retention

    _seed_old_upload(app, lgu_user, days_old=1200)
    with app.app_context():
        counts = apply_retention()
        assert counts == {"lgu_data": 1, "market_data": 1, "subcategory_market_data": 1, "audit_logs": 1,
                          "upload_batches": 0, "staged_expired": 0}
        assert MarketData.query.filter_by(source="Google Places API").count() == 1, "only uploaded rows expire"
        assert "removed" in SystemSetting.get("retention_last_run")
        assert AuditLog.query.filter_by(action="retention_purge").count() == 1


def test_retention_keeps_what_has_not_expired(app, lgu_user):
    from app.services.retention_service import apply_retention

    _seed_old_upload(app, lgu_user, days_old=100)
    with app.app_context():
        assert apply_retention() == {"lgu_data": 0, "market_data": 0, "subcategory_market_data": 0, "audit_logs": 0,
                                     "upload_batches": 0, "staged_expired": 0}


def test_retention_follows_the_admin_setting(app, lgu_user):
    from app.services.retention_service import apply_retention

    _seed_old_upload(app, lgu_user, days_old=100)
    with app.app_context():
        SystemSetting.set("upload_retention_days", "60")
        SystemSetting.set("audit_log_retention_days", "90")
        counts = apply_retention()
    assert counts["lgu_data"] == 1 and counts["audit_logs"] == 1


def test_retention_never_deletes_a_saved_forecast(app, lgu_user):
    from app.services.retention_service import apply_retention

    lgu_id, market_id = _seed_old_upload(app, lgu_user, days_old=1200)
    with app.app_context():
        owner = User(name="Owner", email="owner@privacy.test", role="SME")
        owner.set_password("password123")
        db.session.add(owner)
        db.session.commit()
        profile = SmeProfile(user_id=owner.user_id, business_name="Kape", industry_type=FOOD,
                             location=BARANGAY_NAMES[0])
        db.session.add(profile)
        db.session.commit()
        db.session.add(ForecastResult(sme_id=profile.sme_id, market_id=market_id, lgu_id=lgu_id,
                                      saturation_index=40, viability_score=6, confidence_level=80,
                                      model_version="rf_v1"))
        db.session.commit()
        counts = apply_retention()
        assert counts["lgu_data"] == 0 and counts["market_data"] == 0
        assert ForecastResult.query.count() == 1


def test_admin_can_set_the_retention_periods(app):
    with app.app_context():
        admin = User(name="Admin", email="admin@privacy.test", role="Admin")
        admin.set_password("password123")
        db.session.add(admin)
        db.session.commit()
    client = app.test_client()
    client.post("/login", data={"email": "admin@privacy.test", "password": "password123"})
    assert "Data retention" in client.get("/admin/settings").get_data(as_text=True)
    client.post("/admin/settings", data={"upload_retention_days": "730", "audit_log_retention_days": "5"})
    with app.app_context():
        assert SystemSetting.get("upload_retention_days") == "730"
        assert SystemSetting.get("audit_log_retention_days") == "365", "5 days is below the 30-day floor"


def test_the_pii_key_can_be_rotated_without_losing_details(app):
    from sqlalchemy import text

    from app.utils.privacy import _fernet_cache, rotate_encrypted_values

    with app.app_context():
        app.config["PII_ENCRYPTION_KEY"] = "old-key-" + "x" * 40
        _fernet_cache.clear()
        user = User(name="Juan", email="juan@privacy.test", role="SME")
        user.set_password("password123")
        user.contact_number = "09171234567"
        user.address_line = "12 Mabini St."
        db.session.add(user)
        db.session.commit()
        user_id = user.user_id

        app.config["PII_ENCRYPTION_KEY"] = "new-key-" + "y" * 40
        app.config["PII_ENCRYPTION_PREVIOUS_KEYS"] = "old-key-" + "x" * 40
        result = rotate_encrypted_values()
        assert result == {"rotated": 2, "unreadable": 0}
        assert rotate_encrypted_values()["rotated"] == 0, "a second pass has nothing left to do"

        app.config["PII_ENCRYPTION_PREVIOUS_KEYS"] = ""
        db.session.expire_all()
        reloaded = db.session.get(User, user_id)
        assert reloaded.contact_number == "09171234567", "readable with the new key alone"
        assert reloaded.address_line == "12 Mabini St."
        raw = db.session.execute(text('SELECT contact_number_enc FROM "user" WHERE user_id = :i'),
                                 {"i": user_id}).scalar()
        assert raw.startswith("enc1:") and "0917" not in raw
