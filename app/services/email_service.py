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
        return True
    except Exception:
        return False
