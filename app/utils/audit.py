"""
app/utils/audit.py
--------------------
One helper used everywhere an action needs to be written to the Admin
Module's audit trail -- and it records the five W's.

  WHO    the account, with its name and role as they were at the time
  WHAT   the action, and the specific record it touched (target)
  WHEN   the timestamp
  WHERE  IP address, HTTP method + page, and the device (user agent)
  WHY    the stated reason -- required from an administrator for any
         archive, restore or access change; otherwise the purpose the
         action serves

Only WHAT has to be supplied by the caller. WHO and WHERE are captured
from the request automatically, so the dozens of existing call sites
gained four of the five W's without being edited. WHY falls back to the
action's purpose when no explicit reason is given.

Outside a request -- a background sweep, a CLI script, a test calling a
service directly -- log_action() still writes the row: WHO comes from
user_id if one was passed, and the four WHERE columns are left NULL
rather than invented. The audit page reads a row with no WHERE and no
account as "System".
"""

from flask import has_request_context, request
from flask_login import current_user

from app.extensions import db
from app.models.audit_log import AuditLog


def _primary_key(target):
    """The instance's own primary key, read from the mapper.

    Not guessed from attribute names. An earlier version walked a list
    ("user_id", "sme_id", ...) and took the first one present, which is
    wrong for any model that carries ANOTHER model's key as a foreign
    key: an SmeProfile has a user_id (its owner), so it was logged as
    the owner's id; a ForecastResult has sme_id/market_id/lgu_id, so it
    was logged as its plan's id. The mapper knows which column is the
    key -- ask it."""
    try:
        from sqlalchemy import inspect as sa_inspect

        mapper = sa_inspect(type(target))
        values = [getattr(target, column.key, None) for column in mapper.primary_key]
        values = [v for v in values if v is not None]
        if values:
            return values[0] if len(values) == 1 else "/".join(str(v) for v in values)
    except Exception:  # noqa: BLE001 -- not a mapped class; fall through
        pass
    return getattr(target, "id", None)


def _describe_target(target):
    """(type, id, label) from a model instance, a tuple, or None."""
    if target is None:
        return None, None, None
    if isinstance(target, tuple):
        padded = tuple(target) + (None, None, None)
        return padded[0], padded[1], padded[2]

    type_name = type(target).__name__
    pk = _primary_key(target)
    label = None
    for attr in ("email", "business_name", "title", "barangay", "setting_key"):
        value = getattr(target, attr, None)
        if value:
            label = str(value)
            break
    if label is None:
        for industry_attr, location_attr in (("industry_type", "location"),
                                             ("input_industry_type", "input_location")):
            industry = getattr(target, industry_attr, None)
            location = getattr(target, location_attr, None)
            if industry and location:
                label = f"{industry} @ {location}"
                break
    return type_name, pk, label


def _actor(user_id):
    """(user_id, name, role) for the acting account."""
    try:
        if user_id is None and current_user and getattr(current_user, "is_authenticated", False):
            return current_user.user_id, current_user.name, current_user.role
        if user_id is not None:
            from app.models.user import User

            user = db.session.get(User, user_id)
            if user is not None:
                return user.user_id, user.name, user.role
    except Exception:  # noqa: BLE001 -- identifying the actor must never break logging
        pass
    return user_id, None, None


def _where():
    """(ip, method, route, user_agent) -- all None outside a request,
    e.g. a background sweep or a CLI script."""
    if not has_request_context():
        return None, None, None, None
    try:
        agent = (request.headers.get("User-Agent") or "")[:255] or None
        # request.path, not the full URL: a query string can carry an
        # email address or a search term that has no business in a
        # permanent log.
        return request.remote_addr, request.method, (request.path or "")[:255], agent
    except Exception:  # noqa: BLE001
        return None, None, None, None


def default_reason(action):
    """The purpose an action serves, used as WHY when no explicit
    reason was given."""
    try:
        from app.utils.audit_labels import purpose_for

        return purpose_for(action)
    except Exception:  # noqa: BLE001
        return None


def _clip(value, limit):
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def log_action(action, details=None, user_id=None, *, target=None, target_type=None,
               target_id=None, target_label=None, reason=None):
    """Write one row to audit_logs. Never raises -- a logging failure
    should never break the feature that triggered it.

    `target` may be a model instance (User, SmeProfile, LguData, ...) or a
    (type, id, label) tuple; the explicit target_* arguments override it.
    `reason` is the WHY; when omitted, the action's purpose is recorded.
    """
    try:
        uid, name, role = _actor(user_id)
        t_type, t_id, t_label = _describe_target(target)
        ip, method, route, agent = _where()

        entry = AuditLog(
            user_id=uid,
            actor_name=_clip(name, 100),
            actor_role=_clip(role, 20),
            action=action,
            target_type=_clip(target_type or t_type, 50),
            target_id=_clip(target_id if target_id is not None else t_id, 50),
            target_label=_clip(target_label or t_label, 255),
            details=_clip(details, 500),
            ip_address=ip,
            http_method=method,
            route=route,
            user_agent=agent,
            reason=_clip(reason, 255) or _clip(default_reason(action), 255),
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
