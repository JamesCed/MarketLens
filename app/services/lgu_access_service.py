"""
app/services/lgu_access_service.py
------------------------------------
How an LGU staff member or official gets an LGU role -- safely.

WHY
"LGU Official" is a choice on the public sign-up form, so choosing it
cannot be what grants LGU access: anyone could pick it and upload
"government" data. Instead:

  1. SIGN-UP. The person signs up as an LGU Official and says where they
     work, their position, which role they need (LGU Encoder or LGU Data
     Steward) and who their head of office is. The account is created
     PENDING, with no LGU page and no LGU role, and gets a reference code
     (LGU-XXXXXX). The System Admin is notified.
  2. ENDORSEMENT. Their head of office e-mails the System Admin from an
     official LGU address, quoting the code. On the MarketLens Discord
     the applicant may open a private ticket quoting the same code --
     never an ID, password or personal number.
  3. VERIFICATION. The System Admin checks the endorsement came from the
     official address and confirms by phone through the office's number
     in the LGU directory, then approves in Admin > LGU Access (choosing
     the role) or rejects with a reason. Approval must be confirmed with
     a tick-box saying the endorsement was checked through an official
     channel. Only then does the account get its role.
  4. UPKEEP. Every verified LGU account is re-checked after
     lgu_access_review_days (default 180); access is revoked the day the
     person moves office or leaves. Discord roles follow MarketLens, never
     the other way round.

Every step notifies the person concerned and writes an audit entry.
Accounts created by an administrator are verified at creation; LGU
accounts from before this check count as verified until re-checked.
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

OFFICES = (
    "Business Permits and Licensing Office (BPLO)",
    "City Planning and Development Office (CPDO) / Zoning",
    "City Treasurer's Office",
    "City Assessor's Office",
    "Office of the City Mayor",
    "City Information and Communications Technology Office",
)
OTHER_OFFICE = "Other office"

DEFAULT_OFFICE = "City ICT / MIS Office"
DEFAULT_REVIEW_DAYS = 180
REVIEW_DAYS_RANGE = (30, 730)

_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_NAME = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿÑñ][A-Za-zÀ-ÖØ-öø-ÿÑñ .'\-]{1,119}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DOMAIN = re.compile(r"^(?=.{3,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_DISCORD = re.compile(r"^(?!.*\.\.)[a-z0-9_.]{2,32}$")


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
    """Where heads of office send endorsements: the setting, else the
    support address."""
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
    """What the guide and status pages show about who verifies people."""
    from app.controllers.community_controller import community_invite_url

    return {
        "verification_office": verification_office(),
        "verification_email": verification_email(),
        "lgu_domains": email_domains(),
        "review_days": review_days(),
        "discord_url": community_invite_url(),
        "offices": OFFICES,
        "other_office": OTHER_OFFICE,
    }


# ----------------------------------------------------------- the request

def parse_request_form(form, applicant_email):
    """The LGU part of a sign-up (or a re-submitted request). Returns
    (data, errors); data is JSON-safe so it can wait in the session while
    the e-mail code is confirmed."""
    errors = []
    domains = email_domains()
    office = (form.get("lgu_office") or "").strip()
    if office == OTHER_OFFICE:
        office = " ".join((form.get("lgu_office_other") or "").split())[:120]
        if len(office) < 3:
            errors.append("Type the name of your LGU office.")
    elif office not in OFFICES:
        errors.append("Choose your LGU office.")

    position = " ".join((form.get("lgu_position") or "").split())[:100]
    if len(position) < 2:
        errors.append("Enter your position or job title in the LGU office.")

    requested_role = (form.get("lgu_requested_role") or "").strip()
    if requested_role not in User.LGU_ROLES:
        errors.append("Choose the LGU role you need: LGU Encoder or LGU Data Steward.")

    endorser_name = " ".join((form.get("lgu_endorser_name") or "").split())
    if not _NAME.match(endorser_name):
        errors.append("Enter the full name of your head of office (letters only).")

    endorser_email = (form.get("lgu_endorser_email") or "").strip().lower()
    if not _EMAIL.match(endorser_email):
        errors.append("Enter your head of office's official e-mail address.")
    elif endorser_email == (applicant_email or "").strip().lower():
        errors.append("Your head of office's e-mail must be someone else's: the endorsement has to come from "
                      "another person.")

    if domains:
        shown = ", ".join("@" + d for d in domains)
        if applicant_email and not in_domains(applicant_email, domains):
            errors.append(f"Sign up for an LGU account with your official LGU e-mail address ({shown}).")
        if _EMAIL.match(endorser_email) and not in_domains(endorser_email, domains):
            errors.append(f"Your head of office's e-mail must be an official LGU address ({shown}).")

    discord = (form.get("lgu_discord") or "").strip().lstrip("@").lower()
    if discord and not _DISCORD.match(discord):
        errors.append("Enter your Discord username as it appears in your profile (2-32 lower-case letters, "
                      "numbers, _ or .), or leave it blank.")

    if not form.get("lgu_confirm"):
        errors.append("Confirm that you work in the LGU office you named and that your head of office will "
                      "endorse your request.")

    data = {
        "office": office,
        "position": position,
        "requested_role": requested_role,
        "endorser_name": endorser_name[:120],
        "endorser_email": endorser_email[:150],
        "discord_username": discord or None,
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


def _email(user, subject, body):
    """Best-effort e-mail; the in-app notification is the record."""
    try:
        from app.services import email_service

        if email_service.is_configured():
            email_service.send_account_notice(user.email, user.name, subject, body)
    except Exception:
        pass


def open_request(user, data):
    """Record a request and put the account in "pending". Any earlier
    pending request of the same person is replaced. Returns the request."""
    for old in LguAccessRequest.query.filter_by(user_id=user.user_id, status=REQUEST_PENDING).all():
        old.status = REQUEST_CANCELLED
        old.decided_at = datetime.utcnow()
    request = LguAccessRequest(
        user_id=user.user_id,
        reference_code=_new_code(),
        status=REQUEST_PENDING,
        requested_role=data["requested_role"],
        office=data["office"],
        position=data["position"],
        endorser_name=data["endorser_name"],
        endorser_email=data["endorser_email"],
        discord_username=data.get("discord_username"),
        created_at=datetime.utcnow(),
    )
    db.session.add(request)
    user.lgu_status = "pending"
    user.lgu_role = None
    user.lgu_office = data["office"]
    user.lgu_position = data["position"]
    user.lgu_verified_by = None
    user.lgu_verified_at = None
    _notify(_admins(), f"New LGU access request {request.reference_code}: {user.name}, {data['position']}, "
                       f"{data['office']} ({User.LGU_ROLES[data['requested_role']]}). Verify it in Admin > LGU Access.")
    db.session.commit()
    return request


def latest_request(user):
    return (LguAccessRequest.query.filter_by(user_id=user.user_id)
            .order_by(LguAccessRequest.created_at.desc(), LguAccessRequest.id.desc()).first())


def approve(request_id, admin, role, note="", confirmed=False):
    """The System Admin's approval. Returns (ok, message)."""
    request = db.session.get(LguAccessRequest, request_id)
    note = " ".join((note or "").split())[:255]
    if request is None or request.status != REQUEST_PENDING:
        return False, "That request is no longer waiting for verification."
    if role not in User.LGU_ROLES:
        return False, "Choose the role to give: LGU Encoder or LGU Data Steward."
    if not confirmed:
        return False, ("Tick the box to confirm you checked the endorsement through an official channel "
                       "(official e-mail and a call to the office) before approving.")
    user = db.session.get(User, request.user_id)
    if user is None or user.role != "LGU" or user.is_archived:
        return False, "That account is no longer an active LGU account."

    now = datetime.utcnow()
    request.status = REQUEST_APPROVED
    request.granted_role = role
    request.decided_by = admin.user_id
    request.decided_at = now
    request.decision_note = note or None
    user.lgu_status = "verified"
    user.lgu_role = role
    user.lgu_office = request.office
    user.lgu_position = request.position
    user.lgu_verified_by = admin.user_id
    user.lgu_verified_at = now
    label = User.LGU_ROLES[role]
    _notify([user], f"Your LGU access request {request.reference_code} was approved. You are now an {label} "
                    f"in MarketLens.")
    db.session.commit()
    _email(user, f"MarketLens: your LGU access was approved ({request.reference_code})",
           f"Hi {user.name},\n\nYour LGU access request {request.reference_code} was verified and approved. "
           f"You are now an {label}. Sign in to MarketLens to open your LGU pages.\n\n"
           f"MarketLens staff will never ask for your password or verification code.\n\n-- MarketLens")
    return True, f"{user.name} is now a verified {label}."


def reject(request_id, admin, reason):
    request = db.session.get(LguAccessRequest, request_id)
    reason = " ".join((reason or "").split())[:255]
    if request is None or request.status != REQUEST_PENDING:
        return False, "That request is no longer waiting for verification."
    if not reason:
        return False, "Give the reason, so the applicant knows what to fix."
    user = db.session.get(User, request.user_id)
    request.status = REQUEST_REJECTED
    request.decided_by = admin.user_id
    request.decided_at = datetime.utcnow()
    request.decision_note = reason
    if user is not None and user.role == "LGU":
        user.lgu_status = "rejected"
        user.lgu_role = None
        _notify([user], f"Your LGU access request {request.reference_code} was not approved: {reason}")
    db.session.commit()
    if user is not None:
        _email(user, f"MarketLens: your LGU access request {request.reference_code}",
               f"Hi {user.name},\n\nYour LGU access request {request.reference_code} was not approved.\n\n"
               f"Reason: {reason}\n\nSign in to MarketLens to correct and re-send it.\n\n-- MarketLens")
    return True, f"Request {request.reference_code} was not approved. The applicant has been told why."


def revoke(user_id, admin, reason):
    """Take LGU access away (moved office, left the LGU, misuse)."""
    user = db.session.get(User, user_id)
    reason = " ".join((reason or "").split())[:255]
    if user is None or not user.is_lgu_verified:
        return False, "Only a verified LGU account can have its access removed."
    if not reason:
        return False, "Give the reason for removing LGU access."
    user.lgu_status = "revoked"
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

def pending_requests():
    return (LguAccessRequest.query.filter_by(status=REQUEST_PENDING)
            .order_by(LguAccessRequest.created_at.asc()).all())


def count_pending():
    return LguAccessRequest.query.filter_by(status=REQUEST_PENDING).count()


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
