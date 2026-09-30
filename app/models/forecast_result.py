"""
app/models/forecast_result.py
-------------------------------
Matches the real `forecast_result` table (dss_db_forecast_result.sql):

    forecast_id (PK), sme_id (FK -> sme_profile.sme_id, CASCADE, NOT NULL),
    market_id (FK -> market_data.market_id, CASCADE, NOT NULL),
    lgu_id (FK -> lgu_data.lgu_id, CASCADE, NOT NULL),
    viability_score DECIMAL(5,2), input_industry_type, input_location,
    saturation_index DECIMAL(5,2), recommendation TEXT,
    confidence_level DECIMAL(5,2), model_version VARCHAR(20), forecast_date DATE

Two things worth calling out because they shaped app/services/forecasting_service.py:

1. sme_id/market_id/lgu_id are all NOT NULL -- every forecast_result row
   MUST be tied to one real SmeProfile, one real MarketData snapshot, and
   one real LguData record. There is no "anonymous"/aggregate forecast.
   That's why the forecasting engine has two entry points: an ephemeral
   `compute_scores()` (no DB write -- used for the Saturation Map / Trend
   Reports, which aren't tied to a single SME) and
   `generate_forecast_for_profile()` (writes a row here, used by the
   Home page / Recommendations, which ARE tied to a specific SME plan).

2. There is only ONE text column (`recommendation`) -- no separate JSON
   columns for "reasons"/"risks" bullet lists, and no `cluster_label`
   column. recommendation_service.py formats headline + reasons + risks
   into one readable text block for this column, and `cluster_label`
   below is derived live from `saturation_index` (0-100) using the fixed
   threshold cut points in app/ml/constants.py CLUSTER_THRESHOLDS --
   which are deliberately NOT the K-Means centroids; see the note there.
"""

from datetime import date
from app.extensions import db
from app.ml.constants import CLUSTER_THRESHOLDS, CLUSTER_LABELS_ORDERED


class ForecastResult(db.Model):
    __tablename__ = "forecast_result"

    forecast_id = db.Column(db.Integer, primary_key=True)
    sme_id = db.Column(db.Integer, db.ForeignKey("sme_profile.sme_id", ondelete="CASCADE"), nullable=False)
    market_id = db.Column(db.Integer, db.ForeignKey("market_data.market_id", ondelete="CASCADE"), nullable=False)
    lgu_id = db.Column(db.Integer, db.ForeignKey("lgu_data.lgu_id", ondelete="CASCADE"), nullable=False)

    viability_score = db.Column(db.Numeric(5, 2))          # 0-10 scale, shown to users
    input_industry_type = db.Column(db.String(100))
    input_location = db.Column(db.String(150))
    saturation_index = db.Column(db.Numeric(5, 2))          # 0-100 percentage
    recommendation = db.Column(db.Text)
    confidence_level = db.Column(db.Numeric(5, 2))          # 0-100, from RF tree agreement
    model_version = db.Column(db.String(20))
    forecast_date = db.Column(db.Date, nullable=False, default=date.today)

    notifications = db.relationship("Notification", backref="forecast_result", lazy="dynamic")
    plan_saves = db.relationship("PlanSave", backref="forecast_result", lazy="dynamic", cascade="all, delete-orphan")

    @property
    def saturation_percent(self):
        return round(float(self.saturation_index or 0), 1)

    @property
    def is_oversaturated(self):
        return float(self.saturation_index or 0) > 75.0

    @property
    def cluster_label(self):
        """Low / Moderate / High / Saturated, derived from saturation_index
        using the fixed cut points in app/ml/constants.py CLUSTER_THRESHOLDS
        -- not a stored column, and not the K-Means centroids (see the note
        in app/ml/train_model.py for why the thresholds are fixed)."""
        value = float(self.saturation_index or 0)
        for threshold, label in zip(CLUSTER_THRESHOLDS, CLUSTER_LABELS_ORDERED):
            if value <= threshold:
                return label
        return CLUSTER_LABELS_ORDERED[-1]

    def to_dict(self):
        # Local import -- recommendation_service.py itself imports from
        # app.models (SystemSetting), so importing it at module load
        # time here would risk a circular import; importing it lazily,
        # only when to_dict() actually runs, avoids that entirely.
        from app.services.recommendation_service import parse_recommendation

        return {
            "id": self.forecast_id,
            "sme_id": self.sme_id,
            "market_id": self.market_id,
            "lgu_id": self.lgu_id,
            "industry_type": self.input_industry_type,
            "location": self.input_location,
            "saturation_index": float(self.saturation_index or 0),
            "viability_score": float(self.viability_score or 0),
            "confidence_level": float(self.confidence_level or 0),
            "cluster_label": self.cluster_label,
            # Structured -- {headline, opportunity_type, summary,
            # reasons, risks, generated_by} -- not the raw JSON/legacy
            # text stored in the recommendation column. See
            # recommendation_service.py.
            "recommendation": parse_recommendation(self.recommendation),
            "model_version": self.model_version,
            "forecast_date": self.forecast_date.isoformat() if self.forecast_date else None,
        }

    def __repr__(self):
        return f"<ForecastResult {self.input_industry_type}@{self.input_location} MSI={self.saturation_index}>"
