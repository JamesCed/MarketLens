"""
seed.py
--------
Brings a database up to the state the app needs to run. Safe to run
again at any time -- every step checks before it writes.

    python seed.py                 normal run
    python seed.py --retrain       force the AI model to be retrained
    python seed.py --demo          force the demo accounts on
    python seed.py --no-demo       force the demo accounts off

SAFE TO RUN AGAINST YOUR EXISTING dss_db -- unlike sql/schema.sql (which
DROPs tables), this uses SQLAlchemy's db.create_all(), which only
creates a table that does not already exist. It never drops or alters
your 5 real tables (user, sme_profile, market_data, lgu_data,
forecast_result) or the rows in them.

WHAT IT DOES
  1. Creates any missing table -- in practice the 4 additive ones
     (notifications, plan_saves, audit_logs, system_settings) the first
     time, since the 5 core tables already exist.
  2. Inserts the default system_settings (clustering K, MSI weights,
     early-warning threshold, Places result cap, LLM toggle).
  3. Creates the system@dss.local account that
     app/services/forecasting_service.py attributes auto-generated
     placeholder lgu_data rows to. It has a random unguessable password;
     nobody is meant to log in as it.
  4. Creates login accounts -- SEE THE NEXT SECTION, this is the step
     that behaves differently on a public host.
  5. Trains the AI models -- the market Random Forest and the Plan
     Viability Model built on it -- unless both are already on disk.
     See "Retraining" below.

=====================================================================
DEMO ACCOUNTS AND WHY THEY TURN THEMSELVES OFF IN PRODUCTION
=====================================================================
On localhost this script creates three fixed accounts so you can log in
straight away:

    admin@dss.local / admin123     (Administrator)
    sme@dss.local   / sme12345     (SME / Entrepreneur)
    lgu@dss.local   / lgu12345     (LGU Official)

Those passwords are written, in plain text, in this file -- which lives
in your GitHub repository. On localhost that is fine. On a PUBLIC URL it
is not a demo account, it is an administrator login that anyone who
opens your repo already knows.

So when FLASK_CONFIG=production (which is what Render is set to), this
script creates NONE of them. Instead it creates a single administrator
from two environment variables you set in the host's dashboard:

    ADMIN_EMAIL      e.g. you@example.com
    ADMIN_PASSWORD   something long that is not in any repository

If you do not set those, no login account is created at all and you sign
up through the app's own registration page like any other user -- an
account created that way is an SME, so set the two variables if you want
an Admin. Either way, nothing with a published password exists on the
live site.

Override the automatic choice with --demo / --no-demo, or by setting
SEED_DEMO_ACCOUNTS=true/false. Passing --demo on a public host is then a
decision rather than an accident, which is the point.

=====================================================================
RETRAINING
=====================================================================
Training takes real time and CPU, and the models do not change unless
the reference dataset does -- so this script SKIPS training when both
app/ml/model_store/rf_model.pkl and plan_model.pkl already exist. That
matters on a host: it means `python seed.py` is cheap to re-run, so it
can sit in front of the start command without adding a minute to every
restart.

When only plan_model.pkl is missing (an install from before the Plan
Viability Model existed), only that model is trained, on top of the
market model already on disk (`python -m app.ml.train_model
--plan-only`) -- the market model is left exactly as it was.

Force a full retrain with `python seed.py --retrain` (or
FORCE_RETRAIN=true) after changing app/ml/seed_data.py, app/ml/plan_model.py
or anything in app/ml/train_model.py.
"""

import argparse
import os
import secrets
import sys

from app import create_app
from app.extensions import db
from app.models import User, SystemSetting
from app.services.forecasting_service import SYSTEM_USER_EMAIL

MODEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "app", "ml", "model_store", "rf_model.pkl")
# Stage 2 of the forecast, the Plan Viability Model. Checked separately
# because an install that predates it has rf_model.pkl and not this --
# and that install needs stage 2 trained, not stage 1 retrained.
PLAN_MODEL_FILE = os.path.join(os.path.dirname(MODEL_FILE), "plan_model.pkl")

DEMO_ACCOUNTS = [
    ("System Administrator", "admin@dss.local", "admin123", "Admin"),
    ("Juan Dela Cruz", "sme@dss.local", "sme12345", "SME"),
    ("Maria Santos", "lgu@dss.local", "lgu12345", "LGU"),
]


def _bool(value, default=False):
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _create_account_if_missing(name, email, password, role, show_password=True):
    if User.query.filter_by(email=email).first() is not None:
        print(f"  {email} already exists, skipping.")
        return False
    user = User(name=name, email=email, role=role)
    user.set_password(password)
    db.session.add(user)
    shown = password if show_password else "(the password you set in the environment)"
    print(f"  created account: {email} / {shown} ({role})")
    return True


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="Prepare the database this app runs on.")
    parser.add_argument("--retrain", action="store_true",
                        help="retrain the AI model even if one is already on disk")
    demo = parser.add_mutually_exclusive_group()
    demo.add_argument("--demo", dest="demo", action="store_true", default=None,
                      help="create the fixed demo logins (default on localhost)")
    demo.add_argument("--no-demo", dest="demo", action="store_false",
                      help="create no demo logins (default in production)")
    return parser.parse_args(argv)


def run(argv=None):
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    config_name = os.environ.get("FLASK_CONFIG", "development")
    is_production = config_name.strip().lower() == "production"

    # Precedence, most explicit first: the command-line flag, then the
    # environment variable, then "off in production, on everywhere else".
    if args.demo is not None:
        seed_demo = args.demo
    else:
        seed_demo = _bool(os.environ.get("SEED_DEMO_ACCOUNTS"), default=not is_production)

    app = create_app(config_name)
    with app.app_context():
        print(f"Config: {config_name}")
        print("Creating any missing tables (existing tables are left untouched)...")
        db.create_all()

        print("Ensuring default system settings...")
        SystemSetting.ensure_defaults()

        print("Ensuring the system placeholder account exists...")
        if User.query.filter_by(email=SYSTEM_USER_EMAIL).first() is None:
            system_user = User(name="System (auto-generated data owner)", role="Admin",
                               email=SYSTEM_USER_EMAIL)
            # Nobody is meant to log in as this account, so it gets a
            # password nobody -- including us -- ever sees.
            system_user.set_password(secrets.token_urlsafe(32))
            db.session.add(system_user)
            print(f"  created system account: {SYSTEM_USER_EMAIL}")
        else:
            print(f"  {SYSTEM_USER_EMAIL} already exists, skipping.")

        if seed_demo:
            print("Ensuring demo login accounts exist...")
            for name, email, password, role in DEMO_ACCOUNTS:
                _create_account_if_missing(name, email, password, role)
            if is_production:
                print("  !! These passwords are published in seed.py. You asked for them")
                print("  !! on a production config anyway -- change them before anyone")
                print("  !! else has the URL.")
        else:
            print("Demo accounts: SKIPPED (their passwords are published in this file).")
            admin_email = (os.environ.get("ADMIN_EMAIL") or "").strip()
            admin_password = os.environ.get("ADMIN_PASSWORD") or ""
            if admin_email and admin_password:
                if len(admin_password) < 8:
                    print("  ADMIN_PASSWORD is shorter than 8 characters -- refusing to")
                    print("  create an administrator with it. Set a longer one and re-run.")
                else:
                    print("Ensuring your administrator account exists...")
                    _create_account_if_missing(
                        (os.environ.get("ADMIN_NAME") or "Administrator").strip() or "Administrator",
                        admin_email, admin_password, "Admin", show_password=False,
                    )
            else:
                print("  No ADMIN_EMAIL / ADMIN_PASSWORD set, so no login account was")
                print("  created. Register through the app's own sign-up page, or set")
                print("  those two variables and run this again.")

        db.session.commit()

        # Train when EITHER model is missing. Stage 2 alone when only it
        # is missing: retraining stage 1 is not needed for it, and
        # leaving stage 1 alone keeps every market score on the site
        # exactly where it was.
        plan_only = False
        if args.retrain or _bool(os.environ.get("FORCE_RETRAIN")):
            reason = "forced"
        elif not os.path.exists(MODEL_FILE):
            reason = "no model on disk"
        elif not os.path.exists(PLAN_MODEL_FILE):
            reason = "no plan viability model on disk"
            plan_only = True
        else:
            reason = None

        if reason and plan_only:
            print(f"Training the Plan Viability Model (stage 2) on the existing market model -- {reason}...")
            from app.ml.train_model import train_plan_only

            train_plan_only()
        elif reason:
            print("Training AI models (market Random Forest + a K-Means evaluation pass, "
                  f"then the Plan Viability Model) -- {reason}...")
            from app.ml.train_model import train_and_save

            train_and_save()
        else:
            print("AI models already on disk, skipping training.")
            print("  Use --retrain after changing app/ml/seed_data.py, plan_model.py or train_model.py.")

        print("\nDone. Start the app with:  python app.py")
        print(
            "Optional: for REAL competitor counts (not just the seeded reference figures), "
            "set GOOGLE_PLACES_API_KEY in .env -- no separate seeding step is needed, the app "
            "fetches from Google Places the moment it is needed (Saturation Map, forecasts, "
            "Trend Reports). Leave PLACES_LIVE_FETCH=false on a public host."
        )


if __name__ == "__main__":
    run()
