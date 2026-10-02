"""
app/controllers/admin_controller.py
--------------------------------------
Admin Module: manage user accounts, audit trail, dataset management,
system configuration -- the bullet points listed under "Admin Module"
in the paper's Specific Objectives.

Three things worth calling out because they shaped this file:

1. There is no stored cluster_label column, so "how many forecasts are
   Saturated" is computed from saturation_index using the same cut
   points the training run discovered (app/ml/constants.py
   CLUSTER_THRESHOLDS), not a simple equality filter.

2. NOTHING HERE DELETES. User accounts, lgu_data rows and market_data
   rows are ARCHIVED -- stamped with when, by whom and why, taken out of
   circulation, and restorable. See app/models/archive.py for the long
   version. The short version is that deleting was destructive in ways
   the button never said: forecast_result.market_id / lgu_id cascade on
   delete, so removing one data row silently removed every SME forecast
   ever built on it, and a deleted account took its audit history with
   it. (It also could not work for most accounts anyway --
   lgu_data.uploaded_by is ondelete="RESTRICT", so the database refused
   to delete anyone who had ever uploaded data, including the system
   account that owns every placeholder row.)

3. EVERY ARCHIVE, RESTORE AND ACCESS CHANGE REQUIRES A REASON. The
   client asked for an audit trail that answers the five W's, and WHY is
   the one no amount of request inspection can fill in -- only the
   administrator knows it. So the form will not go through without one,
   and the reason is what the audit trail's WHY column shows for that
   row. The minimum length is deliberately small (MIN_REASON_LENGTH):
   it is there to stop an empty box or a stray "x", not to make anyone
   write an essay.
"""

import csv
import io
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import and_, or_
from sqlalchemy.orm import contains_eager

from app.extensions import db
from app.models import User, AuditLog, LguData, MarketData, SystemSetting, ForecastResult, Notification
from app.models.archive import INCLUDE_ARCHIVED, get_including_archived
from app.ml.constants import CLUSTER_THRESHOLDS
from app.utils.decorators import role_required
from app.utils.audit import log_action
from app.utils.audit_labels import actions_matching, describe_device, label_for, purpose_for, target_type_label
from app.services.forecasting_service import SYSTEM_USER_EMAIL

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

# The WHY an administrator types. Five characters is the smallest
# number that rules out "x", "ok" and "test" while still accepting a
# terse but real answer such as "spam" + one more word, or "dup row".
MIN_REASON_LENGTH = 5

# The audit trail's WHEN is shown in Philippine time, because that is
# the clock the people reading it live by: created_at is stored in UTC
# (datetime.utcnow), and a sign-in at 10:14 in the morning showing as
# 02:14 reads as a break-in, not a sign-in. A fixed +8 offset rather
# than a zoneinfo lookup is exact here -- the Philippines has no
# daylight saving time -- and needs no tzdata package, which the
# project's Windows venv does not have.
DISPLAY_UTC_OFFSET = timedelta(hours=8)
DISPLAY_TZ_LABEL = "PHT"

AUDIT_PER_PAGE = 30

# "none" is the rows with no account behind them: failed sign-ins,
# password-reset requests from signed-out visitors, background jobs.
AUDIT_ROLE_CHOICES = (
    ("Admin", "Admin"),
    ("SME", "SME"),
    ("LGU", "LGU"),
    ("none", "No account (visitor or system)"),
)


def _to_local(value):
    """UTC datetime -> Philippine time, or None."""
    return value + DISPLAY_UTC_OFFSET if value else None


@admin_bp.context_processor
def _admin_template_helpers():
    """Available to admin pages only (a blueprint context processor is
    scoped to its own blueprint's requests)."""
    return {
        "local_time": _to_local,
        "tz_label": DISPLAY_TZ_LABEL,
        "min_reason_length": MIN_REASON_LENGTH,
    }


def _reason_from_form(doing_what):
    """The administrator's stated WHY, or None after flashing why it was
    refused. Whitespace is collapsed first -- a reason is one line in the
    audit trail, and five spaces are not five characters of reason."""
    reason = " ".join((request.form.get("reason") or "").split())
    if len(reason) < MIN_REASON_LENGTH:
        flash(
            f"Please give a reason (at least {MIN_REASON_LENGTH} characters) before you {doing_what}.",
            "warning",
        )
        return None
    return reason[:255]


def _people_by_id(user_ids):
    """{user_id: User} for the given ids -- one query, so a list of
    archived records can name who archived each without a lookup per
    row. User is not hidden when archived, so an archiver whose own
    account has since been archived still resolves."""
    ids = {uid for uid in user_ids if uid is not None}
    if not ids:
        return {}
    return {user.user_id: user for user in User.query.filter(User.user_id.in_(ids)).all()}


def _refresh_figures():
    """Archiving or restoring a data row changes what every analysis
    sees, so the memoised figures built from the old data have to go --
    exactly what an upload does after it lands."""
    from app.services.data_import_service import _clear_analytics_caches

    _clear_analytics_caches()


@admin_bp.route("/dashboard")
@role_required("Admin")
def dashboard():
    # "Users" counts accounts in circulation. Archived accounts are the
    # replacement for deleted ones, so counting them as users would make
    # the number go up every time an administrator removed someone.
    live_users = User.query.filter(User.archived_at.is_(None))
    stats = {
        "total_users": live_users.count(),
        "archived_users": User.query.filter(User.archived_at.isnot(None)).count(),
        "sme_users": live_users.filter(User.role == "SME").count(),
        "lgu_users": live_users.filter(User.role == "LGU").count(),
        "total_forecasts": ForecastResult.query.count(),
        # Archived data rows are already excluded by the global filter
        # (app/models/archive.py), so these are live rows only.
        "total_lgu_data": LguData.query.count(),
        "total_market_data": MarketData.query.count(),
        "total_notifications": Notification.query.count(),
    }
    recent = (
        AuditLog.query.outerjoin(AuditLog.user)
        .options(contains_eager(AuditLog.user))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(10)
        .all()
    )
    return render_template(
        "admin/dashboard.html",
        stats=stats,
        recent_activity=[_present_entry(entry) for entry in recent],
    )


# ------------------------------------------------------------------ users
@admin_bp.route("/users")
@role_required("Admin")
def users():
    current_accounts = User.query.filter(User.archived_at.is_(None)).order_by(User.created_at.desc()).all()
    archived_accounts = User.query.filter(User.archived_at.isnot(None)).order_by(User.archived_at.desc()).all()
    return render_template(
        "admin/users.html",
        users=current_accounts,
        archived_users=archived_accounts,
        archivers=_people_by_id(u.archived_by for u in archived_accounts),
        active_tab="archived" if request.args.get("tab") == "archived" else "current",
        system_email=SYSTEM_USER_EMAIL,
    )


@admin_bp.route("/users/create", methods=["POST"])
@role_required("Admin")
def create_user():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    role = request.form.get("role", "SME")
    contact_number = request.form.get("contact_number", "").strip()

    if not name or not email or not password or role not in ("Admin", "SME", "LGU"):
        flash("Name, email, password are required and role must be Admin, SME or LGU.", "danger")
        return redirect(url_for("admin.users"))

    existing = User.query.filter_by(email=email).first()
    if existing is not None:
        # An archived account still owns its address (email is unique),
        # so say where it is rather than a bare "already exists" that
        # sends the admin looking through a list it is not in.
        if existing.is_archived:
            flash("An archived account already uses that email. Restore it from the Archived tab instead.", "warning")
            return redirect(url_for("admin.users", tab="archived"))
        flash("A user with that email already exists.", "danger")
        return redirect(url_for("admin.users"))

    user = User(name=name, email=email, role=role, contact_number=contact_number or None)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    log_action("admin_create_user", details=f"{role}: {email}", target=user)
    flash(f"Account created for {email}.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/toggle-active", methods=["POST"])
@role_required("Admin")
def toggle_active(user_id):
    user = User.query.get_or_404(user_id)
    if user.user_id == current_user.user_id:
        flash("You cannot suspend your own account.", "warning")
        return redirect(url_for("admin.users"))
    if user.is_archived:
        # Suspending an archived account would change nothing anyone can
        # see (it cannot sign in either way) but would quietly decide
        # what state it comes back in on restore.
        flash(f"{user.email} is archived. Restore it first.", "warning")
        return redirect(url_for("admin.users", tab="archived"))

    suspending = user.status == "active"
    reason = _reason_from_form("suspend this account" if suspending else "reactivate this account")
    if reason is None:
        return redirect(url_for("admin.users"))

    user.status = "inactive" if suspending else "active"
    db.session.commit()
    log_action(
        "admin_toggle_active",
        details="Suspended" if suspending else "Reactivated",
        target=user,
        reason=reason,
    )
    flash(f"{user.email} is now {'suspended' if suspending else 'active'}.", "success")
    return redirect(url_for("admin.users"))


# There is deliberately NO delete route for accounts any more -- see
# point 2 in the module docstring. Archiving is the replacement: the
# account can no longer sign in (User.is_active is False once
# archived_at is set) and leaves the active list, but everything that
# names it -- the audit trail, the datasets it uploaded, its plans --
# still resolves, and it can be restored exactly as it was.
@admin_bp.route("/users/<int:user_id>/archive", methods=["POST"])
@role_required("Admin")
def archive_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.user_id == current_user.user_id:
        flash("You cannot archive your own account.", "warning")
        return redirect(url_for("admin.users"))
    if user.email == SYSTEM_USER_EMAIL:
        flash("The system account can't be archived. It owns the auto-generated placeholder data.", "warning")
        return redirect(url_for("admin.users"))
    if user.is_archived:
        flash(f"{user.email} is already archived.", "info")
        return redirect(url_for("admin.users", tab="archived"))

    reason = _reason_from_form("archive this account")
    if reason is None:
        return redirect(url_for("admin.users"))

    user.archive(current_user.user_id, reason)
    db.session.commit()
    log_action("admin_archive_user", details=f"{user.role} account", target=user, reason=reason)
    flash(f"{user.email} has been archived and can no longer sign in.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/restore", methods=["POST"])
@role_required("Admin")
def restore_user(user_id):
    user = User.query.get_or_404(user_id)
    if not user.is_archived:
        flash(f"{user.email} is not archived.", "info")
        return redirect(url_for("admin.users"))

    reason = _reason_from_form("restore this account")
    if reason is None:
        return redirect(url_for("admin.users", tab="archived"))

    # restore() clears only the archive stamp; status is untouched, so an
    # account that was suspended before it was archived comes back
    # suspended rather than being quietly reactivated.
    user.restore()
    db.session.commit()
    log_action("admin_restore_user", details=f"{user.role} account", target=user, reason=reason)
    state = "can sign in again" if user.is_active else "is back, still suspended"
    flash(f"{user.email} has been restored and {state}.", "success")
    return redirect(url_for("admin.users"))


# ------------------------------------------------------------- audit trail
def _present_entry(entry):
    """One audit row as the five W's, ready to show.

    Every fallback for a row written before the five-W columns existed
    lives here, once, so the page, the dashboard and the CSV export can
    never disagree about what an old row says:

      WHO   the name/role snapshot, else the account as it is now, else
            a plain description of who it could only have been
      WHAT  the label for the action (humanised if unknown)
      WHERE whatever was captured; the template shows a dash for the rest
      WHY   the recorded reason; an old row has none and says so, rather
            than being given one after the fact
    """
    user = entry.user
    name = entry.actor_name or (user.name if user else None)
    if name is None:
        if entry.user_id is not None:
            name = f"Account #{entry.user_id}"
        elif entry.route:
            # Recorded inside a request with nobody signed in -- a failed
            # sign-in, a password-reset request.
            name = "Visitor (not signed in)"
        elif entry.reason:
            # The five-W writer always records a WHY, so a row that has
            # one but no request was written by a background job.
            name = "System"
        else:
            name = "No account recorded"

    reason = entry.reason
    return SimpleNamespace(
        id=entry.id,
        # WHO
        who_name=name,
        who_email=user.email if user else None,
        who_role=entry.actor_role or (user.role if user else None),
        who_archived=bool(user is not None and user.is_archived),
        # WHAT
        action=entry.action,
        what=label_for(entry.action),
        target_type=target_type_label(entry.target_type),
        target_id=entry.target_id,
        target_label=entry.target_label,
        details=entry.details,
        # WHEN
        when=_to_local(entry.created_at),
        when_utc=entry.created_at,
        # WHERE
        ip=entry.ip_address,
        method=entry.http_method,
        route=entry.route,
        device=describe_device(entry.user_agent),
        user_agent=entry.user_agent,
        # WHY -- and whether it is a person's words or the action's
        # standing purpose, so the page can tell the two apart.
        why=reason,
        why_is_stated=bool(reason) and reason != purpose_for(entry.action),
    )


def _parse_day(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _audit_filters():
    """The GET filters, cleaned. Anything unrecognised is dropped rather
    than erroring -- a hand-edited URL should show everything, not a 500."""
    args = request.args
    role = args.get("role", "").strip()
    day_from = _parse_day(args.get("date_from", "").strip())
    day_to = _parse_day(args.get("date_to", "").strip())
    if day_from and day_to and day_from > day_to:
        day_from, day_to = day_to, day_from
    return {
        "q": " ".join(args.get("q", "").split())[:100],
        "role": role if role in {value for value, _label in AUDIT_ROLE_CHOICES} else "",
        "action": args.get("action", "").strip()[:100],
        "date_from": day_from.isoformat() if day_from else "",
        "date_to": day_to.isoformat() if day_to else "",
    }


# "!" rather than the usual backslash: MySQL treats a backslash inside a
# string literal as an escape of its own, so the same ESCAPE clause
# means different things on MySQL and SQLite. "!" means the same on both.
_LIKE_ESCAPE = "!"


def _like(text):
    """%text% with LIKE's own wildcards escaped, so searching for
    "admin_archive" matches that literally instead of treating the
    underscore as "any character"."""
    e = _LIKE_ESCAPE
    escaped = text.replace(e, e + e).replace("%", e + "%").replace("_", e + "_")
    return f"%{escaped}%"


def _audit_query(filters):
    """AuditLog rows matching the filters, newest first, with each row's
    account loaded in the same query (the outer join is also what lets
    the role and search filters fall back to the account for rows that
    predate the actor_* snapshot columns)."""
    query = AuditLog.query.outerjoin(AuditLog.user).options(contains_eager(AuditLog.user))

    if filters["q"]:
        pattern = _like(filters["q"])
        conditions = [
            column.ilike(pattern, escape=_LIKE_ESCAPE)
            for column in (
                AuditLog.action, AuditLog.details, AuditLog.target_label, AuditLog.actor_name,
                AuditLog.reason, AuditLog.ip_address, User.name, User.email,
            )
        ]
        labelled = actions_matching(filters["q"])
        if labelled:
            conditions.append(AuditLog.action.in_(labelled))
        query = query.filter(or_(*conditions))

    if filters["role"] == "none":
        query = query.filter(AuditLog.user_id.is_(None), AuditLog.actor_role.is_(None))
    elif filters["role"]:
        query = query.filter(or_(
            AuditLog.actor_role == filters["role"],
            and_(AuditLog.actor_role.is_(None), User.role == filters["role"]),
        ))

    if filters["action"]:
        query = query.filter(AuditLog.action == filters["action"])

    # The dates the admin picks are Philippine dates; created_at is UTC.
    # "From Oct 1" therefore starts at Oct 1 00:00 PHT = Sep 30 16:00 UTC.
    if filters["date_from"]:
        start = datetime.combine(date.fromisoformat(filters["date_from"]), time.min) - DISPLAY_UTC_OFFSET
        query = query.filter(AuditLog.created_at >= start)
    if filters["date_to"]:
        end = datetime.combine(date.fromisoformat(filters["date_to"]) + timedelta(days=1), time.min)
        query = query.filter(AuditLog.created_at < end - DISPLAY_UTC_OFFSET)

    return query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())


@admin_bp.route("/audit-log")
@role_required("Admin")
def audit_log():
    filters = _audit_filters()
    page = request.args.get("page", 1, type=int)
    pagination = _audit_query(filters).paginate(page=page, per_page=AUDIT_PER_PAGE, error_out=False)

    # Only actions that actually occur -- a dropdown of forty options
    # most of which match nothing is a dropdown nobody uses.
    present_actions = [row[0] for row in db.session.query(AuditLog.action).distinct().all() if row[0]]
    action_choices = sorted(((a, label_for(a)) for a in present_actions), key=lambda pair: pair[1].lower())

    active_filters = {key: value for key, value in filters.items() if value}
    return render_template(
        "admin/audit_log.html",
        pagination=pagination,
        entries=[_present_entry(entry) for entry in pagination.items],
        filters=filters,
        active_filters=active_filters,
        role_choices=AUDIT_ROLE_CHOICES,
        action_choices=action_choices,
    )


def _csv_cell(value):
    """A value safe to put in a spreadsheet cell.

    The audit trail holds text other people typed -- names, reasons,
    search terms, user agents -- and a cell beginning with = + - or @
    is run as a FORMULA when the file is opened in Excel or Sheets.
    Prefixing an apostrophe makes the spreadsheet show it as text.
    """
    if value is None:
        return ""
    text = str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


AUDIT_CSV_HEADER = (
    "When (UTC)", f"When ({DISPLAY_TZ_LABEL})",
    "Who", "Email", "Role",
    "What", "Action code", "Target type", "Target ID", "Target", "Details",
    "IP address", "Method", "Page", "Device", "User agent",
    "Why",
)


@admin_bp.route("/audit-log/export.csv")
@role_required("Admin")
def export_audit_log():
    """The filtered audit trail as a spreadsheet -- the same filters as
    the page, every matching row rather than one page of them."""
    filters = _audit_filters()
    entries = [_present_entry(entry) for entry in _audit_query(filters).all()]

    buffer = io.StringIO()
    # A byte-order mark so Excel reads the file as UTF-8: names such as
    # "Peña" otherwise open as mojibake.
    buffer.write("\ufeff")
    writer = csv.writer(buffer)
    writer.writerow(AUDIT_CSV_HEADER)
    for e in entries:
        writer.writerow([_csv_cell(v) for v in (
            e.when_utc.strftime("%Y-%m-%d %H:%M:%S") if e.when_utc else None,
            e.when.strftime("%Y-%m-%d %H:%M:%S") if e.when else None,
            e.who_name, e.who_email, e.who_role,
            e.what, e.action, e.target_type, e.target_id, e.target_label, e.details,
            e.ip, e.method, e.route, e.device, e.user_agent,
            e.why,
        )])

    # The filters ARE the WHAT of an export -- which slice of the trail
    # left the system -- so unlike an ordinary page view (see _where()
    # in app/utils/audit.py, which keeps query strings out of the log),
    # they are recorded, along with how many rows went out.
    described = ", ".join(f"{key}={value}" for key, value in filters.items() if value) or "no filters"
    log_action("export_audit_log", details=f"{len(entries)} rows ({described})")

    stamp = _to_local(datetime.utcnow()).strftime("%Y%m%d-%H%M")
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=audit_trail_{stamp}.csv"},
    )


# --------------------------------------------------------------- datasets
def _lgu_row_label(row):
    uploaded = row.upload_date.strftime("%b %d, %Y") if row.upload_date else "no date"
    return f"{row.barangay} ({row.source}, uploaded {uploaded})"


def _market_row_label(row):
    recorded = row.date_recorded.strftime("%b %d, %Y") if row.date_recorded else "no date"
    return f"{row.industry_type} @ {row.location} ({row.source}, {recorded})"


def _archived_records():
    """Every archived lgu_data and market_data row, newest archive first,
    as one list for the "Archived records" table.

    These two queries are the reason the include_archived escape hatch
    exists: without it, the global filter would hide exactly the rows
    this section is for."""
    lgu_rows = (
        LguData.query.execution_options(**{INCLUDE_ARCHIVED: True})
        .filter(LguData.archived_at.isnot(None))
        .all()
    )
    market_rows = (
        MarketData.query.execution_options(**{INCLUDE_ARCHIVED: True})
        .filter(MarketData.archived_at.isnot(None))
        .all()
    )
    people = _people_by_id([r.archived_by for r in lgu_rows] + [r.archived_by for r in market_rows])

    records = [
        SimpleNamespace(
            kind="LGU data", label=_lgu_row_label(row), archived_at=row.archived_at,
            archived_by=people.get(row.archived_by), reason=row.archive_reason,
            restore_url=url_for("admin.restore_lgu_data", lgu_id=row.lgu_id),
        )
        for row in lgu_rows
    ] + [
        SimpleNamespace(
            kind="Market data", label=_market_row_label(row), archived_at=row.archived_at,
            archived_by=people.get(row.archived_by), reason=row.archive_reason,
            restore_url=url_for("admin.restore_market_data", market_id=row.market_id),
        )
        for row in market_rows
    ]
    records.sort(key=lambda r: r.archived_at, reverse=True)
    return records


@admin_bp.route("/datasets")
@role_required("Admin")
def datasets():
    # No archived_at filter needed on these four: the global filter in
    # app/models/archive.py already hides archived rows from them, which
    # is the same view of the data every analysis gets.
    lgu_rows = LguData.query.order_by(LguData.upload_date.desc(), LguData.lgu_id.desc()).limit(100).all()
    market_rows = MarketData.query.order_by(MarketData.date_recorded.desc(), MarketData.market_id.desc()).limit(100).all()
    market_total = MarketData.query.count()
    market_real_count = MarketData.query.filter_by(source="Google Places API").count()
    return render_template(
        "admin/datasets.html",
        lgu_rows=lgu_rows,
        market_rows=market_rows,
        market_total=market_total,
        market_real_count=market_real_count,
        archived_records=_archived_records(),
        places_api_key_configured=bool(current_app.config.get("GOOGLE_PLACES_API_KEY")),
    )


@admin_bp.route("/datasets/export-barangay-seed-csv")
@role_required("Admin")
def export_barangay_seed_csv():
    """Downloads the full 76-barangay reference/seed dataset (see
    app/ml/seed_data.py) as a CSV file -- every sourced/generated field
    for every barangay, not just the columns the ML model consumes.
    Same export as running `python export_seed_data.py` -- this route
    just streams it as a download instead of writing a local file."""
    from app.ml.seed_data import write_seed_data_csv

    buffer = io.StringIO()
    write_seed_data_csv(buffer)
    log_action("export_barangay_seed_csv")
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=barangay_seed_data.csv"},
    )


# Archive / restore for the two data tables. There is deliberately NO
# delete route -- see point 2 in the module docstring.
#
# Both look the row up with get_including_archived(): an ordinary
# query cannot see an archived row at all, so restore could never find
# its target, and archiving an already-archived row would 404 instead
# of saying "already archived".
#
# The label and id are read BEFORE the commit and handed to the audit
# log as a tuple, so the audit row describes the record as it was when
# the admin acted on it.
def _set_archived(row, *, archive, action, label, target_type, target_id):
    if archive and row.is_archived:
        flash(f"{label} is already archived.", "info")
        return redirect(url_for("admin.datasets"))
    if not archive and not row.is_archived:
        flash(f"{label} is not archived.", "info")
        return redirect(url_for("admin.datasets"))

    reason = _reason_from_form("archive this record" if archive else "restore this record")
    if reason is None:
        return redirect(url_for("admin.datasets"))

    if archive:
        row.archive(current_user.user_id, reason)
    else:
        row.restore()
    db.session.commit()
    _refresh_figures()
    log_action(action, target=(target_type, target_id, label), reason=reason)

    if archive:
        flash(f"Archived {label}. It no longer counts in any analysis.", "success")
    else:
        flash(f"Restored {label}. It counts in every analysis again.", "success")
    return redirect(url_for("admin.datasets"))


def _lgu_row_or_404(lgu_id):
    row = get_including_archived(LguData, lgu_id)
    if row is None:
        abort(404)
    return row


def _market_row_or_404(market_id):
    row = get_including_archived(MarketData, market_id)
    if row is None:
        abort(404)
    return row


@admin_bp.route("/datasets/lgu/<int:lgu_id>/archive", methods=["POST"])
@role_required("Admin")
def archive_lgu_data(lgu_id):
    row = _lgu_row_or_404(lgu_id)
    return _set_archived(row, archive=True, action="admin_archive_lgu_data",
                         label=_lgu_row_label(row), target_type="LguData", target_id=lgu_id)


@admin_bp.route("/datasets/lgu/<int:lgu_id>/restore", methods=["POST"])
@role_required("Admin")
def restore_lgu_data(lgu_id):
    row = _lgu_row_or_404(lgu_id)
    return _set_archived(row, archive=False, action="admin_restore_lgu_data",
                         label=_lgu_row_label(row), target_type="LguData", target_id=lgu_id)


@admin_bp.route("/datasets/market/<int:market_id>/archive", methods=["POST"])
@role_required("Admin")
def archive_market_data(market_id):
    row = _market_row_or_404(market_id)
    return _set_archived(row, archive=True, action="admin_archive_market_data",
                         label=_market_row_label(row), target_type="MarketData", target_id=market_id)


@admin_bp.route("/datasets/market/<int:market_id>/restore", methods=["POST"])
@role_required("Admin")
def restore_market_data(market_id):
    row = _market_row_or_404(market_id)
    return _set_archived(row, archive=False, action="admin_restore_market_data",
                         label=_market_row_label(row), target_type="MarketData", target_id=market_id)


# ------------------------------------------------------------ settings
def _save_plan_assumptions(form):
    """Save the plan forecast's two Admin-editable assumptions -- the
    daily wage (P1-P100,000) and the gross margin (0.05-0.95) -- and
    return a list of messages for any value refused. A blank field
    leaves the stored value alone, as the other numeric settings do.

    The wage has an UPPER bound as well as "more than zero": 1e37 is
    more than zero, and saved it overflowed the plan model's float32
    input for every plan with staff, so Home and Add Plan failed for all
    of them. DAILY_WAGE_RANGE is the same range plan_assumptions() reads
    the setting back with."""
    from app.ml.constants import DAILY_WAGE_RANGE, GROSS_MARGIN_RANGE
    from app.services.plan_forecast_service import MARGIN_SETTING_KEY, WAGE_SETTING_KEY

    rejected = []

    raw_wage = (form.get(WAGE_SETTING_KEY) or "").strip().replace(",", "")
    if raw_wage:
        low, high = DAILY_WAGE_RANGE
        try:
            wage = float(raw_wage)
        except ValueError:
            wage = None
        if wage is None or not (low <= wage <= high):  # also refuses NaN and infinity
            rejected.append(f"Settings saved, except the daily wage: it must be between ₱{low:,.0f} and "
                            f"₱{high:,.0f} a day.")
        else:
            # Stored at centavo precision -- the precision the forecast
            # uses and records it at (plan_forecast_service.WAGE_DECIMALS)
            # -- written out in full ("12345.67", where :g would have
            # cut it to six significant digits), trailing zeros dropped.
            SystemSetting.set(WAGE_SETTING_KEY, f"{wage:.2f}".rstrip("0").rstrip("."))

    raw_margin = (form.get(MARGIN_SETTING_KEY) or "").strip()
    if raw_margin:
        low, high = GROSS_MARGIN_RANGE
        try:
            margin = float(raw_margin)
        except ValueError:
            margin = None
        if margin is None or not (low <= margin <= high):
            rejected.append(f"Settings saved, except the gross margin: it must be between {low:.2f} and {high:.2f} "
                            "(e.g. 0.40 for 40%).")
        else:
            SystemSetting.set(MARGIN_SETTING_KEY, f"{margin:g}")

    return rejected


@admin_bp.route("/settings", methods=["GET", "POST"])
@role_required("Admin")
def settings():
    if request.method == "POST":
        for key in (
            "kmeans_n_clusters",
            "msi_weight_competitor_density",
            "msi_weight_demand_trend",
            "msi_weight_sociodemographic",
            "saturation_alert_threshold",
            "places_max_results",
        ):
            value = request.form.get(key)
            if value not in (None, ""):
                SystemSetting.set(key, value)

        SystemSetting.set("use_llm_recommendations", "true" if request.form.get("use_llm_recommendations") else "false")

        # Plan forecast assumptions (stage 2 of the forecast). Unlike the
        # MSI weights above, these ARE validated: a wage of 0 would make
        # every plan's payroll free and a margin of 1.0 would make every
        # price list break even on one sale -- both would quietly become
        # forecasts. plan_forecast_service also refuses such values at
        # read time, but refusing them here is what tells the Admin.
        rejected = _save_plan_assumptions(request.form)

        # The Discord invite behind the footer's Community Forum link.
        # Only a discord.gg / discord.com invite is accepted -- anything
        # else would make /community an open redirect.
        from app.controllers.community_controller import SETTING_KEY, is_valid_invite

        invite = (request.form.get("community_invite_url") or "").strip()
        invite_rejected = bool(invite) and not is_valid_invite(invite)
        if invite and not invite_rejected:
            SystemSetting.set(SETTING_KEY, invite)

        log_action("admin_update_settings")
        if invite_rejected:
            flash("Settings saved, except the community link: it must be a Discord invite "
                  "such as https://discord.gg/yourcode.", "warning")
        elif not rejected:
            flash("System settings updated.", "success")
        for message in rejected:
            flash(message, "warning")
        return redirect(url_for("admin.settings"))

    SystemSetting.ensure_defaults()
    all_settings = {row.setting_key: row for row in SystemSetting.query.all()}
    return render_template("admin/settings.html", settings=all_settings, cluster_thresholds=CLUSTER_THRESHOLDS)


# --------------------------------------------------------- LLM status
@admin_bp.route("/llm-status", methods=["GET"])
@admin_bp.route("/llm-status/probe", methods=["GET", "POST"], endpoint="llm_probe")
@role_required("Admin")
def llm_status():
    """Answers "why is the page still showing rule-based text?" in one
    place, as JSON.

    This exists because that question had no answer from outside the
    process. Every failure in llm_service was caught and discarded, so
    a wrong API key, a model name the endpoint does not recognise, a
    spent quota and a malformed response all produced the same
    thing -- rule-based wording, no log line, no clue. On a hosted
    deployment the only way to tell them apart was to add print
    statements and redeploy.

    /admin/llm-status reports the configuration and the most recent
    failure. /admin/llm-status/probe additionally makes a real
    one-sentence call and reports the provider's own answer, or the
    provider's own error.

    NO SECRET IS RETURNED. See llm_service.llm_status(): the key is
    reported as set/not-set, its length, and its first four
    characters, which is enough to tell an AI Studio key ("AIza") from
    an OpenRouter key ("sk-o") from an OAuth access token ("AQ.",
    "ya29") -- the mistake that actually happens -- and useless to
    anyone else. Admin-only on top of that.
    """
    from flask import jsonify

    from app.services import llm_service

    payload = {"status": llm_service.llm_status()}
    if request.path.endswith("/probe"):
        log_action("admin_llm_probe")
        payload["probe"] = llm_service.probe()
        # The status is re-read AFTER the probe so last_failure
        # reflects this attempt rather than whatever failed before it.
        payload["status"] = llm_service.llm_status()
    else:
        payload["hint"] = "Open /admin/llm-status/probe to make a real test call."
    return jsonify(payload)


# ------------------------------------------------------- email status
@admin_bp.route("/email-status", methods=["GET"])
@admin_bp.route("/email-status/probe", methods=["GET", "POST"], endpoint="email_probe")
@role_required("Admin")
def email_status():
    """Answers "why is the verification code not arriving?" without
    registering throwaway accounts and reading the host's logs.

    The failure modes here are few and each has a different fix -- a
    wrong app password, the account's normal password used by mistake,
    a host that blocks outbound SMTP, Gmail's daily limit reached --
    and until now they all produced the same silence, because the send
    was wrapped in a bare `except: return False`.

    /admin/email-status reports the configuration and the last
    failure. /admin/email-status/probe authenticates against Gmail for
    real and reports Gmail's own answer. The probe deliberately stops
    after login and sends no message: a diagnostic that emails
    somebody every time it runs is one nobody dares press.

    NO SECRET IS RETURNED. The app password is reported as set/not-set
    and its length -- a Gmail app password is exactly 16 characters
    once the spaces are stripped, so a length of 19 is itself the
    diagnosis, and the length alone is no use to anyone.
    """
    from flask import jsonify

    from app.services import email_service

    payload = {"status": email_service.status()}
    if request.path.endswith("/probe"):
        log_action("admin_email_probe")
        payload["probe"] = email_service.probe()
        payload["status"] = email_service.status()
    else:
        payload["hint"] = (
            "Open /admin/email-status/probe to authenticate against Gmail for real "
            "(no message is sent)."
        )
    return jsonify(payload)
