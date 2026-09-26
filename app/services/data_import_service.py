"""
app/services/data_import_service.py
--------------------------------------
Parses the Excel/CSV files an LGU account uploads on the Gov't Data
Upload page (manual upload is the paper's stated "optional fallback"
path for agencies without live API access) and inserts rows DIRECTLY
into whichever real table the LGU user picks on the upload form --
lgu_data or market_data. There is no file-metadata tracking table in
the given schema (no filename/size/status columns anywhere on
lgu_data), so nothing about the upload ITSELF is stored -- only the
parsed data rows, exactly as app/models/lgu_data.py's docstring
describes ("this table stores the parsed government record itself").

Handles the paper's "data imputation techniques, where missing values
in essential business records are filled using statistical averages"
limitation directly: any missing numeric cell in a column present in
the uploaded file is filled with that column's own mean (within the
file) before being saved.

Expected columns per dataset_type (case-insensitive; extra columns are
ignored; columns entirely absent from the file are simply left NULL
on the inserted row rather than raising an error):

  LGU_DATA    : barangay [, zoning_info, closure_records, permit_count,
                 business_density, effective_date]
                 (source is chosen on the upload form, not read from
                 the file)

  MARKET_DATA : industry_type, location [, competitor_count,
                 population_density, historical_success_rate,
                 foot_traffic_index, average_rent, date_recorded]
                 (source is chosen on the upload form)
"""

import datetime

from app.extensions import db
from app.models import LguData, MarketData

# ---------------------------------------------------------------------
# PANDAS IS IMPORTED LAZILY, AND THAT IS WORTH 40 MB
# ---------------------------------------------------------------------
# pandas is used in this module and nowhere else in the application:
# it reads the spreadsheet an LGU officer uploads on the Government
# Data Upload page. That is an occasional administrative action, but
# this module is imported by lgu_controller, which is registered as a
# blueprint in create_app() -- so `import pandas` at the top of this
# file ran on EVERY boot, in every gunicorn worker, whether or not
# anyone ever uploaded anything.
#
# Measured on this codebase: bare interpreter 7.8 MB, +numpy 25.3 MB,
# +pandas 65.6 MB. pandas alone is 40.3 MB resident, which is 8% of a
# free Render instance's entire 512 MB, held permanently to serve a
# page most users never open.
#
# So it is loaded on first use instead. The first upload of a process
# pays the import; every other request in the app's life does not. The
# accessor exists rather than a bare import inside each function
# because half a dozen helpers below need it, and one cached lookup
# reads better than six import statements -- sys.modules makes the
# repeat calls free.
_pd = None


def _pandas():
    """pandas, imported on first use. See the note above."""
    global _pd
    if _pd is None:
        import pandas

        _pd = pandas
    return _pd

LGU_NUMERIC_COLUMNS = ["closure_records", "permit_count", "business_density"]
MARKET_NUMERIC_COLUMNS = [
    "competitor_count",
    "population_density",
    "historical_success_rate",
    "foot_traffic_index",
    "average_rent",
]


def _read_any(file_path):
    if file_path.lower().endswith(".csv"):
        return _pandas().read_csv(file_path)
    return _pandas().read_excel(file_path)


def _normalize_columns(df):
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def _impute_numeric(df, columns, errors=None):
    """Mean-imputation, scoped to columns that actually appear in this
    upload -- the paper's stated fallback for missing values.

    MISSING AND UNREADABLE ARE NOT THE SAME THING, and this used to
    treat them as one. to_numeric(errors="coerce") turns a blank cell
    and the word "twelve" into the same NaN, and both were then
    replaced by the column mean. So a typo did not fail the import: it
    quietly became the average of the other rows, and the LGU user was
    told the file imported cleanly.

    Imputing a genuinely EMPTY cell is the documented behaviour and
    stays. A cell with something unreadable in it is reported and the
    import is refused -- the uploader can see what they typed and fix
    it, which nobody can do with a silently averaged value.
    """
    pd = _pandas()
    for col in columns:
        if col not in df.columns:
            continue

        original = df[col]
        converted = pd.to_numeric(original, errors="coerce")

        if errors is not None:
            # Became NaN but was not blank to begin with => unreadable.
            unreadable = converted.isna() & original.notna() & (
                original.astype(str).str.strip() != ""
            )
            for position in range(len(df)):
                if bool(unreadable.iloc[position]):
                    errors.append(RowError(
                        position + _SPREADSHEET_ROW_OFFSET, col,
                        original.iloc[position],
                        "could not be read as a number "
                        "(leave the cell empty if the figure is unknown)",
                    ))

        df[col] = converted
        if df[col].isna().any():
            fill_value = df[col].mean()
            df[col] = df[col].fillna(0 if pd.isna(fill_value) else fill_value)
    return df


def _parse_date(value, default):
    if value is None or (isinstance(value, float) and _pandas().isna(value)):
        return default
    try:
        return _pandas().to_datetime(value).date()
    except Exception:
        return default


def _cell(row, df, column):
    return row.get(column) if column in df.columns and _pandas().notna(row.get(column)) else None


# ---------------------------------------------------------------------
# ROW-LEVEL VALIDATION
# ---------------------------------------------------------------------
# The old pipeline reported one string for a whole file -- "invalid
# literal for int() with base 10: 'N/A'" -- with no row number and no
# column. On a 76-row barangay register that is not an error message,
# it is a scavenger hunt. Every problem found below carries the row as
# the uploader sees it in their spreadsheet, the column, and what was
# actually in the cell.


class RowError:
    """One problem with one cell, addressed to the person who has the
    spreadsheet open."""

    __slots__ = ("row", "column", "value", "message")

    def __init__(self, row, column, value, message):
        self.row = row
        self.column = column
        self.value = value
        self.message = message

    def as_dict(self):
        return {"row": self.row, "column": self.column,
                "value": self.value, "message": self.message}

    def __str__(self):
        where = f"Row {self.row}"
        if self.column:
            where += f", column '{self.column}'"
        shown = "" if self.value in (None, "") else f" (found {self.value!r})"
        return f"{where}: {self.message}{shown}"

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<RowError {self}>"


# A spreadsheet's first data row is row 2 to the person looking at it:
# row 1 is the header. Reporting pandas' 0-based index would send them
# to the wrong line.
_SPREADSHEET_ROW_OFFSET = 2


def _as_int(value, row_number, column, errors, minimum=0):
    """int(), but a bad cell becomes a RowError instead of an exception
    that abandons the rest of the file."""
    if value is None:
        return None
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        errors.append(RowError(row_number, column, value, "expected a whole number"))
        return None
    if minimum is not None and parsed < minimum:
        errors.append(RowError(row_number, column, value, f"cannot be less than {minimum}"))
        return None
    return parsed


def _as_float(value, row_number, column, errors, minimum=0.0):
    if value is None:
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        errors.append(RowError(row_number, column, value, "expected a number"))
        return None
    if minimum is not None and parsed < minimum:
        errors.append(RowError(row_number, column, value, f"cannot be less than {minimum}"))
        return None
    return parsed


def _resolve_barangay(raw_value, row_number, errors):
    """Match a spelling in the file to one of Tarlac City's 76 real
    barangays.

    WHY THIS IS STRICT. A misspelt barangay used to be inserted
    verbatim, producing an lgu_data row for "San Roqe" that nothing
    ever reads: every lookup in the app keys on the canonical name from
    seed_data.BARANGAY_NAMES. The upload reported success, the row
    existed, and the barangay's figures silently never changed. A
    rejected row the uploader can fix is worth far more than a saved
    row nobody will ever read.

    Case and surrounding whitespace are forgiven, and a near miss gets
    a suggestion rather than a bare refusal.
    """
    from app.ml.seed_data import BARANGAY_NAMES

    name = str(raw_value or "").strip()
    if not name:
        errors.append(RowError(row_number, "barangay", raw_value, "barangay is required"))
        return None

    lookup = {b.casefold(): b for b in BARANGAY_NAMES}
    canonical = lookup.get(name.casefold())
    if canonical:
        return canonical

    import difflib

    close = difflib.get_close_matches(name.casefold(), lookup.keys(), n=1, cutoff=0.82)
    hint = f"; did you mean '{lookup[close[0]]}'?" if close else ""
    errors.append(RowError(
        row_number, "barangay", raw_value,
        f"not a recognised Tarlac City barangay{hint}",
    ))
    return None


def import_lgu_data(file_path, source, uploaded_by_user_id, errors=None):
    """Stages one lgu_data row per data row in the uploaded file.

    Does NOT commit -- see process_upload, which owns the transaction
    so that a file with one bad row leaves the database exactly as it
    found it. Returns the number of rows staged.
    """
    errors = errors if errors is not None else []
    df = _normalize_columns(_read_any(file_path))
    if "barangay" not in df.columns:
        raise ValueError("File must contain a 'barangay' column.")

    df = _impute_numeric(df, LGU_NUMERIC_COLUMNS, errors=errors)
    today = datetime.date.today()
    staged = 0

    for position, (_index, row) in enumerate(df.iterrows()):
        row_number = position + _SPREADSHEET_ROW_OFFSET

        # A wholly blank line is spreadsheet debris, not an error --
        # trailing empty rows are almost universal in hand-kept files.
        if all(_cell(row, df, column) is None for column in df.columns):
            continue

        barangay = _resolve_barangay(row.get("barangay"), row_number, errors)
        if barangay is None:
            continue

        zoning_info = _cell(row, df, "zoning_info")
        closure_records = _as_int(_cell(row, df, "closure_records"), row_number,
                                  "closure_records", errors)
        permit_count = _as_int(_cell(row, df, "permit_count"), row_number,
                               "permit_count", errors)
        business_density = _as_float(_cell(row, df, "business_density"), row_number,
                                     "business_density", errors)
        effective_date = _parse_date(_cell(row, df, "effective_date"), today)

        db.session.add(LguData(
            source=source,
            zoning_info=str(zoning_info).strip() if zoning_info is not None else None,
            closure_records=closure_records,
            barangay=barangay,
            permit_count=permit_count,
            business_density=business_density,
            effective_date=effective_date,
            upload_date=today,
            uploaded_by=uploaded_by_user_id,
        ))
        staged += 1

    return staged


def import_market_data(file_path, source, errors=None):
    """Stages one market_data row per data row in the uploaded file.

    Does NOT commit -- process_upload owns the transaction. Returns the
    number of rows staged.
    """
    errors = errors if errors is not None else []
    df = _normalize_columns(_read_any(file_path))
    required = {"industry_type", "location"}
    if not required.issubset(set(df.columns)):
        raise ValueError(f"File must contain columns: {', '.join(sorted(required))}")

    df = _impute_numeric(df, MARKET_NUMERIC_COLUMNS, errors=errors)
    today = datetime.date.today()
    staged = 0

    for position, (_index, row) in enumerate(df.iterrows()):
        row_number = position + _SPREADSHEET_ROW_OFFSET

        if all(_cell(row, df, column) is None for column in df.columns):
            continue

        industry_type = str(row.get("industry_type") or "").strip()
        location = _resolve_barangay(row.get("location"), row_number, errors)
        if not industry_type:
            errors.append(RowError(row_number, "industry_type", row.get("industry_type"),
                                   "industry_type is required"))
        if location is None or not industry_type:
            continue

        # historical_success_rate is a 0-1 FRACTION in the schema. A
        # register that writes it as a percentage would silently store
        # 85.0 where the model expects 0.85, which moves the feature by
        # two orders of magnitude and quietly poisons every score for
        # that barangay. Caught here rather than discovered later.
        success_rate = _as_float(_cell(row, df, "historical_success_rate"), row_number,
                                 "historical_success_rate", errors)
        if success_rate is not None and success_rate > 1.0:
            errors.append(RowError(
                row_number, "historical_success_rate", success_rate,
                "must be a fraction between 0 and 1 (write 0.85, not 85)",
            ))
            continue

        db.session.add(MarketData(
            industry_type=industry_type,
            location=location,
            competitor_count=_as_int(_cell(row, df, "competitor_count"), row_number,
                                     "competitor_count", errors),
            population_density=_as_float(_cell(row, df, "population_density"), row_number,
                                         "population_density", errors),
            historical_success_rate=success_rate,
            foot_traffic_index=_as_float(_cell(row, df, "foot_traffic_index"), row_number,
                                         "foot_traffic_index", errors),
            average_rent=_as_float(_cell(row, df, "average_rent"), row_number,
                                   "average_rent", errors),
            source=source,
            date_recorded=_parse_date(_cell(row, df, "date_recorded"), today),
        ))
        staged += 1

    return staged


# ---------------------------------------------------------------------
# PERMITS -> PER-INDUSTRY COMPETITOR COUNTS
# ---------------------------------------------------------------------
# The column names a Philippine LGU business-permit register actually
# uses for "what trade is this". PSIC is the Philippine Standard
# Industrial Classification; the others are what the same column gets
# called when somebody exports it from a different system.
_INDUSTRY_COLUMNS = ("psic_code", "psic", "industry_type", "industry",
                     "line_of_business", "business_type", "nature_of_business")


def _industry_column(df):
    for name in _INDUSTRY_COLUMNS:
        if name in df.columns:
            return name
    return None


def derive_permit_counts(file_path):
    """Count permits per (industry, barangay) from an uploaded register.

    WHY THIS IS WORTH DOING, AND WHY IT IS NOT JUST permit_count.

    lgu_data.permit_count is a barangay TOTAL -- every trade added
    together. A barangay with 240 permits and 8 coffee shops has a
    competitor density for coffee of 8, not 248, so the total can
    never answer "how many competitors does this plan face". It is a
    useful signal about how built-up a barangay is, which is exactly
    the job business_density already does in the feature vector.

    What the model actually needs is a count PER TRADE, and a permit
    register has that, one row per business, whenever it carries a
    PSIC code or a line-of-business column. Grouping those rows gives
    an official, per-industry competitor count for every barangay in
    the file -- the same shape as a Google Places count, from the
    city's own records.

    Returns {(industry, barangay): count}, or {} when the file has no
    industry column, which is not an error: plenty of registers are
    barangay summaries with no per-business detail, and those still
    import perfectly well as lgu_data rows.
    """
    df = _normalize_columns(_read_any(file_path))
    column = _industry_column(df)
    if column is None or "barangay" not in df.columns:
        return {}

    from app.ml.constants import canonical_industry_for

    counts = {}
    for _index, row in df.iterrows():
        barangay = str(row.get("barangay") or "").strip()
        raw_industry = row.get(column)
        if not barangay or raw_industry is None or str(raw_industry).strip() == "":
            continue

        # A permit register writes trades however the clerk typed them
        # ("Coffee Shop", "coffee shop / cafe", a bare PSIC code). The
        # app's own taxonomy is the only vocabulary the rest of the
        # system understands, so anything that cannot be mapped into it
        # is counted nowhere rather than counted wrongly.
        industry = canonical_industry_for(str(raw_industry).strip())
        if not industry:
            continue

        # Only permits that are actually in force count as competitors.
        status = str(row.get("status") or "").strip().casefold()
        if status and status not in ("active", "approved", "released", "issued", "valid", "renewed"):
            continue

        key = (industry, barangay)
        counts[key] = counts.get(key, 0) + 1
    return counts


def stage_permit_derived_market_rows(file_path, uploaded_by_user_id=None):
    """Write the per-industry permit counts from `derive_permit_counts`
    into market_data as source='DTI' rows.

    market_data is already the table every scoring path reads, and it
    already records where each figure came from, so official permit
    counts land beside the Google Places ones as a peer source rather
    than in a parallel table the ML would have to learn about. That is
    what makes an upload change the recommendations instead of just
    sitting in the database: the next sweep picks these rows up with no
    other code involved.

    Staged, not committed -- process_upload owns the transaction.
    """
    counts = derive_permit_counts(file_path)
    if not counts:
        return 0

    today = datetime.date.today()
    errors = []
    staged = 0
    for (industry, barangay), count in sorted(counts.items()):
        canonical = _resolve_barangay(barangay, 0, errors)
        if canonical is None:
            errors.clear()   # already reported by the lgu_data pass
            continue
        db.session.add(MarketData(
            industry_type=industry,
            location=canonical,
            competitor_count=count,
            source="DTI",
            date_recorded=today,
        ))
        staged += 1
    return staged


def process_upload(dataset_type, file_path, source, uploaded_by_user_id):
    """Returns (records_saved, status, error_message).

    dataset_type is the target table picked on the upload form --
    'LGU_DATA' or 'MARKET_DATA'. source is the matching ENUM value for
    that table (LguData: 'DTI'/'CLUP'/'Other'; MarketData:
    'PSA'/'DTI'/'Manual'). uploaded_by_user_id is always the logged-in
    LGU account's user_id (lgu_data.uploaded_by is NOT NULL).

    ONE TRANSACTION, ALL OR NOTHING.

    This used to commit inside each importer, so a file that failed on
    row 60 left rows 1-59 in the database and reported an error. The
    LGU user then had a half-imported register, no way to tell how far
    it got, and a re-upload would double every row it had already
    saved. Now nothing is committed until the whole file has parsed
    and validated, and any failure rolls the session back to exactly
    the state it was in before.

    On success this also clears the analytics caches. An upload that
    does not change what the city sees is not an upload, and the
    fingerprint would catch most of it anyway -- but "I uploaded the
    permits and the numbers did not move" is the exact complaint this
    whole pipeline exists to prevent, so it is made explicit.
    """
    if dataset_type not in ("LGU_DATA", "MARKET_DATA"):
        return 0, "error", f"Unknown dataset_type: {dataset_type}"

    errors = []
    try:
        if dataset_type == "LGU_DATA":
            count = import_lgu_data(file_path, source, uploaded_by_user_id, errors=errors)
            # A register with a PSIC/line-of-business column also
            # yields per-industry competitor counts. Same transaction:
            # the permits and the counts derived from them are one fact
            # about the city and must not half-land.
            count += stage_permit_derived_market_rows(file_path, uploaded_by_user_id)
        else:
            count = import_market_data(file_path, source, errors=errors)

        if errors:
            db.session.rollback()
            return 0, "failed", _format_errors(errors)

        if count == 0:
            db.session.rollback()
            return 0, "failed", "No usable data rows were found in that file."

        db.session.commit()
    except Exception as exc:  # noqa: BLE001 - surfaced to the LGU user
        db.session.rollback()
        return 0, "failed", str(exc)

    _clear_analytics_caches()
    return count, "success", None


# How many bad rows to name before summarising. A file with 300
# problems does not need 300 lines -- the uploader has a systematic
# mistake and the first few show it.
_MAX_REPORTED_ERRORS = 8


def _format_errors(errors):
    shown = [str(error) for error in errors[:_MAX_REPORTED_ERRORS]]
    remaining = len(errors) - len(shown)
    if remaining > 0:
        shown.append(f"...and {remaining} more problem(s) in the same file.")
    return (
        f"{len(errors)} problem(s) found -- nothing was imported, "
        f"your data is unchanged. " + " ".join(shown)
    )


# =====================================================================
# COLD START: IS THERE REAL LGU DATA YET?
# =====================================================================

def has_active_lgu_data():
    """True once an LGU account has actually uploaded government data.

    WHAT COUNTS, AND WHY IT IS NOT "any lgu_data row".

    lgu_data is never empty. forecasting_service.find_or_create_lgu_data
    auto-creates a placeholder row for any barangay it is asked to
    score, attributed to the system account, so the model always has a
    business_density to compute with. Those rows are the app talking to
    itself -- counting them would make this function return True on a
    database nobody has ever uploaded anything to, which is the exact
    state it exists to detect.

    So a row is an upload when it is BOTH not attributed to the system
    account AND not carrying the placeholder's own zoning note.

    Both checks are needed, and the second is the load-bearing one.
    Authorship alone looked sufficient until a test caught it:
    _get_system_user_id() falls back to "the first Admin, then any
    user" on a database that has no system account yet, so on a fresh
    install the very first placeholder is attributed to whoever
    happens to be signed in -- and the app would then report an
    official dataset that nobody uploaded. The zoning note is written
    by exactly one line of code
    (forecasting_service.PLACEHOLDER_ZONING_NOTE) and read here, so
    the two cannot drift apart.

    Needs no new table and no new column.

    WHAT THIS GATES. City-wide LGU recommendations only. An SME's own
    saved plans, the saturation map and the trend reports keep working
    from market_data as before -- they are answering "what does the
    data we have say", which is a fair question with or without a
    permit register. "Which barangays should the city steer investment
    to" is not, and that is the one this switches off.
    """
    return db.session.query(_real_upload_query(LguData.lgu_id).exists()).scalar() is True


def _real_upload_query(*entities):
    """The lgu_data rows that came from a person, not from the app."""
    from app.services.forecasting_service import PLACEHOLDER_ZONING_NOTE

    query = db.session.query(*entities) if entities else LguData.query
    query = query.filter(
        db.or_(LguData.zoning_info.is_(None),
               LguData.zoning_info != PLACEHOLDER_ZONING_NOTE)
    )
    system_user_id = _system_user_id_or_none()
    if system_user_id is not None:
        query = query.filter(LguData.uploaded_by != system_user_id)
    return query


def active_lgu_dataset_summary():
    """What to tell an LGU user about the data currently in force --
    when it was uploaded, by whom, and how much of the city it covers.
    Returns None when there is no real upload yet."""
    newest = (
        _real_upload_query()
        .order_by(LguData.upload_date.desc(), LguData.lgu_id.desc())
        .first()
    )
    if newest is None:
        return None

    barangays = _real_upload_query(LguData.barangay).distinct().count()
    return {
        "upload_date": newest.upload_date,
        "source": newest.source,
        "barangays_covered": barangays,
        "uploaded_by": newest.uploaded_by,
    }


def _system_user_id_or_none():
    """The id of the account placeholder rows are attributed to, or
    None if it does not exist yet (a brand-new database). Deliberately
    does NOT create it -- a read-only question must not write a row."""
    from app.models.user import User
    from app.services.forecasting_service import SYSTEM_USER_EMAIL

    row = db.session.query(User.user_id).filter_by(email=SYSTEM_USER_EMAIL).first()
    return row[0] if row else None


def _clear_analytics_caches():
    """Forget every memoised figure so the next page reflects the
    upload. Best-effort: a cache that will not clear must not turn a
    successful import into a failed one."""
    try:
        from app.services.response_cache import clear_response_cache
        from app.services.trend_analytics_service import clear_trend_caches

        clear_trend_caches()
        clear_response_cache()
    except Exception:  # pragma: no cover - defensive
        from flask import current_app

        current_app.logger.warning("could not clear analytics caches after upload", exc_info=True)
