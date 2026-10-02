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


# Additive columns that a database created by an older version will not
# have. db.create_all() adds them to a NEW database but never alters an
# existing table, so without this an upgraded install dies on the first
# query with "Unknown column 'user.theme'" (or 'lgu_data.archived_at',
# or 'sme_profile.subcategory', ...).
#
# {table: {column: (SQLite DDL, MySQL DDL)}} -- the two backends this
# project targets. The syntax happens to match for most of these, but
# keeping the pair explicit gives a column that DOES need to differ
# somewhere to go.
#
# Every entry is NULLable or has a DEFAULT, deliberately: adding a NOT
# NULL column with no default to a table that already has rows fails on
# MySQL and is meaningless on SQLite.
_ARCHIVE_COLUMNS = {
    "archived_at": ("DATETIME NULL", "DATETIME NULL"),
    "archived_by": ("INTEGER NULL", "INT NULL"),
    "archive_reason": ("VARCHAR(255) NULL", "VARCHAR(255) NULL"),
}

_USER_COLUMNS = {
    "profile_picture": ("TEXT NULL", "LONGTEXT NULL"),
    "theme": ("VARCHAR(10) NOT NULL DEFAULT 'light'", "VARCHAR(10) NOT NULL DEFAULT 'light'"),
    "notify_recommendations": ("BOOLEAN NOT NULL DEFAULT 0", "TINYINT(1) NOT NULL DEFAULT 0"),
    "notify_weekly_trends": ("BOOLEAN NOT NULL DEFAULT 0", "TINYINT(1) NOT NULL DEFAULT 0"),
    "notify_saturation_change": ("BOOLEAN NOT NULL DEFAULT 1", "TINYINT(1) NOT NULL DEFAULT 1"),
    "notify_newsletter": ("BOOLEAN NOT NULL DEFAULT 0", "TINYINT(1) NOT NULL DEFAULT 0"),
    # First-time walkthrough state -- see User.onboarding_state.
    "onboarding_state": ("VARCHAR(20) NULL", "VARCHAR(20) NULL"),
    # Archive instead of delete -- see app/models/archive.py.
    **_ARCHIVE_COLUMNS,
}

_ADDITIVE_COLUMNS = {
    "user": _USER_COLUMNS,
    "lgu_data": dict(_ARCHIVE_COLUMNS),
    "market_data": dict(_ARCHIVE_COLUMNS),
    # Broader business parameters -- see app/models/sme_profile.py.
    "sme_profile": {
        "subcategory": ("VARCHAR(100) NULL", "VARCHAR(100) NULL"),
        "product_offering": ("TEXT NULL", "TEXT NULL"),
        "innovation_idea": ("TEXT NULL", "TEXT NULL"),
        "offering_details": ("TEXT NULL", "TEXT NULL"),
        # The plan Trash: removing a plan from Home archives it instead
        # of deleting it -- see app/models/sme_profile.py. Without these
        # an upgraded install would fail every plan query the moment
        # SmeProfile gained the archive filter.
        **_ARCHIVE_COLUMNS,
    },
    # The five W's -- see app/models/audit_log.py.
    "audit_logs": {
        "actor_name": ("VARCHAR(100) NULL", "VARCHAR(100) NULL"),
        "actor_role": ("VARCHAR(20) NULL", "VARCHAR(20) NULL"),
        "target_type": ("VARCHAR(50) NULL", "VARCHAR(50) NULL"),
        "target_id": ("VARCHAR(50) NULL", "VARCHAR(50) NULL"),
        "target_label": ("VARCHAR(255) NULL", "VARCHAR(255) NULL"),
        "http_method": ("VARCHAR(10) NULL", "VARCHAR(10) NULL"),
        "route": ("VARCHAR(255) NULL", "VARCHAR(255) NULL"),
        "user_agent": ("VARCHAR(255) NULL", "VARCHAR(255) NULL"),
        "reason": ("VARCHAR(255) NULL", "VARCHAR(255) NULL"),
    },
}


def _quote(table, dialect):
    # `user` is a reserved word on both backends, so every table name is
    # quoted rather than special-casing that one.
    return f"`{table}`" if dialect == "mysql" else f'"{table}"'


def _add_missing_columns():
    """ALTER TABLE for every additive column any existing table is
    missing. Returns {table: [columns added]}.

    Reads the live column list first rather than firing the DDL and
    catching a duplicate-column error: a failed statement aborts the
    surrounding transaction on some backends, which would take the
    migrations after it down too.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    if "user" not in tables:
        return {}          # brand-new database -- create_all() handles it

    dialect = db.engine.dialect.name
    added = {}
    for table, columns in _ADDITIVE_COLUMNS.items():
        if table not in tables:
            continue       # created whole by _create_missing_tables()
        existing = {col["name"] for col in inspector.get_columns(table)}
        for column, (sqlite_ddl, mysql_ddl) in columns.items():
            if column in existing:
                continue
            ddl = mysql_ddl if dialect == "mysql" else sqlite_ddl
            with db.engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {_quote(table, dialect)} ADD COLUMN {column} {ddl}"))
            added.setdefault(table, []).append(column)
    return added


def _add_missing_user_columns():
    """Kept for anything still calling it by its old name: the user
    table's share of _add_missing_columns()."""
    return _add_missing_columns().get("user", [])


def _create_missing_tables():
    """Create any table a model defines that this database does not have
    yet -- subcategory_market_data, for instance.

    Only on a database that is already initialised (it has a `user`
    table). A brand-new database is left entirely to seed.py's
    create_all(), exactly as before, so this cannot change what a fresh
    install looks like. checkfirst=True makes it a no-op for every table
    that already exists; it never alters one.
    """
    from sqlalchemy import inspect

    tables = set(inspect(db.engine).get_table_names())
    if "user" not in tables:
        return []
    missing = [t for t in db.metadata.sorted_tables if t.name not in tables]
    for table in missing:
        table.create(bind=db.engine, checkfirst=True)
    return [t.name for t in missing]


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


# Where a barangay name is stored, per table. Used by
# _merge_barangay_aliases() below.
_BARANGAY_COLUMNS = (
    ("market_data", "location"),
    ("lgu_data", "barangay"),
    ("subcategory_market_data", "location"),
    ("sme_profile", "location"),
)


def _merge_barangay_aliases():
    """Fold differently-written barangay names into the official one.

    A permit register or an old Places run could file rows under "Baras"
    while every other row says "Baras-baras" -- one barangay, two names,
    and the map listed it twice. This renames every stored name that
    seed_data.canonical_barangay() recognises as an official barangay
    written another way. Names it does not recognise are left exactly as
    they are: renaming on a guess would file a count under the wrong
    barangay.

    Runs on every start, and is cheap when there is nothing to do (one
    DISTINCT per table). Returns {table: {old_name: canonical}}.
    """
    from sqlalchemy import inspect, text

    from app.ml.seed_data import BARANGAY_NAMES, canonical_barangay

    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    official = set(BARANGAY_NAMES)
    dialect = db.engine.dialect.name
    merged = {}
    for table, column in _BARANGAY_COLUMNS:
        if table not in tables:
            continue
        quoted = _quote(table, dialect)
        with db.engine.begin() as conn:
            names = [row[0] for row in conn.execute(text(f"SELECT DISTINCT {column} FROM {quoted}"))]
            for name in names:
                if name is None or name in official:
                    continue
                canonical = canonical_barangay(name)
                if canonical and canonical != name:
                    conn.execute(
                        text(f"UPDATE {quoted} SET {column} = :canonical WHERE {column} = :name"),
                        {"canonical": canonical, "name": name},
                    )
                    merged.setdefault(table, {})[name] = canonical
    return merged


def run_startup_migrations(app):
    """Called from create_app(). Never raises: a fresh checkout whose
    tables don't exist yet, or a database that happens to be down at
    boot, must not stop the app from starting -- the migrations simply
    run on the next successful start."""
    with app.app_context():
        try:
            created = _create_missing_tables()
            if created:
                app.logger.info("Created missing tables: %s", ", ".join(created))
        except Exception:  # noqa: BLE001 -- see docstring
            db.session.rollback()

        try:
            added = _add_missing_columns()
            for table, columns in added.items():
                app.logger.info("Added missing %s columns: %s", table, ", ".join(columns))
        except Exception:  # noqa: BLE001 -- see docstring
            db.session.rollback()

        try:
            _retire_system_theme()
        except Exception:  # noqa: BLE001 -- see docstring
            db.session.rollback()

        try:
            merged = _merge_barangay_aliases()
            if merged:
                for table, renames in merged.items():
                    app.logger.info(
                        "Merged barangay names in %s: %s", table,
                        ", ".join(f"{old!r} -> {new!r}" for old, new in renames.items()),
                    )
                # Renaming does not move the data fingerprint the trend
                # caches key on, so they are dropped explicitly.
                from app.services.response_cache import clear_response_cache
                from app.services.trend_analytics_service import clear_trend_caches

                clear_trend_caches(drop_disk=True)
                clear_response_cache()
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
