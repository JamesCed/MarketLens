"""
app/services/data_import_service.py
--------------------------------------
Reads the Excel/CSV files an LGU account uploads on the Gov't Data
Upload page and writes them into the database, in ONE transaction.

TWO DATASETS, TWO TEMPLATES
  Gov't / Zoning Data  (target LGU_DATA -> lgu_data)
      One row per barangay: barangay, zoning_info, business_density
      (all three required), plus permit_count, closure_records and
      effective_date for the record. business_density is the figure
      the saturation model reads for every forecast in the barangay.

  Market Data  (target MARKET_DATA -> market_data)
      The business permit register, one row per business: barangay and
      industry_type (both required), status and line_of_business
      (used to count competitors), business_name and permit_no for
      the record. Grouped by industry and barangay it becomes the
      official competitor count every recommendation is scored from.
      A summary file with one row per industry per barangay
      (industry_type, barangay, competitor_count, ...) is accepted too.

  A combined file -- a permit register that also carries the zoning
  columns, like the bundled sample -- imports under either target and
  fills both tables.

WHAT EVERY SUCCESSFUL UPLOAD WRITES
  * lgu_data: ONE row per barangay in the file. A register's permits
    are totalled per barangay (permits in force, closures), never
    stored one row per permit: the scoring engine only ever reads the
    newest row per barangay, and ten thousand per-permit rows made
    every page that scores anything load all of them.
  * market_data: one competitor count per (industry, barangay) when the
    file names each business's industry, source 'DTI'.
  * subcategory_market_data: one count per (industry, sub-category,
    barangay) when the line of business says what kind of business it
    is (a bakery rather than "food").

WHY IT IS BATCHED
  Rows are written with executemany INSERTs, a few statements for the
  whole file. Writing them one ORM object at a time cost one network
  round trip per row against the hosted MySQL -- about fifteen thousand
  for the sample register -- which outlasted the web server's 120 s
  request limit, so the worker was killed, the transaction rolled back
  and the upload silently never happened.

ALL OR NOTHING
  Nothing is committed until the whole file has been read and checked.
  A row that cannot be read is reported with its row number, column
  and value, and nothing is imported.
"""

import csv
import datetime
import functools
import os
import re
from collections import Counter, defaultdict

from sqlalchemy import insert

from app.extensions import db
from app.models import LguData, MarketData

_pd = None


def _pandas():
    """pandas, imported on first use. Only the legacy .xls format still
    needs it (through xlrd); .xlsx and .csv are read directly, which is
    faster and keeps pandas out of the upload's memory footprint."""
    global _pd
    if _pd is None:
        import pandas

        _pd = pandas
    return _pd


DATASET_LABELS = {"LGU_DATA": "Gov't / Zoning Data", "MARKET_DATA": "Market Data"}

ZONING_REQUIRED = ("barangay", "zoning_info", "business_density")
MARKET_REQUIRED = ("barangay", "industry_type")
MARKET_SUMMARY_NUMERIC = (
    "competitor_count",
    "population_density",
    "historical_success_rate",
    "foot_traffic_index",
    "average_rent",
)
AREA_FEATURE_FIELDS = ("population_density", "foot_traffic_index", "average_rent", "historical_success_rate")

LGU_SOURCE_FOR_MARKET_SOURCE = {"DTI": "DTI", "PSA": "Other", "Manual": "Other"}

_INDUSTRY_COLUMNS = ("psic_code", "psic", "industry_type", "industry",
                     "line_of_business", "business_type", "nature_of_business")
_SUBCATEGORY_TEXT_COLUMNS = ("subcategory", "line_of_business", "nature_of_business", "business_type")
_IN_FORCE_STATUSES = ("active", "approved", "released", "issued", "valid", "renewed")

_HEADER_ALIASES = {
    "location": "barangay",
    "brgy": "barangay",
    "barangay_name": "barangay",
    "zoning": "zoning_info",
    "zone": "zoning_info",
    "zoning_classification": "zoning_info",
    "permit_number": "permit_no",
    "business_permit_no": "permit_no",
    "permit_status": "status",
    "psic": "psic_code",
}

_PERSONAL_TOKENS = frozenset({
    "owner", "owners", "proprietor", "proprietress", "proprietors", "taxpayer", "applicant", "registrant",
    "surname", "firstname", "lastname", "middlename", "fullname", "contact", "phone", "telephone", "tel",
    "mobile", "cellphone", "email", "tin", "sss", "gsis", "philhealth", "pagibig", "birthday", "birthdate",
    "birth", "dob", "age", "sex", "gender", "religion", "citizenship", "nationality", "address", "street",
    "residence", "residential", "signature",
})
_PERSONAL_PAIRS = (("first", "name"), ("last", "name"), ("middle", "name"), ("full", "name"),
                   ("e", "mail"), ("civil", "status"), ("pag", "ibig"))
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_MOBILE_RE = re.compile(r"(?<!\d)(?:\+?63|0)9\d{2}[\s-]?\d{3}[\s-]?\d{4}(?!\d)")

_SPREADSHEET_ROW_OFFSET = 2
_MAX_REPORTED_ERRORS = 8
_INSERT_CHUNK = 2000


class UploadRefused(Exception):
    """The file as a whole cannot be imported (a missing required
    column, an empty sheet). Carries a message for the uploader."""


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

    def __repr__(self):
        return f"<RowError {self}>"


class _Table:
    """The uploaded sheet: normalised column names, the non-empty rows
    as (spreadsheet row number, {column: value}), and the row numbers
    that hold more values than there are column headers."""

    __slots__ = ("columns", "rows", "overflow_rows")

    def __init__(self, columns, rows, overflow_rows=()):
        self.columns = columns
        self.rows = rows
        self.overflow_rows = list(overflow_rows)

    def has(self, column):
        return column in self.columns


def _normalise_header(value):
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return _HEADER_ALIASES.get(text, text)


def _clean(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, float) and value != value:
        return None
    return value


def _build_table(header, raw_rows):
    columns, positions = [], []
    for position, name in enumerate(header):
        normalised = _normalise_header(name)
        if normalised and normalised not in columns:
            columns.append(normalised)
            positions.append(position)
    rows, overflow = [], []
    width = len(header)
    for index, values in enumerate(raw_rows):
        values = list(values)
        record = {}
        for column, position in zip(columns, positions):
            record[column] = _clean(values[position]) if position < len(values) else None
        if len(values) > width and any(_clean(value) is not None for value in values[width:]):
            overflow.append(index + _SPREADSHEET_ROW_OFFSET)
        if any(value is not None for value in record.values()):
            rows.append((index + _SPREADSHEET_ROW_OFFSET, record))
    return _Table(columns, rows, overflow)


def _read_csv(file_path):
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(file_path, newline="", encoding=encoding) as handle:
                reader = csv.reader(handle)
                header = next(reader, None) or []
                return _build_table(header, reader)
        except UnicodeDecodeError:
            continue
    raise UploadRefused("The CSV file's text encoding could not be read. Save it as 'CSV UTF-8' and upload again.")


def _read_xlsx(file_path):
    """The data sheet of a workbook: the first sheet whose header row
    names a barangay, else the first sheet. read_only streams the rows
    instead of building the whole workbook in memory."""
    import openpyxl

    try:
        workbook = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    except Exception as exc:
        raise UploadRefused(f"The Excel file could not be opened ({exc}). Save it as .xlsx or CSV and try again.")
    try:
        chosen = None
        for sheet in workbook.worksheets:
            first = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), None) or ()
            names = {_normalise_header(cell) for cell in first}
            if "barangay" in names:
                chosen = sheet
                break
        if chosen is None:
            chosen = workbook.worksheets[0]
        rows = chosen.iter_rows(values_only=True)
        header = list(next(rows, None) or [])
        return _build_table(header, rows)
    finally:
        workbook.close()


def _read_xls(file_path):
    try:
        frame = _pandas().read_excel(file_path, dtype=object)
    except ImportError:
        raise UploadRefused("Old-style .xls files are not supported here. In Excel choose "
                            "File > Save As > Excel Workbook (.xlsx) or CSV, then upload that file.")
    return _build_table(list(frame.columns), frame.where(frame.notna(), None).values.tolist())


_READ_CACHE = {}


def _read_table(file_path):
    """The uploaded file as a _Table, read once per upload. derive_*()
    and process_upload() may each ask for it; the parse is the slowest
    step of an import, so it is kept for the duration of the upload,
    keyed on the file's path, size and modification time.
    process_upload() drops it when the upload ends."""
    try:
        stat = os.stat(file_path)
        key = (os.path.abspath(file_path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None
    hit = _READ_CACHE.get("table")
    if key is not None and hit is not None and hit[0] == key:
        return hit[1]

    extension = os.path.splitext(file_path)[1].lower()
    if extension == ".csv":
        table = _read_csv(file_path)
    elif extension in (".xlsx", ".xlsm"):
        table = _read_xlsx(file_path)
    elif extension == ".xls":
        table = _read_xls(file_path)
    else:
        raise UploadRefused("Only Excel (.xlsx) or CSV (.csv) files are accepted.")
    if key is not None:
        _READ_CACHE["table"] = (key, table)
    return table


def _number(value):
    """float(value), accepting thousands separators and a peso sign.
    Raises ValueError when the cell holds something that is not a
    number."""
    if isinstance(value, bool):
        raise ValueError(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("₱", "")
    if text.upper().startswith("PHP"):
        text = text[3:].strip()
    return float(text)


def _parse_date(value):
    """A date from a cell, or None when it cannot be read. Excel gives
    datetimes; CSV gives text, ISO first."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, (int, float)) and 20000 < value < 80000:
        return datetime.date(1899, 12, 30) + datetime.timedelta(days=int(value))
    return _parse_date_text(str(value).strip())


@functools.lru_cache(maxsize=512)
def _parse_date_text(text):
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        pass
    for pattern in ("%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _resolve_barangay(raw_value, row_number, errors):
    """Match a spelling in the file to one of Tarlac City's 76 real
    barangays.

    WHY THIS IS STRICT. A misspelt barangay inserted verbatim produces
    a row nothing ever reads: every lookup in the app keys on the
    canonical name from seed_data.BARANGAY_NAMES. A rejected row the
    uploader can fix is worth far more than a saved row nobody will
    ever read. Case, punctuation and known aliases are forgiven, and a
    near miss gets a suggestion rather than a bare refusal.
    """
    name = str(raw_value or "").strip()
    if not name:
        errors.append(RowError(row_number, "barangay", raw_value, "barangay is required"))
        return None
    canonical = _canonical_barangay_cached(name)
    if canonical:
        return canonical

    import difflib

    from app.ml.seed_data import BARANGAY_NAMES

    lookup = {b.casefold(): b for b in BARANGAY_NAMES}
    close = difflib.get_close_matches(name.casefold(), lookup.keys(), n=1, cutoff=0.82)
    hint = f"; did you mean '{lookup[close[0]]}'?" if close else ""
    errors.append(RowError(row_number, "barangay", raw_value,
                           f"not a recognised Tarlac City barangay{hint}"))
    return None


@functools.lru_cache(maxsize=4096)
def _canonical_barangay_cached(name):
    from app.ml.seed_data import BARANGAY_NAMES, canonical_barangay

    for official in BARANGAY_NAMES:
        if official.casefold() == name.casefold():
            return official
    return canonical_barangay(name)


@functools.lru_cache(maxsize=4096)
def _canonical_industry_cached(text):
    from app.ml.constants import canonical_industry_for

    return canonical_industry_for(text)


@functools.lru_cache(maxsize=8192)
def _subcategory_cached(industry, text):
    from app.ml.subcategories import match_subcategory

    return match_subcategory(industry, text)


def _industry_column(table):
    for name in _INDUSTRY_COLUMNS:
        if table.has(name):
            return name
    return None


def _permit_in_force(record):
    status = str(record.get("status") or "").strip().casefold()
    return not status or status in _IN_FORCE_STATUSES


def _is_industry_identifier(raw_value):
    """True when an industry cell is a code rather than a description:
    an exact section name, a single section letter, or a numeric PSIC
    code. Those say WHICH section, never what kind of business."""
    from app.ml.constants import BUSINESS_TYPES

    text = str(raw_value or "").strip()
    if not text:
        return True
    if text.casefold() in {name.casefold() for name in BUSINESS_TYPES}:
        return True
    if len(text) == 1 and text.isalpha():
        return True
    return text.replace(".", "").replace("-", "").isdigit()


def _subcategory_for(table, industry_column, record, industry):
    """The sub-category a register row belongs to, or None. The line of
    business is read first (a few hundred distinct values, so the match
    is cached); the business name and a descriptive industry cell are
    only consulted when it says nothing."""
    primary = " ".join(str(record[c]) for c in _SUBCATEGORY_TEXT_COLUMNS
                       if c != industry_column and record.get(c) is not None)
    raw_industry = record.get(industry_column)
    if industry_column in _SUBCATEGORY_TEXT_COLUMNS or not _is_industry_identifier(raw_industry):
        primary = f"{primary} {raw_industry}".strip()
    match = _subcategory_cached(industry, primary) if primary else None
    if match or record.get("business_name") is None:
        return match
    return _subcategory_cached(industry, f"{primary} {record['business_name']}".strip())


class _Register:
    """A permit register, one row per business, summarised: in-force
    permits per (industry, barangay), per (industry, sub-category,
    barangay), and per barangay."""

    def __init__(self):
        self.counts = Counter()
        self.subcategory_counts = Counter()
        self.in_force = Counter()
        self.not_in_force = Counter()
        self.unmatched_rows = []
        self.duplicate_rows = []
        self.permits_read = 0


def _summarise_register(table, industry_column, errors=None, strict=True):
    """Group a register's rows. strict=True reports an unknown barangay
    or a blank industry as a RowError; strict=False (the derive_*()
    helpers) skips such rows silently."""
    register = _Register()
    seen_permits = set()
    has_permit_no = table.has("permit_no")
    errors = errors if errors is not None else []
    for row_number, record in table.rows:
        if strict:
            barangay = _resolve_barangay(record.get("barangay"), row_number, errors)
        else:
            raw = record.get("barangay")
            barangay = _canonical_barangay_cached(str(raw).strip()) if raw is not None else None
        raw_industry = record.get(industry_column)
        if raw_industry is None:
            if strict:
                errors.append(RowError(row_number, industry_column, None,
                                       f"{industry_column} is required (PSIC section name, letter or code)"))
            continue
        if barangay is None:
            continue
        if has_permit_no and record.get("permit_no") is not None:
            permit = str(record["permit_no"]).strip().casefold()
            if permit in seen_permits:
                register.duplicate_rows.append(row_number)
                continue
            seen_permits.add(permit)
        register.permits_read += 1
        industry = _canonical_industry_cached(str(raw_industry).strip())
        in_force = _permit_in_force(record)
        if in_force:
            register.in_force[barangay] += 1
        else:
            register.not_in_force[barangay] += 1
        if not industry:
            register.unmatched_rows.append(row_number)
            continue
        if not in_force:
            continue
        register.counts[(industry, barangay)] += 1
        subcategory = _subcategory_for(table, industry_column, record, industry)
        if subcategory:
            register.subcategory_counts[(industry, subcategory, barangay)] += 1
    return register


def derive_permit_counts(file_path):
    """{(industry, barangay): in-force permits} from an uploaded register,
    or {} when the file has no industry column. Rows whose barangay or
    trade cannot be matched are not counted."""
    table = _read_table(file_path)
    column = _industry_column(table)
    if column is None or not table.has("barangay"):
        return {}
    return dict(_summarise_register(table, column, strict=False).counts)


def derive_permit_subcategory_counts(file_path):
    """{(industry, sub-category, barangay): in-force permits} -- the
    direct-competition figure (how many BAKERIES, not how many food
    businesses) from the same register rows."""
    table = _read_table(file_path)
    column = _industry_column(table)
    if column is None or not table.has("barangay"):
        return {}
    return dict(_summarise_register(table, column, strict=False).subcategory_counts)


def area_features_for(pairs):
    """{(industry, location): {field: value}} for the four area figures a
    market row carries besides its competitor count.

    A permit register only says how many businesses hold a permit, so a
    row derived from it has no population density, foot traffic, rent or
    success rate of its own. It becomes the newest row for its pair, and
    every scoring path reads the newest row's figures -- left empty, they
    would reach the model as zeros. The figures are taken from the
    freshest earlier row for the same pair that has them, else from the
    barangay's reference profile.
    """
    from app.ml.seed_data import get_barangay_profile

    pairs = set(pairs)
    if not pairs:
        return {}
    industries = sorted({industry for industry, _ in pairs})
    locations = sorted({location for _, location in pairs})
    rows = (
        db.session.query(
            MarketData.industry_type,
            MarketData.location,
            MarketData.date_recorded,
            MarketData.market_id,
            *(getattr(MarketData, field) for field in AREA_FEATURE_FIELDS),
        )
        .filter(
            MarketData.industry_type.in_(industries),
            MarketData.location.in_(locations),
            MarketData.population_density.isnot(None),
        )
        .all()
    )
    freshest = {}
    for industry, location, recorded, market_id, *values in rows:
        key = (industry, location)
        if key not in pairs:
            continue
        rank = (recorded or datetime.date.min, market_id or 0)
        if key not in freshest or rank > freshest[key][0]:
            freshest[key] = (rank, values)

    features = {}
    for key in pairs:
        if key in freshest:
            values = freshest[key][1]
            if all(value is not None for value in values):
                features[key] = dict(zip(AREA_FEATURE_FIELDS, values))
                continue
        profile = get_barangay_profile(key[1])
        if profile:
            features[key] = {field: profile[field] for field in AREA_FEATURE_FIELDS}
    return features


def _carried_barangay_figures(barangays):
    """{barangay: (business_density, zoning_info)} -- what the newest
    lgu_data row already says, so a file that does not carry those
    columns does not blank them. Placeholder zoning notes are not
    carried (they would hide the upload from has_active_lgu_data), and
    a barangay with no row at all falls back to its reference profile."""
    from app.ml.seed_data import get_barangay_profile
    from app.services.forecasting_service import PLACEHOLDER_ZONING_NOTE, _lgu_rows

    newest = _lgu_rows(set(barangays)) if barangays else {}
    carried = {}
    for barangay in barangays:
        row = newest.get(barangay)
        density = row.business_density if row is not None else None
        zoning = row.zoning_info if row is not None and row.zoning_info != PLACEHOLDER_ZONING_NOTE else None
        if density is None:
            profile = get_barangay_profile(barangay)
            density = profile["business_density"] if profile else None
        carried[barangay] = (float(density) if density is not None else None, zoning)
    return carried


def _read_numbers(table, column, errors, required=False, minimum=0.0):
    """{row_number: float} for one numeric column. A blank cell in a
    required column, or anything unreadable anywhere, is a RowError;
    a blank cell elsewhere is filled with the column's average (the
    paper's data imputation)."""
    if not table.has(column):
        return {}
    values, blanks = {}, []
    for row_number, record in table.rows:
        raw = record.get(column)
        if raw is None:
            if required:
                errors.append(RowError(row_number, column, None, f"{column} is required on every row"))
            else:
                blanks.append(row_number)
            continue
        try:
            number = _number(raw)
        except (TypeError, ValueError):
            errors.append(RowError(row_number, column, raw,
                                   "could not be read as a number "
                                   "(leave the cell empty if the figure is unknown)"))
            continue
        if minimum is not None and number < minimum:
            errors.append(RowError(row_number, column, raw, f"cannot be less than {minimum:g}"))
            continue
        values[row_number] = number
    if blanks and values:
        average = sum(values.values()) / len(values)
        for row_number in blanks:
            values[row_number] = average
    return values


def _read_dates(table, column, errors):
    if not table.has(column):
        return {}
    dates = {}
    for row_number, record in table.rows:
        raw = record.get(column)
        if raw is None:
            continue
        parsed = _parse_date(raw)
        if parsed is None:
            errors.append(RowError(row_number, column, raw, "could not be read as a date (use YYYY-MM-DD)"))
            continue
        dates[row_number] = parsed
    return dates


def _joined_text(values, limit=255):
    ranked = [text for text, _n in Counter(values).most_common()]
    return "; ".join(ranked)[:limit] if ranked else None


def _lgu_summary_rows(table, errors, source, uploaded_by, register=None, require_zoning=False,
                      report_barangays=True):
    """One lgu_data row per barangay in the file.

    Zoning columns present in the file are used as written (density
    averaged, zoning classifications listed most common first,
    closures summed); absent ones keep the barangay's current figures.
    permit_count is the number of permits in force when the file is a
    register, else the sum of its permit_count cells."""
    density = _read_numbers(table, "business_density", errors, required=require_zoning)
    permits = _read_numbers(table, "permit_count", errors)
    closures = _read_numbers(table, "closure_records", errors)
    dates = _read_dates(table, "effective_date", errors)

    per_barangay = defaultdict(lambda: {"density": [], "zoning": [], "permits": 0.0, "has_permits": False,
                                        "closures": 0.0, "has_closures": False, "dates": []})
    lookup_errors = errors if report_barangays else []
    for row_number, record in table.rows:
        barangay = _resolve_barangay(record.get("barangay"), row_number, lookup_errors)
        if barangay is None:
            continue
        entry = per_barangay[barangay]
        if row_number in density:
            entry["density"].append(density[row_number])
        zoning = record.get("zoning_info")
        if zoning is not None:
            entry["zoning"].append(str(zoning).strip())
        elif require_zoning:
            errors.append(RowError(row_number, "zoning_info", None, "zoning_info is required on every row"))
        if row_number in permits:
            entry["permits"] += permits[row_number]
            entry["has_permits"] = True
        if row_number in closures:
            entry["closures"] += closures[row_number]
            entry["has_closures"] = True
        if row_number in dates:
            entry["dates"].append(dates[row_number])

    today = datetime.date.today()
    carried = _carried_barangay_figures(list(per_barangay))
    rows = []
    for barangay, entry in per_barangay.items():
        carried_density, carried_zoning = carried.get(barangay, (None, None))
        if register is not None:
            permit_count = register.in_force.get(barangay, 0)
            closure_records = (round(entry["closures"]) if entry["has_closures"]
                               else register.not_in_force.get(barangay, 0))
        else:
            permit_count = round(entry["permits"]) if entry["has_permits"] else None
            closure_records = round(entry["closures"]) if entry["has_closures"] else None
        rows.append({
            "source": source,
            "zoning_info": _joined_text(entry["zoning"]) or carried_zoning,
            "closure_records": closure_records,
            "barangay": barangay,
            "permit_count": permit_count,
            "business_density": (round(sum(entry["density"]) / len(entry["density"]), 2)
                                 if entry["density"] else carried_density),
            "effective_date": max(entry["dates"]) if entry["dates"] else today,
            "upload_date": today,
            "uploaded_by": uploaded_by,
            "archived_at": None,
            "archived_by": None,
            "archive_reason": None,
        })
    return rows


def _register_market_rows(register):
    """market_data and subcategory_market_data rows for a register's
    counts. Each market row carries the area figures it would otherwise
    lack (see area_features_for)."""
    today = datetime.date.today()
    features = area_features_for(register.counts.keys())
    market_rows = []
    for (industry, barangay), count in sorted(register.counts.items()):
        figures = features.get((industry, barangay), {})
        market_rows.append({
            "industry_type": industry,
            "location": barangay,
            "competitor_count": count,
            "population_density": figures.get("population_density"),
            "historical_success_rate": figures.get("historical_success_rate"),
            "foot_traffic_index": figures.get("foot_traffic_index"),
            "average_rent": figures.get("average_rent"),
            "source": "DTI",
            "date_recorded": today,
            "archived_at": None,
            "archived_by": None,
            "archive_reason": None,
        })
    subcategory_rows = [
        {"industry_type": industry, "subcategory": subcategory, "location": barangay,
         "competitor_count": count, "source": "DTI", "date_recorded": today}
        for (industry, subcategory, barangay), count in sorted(register.subcategory_counts.items())
    ]
    return market_rows, subcategory_rows


def _market_summary_rows(table, errors, source):
    """market_data rows from a summary file: one row per industry per
    barangay with its own competitor count and area figures."""
    numbers = {column: _read_numbers(table, column, errors) for column in MARKET_SUMMARY_NUMERIC}
    dates = _read_dates(table, "date_recorded", errors)
    today = datetime.date.today()
    staged = []
    for row_number, record in table.rows:
        barangay = _resolve_barangay(record.get("barangay"), row_number, errors)
        raw_industry = record.get("industry_type")
        industry = None
        if raw_industry is None:
            errors.append(RowError(row_number, "industry_type", None, "industry_type is required"))
        else:
            industry = _canonical_industry_cached(str(raw_industry).strip())
            if industry is None:
                errors.append(RowError(row_number, "industry_type", raw_industry,
                                       "not a recognised industry -- use a PSIC section name, "
                                       "section letter or PSIC code"))
        rate = numbers["historical_success_rate"].get(row_number)
        if rate is not None and rate > 1.0:
            errors.append(RowError(row_number, "historical_success_rate", record.get("historical_success_rate"),
                                   "must be a fraction between 0 and 1 (write 0.85, not 85)"))
            continue
        if barangay is None or industry is None:
            continue
        count = numbers["competitor_count"].get(row_number)
        staged.append({
            "industry_type": industry,
            "location": barangay,
            "competitor_count": int(round(count)) if count is not None else None,
            "population_density": numbers["population_density"].get(row_number),
            "historical_success_rate": rate,
            "foot_traffic_index": numbers["foot_traffic_index"].get(row_number),
            "average_rent": numbers["average_rent"].get(row_number),
            "source": source,
            "date_recorded": dates.get(row_number, today),
            "archived_at": None,
            "archived_by": None,
            "archive_reason": None,
        })

    missing = [field for field in AREA_FEATURE_FIELDS if not table.has(field)]
    if missing and staged:
        features = area_features_for((row["industry_type"], row["location"]) for row in staged)
        for row in staged:
            figures = features.get((row["industry_type"], row["location"]), {})
            for field in missing:
                row[field] = figures.get(field)
    return staged


def _bulk_insert(model, rows):
    """executemany INSERTs in chunks -- one round trip per chunk, not per
    row. Every dict carries the same keys, so each chunk is one
    statement."""
    for start in range(0, len(rows), _INSERT_CHUNK):
        db.session.execute(insert(model), rows[start:start + _INSERT_CHUNK])


def _personal_columns(table):
    """Column headers that name personal information -- an owner's name,
    contact details, a home address, ID numbers, birth dates."""
    flagged = []
    for column in table.columns:
        tokens = column.split("_")
        joined = set(tokens)
        if joined & _PERSONAL_TOKENS or any(a in joined and b in joined for a, b in _PERSONAL_PAIRS):
            flagged.append(column)
    return flagged


def _personal_values(table):
    """(column, row) of the first cell holding an e-mail address or a
    Philippine mobile number, in any column."""
    for row_number, record in table.rows:
        for column, value in record.items():
            if isinstance(value, str) and (_EMAIL_RE.search(value) or _MOBILE_RE.search(value)):
                return column, row_number
    return None


def _refuse_personal_data(table):
    """Data minimisation (RA 10173, Sec. 11 -- proportionality): MarketLens
    needs no one's name, contact details, address or ID numbers to count
    businesses, so a file that carries them is refused before anything
    is read from it, and the uploader is told which columns to remove."""
    flagged = _personal_columns(table)
    if flagged:
        names = ", ".join(f"'{column}'" for column in flagged)
        raise UploadRefused(
            f"This file has column(s) that hold personal information: {names}. MarketLens does not need "
            f"them. Delete those columns (owner names, contact numbers, e-mail addresses, home addresses, "
            f"TIN or other ID numbers, birth dates) and upload the file again -- nothing was imported."
        )
    found = _personal_values(table)
    if found:
        column, row_number = found
        raise UploadRefused(
            f"Row {row_number}, column '{column}' holds an e-mail address or mobile number. Remove contact "
            f"details from the file and upload it again -- nothing was imported."
        )


def _require_columns(table, columns, dataset_type):
    missing = [column for column in columns if not table.has(column)]
    if not missing:
        return
    label = DATASET_LABELS[dataset_type]
    names = ", ".join(f"'{column}'" for column in missing)
    hint = ""
    if dataset_type == "LGU_DATA" and _industry_column(table) and "barangay" not in missing:
        hint = (" This file looks like a business permit list -- choose 'Market Data' as the "
                "target table, or add the zoning columns.")
    elif dataset_type == "MARKET_DATA" and table.has("business_density") and "barangay" not in missing:
        hint = " This file looks like zoning data -- choose 'Gov't / Zoning Data' as the target table."
    raise UploadRefused(
        f"{label} needs the column(s) {names}. Download the {label} template for the exact headers "
        f"-- nothing was imported, your data is unchanged.{hint}"
    )


def _format_errors(errors):
    shown = [str(error) for error in errors[:_MAX_REPORTED_ERRORS]]
    remaining = len(errors) - len(shown)
    if remaining > 0:
        shown.append(f"...and {remaining} more problem(s) in the same file.")
    return (
        f"{len(errors)} problem(s) found -- nothing was imported, "
        f"your data is unchanged. " + " ".join(shown)
    )


def _rows_note(rows, what):
    sample = ", ".join(str(r) for r in rows[:5])
    more = "" if len(rows) <= 5 else f" and {len(rows) - 5:,} more"
    return f"{len(rows):,} {what} (row {sample}{more})"


def _success_message(summary):
    parts = []
    if summary.get("lgu"):
        parts.append(f"{summary['lgu']:,} barangay row(s) saved to Gov't / Zoning Data")
    if summary.get("market"):
        parts.append(f"{summary['market']:,} industry count(s) saved to Market Data")
    if summary.get("subcategory"):
        parts.append(f"{summary['subcategory']:,} sub-category count(s)")
    message = "; ".join(parts) + "."
    register = summary.get("register")
    if register is not None:
        in_force = sum(register.in_force.values())
        message += (f" {register.permits_read:,} permit(s) read, {in_force:,} in force "
                    f"and counted as competitors.")
        if register.unmatched_rows:
            message += " " + _rows_note(register.unmatched_rows,
                                        "permit(s) had an industry that matches no PSIC section and were not counted")
            message += "."
        if register.duplicate_rows:
            message += " " + _rows_note(register.duplicate_rows, "repeated permit number(s) were counted once") + "."
    message += " City-wide recommendations now use this data."
    return message


class _Progress:
    """No-op progress reporter for callers that do not track one."""

    def step_to(self, text):
        pass

    def finish(self, count, status, message):
        pass


def process_upload(dataset_type, file_path, source, uploaded_by_user_id, progress=None):
    """Returns (records_saved, status, message).

    dataset_type is the target picked on the upload form -- 'LGU_DATA'
    (Gov't / Zoning Data) or 'MARKET_DATA'. source is the matching ENUM
    value for that table. uploaded_by_user_id is the LGU account's id.
    `progress`, when given, is told each step and receives the result as
    soon as the data is committed -- before the alert sweep and the
    cache warm-up that follow, which the uploader need not wait for.

    ONE TRANSACTION, ALL OR NOTHING: nothing is committed until the
    whole file has parsed and validated, and any failure rolls the
    session back to exactly the state it was in before.
    """
    progress = progress or _Progress()
    if dataset_type not in DATASET_LABELS:
        return 0, "error", f"Unknown dataset_type: {dataset_type}"

    errors = []
    summary = {}
    try:
        progress.step_to("Checking the file")
        from app.utils.upload_checks import check_upload_content

        problem = check_upload_content(file_path)
        if problem:
            raise UploadRefused(f"{problem} Nothing was imported.")
        progress.step_to("Reading the file")
        table = _read_table(file_path)
        if not table.rows:
            raise UploadRefused("No data rows were found in that file -- the first row must be the "
                                "column headers and the data must start on the second row.")
        _refuse_personal_data(table)
        _require_columns(table, ["barangay"], dataset_type)
        industry_column = _industry_column(table)
        for row_number in table.overflow_rows:
            errors.append(RowError(row_number, None, None,
                                   "has more values than there are column headers -- a value that "
                                   "contains a comma must be in quotes"))

        progress.step_to(f"Checking {len(table.rows):,} row(s)")
        lgu_rows, market_rows, subcategory_rows = [], [], []
        if dataset_type == "LGU_DATA":
            _require_columns(table, ZONING_REQUIRED, dataset_type)
            register = (_summarise_register(table, industry_column, errors)
                        if industry_column else None)
            lgu_rows = _lgu_summary_rows(table, errors, source, uploaded_by_user_id, register=register,
                                         require_zoning=True, report_barangays=register is None)
        else:
            lgu_source = LGU_SOURCE_FOR_MARKET_SOURCE.get(source, "Other")
            if any(table.has(column) for column in MARKET_SUMMARY_NUMERIC):
                _require_columns(table, MARKET_REQUIRED, dataset_type)
                register = None
                market_rows = _market_summary_rows(table, errors, source)
            else:
                if industry_column is None:
                    _require_columns(table, MARKET_REQUIRED, dataset_type)
                register = _summarise_register(table, industry_column, errors)
            lgu_rows = _lgu_summary_rows(table, errors, lgu_source, uploaded_by_user_id,
                                         register=register, report_barangays=False)

        if register is not None and not errors:
            market_rows, subcategory_rows = _register_market_rows(register)
        summary.update(register=register)

        if errors:
            db.session.rollback()
            return 0, "failed", _format_errors(errors)
        if not lgu_rows and not market_rows:
            db.session.rollback()
            return 0, "failed", "No usable data rows were found in that file."

        progress.step_to("Saving to the database")
        from app.models import SubcategoryMarketData

        _bulk_insert(LguData, lgu_rows)
        _bulk_insert(MarketData, market_rows)
        _bulk_insert(SubcategoryMarketData, subcategory_rows)
        db.session.commit()
        summary.update(lgu=len(lgu_rows), market=len(market_rows), subcategory=len(subcategory_rows))
    except UploadRefused as refused:
        db.session.rollback()
        return 0, "failed", str(refused)
    except Exception as exc:
        db.session.rollback()
        return 0, "failed", f"The file could not be imported: {exc}"
    finally:
        _READ_CACHE.clear()

    count = len(lgu_rows) + len(market_rows)
    message = _success_message(summary)
    _clear_analytics_caches()
    progress.finish(count, "success", message)
    progress.step_to("Updating alerts")
    _run_alert_sweeps()
    return count, "success", message


def _run_alert_sweeps():
    """Tell whoever asked to be told that the numbers moved.

    AFTER the commit and after the caches are cleared, in that order:
    the sweep re-reads market_data and re-scores it, so running it
    before the commit would compare the new data against itself, and
    running it before the caches are cleared would read the figures the
    upload just superseded. Wrapped, because an import that succeeded
    must report success even if the alerting behind it is broken.
    """
    try:
        from app.services.market_alert_service import run_all_alert_sweeps

        run_all_alert_sweeps()
    except Exception:
        from flask import current_app

        current_app.logger.warning("could not run market alert sweep after upload",
                                   exc_info=True)


def has_active_lgu_data():
    """True once an LGU account has actually uploaded government data.

    lgu_data is never empty: forecasting_service.find_or_create_lgu_data
    auto-creates a placeholder row for any barangay it is asked to
    score, attributed to the system account. Those rows are the app
    talking to itself, so a row counts as an upload only when it is
    BOTH not attributed to the system account AND not carrying the
    placeholder's zoning note (on a fresh install the first placeholder
    can be attributed to whoever is signed in; the note is written by
    exactly one line of code, so the two cannot drift apart).

    Every successful upload -- zoning or market -- writes one lgu_data
    row per barangay it covers, so either kind switches this on.

    WHAT THIS GATES. City-wide LGU recommendations only.
    """
    from app.services.forecasting_service import lgu_data_fingerprint
    from app.utils.request_memo import process_memo, request_memo

    def compute():
        return process_memo(
            ("has_active_lgu_data", lgu_data_fingerprint()),
            lambda: db.session.query(_real_upload_query(LguData.lgu_id).exists()).scalar() is True,
            ttl=1800,
        )

    return request_memo("has_active_lgu_data", compute)


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
    """When the data in force was uploaded, its source and how much of
    the city it covers; None before any real upload. Memoised per
    request."""
    from app.utils.request_memo import request_memo

    return request_memo("active_lgu_dataset_summary", _active_lgu_dataset_summary)


def _active_lgu_dataset_summary():
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
    None if it does not exist yet. Deliberately does NOT create it -- a
    read-only question must not write a row."""
    from app.models.user import User
    from app.services.forecasting_service import SYSTEM_USER_EMAIL
    from app.utils.request_memo import process_memo, request_memo

    def lookup():
        row = db.session.query(User.user_id).filter_by(email=SYSTEM_USER_EMAIL).first()
        return row[0] if row else None

    return request_memo(
        "system_user_id",
        lambda: process_memo("system_user_id", lookup, keep=lambda value: value is not None),
    )


def _clear_analytics_caches():
    """Forget every memoised figure so the next page reflects the
    upload. Best-effort: a cache that will not clear must not turn a
    successful import into a failed one."""
    try:
        from app.services.response_cache import clear_response_cache
        from app.services.trend_analytics_service import clear_trend_caches
        from app.utils.request_memo import clear_process_memo

        clear_trend_caches()
        clear_response_cache()
        clear_process_memo("has_active_lgu_data")
    except Exception:
        from flask import current_app

        current_app.logger.warning("could not clear analytics caches after upload", exc_info=True)
