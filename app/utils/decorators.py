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
from flask import abort
from flask_login import current_user, login_required


def role_required(*roles):
    # Case-insensitive on purpose: the real `user.role` ENUM values are
    # capitalized ('Admin'/'SME'/'LGU'), but comparing case-insensitively
    # means a typo'd @role_required("admin") in a controller still works
    # correctly instead of silently locking every admin out with a 403.
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
