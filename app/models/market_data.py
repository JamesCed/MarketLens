"""
app/models/market_data.py
--------------------------
Matches the real `market_data` table (dss_db_market_data.sql):

    market_id (PK), industry_type, location, competitor_count,
    population_density, historical_success_rate, foot_traffic_index,
    average_rent, source ENUM('Google Places API','Manual','PSA','DTI'),
    date_recorded

One row = a snapshot of market conditions for one industry+location on
one date. The AI engine reads the most recent row for a given
industry_type/location (creating one via the Google Places API, or a
clearly-flagged simulated estimate, if none exists yet).
"""

from app.extensions import db
from app.models.archive import HiddenWhenArchived


class MarketData(HiddenWhenArchived, db.Model):
    # Archived rather than deleted -- see app/models/archive.py. Deleting
    # used to CASCADE through forecast_result.market_id and silently remove
    # every forecast ever built on the row.
    __tablename__ = "market_data"

    market_id = db.Column(db.Integer, primary_key=True)
    industry_type = db.Column(db.String(100), nullable=False)
    location = db.Column(db.String(150), nullable=False)
    competitor_count = db.Column(db.Integer)
    population_density = db.Column(db.Numeric(10, 2))
    historical_success_rate = db.Column(db.Numeric(5, 2))  # 0.00-1.00 fraction
    foot_traffic_index = db.Column(db.Numeric(6, 2))
    average_rent = db.Column(db.Numeric(10, 2))
    source = db.Column(
        db.Enum("Google Places API", "Manual", "PSA", "DTI", name="market_data_source"),
        nullable=False,
    )
    date_recorded = db.Column(db.Date, nullable=False)

    forecast_results = db.relationship("ForecastResult", backref="market_data", lazy="dynamic")

    def __repr__(self):
        return f"<MarketData {self.industry_type}@{self.location} {self.date_recorded}>"
