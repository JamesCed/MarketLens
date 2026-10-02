"""
app/services/industry_migration.py
-------------------------------------
One-time migration of every stored `industry_type` value onto the PSIC
top-level sections now listed in app/ml/constants.py BUSINESS_TYPES,
and removal of the MICRO-business rows this system no longer counts.

WHY THIS EXISTS
This DSS targets SMEs. Two earlier versions of the industry list were
in use before that scope was tightened:

  1. an original 24-entry list of broad, loosely-PSIC-shaped categories
     ("Retail", "Service", "Health & Wellness"...); and
  2. 22 hyper-local MICRO-business categories added on top of those
     ("Sari-Sari Stores", "Carinderias & Eateries", "Food Carts &
     Street Food Stalls"...).

Both are gone. `industry_type` is free text in the real schema (no
foreign key, no enum -- see app/models/market_data.py), so nothing in
the database stopped those old strings from simply staying there
forever: the dropdown would offer the 20 PSIC sections while the stored
rows, the charts and the competitor counts still spoke the old
vocabulary. Every page would quietly disagree with every other one.

WHAT IT DOES, AND THE RULE BEHIND EACH PART

  * REMAP (non-micro rows). Every old category that describes an
    SME-scale business is rewritten to the PSIC section it belongs to --
    see OLD_TO_PSIC below, which is exhaustive and commented one line
    at a time. "Retail" becomes "Wholesale and Retail Trade; Repair of
    Motor Vehicles and Motorcycles", "Health & Wellness" becomes "Human
    Health and Social Work Activities", and so on.

  * DELETE (micro-only rows). A category whose defining form is a
    stall, cart, kiosk, front-window counter or backyard operation is
    a MICRO enterprise, not an SME -- under the Philippine MSME
    definition (RA 9501/DTI, by asset size) micro tops out at PHP 3M in
    assets, and in practice that is the sari-sari store, the carinderia
    with four tables, the market stall. Those rows are deleted rather
    than folded into a section, because keeping them would put micro
    establishments back into the SME competitor counts this system's
    whole scope is about excluding. MICRO_ONLY below lists them, also
    one commented line at a time.

    Note that five of the 22 micro-era categories are NOT deleted:
    bakeshops, laundry shops, samgyupsal/grill restaurants, hardware &
    construction supply, and agri-supply stores are ordinarily
    SME-scale registered establishments, so they are remapped like any
    other non-micro category. The split is a judgment call and it is
    deliberately in one editable place: move a name between OLD_TO_PSIC
    and MICRO_ONLY and re-run to change it.

  * COLLAPSE (duplicates the remap creates). Two old categories can map
    to one section, so "Retail" and "Wholesale Trade" in the same
    barangay on the same date both become the same
    (industry_type, location, date_recorded) snapshot. market_data is
    defined as one row per industry+location+date, so the freshest row
    wins and the rest are removed -- counting both would double-count
    businesses that the two old searches returned in common.

  * RE-FETCH. Every surviving remapped row still holds a
    competitor_count fetched with the OLD category's Google Places
    search term, which is not what the new section means. The migration
    therefore bumps the existing "recap watermark"
    (app/services/startup_migrations.py), the mechanism already used
    once before when the Places result cap was lifted: rows at or below
    the watermark get exactly one free re-fetch the next time the AI
    engine touches them, then cache normally. No fabricated numbers, no
    mass API burn at boot.

SAFETY
Every row touched is copied into `industry_migration_log` FIRST,
including the complete original row as JSON -- see that model's
docstring. undo() reads that table back and restores everything. The
migration runs at most once (guarded by a SystemSetting flag), never
raises out of startup, and is a no-op on a database that has no old
values left in it.
"""

import json
from datetime import datetime, date

from app.extensions import db
from app.models import MarketData, SmeProfile, ForecastResult, SystemSetting, IndustryMigrationLog
from app.models.archive import get_including_archived

# Set once the migration has run, so it never runs twice.
MIGRATION_FLAG_KEY = "industry_taxonomy_psic_migrated_at"

# ---------------------------------------------------------------------
# OLD CATEGORY -> PSIC SECTION
# ---------------------------------------------------------------------
# Exhaustive map of every category this project has ever shipped that
# describes an SME-scale business, onto the PSIC section it belongs to.
# Anything not listed here and not in MICRO_ONLY below is left alone --
# an unknown free-text industry an LGU upload introduced is that
# office's data, not this migration's to rewrite.
OLD_TO_PSIC = {
    # --- the original 24 broad categories ---
    "Food & Beverage": "Food and Beverage",
    "Retail": "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
    "Service": "Other Service Activities",
    "Construction": "Construction",
    "Wholesale Trade": "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
    "Agriculture, Forestry & Fishing": "Agriculture, Forestry, and Fishing",
    "Manufacturing": "Manufacturing",
    "Transportation & Logistics": "Transportation and Storage",
    "Information Technology & Communication": "Information and Communication",
    "Health & Wellness": "Human Health and Social Work Activities",
    "Education & Training": "Education",
    # Salons/spas/barbershops are PSIC S -- "other personal service activities".
    "Beauty & Personal Care": "Other Service Activities",
    # PSIC G covers "repair of motor vehicles and motorcycles" explicitly.
    "Automotive Services": "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
    "Real Estate": "Real Estate Activities",
    "Financial & Insurance Services": "Financial and Insurance Activities",
    "Tourism & Accommodation": "Accommodation and Food Service Activities",
    "Entertainment & Recreation": "Arts, Entertainment, and Recreation",
    # Making garments/footwear is PSIC C; a shop only SELLING them would
    # be G, but this category was defined as tailoring/production.
    "Textile, Apparel & Footwear": "Manufacturing",
    "Furniture & Handicrafts": "Manufacturing",
    # Printing is PSIC C (publishing alone would be J; this category was
    # predominantly print shops).
    "Printing & Publishing": "Manufacturing",
    "Professional & Technical Services": "Professional, Scientific, and Technical Activities",
    "Warehousing & Storage": "Transportation and Storage",
    "Repair & Maintenance Services": "Other Service Activities",
    "Agri-Business & Farm Supply": "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",

    # --- the 5 micro-era categories that are genuinely SME-scale ---
    # A bakeshop with its own production is a small enterprise, not a
    # front-window counter.
    "Bakeries & Bakeshops": "Food and Beverage",
    # A wash-dry-fold shop runs commercial machines out of leased space.
    "Laundry Shops": "Other Service Activities",
    # Samgyupsal/unli-wings houses are sit-down restaurants.
    "Samgyupsal & Grill Eateries": "Accommodation and Food Service Activities",
    # Hardware and agri-supply stores carry real inventory and are
    # routinely small-to-medium by DTI asset size.
    "Hardware & Construction Supply Stores":
        "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
    "Agri-Supply Stores": "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
}

# ---------------------------------------------------------------------
# MICRO-ONLY CATEGORIES -- rows deleted, not remapped
# ---------------------------------------------------------------------
# Each of these is defined by a form of operation that is micro by
# nature: a stall, a cart, a kiosk, a front-window counter, a roadside
# bay, or a backyard. See the module docstring for the standard applied.
MICRO_ONLY = {
    "Sari-Sari Stores",                     # front-window neighborhood counter
    "Carinderias & Eateries",               # turo-turo, a few tables
    "Milk Tea & Beverage Kiosks",           # kiosk/pop-up stand
    "Food Carts & Street Food Stalls",      # mobile or fixed street stall
    "Water Refilling Stations",             # single-room refilling counter
    "Vulcanizing & Repair Shops",           # roadside tire/repair bay
    "Sari-Sari Barber Shops & Hair Salons", # one- or two-chair neighborhood shop
    "Auto & Motorcycle Wash Bays",          # open wash bay
    "Tailoring & Alteration Shops",         # single-seamstress shop
    "Piso WiFi & Internet Cafes",           # coin-operated micro digital
    "Computer Repair & Printing Shops",     # counter-scale repair/print
    "Dry Goods & Apparel Stalls",           # tiangge/market stall
    "LPG & Cooking Gas Dealers",            # small retail/delivery counter
    "Rice Retailing & Milling Outlets",     # public-market rice stall
    "Meat & Poultry Stalls",                # public-market meat stall
    "Poultry & Backyard Livestock Farms",   # backyard raising
    "Pasalubong & Native Delicacy Shops",   # home-production vending
}


def _serialize(row, columns):
    out = {}
    for col in columns:
        value = getattr(row, col, None)
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            value = str(value)
        out[col] = value
    return out


_MARKET_DATA_COLUMNS = [
    "market_id", "industry_type", "location", "competitor_count", "population_density",
    "historical_success_rate", "foot_traffic_index", "average_rent", "source", "date_recorded",
]


def _log(table_name, row_id, old_value, new_value, action, location=None, row_json=None):
    db.session.add(
        IndustryMigrationLog(
            table_name=table_name,
            row_id=row_id,
            old_industry_type=old_value,
            new_industry_type=new_value,
            action=action,
            location=location,
            row_json=json.dumps(row_json) if row_json is not None else None,
        )
    )


def has_run():
    return SystemSetting.query.get(MIGRATION_FLAG_KEY) is not None


def migrate(force=False):
    """Run the migration. Returns a summary dict:

        {"remapped": n, "deleted_micro": n, "deleted_duplicate": n,
         "profiles_remapped": n, "forecasts_remapped": n, "skipped": bool}

    Idempotent: does nothing (and reports skipped=True) once the flag
    row exists, unless force=True.
    """
    summary = {
        "remapped": 0,
        "deleted_micro": 0,
        "deleted_duplicate": 0,
        "profiles_remapped": 0,
        "forecasts_remapped": 0,
        "skipped": False,
    }
    if has_run() and not force:
        summary["skipped"] = True
        return summary

    # The backup table must exist before anything is written to it. This
    # is checkfirst=True, so it is a no-op when it already does -- an
    # existing install does not need seed.py re-run.
    IndustryMigrationLog.__table__.create(db.session.get_bind(), checkfirst=True)

    # --- market_data: delete micro rows -------------------------------
    micro_rows = MarketData.query.filter(MarketData.industry_type.in_(tuple(MICRO_ONLY))).all()
    for row in micro_rows:
        _log(
            "market_data", row.market_id, row.industry_type, None, "deleted_micro",
            location=row.location, row_json=_serialize(row, _MARKET_DATA_COLUMNS),
        )
        db.session.delete(row)
        summary["deleted_micro"] += 1

    # --- market_data: remap the rest ----------------------------------
    remap_rows = MarketData.query.filter(MarketData.industry_type.in_(tuple(OLD_TO_PSIC))).all()
    for row in remap_rows:
        new_value = OLD_TO_PSIC[row.industry_type]
        _log(
            "market_data", row.market_id, row.industry_type, new_value, "remapped",
            location=row.location, row_json=_serialize(row, _MARKET_DATA_COLUMNS),
        )
        row.industry_type = new_value
        summary["remapped"] += 1

    db.session.flush()

    # --- market_data: collapse duplicates the remap just created ------
    # One row per (industry_type, location, date_recorded) -- keep the
    # freshest market_id, record and remove the rest.
    seen = {}
    for row in (
        MarketData.query
        .order_by(MarketData.market_id.desc())
        .all()
    ):
        key = (row.industry_type, row.location, row.date_recorded)
        if key in seen:
            _log(
                "market_data", row.market_id, row.industry_type, row.industry_type,
                "deleted_duplicate", location=row.location,
                row_json=_serialize(row, _MARKET_DATA_COLUMNS),
            )
            db.session.delete(row)
            summary["deleted_duplicate"] += 1
        else:
            seen[key] = row.market_id

    # --- sme_profile / forecast_result: remap their industry text -----
    # These are the SME's OWN plans and their forecast history. They are
    # never deleted, even for a micro category: that is a person's saved
    # work. A micro plan is remapped to the closest section instead, so
    # it keeps working against the new vocabulary.
    micro_fallbacks = {
        "Sari-Sari Stores": "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
        "Carinderias & Eateries": "Accommodation and Food Service Activities",
        "Milk Tea & Beverage Kiosks": "Food and Beverage",
        "Food Carts & Street Food Stalls": "Accommodation and Food Service Activities",
        "Water Refilling Stations":
            "Water Supply; Sewerage, Waste Management, and Remediation Activities",
        "Vulcanizing & Repair Shops":
            "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
        "Sari-Sari Barber Shops & Hair Salons": "Other Service Activities",
        "Auto & Motorcycle Wash Bays":
            "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
        "Tailoring & Alteration Shops": "Other Service Activities",
        "Piso WiFi & Internet Cafes": "Information and Communication",
        "Computer Repair & Printing Shops": "Other Service Activities",
        "Dry Goods & Apparel Stalls":
            "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
        "LPG & Cooking Gas Dealers":
            "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
        "Rice Retailing & Milling Outlets":
            "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
        "Meat & Poultry Stalls":
            "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles",
        "Poultry & Backyard Livestock Farms": "Agriculture, Forestry, and Fishing",
        "Pasalubong & Native Delicacy Shops": "Food and Beverage",
    }
    plan_map = dict(OLD_TO_PSIC)
    plan_map.update(micro_fallbacks)

    # include_archived: a plan sitting in the owner's Trash is still
    # their plan, and restoring it must bring it back speaking the
    # current vocabulary -- so it is remapped like every other one. The
    # archive filter (app/models/archive.py) would otherwise skip it.
    for profile in (
        SmeProfile.query.execution_options(include_archived=True)
        .filter(SmeProfile.industry_type.in_(tuple(plan_map)))
        .all()
    ):
        new_value = plan_map[profile.industry_type]
        _log("sme_profile", profile.sme_id, profile.industry_type, new_value, "remapped",
             location=profile.location)
        profile.industry_type = new_value
        summary["profiles_remapped"] += 1

    for forecast in ForecastResult.query.filter(
        ForecastResult.input_industry_type.in_(tuple(plan_map))
    ).all():
        new_value = plan_map[forecast.input_industry_type]
        _log("forecast_result", forecast.forecast_id, forecast.input_industry_type, new_value,
             "remapped", location=getattr(forecast, "input_location", None))
        forecast.input_industry_type = new_value
        summary["forecasts_remapped"] += 1

    # --- mark every remapped row for one free re-fetch ----------------
    # A remapped row's competitor_count came from the OLD category's
    # search term. Reuse the existing recap-watermark mechanism (see
    # startup_migrations.py / forecasting_service.find_or_create_market_data):
    # rows at or below the watermark get exactly one live re-fetch when
    # the engine next needs them.
    if summary["remapped"]:
        from app.services.startup_migrations import PLACES_RECAP_WATERMARK_KEY

        highest = db.session.query(db.func.max(MarketData.market_id)).scalar() or 0
        SystemSetting.set(
            PLACES_RECAP_WATERMARK_KEY,
            int(highest),
            "Rows up to here predate the PSIC taxonomy remap; each gets one Places re-fetch",
        )

    SystemSetting.set(
        MIGRATION_FLAG_KEY,
        datetime.utcnow().isoformat(timespec="seconds"),
        "When the one-time industry_type -> PSIC section migration ran",
    )
    db.session.commit()
    return summary


def undo():
    """Restore everything migrate() changed, from industry_migration_log.

    Deleted rows are re-inserted with their original values; remapped
    rows get their old industry_type back. The log is then cleared and
    the ran-once flag removed, so migrate() can run again. Returns a
    summary dict shaped like migrate()'s.
    """
    summary = {"restored_rows": 0, "reverted_remaps": 0}
    entries = IndustryMigrationLog.query.order_by(IndustryMigrationLog.id.desc()).all()

    for entry in entries:
        if entry.action in ("deleted_micro", "deleted_duplicate"):
            payload = json.loads(entry.row_json or "{}")
            if payload and not MarketData.query.get(payload.get("market_id")):
                recorded = payload.get("date_recorded")
                db.session.add(
                    MarketData(
                        market_id=payload.get("market_id"),
                        industry_type=payload.get("industry_type"),
                        location=payload.get("location"),
                        competitor_count=payload.get("competitor_count"),
                        population_density=payload.get("population_density"),
                        historical_success_rate=payload.get("historical_success_rate"),
                        foot_traffic_index=payload.get("foot_traffic_index"),
                        average_rent=payload.get("average_rent"),
                        source=payload.get("source"),
                        date_recorded=date.fromisoformat(recorded) if recorded else date.today(),
                    )
                )
                summary["restored_rows"] += 1
        elif entry.action == "remapped":
            if entry.table_name == "market_data":
                row = MarketData.query.get(entry.row_id)
                if row is not None:
                    row.industry_type = entry.old_industry_type
                    summary["reverted_remaps"] += 1
            elif entry.table_name == "sme_profile":
                # Trashed plans were remapped too, so they are reverted too.
                row = get_including_archived(SmeProfile, entry.row_id)
                if row is not None:
                    row.industry_type = entry.old_industry_type
                    summary["reverted_remaps"] += 1
            elif entry.table_name == "forecast_result":
                row = ForecastResult.query.get(entry.row_id)
                if row is not None:
                    row.input_industry_type = entry.old_industry_type
                    summary["reverted_remaps"] += 1

    for entry in entries:
        db.session.delete(entry)

    flag = SystemSetting.query.get(MIGRATION_FLAG_KEY)
    if flag is not None:
        db.session.delete(flag)

    db.session.commit()
    return summary
