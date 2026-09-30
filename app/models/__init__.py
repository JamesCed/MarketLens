"""
app/models/__init__.py
------------------------
Re-exports every model so the rest of the app can simply do:

    from app.models import User, SmeProfile, MarketData, LguData, ForecastResult, ...

Importing them all here also guarantees SQLAlchemy sees every model
class before db.create_all() runs. The five core models below map 1:1
to the five tables you supplied (dss_db_*.sql); Notification, PlanSave,
AuditLog, SystemSetting and IndustryMigrationLog are additive tables
layered on top (see each file's docstring) that don't alter your five
given tables.

(An earlier version of this project also had a CompetitorSnapshot
model/table -- removed. Real competitor data is now fetched directly,
live, via the Google Places API (New) straight into the real
`market_data` table's `competitor_count` column, per industry+barangay,
the moment the AI engine needs it -- see
app/services/forecasting_service.find_or_create_market_data() and
app/services/places_service.py. There's no separate "competitor
snapshots" table, and no separate bulk-seeding script, anymore.)
"""

from app.models.user import User
from app.models.sme_profile import SmeProfile
from app.models.market_data import MarketData
from app.models.lgu_data import LguData
from app.models.forecast_result import ForecastResult
from app.models.notification import Notification
from app.models.plan_save import PlanSave
from app.models.audit_log import AuditLog
from app.models.system_setting import SystemSetting
from app.models.industry_migration_log import IndustryMigrationLog
from app.models.subcategory_market_data import SubcategoryMarketData
# Community forum tables (channels, posts, comments, reports). Imported
# for its side effect of registering the models with SQLAlchemy; the
# forum code imports them from app.models.forum directly.
from app.models import forum as _forum  # noqa: F401

__all__ = [
    "User",
    "SmeProfile",
    "MarketData",
    "LguData",
    "ForecastResult",
    "Notification",
    "PlanSave",
    "AuditLog",
    "SystemSetting",
    "IndustryMigrationLog",
    "SubcategoryMarketData",
]
