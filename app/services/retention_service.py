"""
app/services/retention_service.py
-----------------------------------
How long uploaded government data and the audit trail are kept, and the
job that removes them when that time is up.

WHY
The Data Privacy Act of 2012 (RA 10173, Sec. 11(e)) allows personal data
to be kept only as long as its purpose needs it. The audit trail holds
personal data -- who did what, from which IP address and browser -- and
the uploaded LGU rows are official records whose figures go stale. Both
now have a retention period an Admin sets in System Settings:

  upload_retention_days     (default 1095 = 3 years) -- lgu_data rows an
                            LGU account uploaded, the permit-derived
                            market_data rows (source DTI / PSA) and their
                            sub-category counts, counted from the upload.
  audit_log_retention_days  (default 365 = 1 year) -- audit_logs entries.

The staging area of the Gov't Data Upload (upload_batches) follows the
upload period too, and a batch still waiting for review or publishing
after STAGING_EXPIRY_DAYS expires: it is marked rejected and its staged
rows are cleared.

LGU access requests (lgu_access_requests: head of office, Discord
username) are deleted with the audit trail once decided; a request not
verified within LGU_REQUEST_EXPIRY_DAYS expires and its details are
cleared.

A row a saved forecast still points to is kept until that forecast is
gone: forecast_result references its market_data and lgu_data rows, and
deleting them would delete the business owner's saved forecast with
them. Such rows hold only barangay totals and counts -- no personal
data.

The job runs at most once a day, on a background thread started by the
first request after the interval (see install). Each run that removes
anything writes one "retention_purge" audit entry with the counts.
"""

import logging
import threading
import time
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

UPLOAD_RETENTION_KEY = "upload_retention_days"
AUDIT_RETENTION_KEY = "audit_log_retention_days"
LAST_RUN_KEY = "retention_last_run"
DEFAULT_UPLOAD_DAYS = 1095
DEFAULT_AUDIT_DAYS = 365
RETENTION_RANGE = (30, 3650)
UPLOADED_MARKET_SOURCES = ("DTI", "PSA")
RUN_INTERVAL_SECONDS = 24 * 3600
STAGING_EXPIRY_DAYS = 60
LGU_REQUEST_EXPIRY_DAYS = 60

_state = {"next": 0.0}
_state_lock = threading.Lock()


def _clamp(value, default):
    low, high = RETENTION_RANGE
    try:
        days = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, days))


def retention_days():
    """(upload_days, audit_days) as configured, clamped to 30..3650."""
    from app.models import SystemSetting

    return (_clamp(SystemSetting.get(UPLOAD_RETENTION_KEY), DEFAULT_UPLOAD_DAYS),
            _clamp(SystemSetting.get(AUDIT_RETENTION_KEY), DEFAULT_AUDIT_DAYS))


def apply_retention(today=None):
    """Delete what has outlived its retention period. Returns the counts
    removed per table."""
    from sqlalchemy import delete, or_, select, update

    from app.extensions import db
    from app.models import AuditLog, ForecastResult, LguData, MarketData, SubcategoryMarketData, SystemSetting
    from app.models.upload_batch import STATUS_APPROVED, STATUS_PENDING, STATUS_REJECTED, UploadBatch
    from app.models.lgu_access_request import REQUEST_EXPIRED, REQUEST_PENDING, LguAccessRequest
    from app.services.data_import_service import _clear_analytics_caches, _system_user_id_or_none
    from app.services.forecasting_service import PLACEHOLDER_ZONING_NOTE

    today = today or date.today()
    upload_days, audit_days = retention_days()
    upload_cutoff = today - timedelta(days=upload_days)
    audit_cutoff = datetime.combine(today - timedelta(days=audit_days), datetime.min.time())
    no_sync = {"synchronize_session": False}

    lgu_filters = [
        LguData.upload_date < upload_cutoff,
        or_(LguData.zoning_info.is_(None), LguData.zoning_info != PLACEHOLDER_ZONING_NOTE),
        LguData.lgu_id.not_in(select(ForecastResult.lgu_id)),
    ]
    system_user_id = _system_user_id_or_none()
    if system_user_id is not None:
        lgu_filters.append(LguData.uploaded_by != system_user_id)

    counts = {
        "lgu_data": db.session.execute(delete(LguData).where(*lgu_filters), execution_options=no_sync).rowcount,
        "market_data": db.session.execute(
            delete(MarketData).where(
                MarketData.source.in_(UPLOADED_MARKET_SOURCES),
                MarketData.date_recorded < upload_cutoff,
                MarketData.market_id.not_in(select(ForecastResult.market_id)),
            ),
            execution_options=no_sync,
        ).rowcount,
        "subcategory_market_data": db.session.execute(
            delete(SubcategoryMarketData).where(
                SubcategoryMarketData.source.in_(UPLOADED_MARKET_SOURCES),
                SubcategoryMarketData.date_recorded < upload_cutoff,
            ),
            execution_options=no_sync,
        ).rowcount,
        "audit_logs": db.session.execute(
            delete(AuditLog).where(AuditLog.created_at < audit_cutoff), execution_options=no_sync,
        ).rowcount,
        # Staging area: a batch record follows the uploaded-data period ...
        "upload_batches": db.session.execute(
            delete(UploadBatch).where(
                UploadBatch.uploaded_at < datetime.combine(upload_cutoff, datetime.min.time())),
            execution_options=no_sync,
        ).rowcount,
        # ... and staged rows nobody approved or published in time are not
        # kept "just in case": the batch expires and its rows are cleared.
        "staged_expired": db.session.execute(
            update(UploadBatch).where(
                UploadBatch.status.in_((STATUS_PENDING, STATUS_APPROVED)),
                UploadBatch.uploaded_at < datetime.combine(today - timedelta(days=STAGING_EXPIRY_DAYS),
                                                           datetime.min.time()),
            ).values(status=STATUS_REJECTED, payload=None,
                     review_note=f"Expired: not reviewed and published within {STAGING_EXPIRY_DAYS} days."),
            execution_options=no_sync,
        ).rowcount,
        # LGU access requests: the verification evidence (head of office,
        # Discord username) is kept as long as the audit trail, then goes.
        "lgu_access_requests": db.session.execute(
            delete(LguAccessRequest).where(
                LguAccessRequest.status != REQUEST_PENDING,
                LguAccessRequest.created_at < audit_cutoff,
            ),
            execution_options=no_sync,
        ).rowcount,
        "lgu_requests_expired": _expire_lgu_requests(db, LguAccessRequest, REQUEST_PENDING, REQUEST_EXPIRED, today),
    }
    counts = {table: max(0, int(n or 0)) for table, n in counts.items()}
    summary = (f"{today.isoformat()}: removed " + ", ".join(f"{n} {t}" for t, n in counts.items())
               + f" (uploads kept {upload_days} days, audit trail {audit_days} days)")
    SystemSetting.set(LAST_RUN_KEY, summary[:255])
    db.session.commit()

    if counts["lgu_data"] or counts["market_data"] or counts["subcategory_market_data"]:
        _clear_analytics_caches()
    if any(counts.values()):
        from app.utils.audit import log_action

        log_action("retention_purge", details=summary[:500], changes=counts)
    return counts


def _expire_lgu_requests(db, model, pending, expired, today):
    """A request nobody verified within LGU_REQUEST_EXPIRY_DAYS expires;
    the account shows "not approved" and can send a new request."""
    from app.models import User

    cutoff = datetime.combine(today - timedelta(days=LGU_REQUEST_EXPIRY_DAYS), datetime.min.time())
    stale = model.query.filter(model.status == pending, model.created_at < cutoff).all()
    for row in stale:
        row.status = expired
        row.decided_at = datetime.utcnow()
        row.decision_note = f"Expired: not verified within {LGU_REQUEST_EXPIRY_DAYS} days."
        row.endorser_name = row.endorser_email = row.discord_username = None
        user = db.session.get(User, row.user_id)
        if user is not None and user.lgu_status == "pending":
            user.lgu_status = "rejected"
    return len(stale)


def _run(app):
    try:
        with app.app_context():
            apply_retention()
    except Exception:
        log.warning("data retention run failed; it will be retried later", exc_info=True)


def maybe_run(app):
    """Start a retention run on a background thread when one is due."""
    now = time.monotonic()
    with _state_lock:
        if now < _state["next"]:
            return False
        _state["next"] = now + RUN_INTERVAL_SECONDS
    threading.Thread(target=_run, args=(app,), name="data-retention", daemon=True).start()
    return True


def install(app):
    """Check once per request (a clock comparison) whether a run is due.
    Not installed under TESTING; tests call apply_retention() directly."""
    if app.config.get("TESTING") or not app.config.get("DATA_RETENTION_ENABLED", True):
        return

    @app.before_request
    def _retention_due():
        if time.monotonic() >= _state["next"]:
            maybe_run(app)
