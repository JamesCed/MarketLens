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
