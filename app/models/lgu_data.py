"""
app/models/lgu_data.py
------------------------
Matches the real `lgu_data` table (dss_db_lgu_data.sql):

    lgu_id (PK), source ENUM('DTI','CLUP','Other'), zoning_info,
    closure_records, barangay, permit_count, business_density,
    effective_date, upload_date, uploaded_by (FK -> user.user_id, RESTRICT)

Note this table stores the *parsed government record itself* (permit
counts, closures, zoning notes, a business-density figure) per
barangay -- it does NOT track uploaded-file metadata (no filename/size/
status columns in the given schema). The Gov't Data Upload page inserts
one row per barangay per uploaded file, straight into this table --
see app/services/data_import_service.py.
"""

from datetime import datetime
from app.extensions import db


class LguData(db.Model):
    __tablename__ = "lgu_data"

    lgu_id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.Enum("DTI", "CLUP", "Other", name="lgu_data_source"), nullable=False)
    zoning_info = db.Column(db.Text)
    closure_records = db.Column(db.Integer)
    barangay = db.Column(db.String(100), nullable=False)
    permit_count = db.Column(db.Integer)
    business_density = db.Column(db.Numeric(10, 2))
    effective_date = db.Column(db.Date)
    upload_date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="RESTRICT"), nullable=False)

    forecast_results = db.relationship("ForecastResult", backref="lgu_data", lazy="dynamic")

    def __repr__(self):
        return f"<LguData {self.barangay} ({self.source})>"
