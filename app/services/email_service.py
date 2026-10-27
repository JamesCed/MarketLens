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

ALERTS. The same transports carry the notification e-mails: early
warnings (send_early_warning for an SME's own plan, send_market_alert
for the market sweep to SME and LGU accounts), new-location
recommendations and the weekly digest. Each has a text and an HTML
copy, a link back to the page it is about (app_link) and a footer
naming the Settings switch that turns it off. Callers hand them to
send_in_background() so no page waits on the mail server.
"""

import html as html_lib
import logging
import os
import queue
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

EARLY_WARNING_SWITCH = "Early warning alerts"


def is_configured():
    """True when SOME transport can send. See _send() for why there are
    two."""
    from flask import current_app

    if current_app.config.get("BREVO_API_KEY"):
        return True
    return bool(current_app.config.get("GMAIL_ADDRESS")
                and current_app.config.get("GMAIL_APP_PASSWORD"))


def _send(to_email, to_name, subject, body, html=None):
    """Deliver one message. True if a transport accepted it.

    `body` is always sent as plain text. `html`, when given, goes with
    it as the formatted version; mail apps that cannot show HTML (and
    spam filters that distrust HTML-only mail) still get the text.

    NEVER RAISES, and this outer guard is the one that makes that true.

    The transports below each catch what they expect -- an SMTP error,
    an HTTP error. This catches what they do not: anything raised
    before their own try block, by a library version that behaves
    differently on the host than it does here, or by a response shape
    nobody anticipated. Registration and password reset both call this
    while the visitor is mid-form, and a mail problem turning into a
    500 loses their typing and tells them nothing.

    "Defensive" is usually a smell. Here it is the actual requirement:
    sending email is the one step in these flows that depends on a
    third party, a network and a credential all at once.
    """
    from flask import current_app

    try:
        if current_app.config.get("BREVO_API_KEY"):
            return _send_via_brevo(to_email, to_name, subject, body, html=html)
        return _send_via_gmail_smtp(to_email, subject, body, html=html)
    except Exception as exc:
        _record_failure(exc, include_traceback=True)
        return False


def _from_address():
    from flask import current_app

    sender_name = current_app.config.get("GMAIL_SENDER_NAME", "SME Market Saturation DSS")
    address = (current_app.config.get("MAIL_FROM_ADDRESS")
               or current_app.config.get("GMAIL_ADDRESS") or "").strip()
    return sender_name, address


def _send_via_gmail_smtp(to_email, subject, body, html=None):
    """The original path: Gmail over STARTTLS on port 587."""
    sender_name, address = _from_address()
    from flask import current_app

    app_password = current_app.config.get("GMAIL_APP_PASSWORD", "")
    if not address or not app_password:
        _record_failure(RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set"))
        return False

    if html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
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
    except Exception as exc:
        _record_failure(exc)
        return False


def _send_via_brevo(to_email, to_name, subject, body, html=None):
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

    payload = {
        "sender": {"name": sender_name, "email": address},
        "to": [{"email": to_email, "name": to_name or to_email}],
        "subject": subject,
        "textContent": body,
    }
    if html:
        payload["htmlContent"] = html

    try:
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": api_key, "content-type": "application/json",
                     "accept": "application/json"},
            json=payload,
            timeout=20,
        )
    except Exception as exc:
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


def send_account_notice(to_email, to_name, subject, body):
    """A message about the account itself (LGU access approved or not).
    Not a marketing notification, so it has no unsubscribe switch.
    True if a transport accepted it."""
    return _send(to_email, to_name, subject, body)


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


def _notification_footer(switch_label):
    from flask import current_app

    sender_name = current_app.config.get("GMAIL_SENDER_NAME", "SME Market Saturation DSS")
    support = current_app.config.get("SUPPORT_EMAIL", "") or _from_address()[1]

    lines = [
        "",
        "--",
        f"You are receiving this because \"{switch_label}\" is switched on in your "
        f"{sender_name} account. Turn it off under Settings > Notifications.",
    ]
    if support:
        lines.append(f"Questions or a figure that looks wrong? Reply or write to {support}.")
    return "\n".join(lines)


def app_link(endpoint, **values):
    """An absolute link to a MarketLens page, for an e-mail.

    Alerts are often sent with no web request behind them -- from the
    background thread that runs after a publish, or after a Places
    refresh -- and there url_for(_external=True) has no host to use.
    PUBLIC_BASE_URL (Render fills RENDER_EXTERNAL_URL in for it) gives
    the address the reader can actually open; inside a request with no
    PUBLIC_BASE_URL set, the address the request came in on is used.
    "" when neither is known: a missing link is better than one that
    points at localhost.
    """
    from flask import current_app, has_request_context, request, url_for

    try:
        base = (current_app.config.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
        if has_request_context():
            path = url_for(endpoint, **values)
            base = base or request.host_url.rstrip("/")
        else:
            if not base:
                return ""
            with current_app.test_request_context("/"):
                path = url_for(endpoint, **values)
        return f"{base}{path}" if base else ""
    except Exception:
        return ""


def _notification_html(heading, body, link, link_label, footer, tone="warning"):
    """The formatted copy of a notification e-mail.

    Inline styles and one table, because mail apps (Gmail above all)
    strip <style> blocks and most CSS layout. Every piece of text is
    escaped: alert wording can contain a barangay or plan name an
    account typed in.
    """
    accent = {"warning": "#c2410c", "info": "#1d4ed8"}.get(tone, "#1d4ed8")
    paragraphs = "".join(
        f'<p style="margin:0 0 12px 0;">{html_lib.escape(part).replace(chr(10), "<br>")}</p>'
        for part in body.strip().split("\n\n") if part.strip()
    )
    button = ""
    if link:
        button = (
            f'<p style="margin:20px 0 4px 0;"><a href="{html_lib.escape(link, quote=True)}" '
            f'style="background:{accent};color:#ffffff;text-decoration:none;padding:10px 18px;'
            f'border-radius:6px;display:inline-block;font-weight:600;">{html_lib.escape(link_label)}</a></p>'
            f'<p style="margin:0;font-size:12px;color:#6b7280;word-break:break-all;">'
            f'{html_lib.escape(link)}</p>'
        )
    footer_html = "<br>".join(html_lib.escape(line) for line in footer.strip().splitlines()
                              if line.strip() and line.strip() != "--")
    return (
        '<!doctype html><html><body style="margin:0;padding:0;background:#f3f4f6;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f3f4f6;padding:24px 12px;"><tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:560px;background:#ffffff;border-radius:10px;overflow:hidden;'
        'font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5;color:#111827;">'
        f'<tr><td style="background:{accent};color:#ffffff;padding:14px 22px;font-size:16px;font-weight:700;">'
        f'{html_lib.escape(heading)}</td></tr>'
        f'<tr><td style="padding:22px;">{paragraphs}{button}</td></tr>'
        f'<tr><td style="padding:14px 22px;background:#f9fafb;font-size:12px;color:#6b7280;">{footer_html}</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def _send_notification(to_email, to_name, subject, body, switch_label, link=None,
                       link_label="Open MarketLens", heading=None, tone="info"):
    """One notification email. True if a transport accepted it.

    Returns False rather than raising when mail is not configured at
    all, so a caller looping over a hundred recipients on a deployment
    with no BREVO_API_KEY does a hundred cheap no-ops instead of a
    hundred failed connections.

    `link`, when given, is the page the reader should open; it goes in
    the text as a plain address and in the HTML copy as a button.
    """
    if not is_configured():
        return False

    greeting = f"Hi {to_name},\n\n" if to_name else ""
    main = greeting + body.strip()
    footer = _notification_footer(switch_label)
    html = _notification_html(heading or subject, main, link, link_label, footer, tone=tone)
    text = main + (f"\n\n{link_label}: {link}" if link else "")
    return _send(to_email, to_name, subject, text + "\n" + footer, html=html)


def send_market_alert(to_email, to_name, subject, body, link=None):
    """An early warning from the market sweep -- a barangay/industry the
    reader follows has actually moved."""
    return _send_notification(to_email, to_name, subject, body, EARLY_WARNING_SWITCH,
                              link=link, heading="MarketLens early warning", tone="warning")


def send_early_warning(to_email, to_name, subject, body, link=None):
    """An early warning on one of the reader's own plans: its forecast
    came back above the city's saturation alert threshold."""
    return _send_notification(to_email, to_name, subject, body, EARLY_WARNING_SWITCH,
                              link=link, link_label="Open your plan",
                              heading="MarketLens early warning", tone="warning")


def send_recommendation_alert(to_email, to_name, subject, body, link=None):
    """"Email notifications for new recommendations" -- a barangay has
    entered the top opportunities for an industry they care about."""
    return _send_notification(to_email, to_name, subject, body,
                              "Email notifications for new recommendations", link=link)


def send_trend_digest(to_email, to_name, subject, body, link=None):
    """"Weekly market trend reports" -- the periodic city digest."""
    return _send_notification(to_email, to_name, subject, body,
                              "Weekly market trend reports", link=link)


_QUEUE_STATE = {"pid": None, "queue": None, "thread": None}
_QUEUE_LOCK = threading.Lock()
_log = logging.getLogger(__name__)


def _email_queue():
    """The process's one outgoing-mail queue, started on first use.

    Re-created when the process id changes: a gunicorn worker forked
    from a parent that already started the thread inherits the queue
    object but not the thread, and mail put on it would never leave.
    """
    with _QUEUE_LOCK:
        state = _QUEUE_STATE
        thread = state["thread"]
        if state["pid"] != os.getpid() or thread is None or not thread.is_alive():
            state["pid"] = os.getpid()
            state["queue"] = queue.Queue()
            state["thread"] = threading.Thread(target=_email_worker, args=(state["queue"],),
                                               name="email-sender", daemon=True)
            state["thread"].start()
        return state["queue"]


def _email_worker(mail_queue):
    while True:
        app, func, args, kwargs = mail_queue.get()
        try:
            with app.app_context():
                func(*args, **kwargs)
        except Exception:
            _log.exception("background e-mail failed")
        finally:
            mail_queue.task_done()


def send_in_background(func, *args, **kwargs):
    """Hand one e-mail to the sender thread and return straight away.

    Sending can take seconds (Brevo's HTTPS call, Gmail's TLS handshake)
    and an alert is usually triggered by something the reader is waiting
    on -- a forecast on the Planning page, an upload being published --
    so the page must not wait for the mail. Returns True when the
    message was queued (or, with EMAIL_IN_BACKGROUND off, sent), False
    when there is no transport to send it with.
    """
    from flask import current_app

    app = current_app._get_current_object()
    if not app.config.get("EMAIL_IN_BACKGROUND", True):
        return bool(func(*args, **kwargs))
    if not is_configured():
        return False
    _email_queue().put((app, func, args, kwargs))
    return True


def wait_for_background_emails(timeout=10.0):
    """Block until the queue is empty or `timeout` passes. For tests and
    for a clean shutdown; True when everything queued has been tried."""
    import time

    mail_queue = _QUEUE_STATE["queue"]
    if mail_queue is None:
        return True
    deadline = time.monotonic() + timeout
    while mail_queue.unfinished_tasks:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


_LAST_FAILURE = {}


def _record_failure(exc, include_traceback=False):
    from datetime import datetime

    detail = f"{type(exc).__name__}: {exc}"
    _LAST_FAILURE.clear()
    _LAST_FAILURE.update({
        "detail": _redact(detail),
        "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "hint": _hint_for(detail),
    })

    if include_traceback:
        import traceback

        _LAST_FAILURE["traceback"] = _redact(traceback.format_exc(), limit=1500)
        _LAST_FAILURE["hint"] = (
            _LAST_FAILURE["hint"]
            or "Unexpected error inside the mail transport -- the traceback below is "
               "the whole of it. This is a bug rather than a configuration problem."
        )

    try:
        from flask import current_app

        current_app.logger.error(
            "outgoing email failed: %s", _LAST_FAILURE["detail"],
            exc_info=include_traceback,
        )
    except Exception:
        pass


def _redact(text, limit=400):
    """No credential may reach a log line or a diagnostics page.
    smtplib puts attempted credentials into some error messages, and a
    traceback can carry a local variable holding an API key."""
    message = str(text or "")
    try:
        from flask import current_app

        for key in ("GMAIL_APP_PASSWORD", "BREVO_API_KEY"):
            secret = (current_app.config.get(key) or "").strip()
            if len(secret) >= 8:
                message = message.replace(secret, "***redacted***")
    except Exception:
        pass
    return message[:limit]


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
    if "key not found" in lowered:
        return ("Brevo does not recognise this credential. The most likely cause is the "
                "wrong KIND of key: Brevo's 'SMTP & API' page offers both an SMTP key "
                "(starts xsmtpsib-, for their mail relay) and an API key (starts "
                "xkeysib-, for the REST API this app uses). Only the xkeysib- one works "
                "here. Open Brevo > SMTP & API > API keys and copy that value into "
                "BREVO_API_KEY.")
    if "unauthorized" in lowered or "401" in lowered:
        return ("Brevo rejected the API key. Check BREVO_API_KEY is the xkeysib- API key "
                "from Brevo > SMTP & API > API keys, copied whole and with no trailing "
                "space.")
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
    except Exception as exc:
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
    except Exception as exc:
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

    brevo_report = {"set": bool(brevo_key), "length": len(brevo_key)}
    if brevo_key:
        if brevo_key.startswith("xkeysib-"):
            brevo_report["kind"] = "API key (correct for this app)"
        elif brevo_key.startswith("xsmtpsib-"):
            brevo_report["kind"] = "SMTP key -- WRONG KIND"
            brevo_report["fix"] = (
                "This is Brevo's SMTP relay password, which their REST API rejects with "
                "401 'Key not found'. Copy the xkeysib- API key from Brevo > SMTP & API "
                "> API keys instead."
            )
        else:
            brevo_report["kind"] = "unrecognised prefix -- expected xkeysib-"

    return {
        "configured": is_configured(),
        "transport": "brevo (HTTPS)" if brevo_key else "gmail smtp (port 587)",
        "brevo_api_key": brevo_report,
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
