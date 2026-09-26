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


def _impute_numeric(df, columns):
    """Mean-imputation, scoped to columns that actually appear in this
    upload -- the paper's stated fallback for missing values."""
    for col in columns:
        if col in df.columns:
            df[col] = _pandas().to_numeric(df[col], errors="coerce")
            if df[col].isna().any():
                fill_value = df[col].mean()
                df[col] = df[col].fillna(0 if _pandas().isna(fill_value) else fill_value)
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


def import_lgu_data(file_path, source, uploaded_by_user_id):
    """Inserts one lgu_data row per data row in the uploaded file.
    Returns the number of rows saved."""
    df = _normalize_columns(_read_any(file_path))
    if "barangay" not in df.columns:
        raise ValueError("File must contain a 'barangay' column.")

    df = _impute_numeric(df, LGU_NUMERIC_COLUMNS)
    today = datetime.date.today()
    saved = 0
    for _, row in df.iterrows():
        barangay = str(row.get("barangay") or "").strip()
        if not barangay:
            continue

        zoning_info = _cell(row, df, "zoning_info")
        closure_records = _cell(row, df, "closure_records")
        permit_count = _cell(row, df, "permit_count")
        business_density = _cell(row, df, "business_density")
        effective_date = _parse_date(_cell(row, df, "effective_date"), today)

        record = LguData(
            source=source,
            zoning_info=str(zoning_info).strip() if zoning_info is not None else None,
            closure_records=int(closure_records) if closure_records is not None else None,
            barangay=barangay,
            permit_count=int(permit_count) if permit_count is not None else None,
            business_density=float(business_density) if business_density is not None else None,
            effective_date=effective_date,
            upload_date=today,
            uploaded_by=uploaded_by_user_id,
        )
        db.session.add(record)
        saved += 1
    db.session.commit()
    return saved


def import_market_data(file_path, source):
    """Inserts one market_data row per data row in the uploaded file.
    Returns the number of rows saved."""
    df = _normalize_columns(_read_any(file_path))
    required = {"industry_type", "location"}
    if not required.issubset(set(df.columns)):
        raise ValueError(f"File must contain columns: {', '.join(sorted(required))}")

    df = _impute_numeric(df, MARKET_NUMERIC_COLUMNS)
    today = datetime.date.today()
    saved = 0
    for _, row in df.iterrows():
        industry_type = str(row.get("industry_type") or "").strip()
        location = str(row.get("location") or "").strip()
        if not industry_type or not location:
            continue

        competitor_count = _cell(row, df, "competitor_count")
        population_density = _cell(row, df, "population_density")
        historical_success_rate = _cell(row, df, "historical_success_rate")
        foot_traffic_index = _cell(row, df, "foot_traffic_index")
        average_rent = _cell(row, df, "average_rent")
        date_recorded = _parse_date(_cell(row, df, "date_recorded"), today)

        record = MarketData(
            industry_type=industry_type,
            location=location,
            competitor_count=int(competitor_count) if competitor_count is not None else None,
            population_density=float(population_density) if population_density is not None else None,
            historical_success_rate=float(historical_success_rate) if historical_success_rate is not None else None,
            foot_traffic_index=float(foot_traffic_index) if foot_traffic_index is not None else None,
            average_rent=float(average_rent) if average_rent is not None else None,
            source=source,
            date_recorded=date_recorded,
        )
        db.session.add(record)
        saved += 1
    db.session.commit()
    return saved


def process_upload(dataset_type, file_path, source, uploaded_by_user_id):
    """Returns (records_saved, status, error_message).

    dataset_type is the target table picked on the upload form --
    'LGU_DATA' or 'MARKET_DATA'. source is the matching ENUM value for
    that table (LguData: 'DTI'/'CLUP'/'Other'; MarketData:
    'PSA'/'DTI'/'Manual'). uploaded_by_user_id is always the logged-in
    LGU account's user_id (lgu_data.uploaded_by is NOT NULL).
    """
    try:
        if dataset_type == "LGU_DATA":
            count = import_lgu_data(file_path, source, uploaded_by_user_id)
        elif dataset_type == "MARKET_DATA":
            count = import_market_data(file_path, source)
        else:
            raise ValueError(f"Unknown dataset_type: {dataset_type}")
        return count, "success", None
    except Exception as exc:  # noqa: BLE001 - surfaced to the LGU user as-is
        return 0, "error", str(exc)
