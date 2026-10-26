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
    "onboarding_state": ("VARCHAR(20) NULL", "VARCHAR(20) NULL"),
    "last_seen_at": ("DATETIME NULL", "DATETIME NULL"),
    "suspended_until": ("DATETIME NULL", "DATETIME NULL"),
    **_ARCHIVE_COLUMNS,
    "first_name": ("VARCHAR(60) NULL", "VARCHAR(60) NULL"),
    "middle_name": ("VARCHAR(60) NULL", "VARCHAR(60) NULL"),
    "last_name": ("VARCHAR(60) NULL", "VARCHAR(60) NULL"),
    "suffix": ("VARCHAR(10) NULL", "VARCHAR(10) NULL"),
    "contact_number_enc": ("TEXT NULL", "TEXT NULL"),
    "gender_enc": ("TEXT NULL", "TEXT NULL"),
    "birthdate_enc": ("TEXT NULL", "TEXT NULL"),
    "address_line_enc": ("TEXT NULL", "TEXT NULL"),
    "address_barangay_enc": ("TEXT NULL", "TEXT NULL"),
    "terms_accepted_at": ("DATETIME NULL", "DATETIME NULL"),
    "lgu_role": ("VARCHAR(20) NULL", "VARCHAR(20) NULL"),
}

_ADDITIVE_COLUMNS = {
    "user": _USER_COLUMNS,
    "lgu_data": dict(_ARCHIVE_COLUMNS),
    "market_data": dict(_ARCHIVE_COLUMNS),
    "sme_profile": {
        "subcategory": ("VARCHAR(100) NULL", "VARCHAR(100) NULL"),
        "product_offering": ("TEXT NULL", "TEXT NULL"),
        "innovation_idea": ("TEXT NULL", "TEXT NULL"),
        "offering_details": ("TEXT NULL", "TEXT NULL"),
        **_ARCHIVE_COLUMNS,
    },
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
        "changes": ("TEXT NULL", "TEXT NULL"),
    },
}


def _quote(table, dialect):
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
        return {}

    dialect = db.engine.dialect.name
    added = {}
    for table, columns in _ADDITIVE_COLUMNS.items():
        if table not in tables:
            continue
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


def _encrypt_legacy_contact_numbers():
    """Move every contact number still sitting in plain text in the
    original `contact_number` column into the encrypted
    `contact_number_enc` column, then clear the plain copy. Runs on every
    start; once nothing is left in the old column it is one cheap query.
    Returns how many were moved."""
    from app.models.user import User

    rows = (
        User.query.execution_options(include_archived=True)
        .filter(User.legacy_contact_number.isnot(None), User.legacy_contact_number != "")
        .all()
    )
    for user in rows:
        user.contact_number = user.legacy_contact_number
        user.legacy_contact_number = None
    if rows:
        db.session.commit()
    return len(rows)


def _hide_personal_details_in_audit_trail():
    """Profile edits recorded before the personal details were encrypted
    kept the old and new contact number in plain text in audit_logs.changes
    (and so in the audit CSV export). Replace those values with "(hidden)"
    -- the entry still shows THAT the number changed. Runs on every start;
    once nothing is left to hide it is one cheap query. Returns how many
    entries were rewritten."""
    import json

    from app.models.audit_log import AuditLog
    from app.services.account_params import PII_FIELDS as _PII_FIELDS

    rows = AuditLog.query.filter(AuditLog.action == "update_profile", AuditLog.changes.isnot(None)).all()
    rewritten = 0
    for row in rows:
        try:
            changes = json.loads(row.changes)
        except (TypeError, ValueError):
            continue
        if not isinstance(changes, dict):
            continue
        dirty = False
        for field in _PII_FIELDS:
            pair = changes.get(field)
            if isinstance(pair, list) and any(v not in (None, "", "(hidden)") for v in pair):
                changes[field] = [None if v in (None, "") else "(hidden)" for v in pair]
                dirty = True
        if dirty:
            row.changes = json.dumps(changes)
            rewritten += 1
    if rewritten:
        db.session.commit()
    return rewritten


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


def _backfill_permit_row_features():
    """Permit-derived market rows written before they carried the area
    figures (see data_import_service.area_features_for) have an empty
    population density, foot traffic, rent and success rate, and being the
    newest row for their pair they fed zeros into the model. Fill them in
    once. Runs on every start; with nothing left to fill it is one cheap
    query. Returns how many rows were filled."""
    from app.services.data_import_service import AREA_FEATURE_FIELDS, area_features_for

    rows = (
        MarketData.query.execution_options(include_archived=True)
        .filter(MarketData.source == "DTI", MarketData.population_density.is_(None))
        .all()
    )
    if not rows:
        return 0
    features = area_features_for((row.industry_type, row.location) for row in rows)
    filled = 0
    for row in rows:
        values = features.get((row.industry_type, row.location))
        if not values:
            continue
        for field in AREA_FEATURE_FIELDS:
            if getattr(row, field) is None:
                setattr(row, field, values[field])
        filled += 1
    if filled:
        db.session.commit()
    return filled


UNMATCHED_BARANGAY_REASON = "Not one of Tarlac City's 76 barangays (archived automatically at start-up)"


def _archive_unmatched_uploaded_barangays():
    """Archive LGU-uploaded rows whose barangay is not one of the 76 and
    is not a known way of writing one. The importer has refused such names
    for a while, so these are left over from older uploads: no score ever
    reads them, and they only showed up as "uploaded name(s) not matched
    to a barangay" on the LGU dashboard. Archived, not deleted, so an
    administrator can restore any of them from Admin > Datasets. Rows the
    app created for itself (placeholders) are left alone. Runs on every
    start; with nothing to archive it is one cheap query. Returns how many
    rows were archived."""
    from app.ml.seed_data import canonical_barangay
    from app.services.data_import_service import _real_upload_query, _system_user_id_or_none

    rows = [row for row in _real_upload_query().all() if not canonical_barangay(row.barangay)]
    if not rows:
        return 0
    archived_by = _system_user_id_or_none()
    for row in rows:
        row.archive(archived_by, UNMATCHED_BARANGAY_REASON)
    db.session.commit()
    return len(rows)


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
        except Exception:
            db.session.rollback()

        try:
            added = _add_missing_columns()
            for table, columns in added.items():
                app.logger.info("Added missing %s columns: %s", table, ", ".join(columns))
        except Exception:
            db.session.rollback()

        try:
            _retire_system_theme()
        except Exception:
            db.session.rollback()

        try:
            moved = _encrypt_legacy_contact_numbers()
            if moved:
                app.logger.info("Encrypted %s contact number(s) left in plain text", moved)
        except Exception:
            db.session.rollback()

        try:
            from app.utils.privacy import rotate_encrypted_values

            rotation = rotate_encrypted_values()
            if rotation["rotated"]:
                app.logger.info("Re-encrypted %s personal detail(s) under the current PII_ENCRYPTION_KEY; "
                                "PII_ENCRYPTION_PREVIOUS_KEYS can now be removed", rotation["rotated"])
            if rotation["unreadable"]:
                app.logger.warning("%s personal detail(s) could not be decrypted with any configured key -- "
                                   "add the key they were written under to PII_ENCRYPTION_PREVIOUS_KEYS",
                                   rotation["unreadable"])
        except Exception:
            db.session.rollback()

        try:
            hidden = _hide_personal_details_in_audit_trail()
            if hidden:
                app.logger.info("Hid personal details in %s audit trail entr(ies)", hidden)
        except Exception:
            db.session.rollback()

        try:
            merged = _merge_barangay_aliases()
            if merged:
                for table, renames in merged.items():
                    app.logger.info(
                        "Merged barangay names in %s: %s", table,
                        ", ".join(f"{old!r} -> {new!r}" for old, new in renames.items()),
                    )
                from app.services.response_cache import clear_response_cache
                from app.services.trend_analytics_service import clear_trend_caches

                clear_trend_caches(drop_disk=True)
                clear_response_cache()
        except Exception:
            db.session.rollback()

        try:
            archived = _archive_unmatched_uploaded_barangays()
            if archived:
                app.logger.info("Archived %s uploaded lgu_data row(s) whose barangay is not one of the 76", archived)
        except Exception:
            db.session.rollback()

        try:
            filled = _backfill_permit_row_features()
            if filled:
                app.logger.info("Filled area figures on %s permit-derived market row(s)", filled)
                from app.services.response_cache import clear_response_cache
                from app.services.trend_analytics_service import clear_trend_caches

                clear_trend_caches(drop_disk=True)
                clear_response_cache()
        except Exception:
            db.session.rollback()

        try:
            SystemSetting.ensure_defaults()
            _set_places_recap_watermark()
        except Exception:
            db.session.rollback()

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
        except Exception:
            db.session.rollback()
