"""
app/services/email_service.py
--------------------------------
OPTIONAL. Sends the SME/LGU self-registration email verification code
(see app/controllers/auth_controller.py) via Gmail SMTP. Uses only
Python's built-in `smtplib` / `email` modules -- no extra package to
install for this.

Configuration is GMAIL_ADDRESS + GMAIL_APP_PASSWORD (app/config.py,
set via .env) -- an "App Password", NOT the account's normal Gmail
password (Google requires 2-Step Verification to be turned on before
it will issue one). See README "Getting a Gmail App Password" for the
exact click-by-click steps.

If Gmail isn't configured (either value blank), or the send fails for
ANY reason (bad credentials, no internet, Gmail rate limit), the
functions below return False/None and auth_controller.py falls back to
registering the account immediately, with no verification step -- a
broken or missing email setup never blocks anyone from signing up.
"""

import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr


def is_configured():
    from flask import current_app

    return bool(current_app.config.get("GMAIL_ADDRESS") and current_app.config.get("GMAIL_APP_PASSWORD"))


def send_verification_code(to_email, to_name, code):
    """Sends the 6-digit code to to_email. Returns True if Gmail
    accepted the message for delivery, False otherwise (never raises)."""
    from flask import current_app

    address = current_app.config.get("GMAIL_ADDRESS", "")
    app_password = current_app.config.get("GMAIL_APP_PASSWORD", "")
    sender_name = current_app.config.get("GMAIL_SENDER_NAME", "SME Market Saturation DSS")
    ttl_minutes = current_app.config.get("EMAIL_VERIFICATION_CODE_TTL_MINUTES", 10)

    if not address or not app_password:
        return False

    body = (
        f"Hi {to_name},\n\n"
        f"Your {sender_name} verification code is:\n\n"
        f"    {code}\n\n"
        f"Enter this code on the registration page to finish creating your account. "
        f"This code expires in {ttl_minutes} minutes.\n\n"
        f"If you didn't try to register, you can safely ignore this email.\n\n"
        f"-- {sender_name}"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"Your {sender_name} verification code: {code}"
    msg["From"] = formataddr((sender_name, address))
    msg["To"] = to_email

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(address, app_password)
            server.sendmail(address, [to_email], msg.as_string())
        _LAST_FAILURE.clear()
        return True
    except Exception as exc:  # noqa: BLE001 - a mail failure must not raise into a request
        # LOGGED, not swallowed. This used to be a bare `return False`,
        # so a wrong app password, a Gmail rate limit and a host with no
        # outbound SMTP all produced exactly the same nothing. What the
        # person sees is "the code never arrived", with no way to tell
        # which of the three it was.
        _record_failure(exc)
        return False


# ---------------------------------------------------------------------
# DIAGNOSTICS
# ---------------------------------------------------------------------
# Kept so "is the mail set up correctly?" can be answered from the
# Admin page instead of by registering throwaway accounts and reading
# the host's logs.
_LAST_FAILURE = {}


def _record_failure(exc):
    from datetime import datetime

    detail = f"{type(exc).__name__}: {exc}"
    _LAST_FAILURE.clear()
    _LAST_FAILURE.update({
        "detail": _redact(detail),
        "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "hint": _hint_for(detail),
    })
    try:
        from flask import current_app

        current_app.logger.error("verification email failed: %s", _LAST_FAILURE["detail"])
    except Exception:  # pragma: no cover - no app context
        pass


def _redact(text):
    """The app password must never reach a log line or a diagnostics
    page; smtplib puts attempted credentials into some error messages."""
    message = str(text or "")
    try:
        from flask import current_app

        secret = (current_app.config.get("GMAIL_APP_PASSWORD") or "").strip()
        if len(secret) >= 8:
            message = message.replace(secret, "***redacted***")
    except Exception:  # pragma: no cover
        pass
    return message[:400]


def _hint_for(detail):
    """Gmail's SMTP errors are precise but not self-explanatory. These
    are the ones that actually happen, translated into the thing to go
    and change."""
    lowered = detail.lower()
    if "535" in lowered or "username and password not accepted" in lowered:
        return ("Gmail rejected the credentials. Check GMAIL_ADDRESS is the account the "
                "app password was issued from, and that GMAIL_APP_PASSWORD is the "
                "16-character app password with the spaces removed -- not the account's "
                "normal password.")
    if "534" in lowered or "application-specific" in lowered:
        return ("Gmail wants an app password rather than a normal one. Turn on 2-Step "
                "Verification, then create one at myaccount.google.com/apppasswords.")
    if any(word in lowered for word in
           ("timed out", "timeout", "unreachable", "connection refused", "network",
            "gaierror", "not supported")):
        return ("Could not reach smtp.gmail.com:587 at all. This host may block outbound "
                "SMTP -- some free tiers do.")
    if any(word in lowered for word in ("daily", "limit", "quota", "550")):
        return "Gmail's sending limit has been reached for today (roughly 500 messages)."
    return None


def last_failure():
    return dict(_LAST_FAILURE)


def probe():
    """Authenticate against Gmail WITHOUT sending anything, and report
    what happened.

    Deliberately stops after login. A diagnostic that emails somebody
    every time it is pressed is one nobody dares press, and the step
    that actually goes wrong is the credential, not the delivery.
    """
    from flask import current_app

    address = (current_app.config.get("GMAIL_ADDRESS") or "").strip()
    app_password = (current_app.config.get("GMAIL_APP_PASSWORD") or "").strip()

    if not address or not app_password:
        missing = [name for name, value in
                   (("GMAIL_ADDRESS", address), ("GMAIL_APP_PASSWORD", app_password))
                   if not value]
        return {"ok": False, "error": f"not configured: {' and '.join(missing)} not set"}

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(address, app_password)
        _LAST_FAILURE.clear()
        return {"ok": True, "detail": f"smtp.gmail.com accepted {address}"}
    except Exception as exc:  # noqa: BLE001
        _record_failure(exc)
        return {"ok": False, "error": last_failure()}


def status():
    """Configuration and the most recent failure, with no secret in it.

    The app password is reported only as set/not-set and its LENGTH. A
    Gmail app password is exactly 16 characters once the spaces are
    stripped, so a length of 19 is itself the diagnosis -- and 16
    characters of length tell an attacker nothing.
    """
    from flask import current_app

    password = current_app.config.get("GMAIL_APP_PASSWORD") or ""
    return {
        "configured": is_configured(),
        "gmail_address": current_app.config.get("GMAIL_ADDRESS", "") or "(not set)",
        "app_password": {
            "set": bool(password),
            "length": len(password),
            "looks_right": len(password) == 16,
        },
        "sender_name": current_app.config.get("GMAIL_SENDER_NAME", ""),
        "require_verification": bool(current_app.config.get("REQUIRE_EMAIL_VERIFICATION", False)),
        "code_ttl_minutes": current_app.config.get("EMAIL_VERIFICATION_CODE_TTL_MINUTES", 10),
        "max_attempts": current_app.config.get("EMAIL_VERIFICATION_MAX_ATTEMPTS", 5),
        "resend_cooldown_seconds": current_app.config.get("EMAIL_VERIFICATION_RESEND_SECONDS", 60),
        "last_failure": last_failure() or None,
    }
