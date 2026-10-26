"""
app/services/staging_service.py
---------------------------------
The Gov't Data Upload's three-step workflow -- separation of duties
between the people who enter data, check it, and put it live:

  1. LGU ENCODER uploads a file. It is read and validated exactly as
     before (data_import_service.prepare_upload) and kept in the staging
     area as an UploadBatch. Nothing live changes.
  2. LGU DATA STEWARD (validator) reviews the batch -- counts, barangays,
     a preview of the rows -- and approves it or rejects it with a
     reason. A steward never reviews a batch they uploaded.
  3. SYSTEM ADMIN publishes an approved batch: its rows are written to
     lgu_data / market_data / subcategory_market_data in one transaction
     and every city-wide figure recalculates. The Admin can also reject.

Each step notifies the next people in line and writes an audit entry.
The rows wait in the batch's payload (compressed JSON); it is cleared
when the batch is published or rejected.
"""

import base64
import json
import zlib
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import update

from app.extensions import db
from app.models import Notification, User
from app.models.upload_batch import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    STATUS_PUBLISHING,
    STATUS_REJECTED,
    UploadBatch,
)

_DATE_FIELDS = ("effective_date", "upload_date", "date_recorded")
PREVIEW_ROWS = 80


def _plain(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _encode(rows):
    return [{key: _plain(value) for key, value in row.items()} for row in rows]


def _decode(rows):
    for row in rows:
        for field in _DATE_FIELDS:
            value = row.get(field)
            if isinstance(value, str):
                row[field] = date.fromisoformat(value[:10])
    return rows


def _pack(data):
    """The staged rows, compressed: a permit register's thousands of
    near-identical rows shrink about twentyfold, so the batch is a small
    write to the database rather than a megabyte."""
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return "z:" + base64.b64encode(zlib.compress(raw, 6)).decode("ascii")


def _unpack(payload):
    if not payload:
        return {}
    if payload.startswith("z:"):
        return json.loads(zlib.decompress(base64.b64decode(payload[2:])).decode("utf-8"))
    return json.loads(payload)


def _notify(users, message):
    for user in users:
        db.session.add(Notification(user_id=user.user_id, type="system", message=message[:500]))


def _stewards():
    return [u for u in User.query.filter_by(role="LGU", status="active").all()
            if u.is_data_steward() and not u.is_archived]


def _admins():
    return [u for u in User.query.filter_by(role="Admin", status="active").all() if not u.is_archived]


def stage_upload(dataset_type, file_path, source, uploaded_by, filename, progress=None):
    """Validate a file and keep it in the staging area. Returns
    (records, status, message, batch_id)."""
    from app.services.data_import_service import _Progress, _success_message, prepare_upload

    progress = progress or _Progress()
    prepared, failure = prepare_upload(dataset_type, file_path, source, uploaded_by, progress)
    if failure:
        return 0, "failed", failure, None

    progress.step_to("Saving to the staging area")
    register = prepared.register
    batch = UploadBatch(
        dataset_type=dataset_type,
        source=source,
        filename=(filename or "upload")[:255],
        status=STATUS_PENDING,
        uploaded_by=uploaded_by,
        uploaded_at=datetime.utcnow(),
        lgu_rows=len(prepared.lgu_rows),
        market_rows=len(prepared.market_rows),
        subcategory_rows=len(prepared.subcategory_rows),
        barangays=len({row["barangay"] for row in prepared.lgu_rows}),
        permits_read=register.permits_read if register is not None else None,
        permits_in_force=sum(register.in_force.values()) if register is not None else None,
        summary=_success_message(prepared.summary(), live=False)[:1000],
        payload=_pack({
            "lgu": _encode(prepared.lgu_rows),
            "market": _encode(prepared.market_rows),
            "subcategory": _encode(prepared.subcategory_rows),
        }),
    )
    db.session.add(batch)
    db.session.flush()
    uploader = db.session.get(User, uploaded_by)
    who = uploader.name if uploader else "An LGU encoder"
    _notify(_stewards(), f"{who} uploaded '{batch.filename}' ({batch.dataset_label}, batch #{batch.id}). "
                         f"It is waiting for your review in Data Review.")
    db.session.commit()

    message = (f"Saved to the staging area as batch #{batch.id}: {batch.summary} "
               f"It goes live after an LGU Data Steward approves it and a System Admin publishes it.")
    progress.finish(prepared.count, "success", message)
    return prepared.count, "success", message, batch.id


def preview(batch, limit=PREVIEW_ROWS):
    """What a reviewer looks at: the barangay rows, the industry totals,
    and how each barangay's permit count compares with what is live."""
    if not batch.payload:
        return {"lgu": [], "industries": [], "available": False}
    from app.services.forecasting_service import _lgu_rows

    data = _unpack(batch.payload)
    lgu = data.get("lgu", [])
    live = _lgu_rows({row["barangay"] for row in lgu}) if lgu else {}
    rows = []
    for row in sorted(lgu, key=lambda r: r["barangay"])[:limit]:
        current = live.get(row["barangay"])
        rows.append({**row, "live_permit_count": current.permit_count if current is not None else None,
                     "live_density": float(current.business_density) if current is not None
                     and current.business_density is not None else None})
    totals = {}
    for row in data.get("market", []):
        totals[row["industry_type"]] = totals.get(row["industry_type"], 0) + int(row.get("competitor_count") or 0)
    industries = sorted(totals.items(), key=lambda item: -item[1])
    return {"lgu": rows, "lgu_total": len(lgu), "industries": industries, "available": True}


def review(batch_id, reviewer, approve, note=""):
    """A Data Steward's decision. Returns (ok, message)."""
    batch = db.session.get(UploadBatch, batch_id)
    note = (note or "").strip()[:500]
    if batch is None or batch.status != STATUS_PENDING:
        return False, "That batch is no longer waiting for review."
    if not reviewer.is_data_steward():
        return False, "Only an LGU Data Steward can review uploaded data."
    if batch.uploaded_by == reviewer.user_id:
        return False, "You uploaded this batch, so another Data Steward must review it."
    if not approve and not note:
        return False, "Please give the reason for rejecting the batch, so the encoder can fix it."

    batch.reviewed_by = reviewer.user_id
    batch.reviewed_at = datetime.utcnow()
    batch.review_note = note or None
    uploader = db.session.get(User, batch.uploaded_by) if batch.uploaded_by else None
    if approve:
        batch.status = STATUS_APPROVED
        _notify(_admins(), f"Batch #{batch.id} ('{batch.filename}', {batch.dataset_label}) was approved by "
                           f"{reviewer.name} and is ready to publish in Admin > Publish Data.")
        if uploader:
            _notify([uploader], f"Your upload '{batch.filename}' (batch #{batch.id}) was approved and is waiting "
                                f"to be published.")
        message = f"Batch #{batch.id} approved. A System Admin can now publish it."
    else:
        batch.status = STATUS_REJECTED
        batch.payload = None
        if uploader:
            _notify([uploader], f"Your upload '{batch.filename}' (batch #{batch.id}) was rejected: {note}")
        message = f"Batch #{batch.id} rejected. The encoder has been told why."
    db.session.commit()
    return True, message


def reject_by_admin(batch_id, admin, note):
    batch = db.session.get(UploadBatch, batch_id)
    note = (note or "").strip()[:500]
    if batch is None or batch.status != STATUS_APPROVED:
        return False, "Only an approved batch can be rejected here."
    if not note:
        return False, "Please give the reason for rejecting the batch."
    batch.status = STATUS_REJECTED
    batch.review_note = f"Rejected by the administrator ({admin.name}): {note}"[:500]
    batch.payload = None
    uploader = db.session.get(User, batch.uploaded_by) if batch.uploaded_by else None
    if uploader:
        _notify([uploader], f"Your upload '{batch.filename}' (batch #{batch.id}) was not published: {note}")
    db.session.commit()
    return True, f"Batch #{batch.id} rejected."


def publish(batch_id, admin):
    """Write an approved batch to the live tables. Returns (ok, message).

    The batch is claimed first with a conditional UPDATE (approved ->
    publishing), so two administrators pressing Publish at once cannot
    write it twice."""
    from app.services.data_import_service import _clear_analytics_caches, write_live

    claimed = db.session.execute(
        update(UploadBatch)
        .where(UploadBatch.id == batch_id, UploadBatch.status == STATUS_APPROVED)
        .values(status=STATUS_PUBLISHING),
        execution_options={"synchronize_session": False},
    ).rowcount
    db.session.commit()
    if claimed != 1:
        return False, "That batch is not approved for publishing (it may already be published)."

    batch = db.session.get(UploadBatch, batch_id)
    try:
        data = _unpack(batch.payload)
        today = date.today()
        lgu = _decode(data.get("lgu", []))
        market = _decode(data.get("market", []))
        subcategory = _decode(data.get("subcategory", []))
        for row in lgu:
            row["upload_date"] = today
        for row in market + subcategory:
            row["date_recorded"] = today
        write_live(lgu, market, subcategory)
        batch.status = STATUS_PUBLISHED
        batch.published_by = admin.user_id
        batch.published_at = datetime.utcnow()
        batch.payload = None
        audience = [u for u in (db.session.get(User, batch.uploaded_by) if batch.uploaded_by else None,
                                db.session.get(User, batch.reviewed_by) if batch.reviewed_by else None) if u]
        _notify(audience, f"Batch #{batch.id} ('{batch.filename}') is now live. City-wide recommendations "
                          f"use the new data.")
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        db.session.execute(update(UploadBatch).where(UploadBatch.id == batch_id).values(status=STATUS_APPROVED))
        db.session.commit()
        return False, f"Batch #{batch_id} could not be published and is still approved: {exc}"

    _clear_analytics_caches()
    return True, (f"Batch #{batch.id} published: {len(lgu):,} Gov't / Zoning row(s), {len(market):,} industry "
                  f"count(s) and {len(subcategory):,} sub-category count(s) are now live.")


def batches(statuses=None, uploaded_by=None, limit=50):
    query = UploadBatch.query
    if statuses:
        query = query.filter(UploadBatch.status.in_(statuses))
    if uploaded_by is not None:
        query = query.filter(UploadBatch.uploaded_by == uploaded_by)
    return query.order_by(UploadBatch.uploaded_at.desc(), UploadBatch.id.desc()).limit(limit).all()


def count_waiting(status):
    return UploadBatch.query.filter_by(status=status).count()
