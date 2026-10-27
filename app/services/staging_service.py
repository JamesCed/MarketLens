"""
app/services/staging_service.py
---------------------------------
The Gov't Data Upload's three-step workflow -- separation of duties
between the people who enter data, check it, and put it live:

  1. LGU ENCODER uploads a file. It is read and validated exactly as
     before (data_import_service.prepare_upload) and kept in the staging
     area as an UploadBatch. Nothing live changes. Until the batch is
     approved the Encoder can still correct it -- edit its barangay rows
     or replace the file -- or archive (withdraw) it.
  2. LGU DATA STEWARD reviews the batch in Gov't Data Review -- counts,
     barangays, a preview of the rows -- and approves it, sends it back
     with a message asking the Encoder to fix or clean the data
     ("changes requested"), or rejects it. A steward never reviews a
     batch they uploaded.
  3. SYSTEM ADMIN publishes an approved batch (Admin > Datasets): its rows
     are written to lgu_data / market_data / subcategory_market_data in
     one transaction and every city-wide figure recalculates. The Admin
     can also reject.

A barangay-scoped account works within its barangay only: an Encoder can
only stage rows for it, and a Data Steward only sees batches for it.

Each step notifies the next people in line and writes an audit entry.
Messages between the Steward and the Encoder are kept on the batch. The
rows wait in the batch's payload (compressed JSON); it is cleared when
the batch is published, rejected or archived.
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
    ARCHIVABLE_STATUSES,
    EDITABLE_STATUSES,
    STATUS_APPROVED,
    STATUS_ARCHIVED,
    STATUS_CHANGES,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    STATUS_PUBLISHING,
    STATUS_REJECTED,
    UploadBatch,
)

_DATE_FIELDS = ("effective_date", "upload_date", "date_recorded")
PREVIEW_ROWS = 80
MESSAGE_LIMIT = 500

# The barangay-row fields an Encoder may correct on the edit page.
EDITABLE_FIELDS = (
    ("zoning_info", "text"),
    ("business_density", "float"),
    ("permit_count", "int"),
    ("closure_records", "int"),
)


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


def _add_message(batch, user, kind, text):
    """Append to the batch's conversation. `kind` names what happened
    ("changes_requested", "edited", "approved", ...)."""
    text = " ".join((text or "").split())[:MESSAGE_LIMIT]
    if not text:
        return
    items = batch.message_list
    items.append({
        "at": datetime.utcnow().isoformat(timespec="seconds"),
        "by": user.name if user else "System",
        "role": (user.lgu_role_label or user.role) if user else "",
        "kind": kind,
        "text": text,
    })
    batch.messages = json.dumps(items[-50:], separators=(",", ":"))


def _stewards(barangay=None):
    """Active, verified Data Stewards who can review a batch for
    `barangay` (None = a city-wide batch: city-wide stewards only see it
    unless it covers one barangay)."""
    result = []
    for user in User.query.filter_by(role="LGU", status="active").all():
        if not user.is_data_steward() or user.is_archived:
            continue
        if user.lgu_scope_key == "barangay" and user.lgu_barangay != barangay:
            continue
        result.append(user)
    return result


def _admins():
    return [u for u in User.query.filter_by(role="Admin", status="active").all() if not u.is_archived]


def _rows_barangays(prepared):
    names = {row["barangay"] for row in prepared.lgu_rows}
    names |= {row.get("location") for row in prepared.market_rows if row.get("location")}
    return names


def _scope_problem(user, prepared):
    """Why a barangay-scoped Encoder may not stage this file, or None."""
    if user is None or user.lgu_scope_key != "barangay":
        return None
    others = sorted(_rows_barangays(prepared) - {user.lgu_barangay})
    if not others:
        return None
    shown = ", ".join(others[:5]) + (f" and {len(others) - 5} more" if len(others) > 5 else "")
    return (f"Your account covers Barangay {user.lgu_barangay} only, but this file has rows for {shown}. "
            f"Upload only {user.lgu_barangay}'s rows, or ask the System Admin for city-wide access.")


def _fill_from_prepared(batch, prepared):
    from app.services.data_import_service import _success_message

    register = prepared.register
    names = _rows_barangays(prepared)
    batch.lgu_rows = len(prepared.lgu_rows)
    batch.market_rows = len(prepared.market_rows)
    batch.subcategory_rows = len(prepared.subcategory_rows)
    batch.barangays = len({row["barangay"] for row in prepared.lgu_rows})
    batch.permits_read = register.permits_read if register is not None else None
    batch.permits_in_force = sum(register.in_force.values()) if register is not None else None
    batch.summary = _success_message(prepared.summary(), live=False)[:1000]
    batch.scope_barangay = next(iter(names)) if len(names) == 1 else None
    batch.payload = _pack({
        "lgu": _encode(prepared.lgu_rows),
        "market": _encode(prepared.market_rows),
        "subcategory": _encode(prepared.subcategory_rows),
    })


# ---------------------------------------------------------------- step 1

def stage_upload(dataset_type, file_path, source, uploaded_by, filename, progress=None):
    """Validate a file and keep it in the staging area. Returns
    (records, status, message, batch_id)."""
    from app.services.data_import_service import _Progress, prepare_upload

    progress = progress or _Progress()
    prepared, failure = prepare_upload(dataset_type, file_path, source, uploaded_by, progress)
    if failure:
        return 0, "failed", failure, None
    uploader = db.session.get(User, uploaded_by)
    problem = _scope_problem(uploader, prepared)
    if problem:
        progress.finish(0, "failed", problem)
        return 0, "failed", problem, None

    progress.step_to("Saving to the staging area")
    batch = UploadBatch(
        dataset_type=dataset_type,
        source=source,
        filename=(filename or "upload")[:255],
        status=STATUS_PENDING,
        uploaded_by=uploaded_by,
        uploaded_at=datetime.utcnow(),
    )
    _fill_from_prepared(batch, prepared)
    db.session.add(batch)
    db.session.flush()
    who = uploader.name if uploader else "An LGU encoder"
    _notify(_stewards(batch.scope_barangay), f"{who} uploaded '{batch.filename}' ({batch.dataset_label}, batch "
                                             f"#{batch.id}). It is waiting for your review in Gov't Data Review.")
    db.session.commit()

    message = (f"Saved to the staging area as batch #{batch.id}: {batch.summary} "
               f"It goes live after an LGU Data Steward approves it and a System Admin publishes it.")
    progress.finish(prepared.count, "success", message)
    return prepared.count, "success", message, batch.id


def _own_batch(batch_id, encoder, statuses):
    batch = db.session.get(UploadBatch, batch_id)
    if batch is None or batch.uploaded_by != encoder.user_id:
        return None, "That upload is not yours."
    if batch.status not in statuses:
        return None, f"Batch #{batch.id} can no longer be changed ({batch.status_label.lower()})."
    return batch, None


def _resubmitted(batch, encoder, note, what):
    was_waiting_on_encoder = batch.status == STATUS_CHANGES
    batch.status = STATUS_PENDING
    batch.updated_at = datetime.utcnow()
    _add_message(batch, encoder, "edited", note or what)
    _notify(_stewards(batch.scope_barangay),
            f"{encoder.name} {'corrected' if was_waiting_on_encoder else 'updated'} '{batch.filename}' "
            f"(batch #{batch.id}): {what}. It is waiting for your review.")


def staged_rows(batch):
    """The barangay rows of a staged batch, for the edit page."""
    return sorted(_unpack(batch.payload).get("lgu", []), key=lambda r: r["barangay"])


def edit_rows(batch_id, encoder, changes, note=""):
    """Apply an Encoder's corrections to the barangay rows of their own
    staged batch. `changes` is {barangay: {field: raw text}}. Returns
    (ok, message)."""
    batch, problem = _own_batch(batch_id, encoder, EDITABLE_STATUSES)
    if problem:
        return False, problem
    data = _unpack(batch.payload)
    rows = {row["barangay"]: row for row in data.get("lgu", [])}
    errors, changed = [], 0
    for barangay, fields in changes.items():
        row = rows.get(barangay)
        if row is None:
            continue
        for field, kind in EDITABLE_FIELDS:
            if field not in fields:
                continue
            raw = (fields[field] or "").strip()
            if kind == "text":
                value = " ".join(raw.split())[:255] or None
                if field == "zoning_info" and not value:
                    errors.append(f"{barangay}: zoning cannot be empty.")
                    continue
            elif raw == "":
                value = None
            else:
                try:
                    value = float(raw) if kind == "float" else int(raw)
                except ValueError:
                    errors.append(f"{barangay}: '{raw}' is not a valid {field.replace('_', ' ')}.")
                    continue
                if value < 0:
                    errors.append(f"{barangay}: {field.replace('_', ' ')} cannot be negative.")
                    continue
                if kind == "float":
                    value = round(value, 2)
            if row.get(field) != value:
                row[field] = value
                changed += 1
    if errors:
        return False, "Nothing was saved. " + " ".join(errors[:5])
    if not changed and not (note or "").strip():
        return False, "No changes to save."
    data["lgu"] = list(rows.values())
    batch.payload = _pack(data)
    _resubmitted(batch, encoder, note, f"{changed} value(s) corrected" if changed else "note added")
    db.session.commit()
    return True, f"Batch #{batch.id} saved and sent back for review."


def replace_file(batch_id, encoder, file_path, filename, note=""):
    """Replace a staged batch's rows with a corrected file (same dataset
    and source). Returns (ok, message)."""
    from app.services.data_import_service import prepare_upload

    batch, problem = _own_batch(batch_id, encoder, EDITABLE_STATUSES)
    if problem:
        return False, problem
    prepared, failure = prepare_upload(batch.dataset_type, file_path, batch.source, encoder.user_id)
    if failure:
        return False, f"The corrected file was not used: {failure}"
    problem = _scope_problem(encoder, prepared)
    if problem:
        return False, problem
    batch = db.session.get(UploadBatch, batch_id)
    _fill_from_prepared(batch, prepared)
    batch.filename = (filename or batch.filename)[:255]
    _resubmitted(batch, encoder, note, f"file replaced with '{batch.filename}'")
    db.session.commit()
    return True, f"Batch #{batch.id} now uses '{batch.filename}' and was sent back for review."


def archive(batch_id, encoder, note=""):
    """The Encoder withdraws their own batch before it goes live."""
    batch, problem = _own_batch(batch_id, encoder, ARCHIVABLE_STATUSES)
    if problem:
        return False, problem
    was = batch.status
    batch.status = STATUS_ARCHIVED
    batch.payload = None
    batch.updated_at = datetime.utcnow()
    _add_message(batch, encoder, "archived", note or "Archived by the encoder.")
    if was in (STATUS_PENDING, STATUS_CHANGES):
        _notify(_stewards(batch.scope_barangay), f"{encoder.name} archived '{batch.filename}' (batch #{batch.id}); "
                                                 f"it no longer needs review.")
    elif was == STATUS_APPROVED:
        _notify(_admins(), f"{encoder.name} withdrew approved batch #{batch.id} ('{batch.filename}'); "
                           f"it will not be published.")
    db.session.commit()
    return True, f"Batch #{batch.id} archived."


# ---------------------------------------------------------------- step 2

def can_review(steward, batch):
    if not steward.is_data_steward():
        return False
    if steward.lgu_scope_key == "barangay":
        return batch.scope_barangay == steward.lgu_barangay
    return True


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


DECISIONS = ("approve", "changes", "reject")


def review(batch_id, reviewer, decision=None, note="", approve=None):
    """A Data Steward's decision: "approve", "changes" (send it back to
    the Encoder with a message) or "reject". Returns (ok, message).

    `approve=True/False` (or decision True/False) still means approve /
    reject, for callers from before "request changes" existed."""
    if approve is not None:
        decision = bool(approve)
    if decision is True:
        decision = "approve"
    elif decision is False:
        decision = "reject"
    batch = db.session.get(UploadBatch, batch_id)
    note = " ".join((note or "").split())[:MESSAGE_LIMIT]
    if decision not in DECISIONS:
        return False, "Choose approve, request changes or reject."
    if batch is None or batch.status not in (STATUS_PENDING, STATUS_CHANGES):
        return False, "That batch is no longer waiting for review."
    if decision != "reject" and batch.status != STATUS_PENDING:
        return False, "That batch is waiting for the encoder's corrections."
    if not reviewer.is_data_steward():
        return False, "Only an LGU Data Steward can review uploaded data."
    if batch.uploaded_by == reviewer.user_id:
        return False, "You uploaded this batch, so another Data Steward must review it."
    if not can_review(reviewer, batch):
        return False, f"This batch is outside your scope ({reviewer.lgu_scope_label})."
    if decision != "approve" and not note:
        return False, ("Write a message saying what the encoder should fix or clean."
                       if decision == "changes" else
                       "Please give the reason for rejecting the batch, so the encoder can fix it.")

    batch.reviewed_by = reviewer.user_id
    batch.reviewed_at = datetime.utcnow()
    batch.review_note = note or None
    uploader = db.session.get(User, batch.uploaded_by) if batch.uploaded_by else None
    if decision == "approve":
        batch.status = STATUS_APPROVED
        _add_message(batch, reviewer, "approved", note)
        _notify(_admins(), f"Batch #{batch.id} ('{batch.filename}', {batch.dataset_label}) was approved by "
                           f"{reviewer.name} and is ready to publish in Admin > Datasets.")
        if uploader:
            _notify([uploader], f"Your upload '{batch.filename}' (batch #{batch.id}) was approved and is waiting "
                                f"to be published.")
        message = f"Batch #{batch.id} approved. A System Admin can now publish it."
    elif decision == "changes":
        batch.status = STATUS_CHANGES
        _add_message(batch, reviewer, "changes_requested", note)
        if uploader:
            _notify([uploader], f"Batch #{batch.id} ('{batch.filename}') needs changes before it can be approved: "
                                f"{note}")
        message = f"Batch #{batch.id} sent back to the encoder with your message."
    else:
        batch.status = STATUS_REJECTED
        batch.payload = None
        _add_message(batch, reviewer, "rejected", note)
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
    _add_message(batch, admin, "rejected", f"Not published: {note}")
    uploader = db.session.get(User, batch.uploaded_by) if batch.uploaded_by else None
    if uploader:
        _notify([uploader], f"Your upload '{batch.filename}' (batch #{batch.id}) was not published: {note}")
    db.session.commit()
    return True, f"Batch #{batch.id} rejected."


# ---------------------------------------------------------------- step 3

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


# -------------------------------------------------------------- listings

def batches(statuses=None, uploaded_by=None, limit=50):
    query = UploadBatch.query
    if statuses:
        query = query.filter(UploadBatch.status.in_(statuses))
    if uploaded_by is not None:
        query = query.filter(UploadBatch.uploaded_by == uploaded_by)
    return query.order_by(UploadBatch.uploaded_at.desc(), UploadBatch.id.desc()).limit(limit).all()


def batches_for_steward(steward, statuses, limit=50):
    """Batches in `statuses` this Data Steward may review (their scope)."""
    query = UploadBatch.query.filter(UploadBatch.status.in_(statuses))
    if steward.lgu_scope_key == "barangay":
        query = query.filter(UploadBatch.scope_barangay == steward.lgu_barangay)
    order = (UploadBatch.uploaded_at.asc() if statuses == [STATUS_PENDING] else UploadBatch.uploaded_at.desc())
    return query.order_by(order, UploadBatch.id).limit(limit).all()


def recently_published(limit=10):
    return (UploadBatch.query.filter_by(status=STATUS_PUBLISHED)
            .order_by(UploadBatch.published_at.desc(), UploadBatch.id.desc()).limit(limit).all())


def count_waiting(status):
    return UploadBatch.query.filter_by(status=status).count()


def count_waiting_for(steward):
    if steward is None or not steward.is_data_steward():
        return 0
    query = UploadBatch.query.filter_by(status=STATUS_PENDING)
    if steward.lgu_scope_key == "barangay":
        query = query.filter(UploadBatch.scope_barangay == steward.lgu_barangay)
    return query.count()
