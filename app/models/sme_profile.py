"""
app/models/sme_profile.py
--------------------------
Matches the real `sme_profile` table (dss_db_sme_profile.sql):

    sme_id (PK), user_id (FK -> user.user_id, CASCADE), business_name,
    industry_type, location, startup_capital, registration_date,
    employee_count, business_stage ENUM('startup','existing') DEFAULT 'startup',
    monthly_revenue_est, status ENUM('active','inactive') DEFAULT 'active'

`location` is a free-text field (e.g. a barangay name) -- there is no
barangays table in the given schema, so no foreign key here; the AI
engine and the map geocode this text on demand instead of looking up a
stored lat/lng.

TRASH IS AN ARCHIVE, NOT A DELETE
Removing a plan from the Home page moves it to Trash: the row is
stamped archived_at / archived_by / archive_reason (HiddenWhenArchived,
app/models/archive.py) and kept, with every forecast it ever produced.
The global archive filter then hides the PLAN from every ordinary
SmeProfile query -- the Home choice bar, Recommendations, the alert
sweeps, the registered-plan counts -- so a plan in Trash cannot keep
quietly feeding a page, and restoring it brings it back exactly as it
was. Before this a plan was hard-deleted, and the delete cascaded
through its forecast history and every bookmark on it: one mis-click
erased an owner's saved analysis.

What the filter does NOT reach: forecast_result and plan_save carry no
archive stamp of their own, so a query on those tables that does not
join to sme_profile still sees a trashed plan's rows -- today that is
the Settings "Saved Plans" count, the LGU dashboard's forecast tallies
and the trend pages' monthly forecast averages. Joining such a count to
SmeProfile is what makes it a count of live plans.

Reading a trashed plan is explicit and greppable:
.execution_options(include_archived=True) or
get_including_archived(SmeProfile, sme_id) -- the Trash dialog and the
restore route are the only callers. Relationship loads are not filtered
(see archive.py), so a historical forecast still resolves its plan.

CAPITAL
The column is still `startup_capital` -- renaming a column on a live
MySQL database is a destructive migration -- but everything the user
sees says "Capital", and capital is now REQUIRED (> 0) by the one plan
parser, app/services/plan_params.py, because the plan viability model
uses it (capital runway vs. the ramp-up period). `capital` below is the
read-side name. A legacy plan saved before capital was required reads
0 and is flagged on the Home page.
"""

from datetime import datetime, date
from app.extensions import db
from app.models.archive import HiddenWhenArchived


class SmeProfile(HiddenWhenArchived, db.Model):
    # Moved to Trash rather than deleted -- see the module docstring.
    __tablename__ = "sme_profile"

    sme_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="CASCADE"), nullable=False)
    business_name = db.Column(db.String(150), nullable=False)
    industry_type = db.Column(db.String(100), nullable=False)
    location = db.Column(db.String(150), nullable=False)
    startup_capital = db.Column(db.Numeric(12, 2))
    registration_date = db.Column(db.Date)
    employee_count = db.Column(db.Integer)
    business_stage = db.Column(db.Enum("startup", "existing", name="sme_business_stage"), nullable=False,
                                default="startup")
    # NO LONGER COLLECTED. Removed from every input form in the
    # revisions round: an entrepreneur planning a business does not have
    # a revenue figure yet, and asking for one invited a guess that then
    # drove the ROI estimate. The column stays (dropping it would be a
    # destructive migration on a live database) but nothing reads it.
    monthly_revenue_est = db.Column(db.Numeric(12, 2))
    status = db.Column(db.Enum("active", "inactive", name="sme_status"), nullable=False, default="active")

    # ---------------- Broader business parameters ----------------
    # Additive. "Food and Beverage" is too coarse to plan with: a
    # pandesal bakery in a barangay crowded with eateries faces very
    # little DIRECT competition, and the industry-level saturation
    # figure cannot see that. These four columns are what let it.
    #
    # subcategory     -- a key from app/ml/subcategories.py, e.g.
    #                    "bakery" under Food and Beverage. Drives the
    #                    direct-competition figure.
    # product_offering -- what the business actually sells or serves,
    #                    in the owner's own words.
    # innovation_idea -- optional. What makes this one different. The
    #                    AI assessment reads the words; the plan viability
    #                    model (app/ml/plan_model.py) sees only whether
    #                    there IS one (has_innovation_idea). Neither ever
    #                    changes the stage-1 market saturation score.
    # offering_details -- optional JSON list of {"item", "price"}: the
    #                    "sub-plan" (menu, price list). Its prices feed the
    #                    plan viability model's required-daily-sales check.
    subcategory = db.Column(db.String(100), nullable=True)
    product_offering = db.Column(db.Text, nullable=True)
    innovation_idea = db.Column(db.Text, nullable=True)
    offering_details = db.Column(db.Text, nullable=True)

    forecast_results = db.relationship(
        "ForecastResult", backref="sme_profile", lazy="dynamic", cascade="all, delete-orphan"
    )

    def years_in_operation(self):
        """0 for a not-yet-opened 'startup' plan; years since
        registration_date for an 'existing' business. Used as one of the
        Random Forest's input features."""
        if self.business_stage != "existing" or not self.registration_date:
            return 0.0
        delta_days = (date.today() - self.registration_date).days
        return round(max(0.0, delta_days / 365.25), 1)

    def latest_forecast(self):
        from app.models.forecast_result import ForecastResult

        return self.forecast_results.order_by(ForecastResult.forecast_date.desc(),
                                               ForecastResult.forecast_id.desc()).first()

    @property
    def capital(self):
        """The plan's capital in pesos, as a float -- 0.0 for a legacy plan
        saved before capital was required. The column keeps its original
        name (startup_capital); this is the name every screen, the plan
        viability model and the AI narration use."""
        return float(self.startup_capital or 0)

    @property
    def capital_missing(self):
        """True for a plan with no capital on file (saved before capital
        was required). It still forecasts -- with capital adequacy 0 --
        but the Home page asks the owner to add it."""
        return self.capital <= 0

    def to_dict(self):
        """JSON shape returned by /api/my-plans and by the plan-update
        route (the Home page's Edit dialog reads `success` and reloads).
        `capital` is the current name; `startup_capital` stays for any
        older caller still reading it."""
        return {
            "sme_id": self.sme_id,
            "business_name": self.business_name,
            "industry_type": self.industry_type,
            "location": self.location,
            "capital": self.capital,
            "startup_capital": float(self.startup_capital) if self.startup_capital is not None else 0,
            "registration_date": self.registration_date.isoformat() if self.registration_date else None,
            "employee_count": self.employee_count,
            "business_stage": self.business_stage,
            "subcategory": self.subcategory,
            "subcategory_label": self.subcategory_label,
            "product_offering": self.product_offering,
            "innovation_idea": self.innovation_idea,
            "offering_items": self.offering_items,
            "status": self.status,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
        }

    @property
    def subcategory_label(self):
        if not self.subcategory:
            return None
        from app.ml.subcategories import subcategory_label

        return subcategory_label(self.industry_type, self.subcategory)

    @property
    def offering_items(self):
        """offering_details parsed back into a list of {"item", "price"}.
        A malformed value reads as an empty list rather than raising: this
        is optional context for the recommendation, never something a page
        should fail over."""
        import json

        if not self.offering_details:
            return []
        try:
            items = json.loads(self.offering_details)
        except (TypeError, ValueError):
            return []
        return [i for i in items if isinstance(i, dict) and i.get("item")] if isinstance(items, list) else []

    def __repr__(self):
        return f"<SmeProfile {self.industry_type}@{self.location} user={self.user_id}>"
