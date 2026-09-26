"""
app/models/plan_save.py
-------------------------
ADDITIVE table backing the "Save to My Plans" button on the
Recommendations page. References user.user_id and
forecast_result.forecast_id only -- doesn't alter either table.
"""

from datetime import datetime
from app.extensions import db


class PlanSave(db.Model):
    __tablename__ = "plan_saves"
    __table_args__ = (db.UniqueConstraint("user_id", "forecast_result_id", name="uq_plan_saves"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="CASCADE"), nullable=False)
    forecast_result_id = db.Column(
        db.Integer, db.ForeignKey("forecast_result.forecast_id", ondelete="CASCADE"), nullable=False
    )
    saved_at = db.Column(db.DateTime, default=datetime.utcnow)
