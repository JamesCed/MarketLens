"""
app/extensions.py
------------------
Every Flask extension is instantiated here (unbound) and then attached
to the real app inside app/__init__.py via `extension.init_app(app)`.
Keeping them in their own module avoids circular imports between
models, controllers and the app factory.
"""

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf import CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()

login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "warning"


# ---------------------------------------------------------------------
# Response cache (Flask-Caching)
# ---------------------------------------------------------------------
# GUARDED IMPORT, DELIBERATELY. This is the only extension here that is
# allowed to be absent. It is a performance layer -- every route it
# wraps computes the correct answer without it -- so an install that
# has not picked up the new requirement should serve a slower page,
# not fail to boot. A hard import here would turn a missing wheel into
# a dead site, which is a bad trade for a cache.
#
# The backend is chosen in app/__init__.py: FileSystemCache, NOT
# SimpleCache. SimpleCache holds every cached payload in the worker's
# own memory, and a trend report is a few hundred KB -- on a 512 MB
# instance that is the wrong place to put it.
try:  # pragma: no cover - exercised by whether the package is installed
    from flask_caching import Cache

    cache = Cache()
    CACHING_AVAILABLE = True
except ImportError:  # pragma: no cover
    cache = None
    CACHING_AVAILABLE = False
