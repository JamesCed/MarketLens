"""
app/models/subcategory_market_data.py
---------------------------------------
ADDITIVE table. Direct-competitor counts one level below the industry:
how many BAKERIES are in Tibag, not how many food businesses.

Only MEASURED counts are stored here -- a live Google Places search for
the sub-category, or a count derived from an LGU permit register's
line-of-business column. When neither exists the system estimates, and
the estimate is computed on the fly and labelled as one; it is never
written here. A table that mixed estimates in with measurements could
not later be told apart, which is the same reason market_data carries a
`source` on every row.

One row per (industry_type, subcategory, location, source, date). The
newest row per source wins, and across sources the larger count wins --
the same max() reconciliation market_data uses, for the same reason:
both sources undercount, in different directions. See
app/services/subcategory_service.py.
"""

from datetime import date

from app.extensions import db


class SubcategoryMarketData(db.Model):
    __tablename__ = "subcategory_market_data"

    id = db.Column(db.Integer, primary_key=True)
    industry_type = db.Column(db.String(100), nullable=False)
    subcategory = db.Column(db.String(100), nullable=False)
    location = db.Column(db.String(150), nullable=False)
    competitor_count = db.Column(db.Integer, nullable=False)
    # Plain string rather than an Enum: 'Google Places API' or 'DTI'
    # (a permit register). A string needs no migration to add a source.
    source = db.Column(db.String(40), nullable=False)
    date_recorded = db.Column(db.Date, nullable=False, default=date.today)

    def __repr__(self):
        return (f"<SubcategoryMarketData {self.subcategory}/{self.industry_type}"
                f"@{self.location} = {self.competitor_count} ({self.source})>")
