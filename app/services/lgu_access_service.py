"""
app/services/lgu_access_service.py
------------------------------------
How an LGU staff member or official gets an LGU role -- safely.

WHY
Anyone can open the registration form and say they work for the city,
so registering cannot be what grants an LGU role: an unchecked account
could upload "government" data. Instead:

  1. REGISTRATION (/register/lgu). The person gives their name, official
     work e-mail and password; their Tarlac City context (city-wide or
     one barangay, department / office, official job title); and the
     role they need (LGU Encoder or LGU Data Steward) with a short
     reason. The account is created "pending_verification" and the
     System Admins are notified.
  2. WHILE PENDING. The account can open the LGU Dashboard, the
     Diversification Plan, the Saturation Map and the Trend Reports, but
     has no LGU role: Gov't Data Upload / Review show as "Pending Request
     Access..." and cannot be opened. It stays pending until an admin
     decides -- requests do not expire on their own.
  3. DECISION. In Admin > Manage Users > LGU access requests the System
     Admin approves (choosing the role, after confirming the person with
     their office) or rejects with a reason. The applicant is notified
     in MarketLens and by e-mail, with a link straight to the page their
     role opens (Gov't Data Upload for an Encoder, Gov't Data Review for
     a Data Steward) or, when rejected, to their request page.
  4. UPKEEP. Verified LGU accounts are re-checked after
     lgu_access_review_days (default 180), and access is removed the day
     the person moves office or leaves.

A barangay-scoped account is limited to its barangay: an Encoder can
only stage rows for it, a Data Steward only reviews uploads for it
(staging_service). Accounts created by an administrator are verified at
creation; LGU accounts from before verification count as verified until
re-checked.
"""

import re
import secrets
from datetime import datetime, timedelta

from app.extensions import db
from app.models import Notification, SystemSetting, User
from app.models.lgu_access_request import (
    REQUEST_APPROVED,
    REQUEST_CANCELLED,
    REQUEST_PENDING,
    REQUEST_REJECTED,
    LguAccessRequest,
)

PENDING = "pending_verification"
VERIFIED = "verified"
REJECTED = "rejected"
REVOKED = "revoked"

OFFICES = (
    "City Planning and Development Office (CPDO)",
    "Business Permits and Licensing Office (BPLO)",
    "City Assessor's Office",
    "City Treasurer's Office",
    "Office of the City Mayor",
    "Barangay Executive Office",
)
SCOPES = User.LGU_SCOPES

DEFAULT_OFFICE = "City ICT / MIS Office"
DEFAULT_REVIEW_DAYS = 180
REVIEW_DAYS_RANGE = (30, 730)
REVIEW_WINDOW_HOURS = 24
REASON_LENGTH = (10, 300)

_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_DOMAIN = re.compile(r"^(?=.{3,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_TITLE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿÑñ0-9][A-Za-zÀ-ÖØ-öø-ÿÑñ0-9 .,'&()/\-]{1,99}$")


# ------------------------------------------------------------- settings

def email_domains():
    """Official LGU e-mail domains (System Settings), lower-case."""
    raw = SystemSetting.get("lgu_email_domains", "") or ""
    return [d for d in (part.strip().lower().lstrip("@") for part in raw.split(",")) if d]


def parse_domains(raw):
    """(domains, invalid) from a comma-separated setting value."""
    domains, invalid = [], []
    for part in (raw or "").split(","):
        domain = part.strip().lower().lstrip("@")
        if not domain:
            continue
        (domains if _DOMAIN.match(domain) else invalid).append(domain)
    return domains, invalid


def in_domains(email, domains):
    host = (email or "").rsplit("@", 1)[-1].lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def verification_office():
    return (SystemSetting.get("lgu_verification_office", DEFAULT_OFFICE) or "").strip() or DEFAULT_OFFICE


def verification_email():
    """Where LGU staff can write about their request: the setting, else
    the support address."""
    from flask import current_app

    value = (SystemSetting.get("lgu_verification_email", "") or "").strip()
    return value or current_app.config.get("SUPPORT_EMAIL", "")


def review_days():
    try:
        days = int(float(SystemSetting.get("lgu_access_review_days", DEFAULT_REVIEW_DAYS)))
    except (TypeError, ValueError):
        days = DEFAULT_REVIEW_DAYS
    low, high = REVIEW_DAYS_RANGE
    return max(low, min(high, days))


def guide_context():
    """What the registration form, the guide and the request page show."""
    from app.ml.seed_data import BARANGAY_NAMES

    return {
        "verification_office": verification_office(),
        "verification_email": verification_email(),
        "lgu_domains": email_domains(),
        "review_days": review_days(),
        "review_hours": REVIEW_WINDOW_HOURS,
        "offices": OFFICES,
        "lgu_scopes": SCOPES,
        "lgu_barangays": sorted(BARANGAY_NAMES),
        "lgu_roles": User.LGU_ROLES,
        "reason_length": REASON_LENGTH,
    }


# ----------------------------------------------------------- the request

def parse_request_form(form, applicant_email):
    """The "Tarlac City LGU context" and "Role selection & system scope"
    parts of an LGU registration (or of a re-sent request). Returns
    (data, errors); data is JSON-safe so it can wait in the session while
    the e-mail code is confirmed."""
    from app.ml.seed_data import BARANGAY_NAMES

    errors = []
    domains = email_domains()

    scope = (form.get("lgu_scope") or "").strip()
    barangay = (form.get("lgu_barangay") or "").strip()
    if scope not in SCOPES:
        errors.append("Choose your jurisdiction scope: City-wide or a specific barangay.")
    if scope == "barangay":
        if barangay not in BARANGAY_NAMES:
            errors.append("Choose the barangay you work for (one of Tarlac City's 76 barangays).")
    else:
        barangay = ""

    office = (form.get("lgu_office") or "").strip()
    if office not in OFFICES:
        errors.append("Choose your LGU department or office.")

    position = " ".join((form.get("lgu_position") or "").split())[:100]
    if not _TITLE.match(position):
        errors.append("Enter your official job title or designation (e.g. Planning Officer).")

    requested_role = (form.get("lgu_requested_role") or "").strip()
    if requested_role not in User.LGU_ROLES:
        errors.append("Choose the LGU role you need: LGU Encoder or LGU Data Steward.")

    reason = " ".join((form.get("lgu_reason") or "").split())
    low, high = REASON_LENGTH
    if len(reason) < low:
        errors.append(f"Briefly say which datasets your office will manage (at least {low} characters).")
    reason = reason[:high]

    if domains and applicant_email and not in_domains(applicant_email, domains):
        shown = ", ".join("@" + d for d in domains)
        errors.append(f"Register with your official LGU work e-mail address ({shown}).")

    data = {
        "scope": scope if scope in SCOPES else "city",
        "barangay": barangay or None,
        "office": office,
        "position": position,
        "requested_role": requested_role,
        "reason": reason,
    }
    return data, errors


def _new_code():
    for _ in range(20):
        code = "LGU-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        if not LguAccessRequest.query.filter_by(reference_code=code).first():
            return code
    raise RuntimeError("could not generate a unique reference code")


def _notify(users, message):
    for user in users:
        db.session.add(Notification(user_id=user.user_id, type="system", message=message[:500]))


def _admins():
    from app.services.data_import_service import _system_user_id_or_none

    system_id = _system_user_id_or_none()
    return [u for u in User.query.filter_by(role="Admin", status="active").all()
            if not u.is_archived and u.user_id != system_id]


def _link(endpoint):
    """An absolute link for an e-mail; falls back to the path."""
    from flask import url_for

    try:
        return url_for(endpoint, _external=True)
    except Exception:
        return ""


def _email(user, subject, body):
    """Best-effort e-mail; the in-app notification is the record.
    Returns True when a transport accepted it."""
    try:
        from app.services import email_service

        if email_service.is_configured():
            return bool(email_service.send_account_notice(user.email, user.name, subject, body))
    except Exception:
        pass
    return False


def _apply_context(user, data):
    user.lgu_office = data["office"]
    user.lgu_position = data["position"]
    user.lgu_scope = "barangay" if data.get("scope") == "barangay" and data.get("barangay") else "city"
    user.lgu_barangay = data.get("barangay") if user.lgu_scope == "barangay" else None


def open_request(user, data):
    """Record a request and put the account in "pending_verification".
    Any earlier pending request of the same person is replaced. Returns
    the request."""
    for old in LguAccessRequest.query.filter(LguAccessRequest.user_id == user.user_id,
                                             LguAccessRequest.status.in_((REQUEST_PENDING, "pending"))).all():
        old.status = REQUEST_CANCELLED
        old.decided_at = datetime.utcnow()
    request = LguAccessRequest(
        user_id=user.user_id,
        reference_code=_new_code(),
        status=REQUEST_PENDING,
        requested_role=data["requested_role"],
        office=data["office"],
        position=data["position"],
        jurisdiction_scope="barangay" if data.get("scope") == "barangay" else "city",
        target_barangay=data.get("barangay") if data.get("scope") == "barangay" else None,
        reason=data.get("reason") or None,
        created_at=datetime.utcnow(),
    )
    db.session.add(request)
    user.lgu_status = PENDING
    user.lgu_role = None
    _apply_context(user, data)
    user.lgu_verified_by = None
    user.lgu_verified_at = None
    _notify(_admins(), f"New LGU access request from {user.name}: {data['position']}, {data['office']} "
                       f"({request.scope_label}), asking for {User.LGU_ROLES[data['requested_role']]}. "
                       f"Review it in Admin > Manage Users.")
    db.session.commit()
    return request


def latest_request(user):
    return (LguAccessRequest.query.filter_by(user_id=user.user_id)
            .order_by(LguAccessRequest.created_at.desc(), LguAccessRequest.id.desc()).first())


def approve(request_id, admin, role, note="", confirmed=False):
    """The System Admin's approval. Returns (ok, message)."""
    request = db.session.get(LguAccessRequest, request_id)
    note = " ".join((note or "").split())[:255]
    if request is None or not request.is_pending:
        return False, "That request is no longer waiting for a decision."
    if role not in User.LGU_ROLES:
        return False, "Choose the role to give: LGU Encoder or LGU Data Steward."
    if not confirmed:
        return False, ("Tick the box to confirm you checked that this person works in the office they named "
                       "before approving.")
    user = db.session.get(User, request.user_id)
    if user is None or user.role != "LGU" or user.is_archived:
        return False, "That account is no longer an active LGU account."

    now = datetime.utcnow()
    request.status = REQUEST_APPROVED
    request.granted_role = role
    request.decided_by = admin.user_id
    request.decided_at = now
    request.decision_note = note or None
    user.lgu_status = VERIFIED
    user.lgu_role = role
    _apply_context(user, {"office": request.office, "position": request.position,
                          "scope": request.jurisdiction_scope, "barangay": request.target_barangay})
    user.lgu_verified_by = admin.user_id
    user.lgu_verified_at = now
    label = User.LGU_ROLES[role]
    page, link = (("Gov't Data Review", _link("lgu.data_review")) if role == "steward"
                  else ("Gov't Data Upload", _link("lgu.government_upload")))
    _notify([user], f"Your LGU access request was approved. You are now an {label} ({user.lgu_scope_label}); "
                    f"{page} is open in your menu.")
    db.session.commit()
    sent = _email(user, f"MarketLens: your {label} access was approved",
                  f"Hi {user.name},\n\n"
                  f"Your request for LGU access in MarketLens was approved. You are now an {label} "
                  f"({user.lgu_position}, {user.lgu_office}; {user.lgu_scope_label}).\n\n"
                  f"Open {page}:\n{link}\n\n"
                  + (f"Note from the administrator: {note}\n\n" if note else "")
                  + "MarketLens staff will never ask for your password or verification code.\n\n-- MarketLens")
    return True, f"{user.name} is now a verified {label}." + (" An e-mail was sent." if sent else "")


def reject(request_id, admin, reason):
    request = db.session.get(LguAccessRequest, request_id)
    reason = " ".join((reason or "").split())[:255]
    if request is None or not request.is_pending:
        return False, "That request is no longer waiting for a decision."
    if not reason:
        return False, "Give the reason, so the applicant knows what to fix."
    user = db.session.get(User, request.user_id)
    request.status = REQUEST_REJECTED
    request.decided_by = admin.user_id
    request.decided_at = datetime.utcnow()
    request.decision_note = reason
    if user is not None and user.role == "LGU":
        user.lgu_status = REJECTED
        user.lgu_role = None
        _notify([user], f"Your LGU access request was rejected: {reason}")
    db.session.commit()
    sent = False
    if user is not None:
        sent = _email(user, "MarketLens: your LGU access request was not approved",
                      f"Hi {user.name},\n\nYour request for {request.requested_role_label} access in MarketLens "
                      f"was not approved.\n\nReason: {reason}\n\n"
                      f"You can correct and re-send it here:\n{_link('lgu.access_status')}\n\n-- MarketLens")
    return True, (f"Request from {user.name if user else 'that account'} rejected. The applicant has been told why."
                  + (" An e-mail was sent." if sent else ""))


def revoke(user_id, admin, reason):
    """Take LGU access away (moved office, left the LGU, misuse)."""
    user = db.session.get(User, user_id)
    reason = " ".join((reason or "").split())[:255]
    if user is None or not user.is_lgu_verified:
        return False, "Only a verified LGU account can have its access removed."
    if not reason:
        return False, "Give the reason for removing LGU access."
    user.lgu_status = REVOKED
    user.lgu_role = None
    user.lgu_verified_by = admin.user_id
    user.lgu_verified_at = datetime.utcnow()
    _notify([user], f"Your LGU access in MarketLens was removed: {reason}")
    db.session.commit()
    return True, f"LGU access removed for {user.name}."


def reverify(user_id, admin):
    """Record the periodic re-check of a verified LGU account."""
    user = db.session.get(User, user_id)
    if user is None or not user.is_lgu_verified:
        return False, "Only a verified LGU account can be re-verified."
    user.lgu_verified_by = admin.user_id
    user.lgu_verified_at = datetime.utcnow()
    db.session.commit()
    return True, f"{user.name} re-verified. Next check due in {review_days()} days."


# -------------------------------------------------------------- listings

def _pending_filter():
    return LguAccessRequest.status.in_((REQUEST_PENDING, "pending"))


def pending_requests():
    return LguAccessRequest.query.filter(_pending_filter()).order_by(LguAccessRequest.created_at.asc()).all()


def count_pending():
    return LguAccessRequest.query.filter(_pending_filter()).count()


def recent_decisions(limit=20):
    return (LguAccessRequest.query.filter(LguAccessRequest.status.in_((REQUEST_APPROVED, REQUEST_REJECTED)))
            .order_by(LguAccessRequest.decided_at.desc()).limit(limit).all())


def is_review_due(user, now=None):
    if not user.is_lgu_verified:
        return False
    if user.lgu_verified_at is None:
        return True
    return (now or datetime.utcnow()) - user.lgu_verified_at > timedelta(days=review_days())


def verified_staff():
    users = [u for u in User.query.filter_by(role="LGU").order_by(User.name).all()
             if not u.is_archived and u.is_lgu_verified]
    now = datetime.utcnow()
    return [{"user": u, "review_due": is_review_due(u, now)} for u in users]


def role_coverage():
    """How many verified, active Encoders and Data Stewards there are --
    the upload workflow needs at least one of each."""
    counts = {"encoder": 0, "steward": 0}
    for user in User.query.filter_by(role="LGU", status="active").all():
        key = user.lgu_role_key
        if key and not user.is_archived:
            counts[key] += 1
    return counts


def admin_panel_context():
    """Everything the LGU access panel on Admin > Manage Users shows."""
    domains = email_domains()
    pending = [{
        "row": row,
        "applicant_domain_ok": in_domains(row.user.email, domains) if domains and row.user else None,
    } for row in pending_requests()]
    staff = verified_staff()
    decisions = recent_decisions()
    decider_ids = {r.decided_by for r in decisions} | {s["user"].lgu_verified_by for s in staff}
    decider_ids.discard(None)
    deciders = {u.user_id: u for u in User.query.filter(User.user_id.in_(decider_ids)).all()} if decider_ids else {}
    return {
        "lgu_pending": pending,
        "lgu_staff": staff,
        "lgu_decisions": decisions,
        "lgu_deciders": deciders,
        "lgu_coverage": role_coverage(),
        "lgu_roles": User.LGU_ROLES,
        "lgu_domains": domains,
        "review_days": review_days(),
        "verification_email": verification_email(),
    }
