"""
app/utils/decorators.py
-------------------------
Role-Based Access Control (RBAC) helper. Wrap any view with
@role_required("lgu") (or several roles: @role_required("lgu", "admin"))
and it will:
  1. redirect anonymous users to the login page (handled by @login_required),
  2. return HTTP 403 for a logged-in user whose role isn't allowed.

This is what actually enforces "the gov't data upload page is only
seen by the LGU account, not entrepreneurs/business owners" -- the
nav link is also hidden for non-LGU users in the sidebar template,
but the route itself is protected here too, since hiding a link is
NOT security by itself.
"""

from functools import wraps
from flask import abort, render_template
from flask_login import current_user, login_required


def role_required(*roles):
    allowed = {r.lower() for r in roles}

    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def wrapped(*args, **kwargs):
            if (current_user.role or "").lower() not in allowed:
                abort(403)
            return view_func(*args, **kwargs)

        return wrapped

    return decorator


def upload_approval_required(view_func):
    """Gov't Data Upload is locked until an Administrator approves the
    LGU account in Admin > Manage Users (V3.9). A new LGU sign-up -- or
    someone only claiming to be LGU staff -- gets the locked page (HTTP
    403) instead of the upload form, whether they click the menu or type
    the address. Use under @role_required("LGU")."""

    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not getattr(current_user, "can_upload_gov_data", False):
            from flask import request

            if request.method == "POST":
                from app.utils.audit import log_action

                log_action("lgu_upload_blocked", details="Gov't Data Upload refused: account not approved yet",
                           target=current_user._get_current_object())
            return render_template("lgu/upload_locked.html"), 403
        return view_func(*args, **kwargs)

    return wrapped
