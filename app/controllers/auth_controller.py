"""
app/controllers/auth_controller.py
-------------------------------------
Registration, login and logout. Plain HTML forms (not Flask-WTF form
classes) are used on purpose -- it keeps the beginner-friendly path
obvious: read `request.form`, validate, save. CSRF protection is still
active (Flask-WTF's CSRFProtect is enabled in app/extensions.py); every
form template includes {{ csrf_token() }} in a hidden input.

Account types a visitor can self-register as: 'SME' (entrepreneur /
business owner) or 'LGU' (Local Government Unit) -- matching the real
user.role ENUM exactly. 'Admin' accounts are NOT self-registrable --
per the paper's RBAC section, only an existing Admin creates other
Admins (see app/controllers/admin_controller.py and seed.py, which
creates the first Admin account).

Note the real `user` table has only name/email/password/role/
contact_number/status -- no location/organization_name column. An SME's
business details (industry, location, capital, etc.) live in a separate
SmeProfile row.

SIGN-UP COLLECTS THE FIRST BUSINESS PLAN (SME accounts only):
registration asks for the business parameters BEFORE the account
fields, and creates the matching SmeProfile the moment the account
exists. That profile is an ordinary business plan -- it appears in "My
Plans" and on the Home page straight away, exactly like one added
later, because both are just rows in `sme_profile`.

Collecting it here rather than after the first login is deliberate:
this system has nothing to show an SME without a plan. The Home page,
the recommendations and the personalised half of the map all key off
one, so an account created without a plan lands on an empty app.

LGU accounts skip that step entirely -- an LGU official is planning for
the city, not running a business, and has no SmeProfile.

EMAIL VERIFICATION (SME/LGU self-registration only):
when a mail transport is configured (Brevo over HTTPS or Gmail over
SMTP -- see app/services/email_service.py), register() does NOT create
the User row immediately. Instead it stashes the validated form data
(with the password already hashed -- the raw password is never held
onto) plus a 6-digit code in the session, emails the code, and sends
the visitor to verify_email() to confirm they own that inbox before
the account is actually created.

WHAT HAPPENS WHEN THE SEND FAILS depends on REQUIRE_EMAIL_VERIFICATION
(app/config.py), which defaults to ON whenever a transport is
configured -- configuring one is the act of saying you want
verification. With it on, a failed send blocks the account and says so.
With it off, or with no transport configured at all, registration falls
back to creating the account immediately, so a deployment that never
set up mail is never blocked by it.

The earlier behaviour was always to fall back. That was changed because
it failed in the worst direction: a mistyped app password did not
produce "verification is broken", it produced "verification quietly
stopped happening", on a deployment that looked healthy.
"""

import json
import secrets
from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, redirect, url_for, request, flash, session, current_app
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash

import re

from app.extensions import db
from app.models import User, SmeProfile
from app.services.account_params import compose_full_name
from app.ml.constants import BUSINESS_TYPES
from app.ml.seed_data import BARANGAY_NAMES
from app.utils.audit import log_action
from app.services import email_service

auth_bp = Blueprint("auth", __name__)

ROLE_MAP = {"sme": "SME", "lgu": "LGU"}
PENDING_SESSION_KEY = "pending_registration"

BUSINESS_STAGES = ["startup", "existing"]


def _collect_business_params(form):
    """Pull the first business plan out of the registration form.

    Returns (data, errors). Delegates to app/services/plan_params.py,
    the one parser every plan form shares, so a plan entered at sign-up
    is validated exactly like one added later from Home or edited in
    Settings. `data` is JSON-safe on purpose: it is held in the session
    until the email code is confirmed.
    """
    from app.services.plan_params import parse_plan_form

    return parse_plan_form(form)


def _create_user_from_pending(pending):
    """Shared by the "email verified" path and the "Gmail not
    configured" fallback path -- builds and saves the real User row
    from a validated pending-registration dict. `password` in the dict
    is already a Werkzeug hash (see register()), never a raw password.

    For an SME this also creates the first business plan from the
    parameters given during sign-up, so the new account arrives with
    something in "My Plans" instead of an empty Home page.
    """
    from app.services.account_params import apply_personal_info
    from app.utils.privacy import decrypt_value

    user = User(
        name=pending["full_name"],
        email=pending["email"],
        role=pending["role"],
    )
    personal = json.loads(decrypt_value(pending["personal"]) or "{}") if pending.get("personal") else None
    if personal:
        apply_personal_info(user, personal)
    elif pending.get("contact_number"):
        user.contact_number = pending["contact_number"]
    if pending.get("terms_accepted_at"):
        user.terms_accepted_at = datetime.fromisoformat(pending["terms_accepted_at"])
    user.password = pending["password_hash"]
    db.session.add(user)
    db.session.commit()
    log_action("register", details=f"New {pending['role']} account: {pending['email']}", user_id=user.user_id)

    # LGU sign-ups get NO LGU access yet: the account waits for the System
    # Admin to verify the person (app/services/lgu_access_service.py).
    if pending["role"] == "LGU":
        from app.services import lgu_access_service

        lgu = pending.get("lgu") or {}
        if lgu.get("endorser_email"):
            lgu = dict(lgu, endorser_email=decrypt_value(lgu["endorser_email"]),
                       endorser_name=decrypt_value(lgu["endorser_name"]),
                       discord_username=decrypt_value(lgu["discord_username"]) if lgu.get("discord_username") else None)
            row = lgu_access_service.open_request(user, lgu)
            user.lgu_reference_code = row.reference_code
            log_action("lgu_access_requested", details=f"{row.reference_code}: {lgu['position']}, {lgu['office']} "
                                                       f"({row.requested_role_label})", user_id=user.user_id,
                       target=user)
        else:
            user.lgu_status = "pending"
            db.session.commit()

    business = pending.get("business")
    if business and not SmeProfile.query.filter_by(user_id=user.user_id).first():
        from app.services.plan_params import apply_plan_data

        profile = apply_plan_data(SmeProfile(user_id=user.user_id), business)
        db.session.add(profile)
        db.session.commit()
        log_action(
            "create_plan",
            details=f"First plan from sign-up: {profile.industry_type} @ {profile.location}",
            user_id=user.user_id,
            target=profile,
        )

    return user


def _created_message(pending, user, verified_email):
    """What the sign-in page says once the account exists."""
    start = "Email verified -- " if verified_email else ""
    if pending.get("business"):
        return (f"{start}your account and your first business plan are ready. Log in to see its forecast."
                if verified_email else "Account created with your first business plan. Log in to see its forecast.")
    code = getattr(user, "lgu_reference_code", None)
    if pending.get("role") == "LGU" and code:
        return (f"{start}your account was created. Your LGU access request {code} is waiting for verification by "
                f"the System Admin -- sign in to see what to do next.")
    return f"{start}your account has been created. You can now log in." if verified_email else \
        "Account created. You can now log in."


def _register_form_context(form=None):
    """Everything auth/register.html needs to redraw itself, including
    after a validation error -- the dropdowns AND whatever the visitor
    had already typed, so a mistake in step 3 never wipes step 2."""
    from app.ml.subcategories import as_client_payload

    offering_items = []
    if form is not None and hasattr(form, "getlist"):
        prices = form.getlist("offering_price")
        for i, name in enumerate(form.getlist("offering_item")):
            if (name or "").strip():
                offering_items.append({"item": name, "price": prices[i] if i < len(prices) else ""})
    from app.services.account_params import GENDERS, MIN_AGE, PASSWORD_HINT, PASSWORD_RULES, SUFFIXES

    today = date.today()
    try:
        latest_birthdate = today.replace(year=today.year - MIN_AGE)
    except ValueError:
        latest_birthdate = today.replace(year=today.year - MIN_AGE, day=28)
    return {
        "form": form if form is not None else {},
        "business_types": BUSINESS_TYPES,
        "locations": BARANGAY_NAMES,
        "business_stages": BUSINESS_STAGES,
        "today": today.isoformat(),
        "subcategories": as_client_payload(),
        "offering_items": offering_items,
        "suffixes": SUFFIXES,
        "genders": GENDERS,
        "latest_birthdate": latest_birthdate.isoformat(),
        "password_rules": [label for _key, label, _test in PASSWORD_RULES],
        "password_hint": PASSWORD_HINT,
        "lgu_roles": User.LGU_ROLES,
        **_lgu_guide_context(),
    }


def _lgu_guide_context():
    from app.services.lgu_access_service import guide_context

    try:
        return guide_context()
    except Exception:
        from app.services.lgu_access_service import DEFAULT_OFFICE, OFFICES, OTHER_OFFICE

        return {"verification_office": DEFAULT_OFFICE, "verification_email": current_app.config.get("SUPPORT_EMAIL", ""),
                "lgu_domains": [], "review_days": 180, "discord_url": "", "offices": OFFICES,
                "other_office": OTHER_OFFICE}


def _home_url():
    """The signed-in user's own start page. Redirecting straight there,
    rather than to "/" which then redirects again, saves the browser a
    whole extra round trip after signing in."""
    if not current_user.is_authenticated:
        return url_for("auth.login")
    if current_user.is_sme():
        return url_for("sme.home")
    if current_user.is_lgu():
        return url_for("lgu.dashboard") if current_user.is_lgu_verified else url_for("lgu.access_status")
    return url_for("admin.dashboard")


@auth_bp.route("/")
def index():
    return redirect(_home_url())


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(_home_url())

    if request.method == "POST":
        from app.services.account_params import parse_personal_info, password_problems
        from app.utils import captcha
        from app.utils.privacy import encrypt_value
        from app.utils.rate_limit import client_ip, hit, wait_text

        allowed, retry_after = hit("register", client_ip())
        if not allowed:
            flash(f"Too many sign-up attempts from this network. Please wait {wait_text(retry_after)} "
                  f"and try again.", "danger")
            return render_template("auth/register.html", **_register_form_context(request.form)), 429
        if not captcha.is_human("register", fresh=True):
            flash(captcha.REQUIRED_MESSAGE, "danger")
            return render_template("auth/register.html", **_register_form_context(request.form))

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        role = ROLE_MAP.get(request.form.get("role", "sme").strip().lower())

        errors = []
        personal, personal_errors = parse_personal_info(request.form)
        errors.extend(personal_errors)
        if not email or not password:
            errors.append("Email and password are required.")
        elif not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            errors.append("Enter a valid email address.")
        errors.extend(password_problems(password))
        if password != confirm_password:
            errors.append("Passwords do not match.")
        if not request.form.get("accept_terms"):
            errors.append("Please read and accept the Terms and Conditions and the Privacy Policy.")
        if role is None:
            errors.append("Invalid account type.")
        if User.query.filter_by(email=email).first():
            errors.append("An account with that email already exists.")
        full_name = compose_full_name(personal["first_name"], personal["middle_name"],
                                      personal["last_name"], personal["suffix"])

        business = None
        if role == "SME":
            business, business_errors = _collect_business_params(request.form)
            errors.extend(business_errors)

        lgu = None
        if role == "LGU":
            from app.services.lgu_access_service import parse_request_form

            lgu, lgu_errors = parse_request_form(request.form, email)
            errors.extend(lgu_errors)
            # The endorser's details wait in the session encrypted, like
            # the applicant's own personal details.
            lgu = dict(lgu, endorser_name=encrypt_value(lgu["endorser_name"]),
                       endorser_email=encrypt_value(lgu["endorser_email"]),
                       discord_username=encrypt_value(lgu["discord_username"]) if lgu["discord_username"] else None)

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("auth/register.html", **_register_form_context(request.form))

        pending = {
            "full_name": full_name,
            "email": email,
            "password_hash": generate_password_hash(password),
            "role": role,
            "personal": encrypt_value(json.dumps(personal)),
            "terms_accepted_at": datetime.utcnow().isoformat(),
            "business": business,
            "lgu": lgu,
        }

        if email_service.is_configured():
            code = f"{secrets.randbelow(1_000_000):06d}"
            ttl = int(current_app.config.get("EMAIL_VERIFICATION_CODE_TTL_MINUTES", 10))
            sent = email_service.send_verification_code(email, full_name, code)
            if sent:
                pending["code"] = code
                pending["expires_at"] = (datetime.utcnow() + timedelta(minutes=ttl)).isoformat()
                pending["attempts"] = 0
                pending["last_sent_at"] = datetime.utcnow().isoformat()
                session[PENDING_SESSION_KEY] = pending
                flash(f"We sent a 6-digit verification code to {email}. Enter it below to finish creating your account.", "info")
                return redirect(url_for("auth.verify_email"))

            failure = email_service.last_failure()
            current_app.logger.error(
                "verification email to %s could not be sent: %s%s",
                email,
                failure.get("detail", "no detail recorded"),
                f" -- {failure['hint']}" if failure.get("hint") else "",
            )
            if current_app.config.get("REQUIRE_EMAIL_VERIFICATION", False):
                flash(
                    "We could not send your verification code right now, so your account "
                    "was not created. Please try again in a few minutes -- if it keeps "
                    "failing, contact us and we will sort it out.",
                    "danger",
                )
                return render_template("auth/register.html", **_register_form_context(request.form))

            flash("Couldn't send a verification email right now, so your account was created directly.", "warning")

        user = _create_user_from_pending(pending)
        flash(_created_message(pending, user, verified_email=False), "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/register.html", **_register_form_context())


@auth_bp.route("/terms")
def terms():
    """The Terms and Conditions -- public, so they can be read before
    signing up (the sign-up form links here and opens the same text in a
    dialog)."""
    return render_template("auth/terms.html")


@auth_bp.route("/privacy")
def privacy():
    """The Privacy Policy (Data Privacy Act of 2012, RA 10173) -- public
    for the same reason as the terms."""
    return render_template("auth/privacy.html")


@auth_bp.route("/lgu-access-guide")
def lgu_access_guide():
    """How LGU staff and officials get an LGU role, safely -- public, so
    it can be read (and linked from the Discord community) before an
    account exists. See app/services/lgu_access_service.py."""
    return render_template("auth/lgu_access_guide.html", lgu_roles=User.LGU_ROLES, **_lgu_guide_context())


@auth_bp.route("/register/verify", methods=["GET", "POST"])
def verify_email():
    pending = session.get(PENDING_SESSION_KEY)
    if not pending or "code" not in pending:
        flash("Start registration again.", "warning")
        return redirect(url_for("auth.register"))

    if request.method == "POST":
        entered = request.form.get("code", "").strip()
        expires_at = datetime.fromisoformat(pending["expires_at"])

        if datetime.utcnow() > expires_at:
            session.pop(PENDING_SESSION_KEY, None)
            flash("That code expired. Please register again.", "danger")
            return redirect(url_for("auth.register"))

        if not entered or entered != pending["code"]:
            max_attempts = int(current_app.config.get("EMAIL_VERIFICATION_MAX_ATTEMPTS", 5))
            pending["attempts"] = int(pending.get("attempts", 0)) + 1
            remaining = max_attempts - pending["attempts"]

            if remaining <= 0:
                session.pop(PENDING_SESSION_KEY, None)
                current_app.logger.warning(
                    "verification for %s abandoned after %d incorrect codes",
                    pending["email"], pending["attempts"],
                )
                flash(
                    "Too many incorrect codes. For your security that registration was "
                    "cancelled -- please start again.",
                    "danger",
                )
                return redirect(url_for("auth.register"))

            session[PENDING_SESSION_KEY] = pending
            flash(
                f"Incorrect code -- {remaining} attempt{'s' if remaining != 1 else ''} left.",
                "danger",
            )
            return render_template("auth/verify_email.html", email=pending["email"])

        if User.query.filter_by(email=pending["email"]).first():
            session.pop(PENDING_SESSION_KEY, None)
            flash("An account with that email already exists. Please log in.", "danger")
            return redirect(url_for("auth.login"))

        user = _create_user_from_pending(pending)
        session.pop(PENDING_SESSION_KEY, None)
        flash(_created_message(pending, user, verified_email=True), "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/verify_email.html", email=pending["email"])


@auth_bp.route("/register/resend-code", methods=["POST"])
def resend_verification_code():
    from app.utils.rate_limit import client_ip, hit, wait_text

    pending = session.get(PENDING_SESSION_KEY)
    if not pending:
        flash("Start registration again.", "warning")
        return redirect(url_for("auth.register"))
    allowed, retry_after = hit("send_code", client_ip())
    if not allowed:
        flash(f"Too many codes requested. Please wait {wait_text(retry_after)}.", "danger")
        return redirect(url_for("auth.verify_email"))

    cooldown = int(current_app.config.get("EMAIL_VERIFICATION_RESEND_SECONDS", 60))
    last_sent = pending.get("last_sent_at")
    if last_sent:
        waited = (datetime.utcnow() - datetime.fromisoformat(last_sent)).total_seconds()
        if waited < cooldown:
            flash(
                f"A code was just sent. Please wait {int(cooldown - waited)} more second(s) "
                f"before asking for another -- check your spam folder in the meantime.",
                "warning",
            )
            return redirect(url_for("auth.verify_email"))

    ttl = int(current_app.config.get("EMAIL_VERIFICATION_CODE_TTL_MINUTES", 10))
    code = f"{secrets.randbelow(1_000_000):06d}"
    sent = email_service.send_verification_code(pending["email"], pending["full_name"], code)
    if sent:
        pending["code"] = code
        pending["expires_at"] = (datetime.utcnow() + timedelta(minutes=ttl)).isoformat()
        pending["last_sent_at"] = datetime.utcnow().isoformat()
        pending["attempts"] = 0
        session[PENDING_SESSION_KEY] = pending
        flash("A new code was sent to your email.", "success")
    else:
        flash("Couldn't send a new code right now -- please try again in a moment.", "danger")
    return redirect(url_for("auth.verify_email"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(_home_url())

    if request.method == "POST":
        from app.utils import captcha
        from app.utils.rate_limit import client_ip, hit, peek, reset, wait_text

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))
        attempt_key = f"{client_ip()}|{email}"

        allowed, retry_after = peek("login_failed", attempt_key)
        if allowed:
            allowed, retry_after = hit("login_ip", client_ip())
        if not allowed:
            flash(f"Too many sign-in attempts. Please wait {wait_text(retry_after)} and try again, "
                  f"or reset your password.", "danger")
            return render_template("auth/login.html", email=email), 429

        if not captcha.is_human("login", fresh=True):
            flash(captcha.REQUIRED_MESSAGE, "danger")
            return render_template("auth/login.html", email=email)

        user = User.query.filter_by(email=email).first()
        if user is None or not user.check_password(password):
            hit("login_failed", attempt_key)
            log_action("login_failed", details=f"email={email}")
            flash("Invalid email or password.", "danger")
            return render_template("auth/login.html", email=email)
        reset("login_failed", attempt_key)

        if user.lift_expired_suspension():
            db.session.commit()
            log_action("admin_toggle_active", details="Reactivated: timed suspension ended", target=user,
                       user_id=user.user_id, changes={"status": ["suspended", "active"]},
                       reason="The suspension period set by an administrator ended")

        if not user.is_active:
            if user.status == "inactive" and user.suspended_until and user.archived_at is None:
                until = (user.suspended_until + timedelta(hours=8)).strftime("%B %d, %Y %I:%M %p")
                flash(f"This account is suspended until {until}. Contact your administrator.", "danger")
            else:
                flash("This account has been deactivated. Contact your administrator.", "danger")
            return render_template("auth/login.html", email=email)

        login_user(user, remember=remember)
        user.touch_last_seen()
        db.session.commit()
        log_action("login", details=f"role={user.role}")
        flash(f"Welcome back, {user.name.split(' ')[0]}!", "success")
        return redirect(_home_url())

    return render_template("auth/login.html", email="")


@auth_bp.route("/logout")
@login_required
def logout():
    log_action("logout")
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


PASSWORD_RESET_SESSION_KEY = "pending_password_reset"


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(_home_url())

    if request.method == "POST":
        from app.utils import captcha
        from app.utils.rate_limit import client_ip, hit, wait_text

        email = request.form.get("email", "").strip().lower()
        if not email:
            flash("Please enter the email address you registered with.", "danger")
            return render_template("auth/forgot_password.html", email="")
        allowed, retry_after = hit("forgot_password", client_ip())
        if not allowed:
            flash(f"Too many reset requests. Please wait {wait_text(retry_after)} and try again.", "danger")
            return render_template("auth/forgot_password.html", email=email), 429
        if not captcha.is_human("forgot_password", fresh=True):
            flash(captcha.REQUIRED_MESSAGE, "danger")
            return render_template("auth/forgot_password.html", email=email)

        user = User.query.filter_by(email=email).first()
        ttl = int(current_app.config.get("PASSWORD_RESET_CODE_TTL_MINUTES", 15))

        code = f"{secrets.randbelow(1_000_000):06d}"
        session[PASSWORD_RESET_SESSION_KEY] = {
            "email": email,
            "code": code,
            "expires_at": (datetime.utcnow() + timedelta(minutes=ttl)).isoformat(),
            "attempts": 0,
            "last_sent_at": datetime.utcnow().isoformat(),
        }

        if user is not None and user.is_active:
            if email_service.send_password_reset_code(email, user.name, code):
                log_action("password_reset_requested", details=f"email={email}")
            else:
                current_app.logger.error(
                    "password reset code for %s could not be sent: %s%s",
                    email,
                    email_service.last_failure().get("detail", "no detail recorded"),
                    (f" -- {email_service.last_failure()['hint']}"
                     if email_service.last_failure().get("hint") else ""),
                )

        flash(
            f"If an account exists for {email}, a six-digit reset code is on its way. "
            f"Check your inbox and your spam folder, and make sure that address is "
            f"spelled the way you registered it.",
            "info",
        )
        return redirect(url_for("auth.reset_password"))

    return render_template("auth/forgot_password.html", email="")


@auth_bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    if current_user.is_authenticated:
        return redirect(_home_url())

    pending = session.get(PASSWORD_RESET_SESSION_KEY)

    if request.method == "POST":
        if not pending:
            flash("That reset code is no longer valid. Please request a new one.", "danger")
            return redirect(url_for("auth.forgot_password"))

        entered = request.form.get("code", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if datetime.utcnow() > datetime.fromisoformat(pending["expires_at"]):
            session.pop(PASSWORD_RESET_SESSION_KEY, None)
            flash("That code expired. Please request a new one.", "danger")
            return redirect(url_for("auth.forgot_password"))

        from app.services.account_params import password_problems

        problems = password_problems(password)
        if problems:
            for problem in problems:
                flash(problem, "danger")
            return render_template("auth/reset_password.html", email=pending["email"])
        if password != confirm:
            flash("The two passwords do not match.", "danger")
            return render_template("auth/reset_password.html", email=pending["email"])

        if not entered or not secrets.compare_digest(entered, pending["code"]):
            max_attempts = int(current_app.config.get("EMAIL_VERIFICATION_MAX_ATTEMPTS", 5))
            pending["attempts"] = int(pending.get("attempts", 0)) + 1
            remaining = max_attempts - pending["attempts"]

            if remaining <= 0:
                session.pop(PASSWORD_RESET_SESSION_KEY, None)
                log_action("password_reset_abandoned", details=f"email={pending['email']}")
                flash(
                    "Too many incorrect codes. For your security that reset was "
                    "cancelled -- please request a new one.",
                    "danger",
                )
                return redirect(url_for("auth.forgot_password"))

            session[PASSWORD_RESET_SESSION_KEY] = pending
            flash(
                f"Incorrect code -- {remaining} attempt{'s' if remaining != 1 else ''} left.",
                "danger",
            )
            return render_template("auth/reset_password.html", email=pending["email"])

        user = User.query.filter_by(email=pending["email"]).first()
        if user is None or not user.is_active:
            session.pop(PASSWORD_RESET_SESSION_KEY, None)
            flash("That account is no longer available. Please contact us.", "danger")
            return redirect(url_for("auth.login"))

        user.set_password(password)
        db.session.commit()
        session.pop(PASSWORD_RESET_SESSION_KEY, None)
        log_action("password_reset_completed", details=f"email={user.email}")
        flash("Your password has been changed. You can now sign in with it.", "success")
        return redirect(url_for("auth.login"))

    if not pending:
        return redirect(url_for("auth.forgot_password"))
    return render_template("auth/reset_password.html", email=pending["email"])


@auth_bp.route("/reset-password/resend", methods=["POST"])
def resend_password_reset_code():
    from app.utils.rate_limit import client_ip, hit, wait_text

    pending = session.get(PASSWORD_RESET_SESSION_KEY)
    if not pending:
        return redirect(url_for("auth.forgot_password"))
    allowed, retry_after = hit("send_code", client_ip())
    if not allowed:
        flash(f"Too many codes requested. Please wait {wait_text(retry_after)}.", "danger")
        return redirect(url_for("auth.reset_password"))

    cooldown = int(current_app.config.get("EMAIL_VERIFICATION_RESEND_SECONDS", 60))
    last_sent = pending.get("last_sent_at")
    if last_sent:
        waited = (datetime.utcnow() - datetime.fromisoformat(last_sent)).total_seconds()
        if waited < cooldown:
            flash(
                f"A code was just sent. Please wait {int(cooldown - waited)} more second(s).",
                "warning",
            )
            return redirect(url_for("auth.reset_password"))

    user = User.query.filter_by(email=pending["email"]).first()
    ttl = int(current_app.config.get("PASSWORD_RESET_CODE_TTL_MINUTES", 15))
    code = f"{secrets.randbelow(1_000_000):06d}"
    if user is not None and email_service.send_password_reset_code(pending["email"], user.name, code):
        pending["code"] = code
        pending["expires_at"] = (datetime.utcnow() + timedelta(minutes=ttl)).isoformat()
        pending["last_sent_at"] = datetime.utcnow().isoformat()
        pending["attempts"] = 0
        session[PASSWORD_RESET_SESSION_KEY] = pending
        flash("A new reset code was sent.", "success")
    else:
        flash("Couldn't send a new code right now -- please try again in a moment.", "danger")
    return redirect(url_for("auth.reset_password"))
