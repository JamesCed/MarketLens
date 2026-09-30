"""
app/controllers/onboarding_controller.py
------------------------------------------
The first-time walkthrough: "Is this your first time here?" and the
guided tour that follows a yes (see static/js/tour.js for the tour
itself and static/js/tour_steps.js for what it says).

The SERVER remembers only one thing -- User.onboarding_state -- and
remembers it so that the question is asked once per account, not once
per browser:

  NULL        never asked. Every account that existed before the
              walkthrough did starts here, so each of them is asked once.
  'touring'   said yes (or pressed "Take the tour"); a tour is under way.
  'completed' reached the last step.
  'skipped'   said no, or left the tour early.

Which STEP someone is on is deliberately not stored here. It lives in
the browser's sessionStorage, keyed by user id: it changes on every
click, it only means anything to the tab that is showing the tour, and
writing it to the database would turn every "Next" into a request and
an UPDATE for no benefit to anyone else.

Every endpoint is POST-only and answers JSON, because the only caller is
tour.js's fetch(). CSRF protection applies as usual -- tour.js sends the
token from base.html's <meta name="csrf-token"> in X-CSRFToken.

This module also provides window.DSS_ONBOARDING to every signed-in page
(the context processor below) and keeps the prompt off error pages (the
before_render_template hook at the bottom).
"""

from flask import Blueprint, jsonify, request, url_for, has_request_context
from flask import before_render_template
from flask_login import current_user, login_required

from app.extensions import db
from app.utils.audit import log_action

onboarding_bp = Blueprint("onboarding", __name__, url_prefix="/onboarding")

STATE_TOURING = "touring"
STATE_COMPLETED = "completed"
STATE_SKIPPED = "skipped"


def _clean_int(value, low=0, high=999):
    """A step number from the browser, or None. It only ever reaches the
    audit trail's free-text details, but it is still client input, so it
    is bounded rather than trusted."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def _progress_note():
    """ "at step 5 of 22" when tour.js sent where the person was, so the
    audit trail can tell "said no at the prompt" from "left halfway"."""
    payload = request.get_json(silent=True) or {}
    step = _clean_int(payload.get("step"))
    total = _clean_int(payload.get("total"))
    if step is None:
        return None
    return f"at step {step} of {total}" if total else f"at step {step}"


def _set_state(state, action, details):
    current_user.onboarding_state = state
    db.session.commit()
    # target=the account itself, so the audit page's "Target" column
    # names whose walkthrough this was rather than leaving it blank.
    log_action(action, details=details, target=current_user._get_current_object())
    return jsonify({"ok": True, "state": state})


@onboarding_bp.route("/start", methods=["POST"])
@login_required
def start():
    """Answered "Yes -- show me around" at the first-time prompt."""
    return _set_state(STATE_TOURING, "onboarding_started",
                      f"Said yes at the first-time prompt (role={current_user.role})")


@onboarding_bp.route("/restart", methods=["POST"])
@login_required
def restart():
    """Pressed "Take the tour" in the sidebar -- a replay, from step 1.

    Logged as onboarding_started too: it IS a walkthrough starting. The
    details say it was a replay, which is the only difference."""
    previous = current_user.onboarding_state or "never asked"
    return _set_state(STATE_TOURING, "onboarding_started",
                      f"Replayed the tour from the sidebar (was {previous})")


@onboarding_bp.route("/complete", methods=["POST"])
@login_required
def complete():
    """Reached "You're all set!" and pressed Finish."""
    return _set_state(STATE_COMPLETED, "onboarding_completed", "Finished every step of the tour")


@onboarding_bp.route("/skip", methods=["POST"])
@login_required
def skip():
    """Two ways to get here: "No, I've used it before" at the prompt, or
    "Exit tour" part-way through. Both mean "don't show me this again
    unless I ask", so they share a state; the details tell them apart."""
    note = _progress_note()
    details = f"Left the tour {note}" if note else "Said no at the first-time prompt"
    return _set_state(STATE_SKIPPED, "onboarding_skipped", details)


# ---------------------------------------------------------------------
# window.DSS_ONBOARDING, for every signed-in page
# ---------------------------------------------------------------------
@onboarding_bp.app_context_processor
def inject_onboarding():
    """Everything tour.js needs, handed over as one object that
    shared/_onboarding.html writes out with |tojson.

    Built here rather than in the template so the URLs come from
    url_for (a renamed route cannot silently break the tour) and so the
    tests can read exactly what the page will get."""
    if not has_request_context() or not current_user.is_authenticated:
        return {"DSS_ONBOARDING": None}
    return {
        "DSS_ONBOARDING": {
            "state": current_user.onboarding_state,
            "role": current_user.role,
            "userId": current_user.user_id,
            "firstName": (current_user.name or "").split(" ")[0],
            # The steps name pages as plain paths ("/home"). If the app is
            # ever mounted under a prefix, tour.js puts this in front.
            "root": request.script_root or "",
            # True on pages where the prompt must not appear -- see
            # _suppress_on_error_pages below.
            "suppressed": False,
            "urls": {
                "start": url_for("onboarding.start"),
                "skip": url_for("onboarding.skip"),
                "complete": url_for("onboarding.complete"),
                "restart": url_for("onboarding.restart"),
            },
        }
    }


# ---------------------------------------------------------------------
# Not on error pages
# ---------------------------------------------------------------------
# errors/403.html, 404.html and 500.html extend base.html, and for a
# signed-in visitor they render the full dashboard shell -- so without
# this, "Welcome to MarketLens!" would pop up over "Page not found", and
# a tour could try to resume on a page that has none of its targets.
#
# The error handlers in app/__init__.py are not this module's to edit,
# so instead of each of them passing a flag, this listens for any
# template under errors/ (or auth/, belt and braces: those only render
# for signed-out visitors today) and marks the onboarding config as
# suppressed. "Take the tour" in the sidebar still works there -- it
# navigates to the first page of the tour anyway.
SUPPRESSED_TEMPLATE_PREFIXES = ("errors/", "auth/")


def _suppress_on_error_pages(sender, template=None, context=None, **_extra):
    name = getattr(template, "name", None) or ""
    if context is None or not name.startswith(SUPPRESSED_TEMPLATE_PREFIXES):
        return
    config = context.get("DSS_ONBOARDING")
    if config:
        context["DSS_ONBOARDING"] = dict(config, suppressed=True)


@onboarding_bp.record_once
def _connect_signals(state):
    # Scoped to this app (sender=state.app) so a second app built in the
    # same process -- every test builds its own -- gets its own hook.
    before_render_template.connect(_suppress_on_error_pages, state.app)
