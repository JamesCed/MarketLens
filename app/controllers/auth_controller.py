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

import secrets
from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, redirect, url_for, request, flash, session, current_app
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models import User, SmeProfile
from app.ml.constants import BUSINESS_TYPES
from app.ml.seed_data import BARANGAY_NAMES
from app.utils.audit import log_action
from app.services import email_service

auth_bp = Blueprint("auth", __name__)

ROLE_MAP = {"sme": "SME", "lgu": "LGU"}
PENDING_SESSION_KEY = "pending_registration"

# Kept in step with sme_controller.BUSINESS_STAGES and the
# sme_profile.business_stage ENUM.
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
    user = User(
        name=pending["full_name"],
        email=pending["email"],
        role=pending["role"],
        contact_number=pending["contact_number"],
    )
    user.password = pending["password_hash"]
    db.session.add(user)
    db.session.commit()
    log_action("register", details=f"New {pending['role']} account: {pending['email']}", user_id=user.user_id)

    business = pending.get("business")
    if business and not SmeProfile.query.filter_by(user_id=user.user_id).first():
        # ONE row, and only one. The parameters entered during sign-up are
        # not copied anywhere afterwards -- `sme_profile` IS where a
        # business plan lives, and "My Plans" and the Home page both read
        # straight from it (see SmeProfile.to_dict and the My Plans popup
        # in shared/_topbar.html). So this single insert is the plan; no
        # second record is created to represent it, and no forecast row is
        # written either.
        #
        # The existence check guards the one way a duplicate could appear:
        # a double-submitted verification form, or a retried request,
        # calling this twice for the same account.
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
        # No forecast is run here on purpose. Scoring loads the trained
        # model and can hit the Places API, which would make the visitor
        # wait -- possibly through a request timeout -- on the one page
        # where abandoning costs them the whole account. The plan is
        # ready; the forecast runs when they open Home.

    return user


def _register_form_context(form=None):
    """Everything auth/register.html needs to redraw itself, including
    after a validation error -- the dropdowns AND whatever the visitor
    had already typed, so a mistake in step 3 never wipes step 2."""
    from app.ml.subcategories import as_client_payload

    offering_items = []
    if form is not None and hasattr(form, "getlist"):
        # Redraw the optional price list rows the visitor had typed.
        prices = form.getlist("offering_price")
        for i, name in enumerate(form.getlist("offering_item")):
            if (name or "").strip():
                offering_items.append({"item": name, "price": prices[i] if i < len(prices) else ""})
    return {
        "form": form if form is not None else {},
        "business_types": BUSINESS_TYPES,
        "locations": BARANGAY_NAMES,
        "business_stages": BUSINESS_STAGES,
        "today": date.today().isoformat(),
        "subcategories": as_client_payload(),
        "offering_items": offering_items,
    }


@auth_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(
            url_for("sme.home") if current_user.is_sme()
            else url_for("lgu.dashboard") if current_user.is_lgu()
            else url_for("admin.dashboard")
        )
    return redirect(url_for("auth.login"))


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("auth.index"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        contact_number = request.form.get("contact_number", "").strip()
        role = ROLE_MAP.get(request.form.get("role", "sme").strip().lower())

        errors = []
        if not full_name or not email or not password:
            errors.append("Full name, email and password are required.")
        if password != confirm_password:
            errors.append("Passwords do not match.")
        if len(password) < 6:
            errors.append("Password must be at least 6 characters.")
        if role is None:
            errors.append("Invalid account type.")
        if User.query.filter_by(email=email).first():
            errors.append("An account with that email already exists.")

        # The first business plan, for SME sign-ups only. Validated in the
        # same pass as the account fields so the visitor sees everything
        # that is wrong at once rather than one error per submit.
        business = None
        if role == "SME":
            business, business_errors = _collect_business_params(request.form)
            errors.extend(business_errors)

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("auth/register.html", **_register_form_context(request.form))

        pending = {
            "full_name": full_name,
            "email": email,
            "password_hash": generate_password_hash(password),
            "role": role,
            "contact_number": contact_number or None,
            "business": business,
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

            # Gmail is configured but the send failed -- a wrong app
            # password, a Gmail rate limit, or a host that does not
            # allow outbound SMTP. What happens next is now a
            # deliberate choice rather than an accident.
            #
            # Creating the account anyway was the old behaviour, and on
            # a deployment that MEANS to verify people it is the worst
            # available outcome: a mistyped app password stops looking
            # like "verification is broken" and starts looking like
            # "verification quietly is not happening". Nobody notices
            # until they wonder why unverified accounts exist.
            # The reason is whatever the transport recorded, not a
            # guess. This line used to name GMAIL_ADDRESS and port 587
            # unconditionally -- so on a deployment using the Brevo
            # transport it sent the reader to check two settings that
            # were not involved, directly underneath the line stating
            # the real cause. An error message that contradicts the one
            # above it is worse than no error message.
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

        _create_user_from_pending(pending)
        flash(
            "Account created with your first business plan. Log in to see its forecast."
            if pending.get("business")
            else "Account created. You can now log in.",
            "success",
        )
        return redirect(url_for("auth.login"))

    return render_template("auth/register.html", **_register_form_context())


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
            # ATTEMPTS ARE CAPPED. A six-digit code is one chance in a
            # million per guess, which is only protection if the number
            # of guesses is bounded -- unbounded, an attacker can post
            # guesses as fast as HTTP allows for the whole ten-minute
            # window, and a million is not a large number at that rate.
            #
            # Burning the pending registration rather than locking a
            # timer keeps it simple and costs an honest typist nothing
            # but starting the form again.
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
            # Extremely unlikely (someone else registered the same email
            # while this code was pending), but handle it cleanly.
            session.pop(PENDING_SESSION_KEY, None)
            flash("An account with that email already exists. Please log in.", "danger")
            return redirect(url_for("auth.login"))

        _create_user_from_pending(pending)
        session.pop(PENDING_SESSION_KEY, None)
        flash(
            "Email verified -- your account and your first business plan are ready. Log in to see its forecast."
            if pending.get("business")
            else "Email verified -- your account has been created. You can now log in.",
            "success",
        )
        return redirect(url_for("auth.login"))

    return render_template("auth/verify_email.html", email=pending["email"])


@auth_bp.route("/register/resend-code", methods=["POST"])
def resend_verification_code():
    pending = session.get(PENDING_SESSION_KEY)
    if not pending:
        flash("Start registration again.", "warning")
        return redirect(url_for("auth.register"))

    # THROTTLED. A free Gmail account sends roughly 500 messages a day,
    # and an unthrottled resend button spends that at whatever rate
    # somebody can click -- or floods a stranger's inbox using your
    # address, since the recipient is chosen by whoever filled in the
    # registration form.
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
        # A fresh code deserves a fresh allowance of attempts -- but
        # resending must not become a way to buy unlimited guesses at
        # the OLD code, which is why the code itself is replaced above.
        pending["attempts"] = 0
        session[PENDING_SESSION_KEY] = pending
        flash("A new code was sent to your email.", "success")
    else:
        flash("Couldn't send a new code right now -- please try again in a moment.", "danger")
    return redirect(url_for("auth.verify_email"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("auth.index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))

        user = User.query.filter_by(email=email).first()
        if user is None or not user.check_password(password):
            log_action("login_failed", details=f"email={email}")
            flash("Invalid email or password.", "danger")
            return render_template("auth/login.html", email=email)

        if not user.is_active:
            flash("This account has been deactivated. Contact your administrator.", "danger")
            return render_template("auth/login.html", email=email)

        login_user(user, remember=remember)
        log_action("login", details=f"role={user.role}")
        flash(f"Welcome back, {user.name.split(' ')[0]}!", "success")
        return redirect(url_for("auth.index"))

    return render_template("auth/login.html", email="")


@auth_bp.route("/logout")
@login_required
def logout():
    log_action("logout")
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


# =====================================================================
# FORGOTTEN PASSWORD
# =====================================================================
# Same shape as registration verification -- a six-digit code to the
# address on file -- and the same three protections, for the same
# reasons: a bounded number of guesses, a throttle on resending, and a
# code that expires.
#
# ONE THING IS DELIBERATELY DIFFERENT, AND IT IS THE IMPORTANT ONE.
#
# This form must not reveal whether an address has an account. "No
# account with that email" turns the page into a free membership
# oracle: submit a list of addresses, and every one that comes back
# "sent" is a confirmed user of this system -- which, for a system
# whose users are named business owners and city officials, is not a
# harmless disclosure. So the response is identical either way, and
# the code is only actually emailed when the account exists.
#
# The cost is a real one: somebody who mistypes their own address gets
# the same reassuring message and no email. That is why the wording
# names the address back to them and says to check it.

PASSWORD_RESET_SESSION_KEY = "pending_password_reset"


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("auth.index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email:
            flash("Please enter the email address you registered with.", "danger")
            return render_template("auth/forgot_password.html", email="")

        user = User.query.filter_by(email=email).first()
        ttl = int(current_app.config.get("PASSWORD_RESET_CODE_TTL_MINUTES", 15))

        # A code is generated either way, and the session state is set
        # either way. Only the SENDING depends on the account existing.
        #
        # THE SESSION STATE MATTERS AS MUCH AS THE FLASH MESSAGE. An
        # earlier version set it only for real accounts, so an unknown
        # address bounced straight back to this form while a known one
        # went on to the reset page -- identical wording, completely
        # different page, and the membership oracle this route exists
        # to avoid was rebuilt out of a redirect. A test caught it.
        #
        # The decoy code is never emailed to anybody, so it cannot be
        # entered; the attempt cap disposes of it after a few guesses,
        # and reset_password() looks the account up again before
        # changing anything.
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
                # Logged, not shown. Telling this visitor the send
                # failed would confirm the account exists, which is the
                # one thing this route must not do.
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
        return redirect(url_for("auth.index"))

    pending = session.get(PASSWORD_RESET_SESSION_KEY)

    if request.method == "POST":
        # No pending reset in this session. Deliberately the same
        # message as a wrong code: "that code is not valid" tells a
        # visitor nothing about whether an account exists.
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

        if len(password) < 6:
            flash("Your new password must be at least 6 characters.", "danger")
            return render_template("auth/reset_password.html", email=pending["email"])
        if password != confirm:
            flash("The two passwords do not match.", "danger")
            return render_template("auth/reset_password.html", email=pending["email"])

        if not entered or not secrets.compare_digest(entered, pending["code"]):
            # compare_digest rather than != : a plain comparison on a
            # secret returns faster the earlier it finds a difference,
            # which leaks the code one character at a time to anybody
            # patient enough to measure it. The attempt cap makes that
            # attack impractical anyway; using the constant-time
            # comparison costs nothing and removes the question.
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
    pending = session.get(PASSWORD_RESET_SESSION_KEY)
    if not pending:
        return redirect(url_for("auth.forgot_password"))

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
