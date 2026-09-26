"""
app/services/startup_migrations.py
-------------------------------------
Small, idempotent data migrations that run ONCE per application start,
from create_app().

WHY THIS FILE EXISTS
`SystemSetting.ensure_defaults()` used to be called only by `seed.py`,
the Admin "System Settings" save handler, and the test fixtures --
never by the running app. That meant a default whose MEANING changed in
a later version was never applied to an existing database: the app
shipped with `places_max_results = 0` ("unlimited"), but every existing
install kept the `"20"` row seeded months earlier, so every Google
Places lookup silently stayed capped at 20 results per barangay. The
symptom was a verification table where literally every row read exactly
"20 businesses found".

Everything here is safe to run on every boot, and safe to run on a
database that is already up to date.
"""

from app.extensions import db
from app.models import MarketData, SystemSetting

# Marks the point in market_data's history BEFORE which every Google
# Places row was fetched under the old 20-result cap. Rows at or below
# this market_id get one free re-fetch (see
# forecasting_service.find_or_create_market_data), after which their
# replacement row sits above the watermark and is cached normally.
PLACES_RECAP_WATERMARK_KEY = "places_recap_watermark_market_id"


def _set_places_recap_watermark():
    """Record the highest market_id that existed the first time this
    version ran. Only ever written once -- re-running must not move the
    watermark forward, or rows fetched since would be re-fetched too."""
    if SystemSetting.query.get(PLACES_RECAP_WATERMARK_KEY) is not None:
        return False

    highest = db.session.query(db.func.max(MarketData.market_id)).scalar() or 0
    SystemSetting.set(
        PLACES_RECAP_WATERMARK_KEY,
        int(highest),
        "Highest market_id fetched under the old 20-result Places cap; rows up to here get one re-fetch",
    )
    return True


def get_places_recap_watermark():
    """market_id at/below which a Google Places row is considered
    'captured under the old cap' and worth re-fetching once."""
    try:
        return int(SystemSetting.get_float(PLACES_RECAP_WATERMARK_KEY, 0))
    except (TypeError, ValueError):
        return 0


# Additive columns on `user` that a database created by an older version
# will not have. db.create_all() adds them to a NEW database but never
# alters an existing table, so without this an upgraded install dies on
# the first query with "Unknown column 'user.theme'".
#
# {column: (SQLite DDL, MySQL DDL)} -- the two backends this project
# targets. The syntax happens to match for these three, but keeping the
# pair explicit gives a column that DOES need to differ somewhere to go.
_USER_COLUMNS = {
    "profile_picture": ("TEXT NULL", "LONGTEXT NULL"),
    "theme": ("VARCHAR(10) NOT NULL DEFAULT 'light'", "VARCHAR(10) NOT NULL DEFAULT 'light'"),
    "notify_recommendations": ("BOOLEAN NOT NULL DEFAULT 0", "TINYINT(1) NOT NULL DEFAULT 0"),
    "notify_weekly_trends": ("BOOLEAN NOT NULL DEFAULT 0", "TINYINT(1) NOT NULL DEFAULT 0"),
    "notify_saturation_change": ("BOOLEAN NOT NULL DEFAULT 1", "TINYINT(1) NOT NULL DEFAULT 1"),
    "notify_newsletter": ("BOOLEAN NOT NULL DEFAULT 0", "TINYINT(1) NOT NULL DEFAULT 0"),
}


def _add_missing_user_columns():
    """ALTER TABLE `user` for any additive column it is missing.

    Reads the live column list first rather than firing the DDL and
    catching a duplicate-column error: a failed statement aborts the
    surrounding transaction on some backends, which would take the
    migrations after it down too.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "user" not in inspector.get_table_names():
        return []          # brand-new database -- create_all() handles it

    existing = {col["name"] for col in inspector.get_columns("user")}
    dialect = db.engine.dialect.name
    added = []

    for column, (sqlite_ddl, mysql_ddl) in _USER_COLUMNS.items():
        if column in existing:
            continue
        ddl = mysql_ddl if dialect == "mysql" else sqlite_ddl
        with db.engine.begin() as conn:
            conn.execute(text(f'ALTER TABLE "user" ADD COLUMN {column} {ddl}'
                              if dialect != "mysql"
                              else f"ALTER TABLE `user` ADD COLUMN {column} {ddl}"))
        added.append(column)

    return added


def _retire_system_theme():
    """'match system' was offered briefly and then dropped, so any row
    still holding it would resolve to light on every page load without
    the stored value ever agreeing with what the person sees. Rewrite
    them once so the column says what the UI says."""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "user" not in inspector.get_table_names():
        return 0
    if "theme" not in {col["name"] for col in inspector.get_columns("user")}:
        return 0

    quoted = "`user`" if db.engine.dialect.name == "mysql" else '"user"'
    with db.engine.begin() as conn:
        result = conn.execute(text(f"UPDATE {quoted} SET theme = 'light' WHERE theme = 'system'"))
    return result.rowcount or 0


def run_startup_migrations(app):
    """Called from create_app(). Never raises: a fresh checkout whose
    tables don't exist yet, or a database that happens to be down at
    boot, must not stop the app from starting -- the migrations simply
    run on the next successful start."""
    with app.app_context():
        try:
            added = _add_missing_user_columns()
            if added:
                app.logger.info("Added missing user columns: %s", ", ".join(added))
        except Exception:  # noqa: BLE001 -- see docstring
            db.session.rollback()

        try:
            _retire_system_theme()
        except Exception:  # noqa: BLE001 -- see docstring
            db.session.rollback()

        try:
            SystemSetting.ensure_defaults()
            _set_places_recap_watermark()
        except Exception:  # noqa: BLE001 -- see docstring; boot must survive any DB state
            db.session.rollback()

        # One-time move of every stored industry_type onto the PSIC
        # sections, plus removal of the micro-business rows this system
        # (SME-scoped) no longer counts. Guarded, logged and reversible
        # -- see app/services/industry_migration.py. Kept in its own
        # try/except so a failure here can never take down boot either.
        try:
            from app.services.industry_migration import migrate as migrate_industries

            summary = migrate_industries()
            if not summary.get("skipped"):
                app.logger.info(
                    "Industry taxonomy migration: %s remapped, %s micro rows deleted, "
                    "%s duplicates collapsed, %s plans and %s forecasts remapped "
                    "(all recorded in industry_migration_log)",
                    summary["remapped"], summary["deleted_micro"], summary["deleted_duplicate"],
                    summary["profiles_remapped"], summary["forecasts_remapped"],
                )
        except Exception:  # noqa: BLE001 -- see docstring
            db.session.rollback()
