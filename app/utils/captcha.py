"""
app/utils/captcha.py
----------------------
Invisible human verification -- Cloudflare Turnstile (default) or Google
reCAPTCHA v3 -- for the routes bots go after.

WHERE IT IS CHECKED
  * Sign-in, sign-up and "forgot password": a FRESH token on every
    submission (fresh=True). These are what credential-stuffing and
    sign-up spam scripts hit.
  * The heavy routes -- creating, re-forecasting or saving a plan (AI
    models plus live Google Places lookups), the single-location
    forecast, the nearby-competitor search, the Places batch refresh and
    the AI forecast transcript. One verified token grants a "human pass"
    in the signed session for CAPTCHA_PASS_MINUTES (default 30), so a
    person working normally is not challenged on every click, while a
    script must keep solving challenges -- and the rate limits in
    app/utils/rate_limit.py cap it either way.

HOW THE BROWSER GETS A TOKEN
  static/js/captcha.js: forms marked data-captcha="<action>" fetch a
  token before they submit (hidden field captcha_token), and a fetch()
  answered with 403 + "X-Captcha-Required: 1" is retried after the page
  posts a token to /api/captcha/verify. With Turnstile's
  interaction-only appearance nothing is shown unless Cloudflare needs
  the visitor to click.

CONFIGURATION (environment)
  CAPTCHA_PROVIDER     turnstile (default) | recaptcha | none
  CAPTCHA_SITE_KEY     public site key
  CAPTCHA_SECRET_KEY   secret key -- Render environment only, never a file
  CAPTCHA_MIN_SCORE    reCAPTCHA v3 score needed (default 0.5)
  CAPTCHA_PASS_MINUTES human pass lifetime for heavy routes (default 30)
  CAPTCHA_FAIL_OPEN    true = allow the request when the provider cannot be
                       reached (default false: refuse)
  CAPTCHA_EXPECTED_HOSTNAME  optional, e.g. marketlens-w1ov.onrender.com

Without both keys verification is OFF (local development, tests); a
production start logs a warning.
"""

import logging
import time

from flask import current_app, has_request_context, jsonify, request, session

log = logging.getLogger(__name__)

VERIFY_URLS = {
    "turnstile": "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    "recaptcha": "https://www.google.com/recaptcha/api/siteverify",
}
TOKEN_FIELDS = ("captcha_token", "cf-turnstile-response", "g-recaptcha-response")
PASS_KEY = "dss_human_until"
REQUIRED_MESSAGE = ("Please complete the quick human verification and try again. If it keeps failing, "
                    "reload the page and check your connection.")


def provider():
    name = (current_app.config.get("CAPTCHA_PROVIDER") or "turnstile").strip().lower()
    return name if name in VERIFY_URLS else "none"


def enabled():
    return (provider() != "none"
            and bool((current_app.config.get("CAPTCHA_SITE_KEY") or "").strip())
            and bool((current_app.config.get("CAPTCHA_SECRET_KEY") or "").strip()))


def client_config():
    """What the browser needs (no secret)."""
    if not enabled():
        return {"enabled": False}
    return {"enabled": True, "provider": provider(),
            "siteKey": current_app.config["CAPTCHA_SITE_KEY"].strip()}


def token_from_request():
    if not has_request_context():
        return ""
    for field in TOKEN_FIELDS:
        value = request.form.get(field) if request.form else None
        if value:
            return value.strip()
    return (request.headers.get("X-Captcha-Token") or "").strip()


def _post_verify(url, data):
    """The provider's verdict as a dict. Separate so tests can replace
    it without network access."""
    import requests

    response = requests.post(url, data=data, timeout=6)
    response.raise_for_status()
    return response.json()


def verify_token(token, action=None):
    """True when the provider confirms the token (right action and, for
    reCAPTCHA v3, a high enough score)."""
    if not enabled():
        return True
    if not token or len(token) > 4096:
        return False
    name = provider()
    data = {"secret": current_app.config["CAPTCHA_SECRET_KEY"].strip(), "response": token}
    if has_request_context() and request.remote_addr:
        data["remoteip"] = request.remote_addr
    try:
        result = _post_verify(VERIFY_URLS[name], data)
    except Exception:
        log.warning("CAPTCHA verification could not reach %s", name, exc_info=True)
        return bool(current_app.config.get("CAPTCHA_FAIL_OPEN", False))
    if not result.get("success"):
        log.info("CAPTCHA refused: %s", result.get("error-codes"))
        return False
    returned_action = result.get("action")
    if action and returned_action and returned_action != action:
        log.info("CAPTCHA action mismatch: expected %s, got %s", action, returned_action)
        return False
    expected_host = (current_app.config.get("CAPTCHA_EXPECTED_HOSTNAME") or "").strip()
    if expected_host and result.get("hostname") and result["hostname"] != expected_host:
        return False
    if name == "recaptcha":
        threshold = float(current_app.config.get("CAPTCHA_MIN_SCORE", 0.5) or 0.5)
        if float(result.get("score", 0.0)) < threshold:
            return False
    return True


def has_human_pass():
    try:
        return float(session.get(PASS_KEY, 0)) > time.time()
    except (TypeError, ValueError):
        return False


def grant_human_pass():
    minutes = float(current_app.config.get("CAPTCHA_PASS_MINUTES", 30) or 30)
    session[PASS_KEY] = time.time() + minutes * 60


def is_human(action, fresh=False):
    """True when this request passes verification. fresh=True ignores
    (and never grants) the session pass -- for sign-in and sign-up."""
    if not enabled():
        return True
    if not fresh and has_human_pass():
        return True
    if verify_token(token_from_request(), action):
        if not fresh:
            grant_human_pass()
        return True
    return False


def required_response(action):
    """The JSON reply that makes captcha.js obtain a token and retry."""
    response = jsonify({"error": REQUIRED_MESSAGE, "captcha_required": True, "action": action})
    response.status_code = 403
    response.headers["X-Captcha-Required"] = "1"
    response.headers["X-Captcha-Action"] = action
    return response


def rate_limited_response(retry_after):
    from app.utils.rate_limit import wait_text

    response = jsonify({"error": f"Too many requests. Please wait {wait_text(retry_after)} and try again.",
                        "rate_limited": True, "retry_after": retry_after})
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


def protect_api(action, bucket):
    """Decorator for JSON routes that run the models or spend API quota:
    rate limit per account, then human verification (session pass)."""
    import functools

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            from flask_login import current_user

            from app.utils.rate_limit import client_ip, hit

            who = f"user:{current_user.user_id}" if current_user.is_authenticated else f"ip:{client_ip()}"
            allowed, retry_after = hit(bucket, who)
            if not allowed:
                return rate_limited_response(retry_after)
            if not is_human(action):
                return required_response(action)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def warn_if_disabled(app):
    if not app.config.get("TESTING") and not app.config.get("DEBUG"):
        with app.app_context():
            if not enabled():
                app.logger.warning("Human verification (CAPTCHA) is OFF: set CAPTCHA_SITE_KEY and "
                                   "CAPTCHA_SECRET_KEY to protect sign-in, sign-up and the AI routes.")
