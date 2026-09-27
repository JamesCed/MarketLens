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
    """True when SOME transport can send. See _send() for why there are
    two."""
    from flask import current_app

    if current_app.config.get("BREVO_API_KEY"):
        return True
    return bool(current_app.config.get("GMAIL_ADDRESS")
                and current_app.config.get("GMAIL_APP_PASSWORD"))


# =====================================================================
# TWO TRANSPORTS, AND WHY
# =====================================================================
# smtplib to Gmail was the only way out of here, and on 26 September
# 2026 Render began blocking outbound traffic to SMTP ports 25, 465 and
# 587 on FREE web services. The connection never leaves the host, so
# there is no app password, timeout or retry that fixes it -- the
# symptom is simply that codes stop arriving, on a deployment where
# nothing about the code changed.
#
# So there is a second transport that speaks HTTPS on port 443, which
# is not blocked: Brevo's transactional email API. Set BREVO_API_KEY
# and it is used; leave it unset and Gmail SMTP is used exactly as
# before, which keeps local development and any paid host working with
# no configuration at all.
#
# The message itself is built once, above the transport split. Two
# copies of the wording is how a change lands in the verification email
# and not in the password-reset one.

def _send(to_email, to_name, subject, body):
    """Deliver one plain-text message. True if a transport accepted it.
    Never raises -- a mail failure must not 500 a registration."""
    from flask import current_app

    if current_app.config.get("BREVO_API_KEY"):
        return _send_via_brevo(to_email, to_name, subject, body)
    return _send_via_gmail_smtp(to_email, subject, body)


def _from_address():
    from flask import current_app

    sender_name = current_app.config.get("GMAIL_SENDER_NAME", "SME Market Saturation DSS")
    address = (current_app.config.get("MAIL_FROM_ADDRESS")
               or current_app.config.get("GMAIL_ADDRESS") or "").strip()
    return sender_name, address


def _send_via_gmail_smtp(to_email, subject, body):
    """The original path: Gmail over STARTTLS on port 587."""
    sender_name, address = _from_address()
    from flask import current_app

    app_password = current_app.config.get("GMAIL_APP_PASSWORD", "")
    if not address or not app_password:
        _record_failure(RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set"))
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
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


def _send_via_brevo(to_email, to_name, subject, body):
    """HTTPS on port 443, which free hosts do not block.

    `requests` is already a dependency (the Places API uses it), so
    this adds nothing to install. The sender address must be one Brevo
    has verified -- an unverified sender is the one failure here that
    looks like a configuration error rather than a network one, so
    _hint_for() calls it out by name.
    """
    import requests
    from flask import current_app

    api_key = (current_app.config.get("BREVO_API_KEY") or "").strip()
    sender_name, address = _from_address()
    if not address:
        _record_failure(RuntimeError(
            "BREVO_API_KEY is set but no sender address -- set MAIL_FROM_ADDRESS "
            "or GMAIL_ADDRESS to the address you verified with Brevo"
        ))
        return False

    try:
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": api_key, "content-type": "application/json",
                     "accept": "application/json"},
            json={
                "sender": {"name": sender_name, "email": address},
                "to": [{"email": to_email, "name": to_name or to_email}],
                "subject": subject,
                "textContent": body,
            },
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        _record_failure(exc)
        return False

    if response.status_code in (200, 201, 202):
        _LAST_FAILURE.clear()
        return True

    _record_failure(RuntimeError(
        f"Brevo returned HTTP {response.status_code}: {response.text[:300]}"
    ))
    return False


def send_verification_code(to_email, to_name, code):
    """Sends the 6-digit registration code. True if a transport
    accepted the message for delivery."""
    from flask import current_app

    sender_name = current_app.config.get("GMAIL_SENDER_NAME", "SME Market Saturation DSS")
    ttl_minutes = current_app.config.get("EMAIL_VERIFICATION_CODE_TTL_MINUTES", 10)

    body = (
        f"Hi {to_name},\n\n"
        f"Your {sender_name} verification code is:\n\n"
        f"    {code}\n\n"
        f"Enter this code on the registration page to finish creating your account. "
        f"This code expires in {ttl_minutes} minutes.\n\n"
        f"If you didn't try to register, you can safely ignore this email.\n\n"
        f"-- {sender_name}"
    )
    return _send(to_email, to_name, f"Your {sender_name} verification code: {code}", body)


def send_password_reset_code(to_email, to_name, code):
    """Sends the 6-digit password-reset code.

    Worded differently from the registration code on purpose. A reset
    code arriving unbidden means somebody typed this address into the
    forgot-password form, and the reader needs to be told that plainly
    -- "ignore this email" is not enough when the right response may be
    to change their password.
    """
    from flask import current_app

    sender_name = current_app.config.get("GMAIL_SENDER_NAME", "SME Market Saturation DSS")
    ttl_minutes = current_app.config.get("PASSWORD_RESET_CODE_TTL_MINUTES", 15)

    body = (
        f"Hi {to_name},\n\n"
        f"Someone asked to reset the {sender_name} password for this address. "
        f"Your reset code is:\n\n"
        f"    {code}\n\n"
        f"Enter it on the password reset page. This code expires in {ttl_minutes} minutes.\n\n"
        f"If that was not you, no action has been taken and your password is unchanged. "
        f"You can ignore this email -- though if you did not expect it, it is worth "
        f"making sure your password is still one only you know.\n\n"
        f"-- {sender_name}"
    )
    return _send(to_email, to_name, f"Your {sender_name} password reset code: {code}", body)


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
        return ("Could not reach smtp.gmail.com:587 at all -- the connection never left "
                "this host. Render blocks outbound SMTP (ports 25, 465, 587) on FREE web "
                "services as of 26 September 2026, and no app password fixes that. Either "
                "upgrade to a paid instance, or set BREVO_API_KEY to send over HTTPS "
                "instead (see app/services/email_service.py).")
    if "unauthorized" in lowered or "401" in lowered:
        return "Brevo rejected the API key. Check BREVO_API_KEY."
    if "sender" in lowered and ("not valid" in lowered or "400" in lowered):
        return ("Brevo will only send from a verified sender. Add the address in "
                "MAIL_FROM_ADDRESS (or GMAIL_ADDRESS) to Brevo under Senders & IPs, "
                "confirm the email it sends you, then try again.")
    if any(word in lowered for word in ("daily", "limit", "quota", "550")):
        return "Gmail's sending limit has been reached for today (roughly 500 messages)."
    return None


def last_failure():
    return dict(_LAST_FAILURE)


def probe():
    """Check whichever transport is configured, WITHOUT sending
    anything.

    Deliberately stops short of delivery. A diagnostic that emails
    somebody every time it is pressed is one nobody dares press, and
    the steps that actually go wrong are the credential and the
    network, both of which show up before a message is accepted.
    """
    from flask import current_app

    if current_app.config.get("BREVO_API_KEY"):
        return _probe_brevo()
    return _probe_smtp()


def _probe_smtp():
    from flask import current_app

    address = (current_app.config.get("GMAIL_ADDRESS") or "").strip()
    app_password = (current_app.config.get("GMAIL_APP_PASSWORD") or "").strip()

    if not address or not app_password:
        missing = [name for name, value in
                   (("GMAIL_ADDRESS", address), ("GMAIL_APP_PASSWORD", app_password))
                   if not value]
        return {"ok": False, "transport": "smtp",
                "error": f"not configured: {' and '.join(missing)} not set"}

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(address, app_password)
        _LAST_FAILURE.clear()
        return {"ok": True, "transport": "smtp",
                "detail": f"smtp.gmail.com accepted {address}"}
    except Exception as exc:  # noqa: BLE001
        _record_failure(exc)
        return {"ok": False, "transport": "smtp", "error": last_failure()}


def _probe_brevo():
    """Calls Brevo's /account endpoint -- authenticates the key and
    sends no mail."""
    import requests
    from flask import current_app

    api_key = (current_app.config.get("BREVO_API_KEY") or "").strip()
    try:
        response = requests.get(
            "https://api.brevo.com/v3/account",
            headers={"api-key": api_key, "accept": "application/json"},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        _record_failure(exc)
        return {"ok": False, "transport": "brevo", "error": last_failure()}

    if response.status_code == 200:
        _LAST_FAILURE.clear()
        _sender_name, address = _from_address()
        return {
            "ok": True, "transport": "brevo",
            "detail": "Brevo accepted the API key",
            "sending_as": address or "(no sender address set)",
            "reminder": "the sending address must also be verified in Brevo "
                        "under Senders, Domains & Dedicated IPs",
        }

    _record_failure(RuntimeError(
        f"Brevo returned HTTP {response.status_code}: {response.text[:300]}"
    ))
    return {"ok": False, "transport": "brevo", "error": last_failure()}


def status():
    """Configuration and the most recent failure, with no secret in it.

    The app password is reported only as set/not-set and its LENGTH. A
    Gmail app password is exactly 16 characters once the spaces are
    stripped, so a length of 19 is itself the diagnosis -- and 16
    characters of length tell an attacker nothing.
    """
    from flask import current_app

    password = current_app.config.get("GMAIL_APP_PASSWORD") or ""
    brevo_key = current_app.config.get("BREVO_API_KEY") or ""
    return {
        "configured": is_configured(),
        "transport": "brevo (HTTPS)" if brevo_key else "gmail smtp (port 587)",
        "brevo_api_key": {"set": bool(brevo_key), "length": len(brevo_key)},
        "sending_as": _from_address()[1] or "(not set)",
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
