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
    # The day-scoped cap on live Google Places lookups. Places API
    # (New) bills per call, and with PLACES_LIVE_FETCH=true on a public
    # URL every signed-in visitor can trigger them -- so the
    # per-request cap in forecasting_service bounds the wrong quantity
    # on its own. 0 means unlimited. This bounds what the APP spends; a
    # quota set in the Google Cloud Console bounds what the KEY can
    # spend, which also covers anything using it outside this app.
    "places_daily_call_budget": (
        "500",
        "Maximum live Google Places lookups per day (0 = unlimited). Reaching it is not an error: "
        "the app serves the competitor counts already on file, exactly as when live fetching is off.",
    ),
    "places_calls_today": ("0", "Live Google Places lookups spent so far today (maintained by the app)"),
    "places_calls_day": ("", "The date places_calls_today refers to (maintained by the app)"),
    # The community is the project's Discord server; the footer's
    # "Community Forum" link opens this invite. See
    # app/controllers/community_controller.py.
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
    # The two ASSUMPTIONS the Plan Viability Model (stage 2 of the
    # forecast, app/services/plan_forecast_service.py) needs that no plan
    # form collects. Defaults and their sources are in app/ml/constants.py;
    # they are editable here so the next wage order does not need a
    # redeploy. Neither requires retraining: the model sees the wage only
    # through monthly fixed cost / capital runway, and the margin only
    # through required daily sales. Descriptions stay under 255
    # characters (the column's width).
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
    def get(key, default=None):
        row = SystemSetting.query.get(key)
        return row.setting_value if row else default

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

    # One-off upgrades for installs created before a default changed
    # meaning. Each entry is key -> (old_default, new_default): the
    # stored value is rewritten ONLY if it is still byte-for-byte the
    # old default, i.e. nobody has deliberately tuned it. A value an
    # Admin actually chose is never touched.
    _DEFAULT_UPGRADES = {
        # "20" used to mean "keep the first 20 results"; Places lookups
        # are now unpaged-and-unlimited (0), so a barangay reports the
        # businesses it really has instead of flat-lining at 20. An
        # install that still carries the old default gets the new one.
        "places_max_results": ("20", "0"),
        # use_llm_recommendations used to be here too ("false" -> "true",
        # from when the LLM recommendation became the default). It was
        # removed because, on a two-valued switch, "still the old
        # default" and "the Admin turned it off" are the same string.
        # ensure_defaults() runs at every boot AND on every visit to
        # Admin > System Settings, so unticking "Use an LLM..." was
        # undone by the redirect that followed the save. The switch
        # could not be turned off, although the page and
        # Reference/FORECAST_MODEL.md promise rule-based text when it
        # is. Every install that booted since the default changed has
        # already been upgraded, so the entry had nothing left to do
        # except that.
    }

    @staticmethod
    def ensure_defaults():
        """Insert any missing default settings, and migrate values that
        are still sitting on a superseded default. Safe to call every
        startup."""
        changed = False
        for key, (value, description) in DEFAULT_SETTINGS.items():
            row = SystemSetting.query.get(key)
            if row is None:
                db.session.add(SystemSetting(setting_key=key, setting_value=value, description=description))
                changed = True
                continue

            upgrade = SystemSetting._DEFAULT_UPGRADES.get(key)
            if upgrade and str(row.setting_value).strip() == upgrade[0]:
                row.setting_value = upgrade[1]
                row.description = description
                changed = True

            # The DESCRIPTION is the code's own note on what a setting
            # does -- no screen edits it, and nothing passes one to set()
            # -- so an install created before a setting's meaning changed
            # gets the current note, while its VALUE (the Admin's choice)
            # is left alone. Without this, use_llm_recommendations kept
            # saying "GPT-4o-mini via OpenRouter, or Claude" on every
            # existing install after the forecast narration went Gemini
            # first.
            if row.description != description:
                row.description = description
                changed = True
        if changed:
            db.session.commit()
