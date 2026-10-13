"""
app/models/system_setting.py
------------------------------
SYSTEM_SETTINGS table -- Admin Module "system configuration and
settings management". A simple key/value store so an Admin can tune
the AI engine (clustering K, MSI weights, early-warning threshold,
the plan forecast's wage and gross-margin assumptions, whether to use
the optional LLM for recommendation text) without touching code or
redeploying.
"""

from datetime import datetime
from app.extensions import db

DEFAULT_SETTINGS = {
    "kmeans_n_clusters": ("4", "Number of K-Means clusters (saturation zones: Low/Moderate/High/Saturated)"),
    "msi_weight_competitor_density": ("0.45", "MSI weight w1 for Competitor Density Score"),
    "msi_weight_demand_trend": ("0.35", "MSI weight w2 for Demand Trend Score"),
    "msi_weight_sociodemographic": ("0.20", "MSI weight w3 for Socio-Demographic Score"),
    "saturation_alert_threshold": ("75", "Saturation Index (0-100) above which an early-warning notification fires"),
    "places_max_results": ("0", "Max competitor results kept per Google Places text search (0 = unlimited)"),
    "places_daily_call_budget": (
        "500",
        "Maximum live Google Places lookups per day (0 = unlimited). Reaching it is not an error: "
        "the app serves the competitor counts already on file, exactly as when live fetching is off.",
    ),
    "places_calls_today": ("0", "Live Google Places lookups spent so far today (maintained by the app)"),
    "places_calls_day": ("", "The date places_calls_today refers to (maintained by the app)"),
    "community_invite_url": (
        "https://discord.gg/4EBtST2Bk",
        "Invite link to the MarketLens Discord community (opened by the footer's Community Forum link)",
    ),
    "use_llm_recommendations": (
        "true",
        "true = an LLM writes the recommendation text and explains the forecast (Gemini first for the forecast, "
        "then LLM_PROVIDER, then the other; keys in .env), false = the built-in rule-based generator. "
        "Figures always come from the trained models.",
    ),
    "plan_daily_wage_php": (
        "590",
        "Daily wage per employee (PHP) used for plan payroll = employees x wage x 26 days. Default: DOLE "
        "Wage Order RBIII-26 (2nd tranche, eff. 16 Apr 2026), Tarlac retail & service, P590/day. P1-P100,000.",
    ),
    "plan_gross_margin": (
        "0.40",
        "Assumed gross margin (share of each sale left after cost of goods, 0.05-0.95) used to turn a plan's "
        "prices into required daily sales. An assumption, not a measurement: no plan form collects costs.",
    ),
}


class SystemSetting(db.Model):
    __tablename__ = "system_settings"

    setting_key = db.Column(db.String(100), primary_key=True)
    setting_value = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(255))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @staticmethod
    def all_values():
        """Every setting as {key: value}, read in ONE query per request.

        Pages ask for several settings each (Admin > System Settings
        asked for 16 one at a time); a single SELECT of a table this small
        is cheaper than even two primary-key lookups over the network.
        Any write clears the per-request memo, so set() followed by get()
        in the same request still reads the new value."""
        from app.utils.request_memo import request_memo

        return request_memo(
            "system_settings",
            lambda: {row.setting_key: row.setting_value for row in SystemSetting.query.all()},
        )

    @staticmethod
    def get(key, default=None):
        value = SystemSetting.all_values().get(key)
        return value if value is not None else default

    @staticmethod
    def get_float(key, default=0.0):
        value = SystemSetting.get(key)
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def get_bool(key, default=False):
        value = SystemSetting.get(key)
        if value is None:
            return default
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    @staticmethod
    def set(key, value, description=None):
        row = SystemSetting.query.get(key)
        if row is None:
            row = SystemSetting(setting_key=key, setting_value=str(value), description=description)
            db.session.add(row)
        else:
            row.setting_value = str(value)
            if description:
                row.description = description
        db.session.commit()
        return row

    _DEFAULT_UPGRADES = {
        "places_max_results": ("20", "0"),
    }

    @staticmethod
    def ensure_defaults():
        """Insert any missing default settings, and migrate values that
        are still sitting on a superseded default. Safe to call every
        startup."""
        changed = False
        existing = {row.setting_key: row for row in SystemSetting.query.all()}
        for key, (value, description) in DEFAULT_SETTINGS.items():
            row = existing.get(key)
            if row is None:
                db.session.add(SystemSetting(setting_key=key, setting_value=value, description=description))
                changed = True
                continue

            upgrade = SystemSetting._DEFAULT_UPGRADES.get(key)
            if upgrade and str(row.setting_value).strip() == upgrade[0]:
                row.setting_value = upgrade[1]
                row.description = description
                changed = True

            if row.description != description:
                row.description = description
                changed = True
        if changed:
            db.session.commit()
