"""
tests/test_onboarding.py
--------------------------
The first-time walkthrough: "Is this your first time here?" and the
guided tour behind a yes (app/controllers/onboarding_controller.py,
templates/shared/_onboarding.html, static/js/tour.js, tour_steps.js).

What is worth pinning down here:

  1. The question is asked ONCE per account. It appears while
     onboarding_state is NULL and never after an answer -- and never on
     the sign-in page or an error page.
  2. Each answer is recorded: the right state on the user row, and the
     right action in the audit trail.
  3. The endpoints are sign-in-only and POST-only.
  4. The tour cannot send anyone somewhere broken: every page a step
     names is a real route that step's role is allowed to open.
  5. The assets are on the page exactly once, and the replay button is
     "Start the tutorial" in Settings › Tutorial -- no longer "Take the
     tour" in the sidebar, whose footer now holds Settings and Support.
     Moving the button changed nothing about the first visit: an
     account never asked still gets the prompt by itself (section 1).

The engine's pure logic (step persistence, the missing-element
fallback, card placement) is tested in tests/tour_logic_test.js, which
test_the_javascript_logic_tests_pass below runs when Node is installed.
"""

import json
import os
import re
import shutil
import subprocess

import pytest

from app import create_app
from app.extensions import db
from app.models import AuditLog, SystemSetting, User

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_JS = os.path.join(ROOT, "app", "static", "js")
ENDPOINTS = ("start", "skip", "complete", "restart")


@pytest.fixture
def app():
    app = create_app("testing")

    # One client's sign-in must not leak into another's request: the
    # fixture keeps one app context pushed for the whole test, so
    # flask.g -- where Flask-Login caches the user -- would otherwise
    # survive between requests. Same fixture as
    # test_admin_archive_and_audit.py, for the same reason.
    @app.before_request
    def _forget_cached_login():
        from flask import g

        g.pop("_login_user", None)

    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _user(email="owner@tour.test", role="SME", state=None, name="Sam Sari-Sari"):
    user = User(name=name, email=email, role=role)
    user.set_password("password123")
    user.onboarding_state = state
    db.session.add(user)
    db.session.commit()
    return user


def _login(app, email="owner@tour.test"):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": "password123"})
    return client


def _state(email="owner@tour.test"):
    db.session.expire_all()
    return User.query.filter_by(email=email).one().onboarding_state


def _config(body):
    """The window.DSS_ONBOARDING object the page hands to tour.js."""
    match = re.search(r"window\.DSS_ONBOARDING = (\{.*?\});</script>", body)
    assert match, "window.DSS_ONBOARDING is not on the page"
    return json.loads(match.group(1))


PROMPT = 'id="dssOnboardingPrompt"'


# =====================================================================
# 1. The prompt: once, and only where it belongs
# =====================================================================

@pytest.mark.parametrize("role", ["SME", "LGU", "Admin"])
def test_the_prompt_appears_for_an_account_never_asked(app, role):
    user = _user(role=role)
    body = _login(app).get("/settings").get_data(as_text=True)

    assert PROMPT in body
    assert "Is this your first time here?" in body
    assert "Yes &mdash; show me around" in body
    assert "No, I&rsquo;ve used it before" in body

    config = _config(body)
    assert config["state"] is None
    assert config["role"] == role
    assert config["userId"] == user.user_id
    assert config["suppressed"] is False
    assert config["urls"] == {
        "start": "/onboarding/start",
        "skip": "/onboarding/skip",
        "complete": "/onboarding/complete",
        "restart": "/onboarding/restart",
    }


def test_the_prompt_says_what_marketlens_is_for_that_role(app):
    _user("sme@tour.test", role="SME")
    _user("lgu@tour.test", role="LGU")
    _user("admin@tour.test", role="Admin")
    def prompt_text(email):
        body = _login(app, email).get("/settings").get_data(as_text=True)
        return " ".join(body.split())  # the template wraps the sentence

    assert "the best barangay in Tarlac City for your business" in prompt_text("sme@tour.test")
    assert "so you can plan and guide investors" in prompt_text("lgu@tour.test")
    assert "As an administrator, you keep its accounts" in prompt_text("admin@tour.test")


@pytest.mark.parametrize("state", ["skipped", "completed", "touring"])
def test_the_prompt_is_not_shown_once_answered(app, state):
    _user(state=state)
    body = _login(app).get("/settings").get_data(as_text=True)

    assert PROMPT not in body
    assert "Is this your first time here?" not in body
    # The config is still there: a tour in progress resumes from it, and
    # "Start the tutorial" (Settings › Tutorial) needs it whatever the
    # state.
    assert _config(body)["state"] == state


def test_answering_no_really_stops_the_prompt(app):
    """End to end: asked, answer no, not asked again."""
    _user()
    client = _login(app)
    assert PROMPT in client.get("/settings").get_data(as_text=True)
    client.post("/onboarding/skip")
    assert PROMPT not in client.get("/settings").get_data(as_text=True)
    assert PROMPT not in client.get("/trend-reports").get_data(as_text=True)


def test_nothing_onboarding_on_the_sign_in_page(app):
    body = app.test_client().get("/login").get_data(as_text=True)
    assert PROMPT not in body
    assert "window.DSS_ONBOARDING" not in body
    assert "/static/js/tour.js" not in body
    assert "/static/css/tour.css" not in body


@pytest.mark.parametrize("path, status", [
    ("/no-such-page", 404),
    ("/admin/dashboard", 403),  # an SME opening an admin page
])
def test_the_prompt_never_appears_on_an_error_page(app, path, status):
    _user(role="SME")  # never asked -- the prompt WOULD show on a normal page
    response = _login(app).get(path)
    body = response.get_data(as_text=True)

    assert response.status_code == status
    assert PROMPT not in body
    # Marked suppressed, so tour.js neither prompts nor resumes a tour
    # here -- but the config is still on the page, so a replay button
    # ([data-tour-replay]) would still have what it needs.
    assert _config(body)["suppressed"] is True


# =====================================================================
# 2. The endpoints
# =====================================================================

@pytest.mark.parametrize("endpoint, state, action", [
    ("start", "touring", "onboarding_started"),
    ("skip", "skipped", "onboarding_skipped"),
    ("complete", "completed", "onboarding_completed"),
    ("restart", "touring", "onboarding_started"),
])
def test_each_endpoint_records_the_state_and_the_audit_action(app, endpoint, state, action):
    user = _user()
    response = _login(app).post(f"/onboarding/{endpoint}")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "state": state}
    assert _state() == state

    rows = AuditLog.query.filter_by(action=action).all()
    assert len(rows) == 1
    assert rows[0].user_id == user.user_id
    assert rows[0].target_type == "User"
    assert rows[0].target_id == str(user.user_id)
    assert rows[0].route == f"/onboarding/{endpoint}"


def test_restart_replays_a_finished_or_skipped_tour(app):
    _user(state="completed")
    client = _login(app)
    assert client.post("/onboarding/restart").get_json()["state"] == "touring"
    assert _state() == "touring"
    row = AuditLog.query.filter_by(action="onboarding_started").one()
    assert "Replayed" in row.details and "completed" in row.details


def test_skip_says_whether_they_declined_or_left_part_way(app):
    _user()
    client = _login(app)
    client.post("/onboarding/skip")
    client.post("/onboarding/skip", json={"step": 5, "total": 22})
    client.post("/onboarding/skip", json={"step": "drop table", "total": -3})

    details = [row.details for row in
               AuditLog.query.filter_by(action="onboarding_skipped").order_by(AuditLog.id)]
    assert details[0] == "Said no at the first-time prompt"
    assert details[1] == "Left the tour at step 5 of 22"
    # Client input that isn't a sane step number is dropped, not logged.
    assert details[2] == "Said no at the first-time prompt"


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_the_endpoints_require_a_sign_in(app, endpoint):
    response = app.test_client().post(f"/onboarding/{endpoint}")
    assert response.status_code in (302, 401)
    if response.status_code == 302:
        assert "/login" in response.headers["Location"]
    assert AuditLog.query.filter(AuditLog.action.like("onboarding_%")).count() == 0


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_the_endpoints_are_post_only(app, endpoint):
    _user()
    client = _login(app)
    assert client.get(f"/onboarding/{endpoint}").status_code == 405
    assert _state() is None, "a GET must not change anything"


def test_the_endpoints_take_the_csrf_token_the_way_tour_js_sends_it(app):
    """The testing config switches CSRF off, so switch it back on (after
    signing in) and check both halves: a forged POST is refused, and the
    token from base.html's <meta name="csrf-token">, sent as X-CSRFToken
    -- exactly what tour.js's post() does -- is accepted."""
    _user()
    client = _login(app)
    app.config["WTF_CSRF_ENABLED"] = True

    assert client.post("/onboarding/start").status_code == 400
    assert _state() is None

    page = client.get("/settings").get_data(as_text=True)
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page).group(1)
    response = client.post("/onboarding/start", json={}, headers={"X-CSRFToken": token})
    assert response.status_code == 200
    assert _state() == "touring"


# =====================================================================
# 3. The page wiring
# =====================================================================

def _asset_counts(body):
    return {
        "css": body.count("/static/css/tour.css"),
        "steps": body.count("/static/js/tour_steps.js"),
        "engine": body.count("/static/js/tour.js"),
        "config": body.count("window.DSS_ONBOARDING ="),
    }


@pytest.mark.parametrize("state", [None, "completed"])
def test_base_includes_the_tour_assets_exactly_once(app, state):
    _user(state=state)
    body = _login(app).get("/settings").get_data(as_text=True)
    assert _asset_counts(body) == {"css": 1, "steps": 1, "engine": 1, "config": 1}

    # Order matters: the steps must exist before the engine reads them,
    # and the engine runs after the page's own scripts (main.js et al).
    main = body.index("/static/js/main.js")
    steps = body.index("/static/js/tour_steps.js")
    engine = body.index("/static/js/tour.js")
    assert main < steps < engine
    assert body.index("window.DSS_ONBOARDING =") < engine
    assert body.index("/static/css/tour.css") < body.index("</head>")


def _sidebar_footer(body):
    return body[body.index('class="dss-sidebar-footer"'):body.index("</aside>")]


@pytest.mark.parametrize("role", ["SME", "LGU", "Admin"])
def test_the_replay_button_is_in_settings_tutorial_not_the_sidebar(app, role):
    """"Take the tour" moved from the sidebar footer to Settings ›
    Tutorial. The footer now holds Settings, then Support."""
    _user(role=role, state="completed")
    body = _login(app).get("/settings?section=tutorial").get_data(as_text=True)

    footer = _sidebar_footer(body)
    assert "data-tour-replay" not in footer
    assert "Take the tour" not in body
    assert 'data-tour="nav-settings"' in footer
    assert footer.index('data-tour="nav-settings"') < footer.index("#supportModal"), \
        "Settings sits above Support"

    pane = body[body.index('id="section-tutorial"'):]
    pane = pane[:pane.index("</section>")]
    assert "data-tour-replay" in pane
    assert "Start the tutorial" in pane
    # Exactly one replay button on the page -- the Tutorial pane's.
    assert len(re.findall(r"<[a-z]+\b[^>]*\bdata-tour-replay\b[^>]*>", body)) == 1


def test_moving_the_button_left_the_first_visit_alone(app):
    """An account never asked is still asked, on whatever signed-in page
    it lands on first, without going anywhere near Settings."""
    _user(email="fresh@tour.test")
    body = _login(app, "fresh@tour.test").get("/trend-reports").get_data(as_text=True)
    assert PROMPT in body
    assert _config(body)["state"] is None
    assert "data-tour-replay" not in body, "the replay button belongs to Settings only"


@pytest.mark.parametrize("role, expected", [
    # No Community entry: the community is the Discord server, opened
    # from the footer (see tests/test_community_discord.py). No Settings
    # entry either: it is in the sidebar FOOTER now (checked below).
    ("SME", ["nav-home", "nav-saturation-map", "nav-trend-reports", "nav-recommendations"]),
    ("LGU", ["nav-lgu-dashboard", "nav-saturation-map", "nav-trend-reports",
             "nav-data-upload"]),
    ("Admin", ["nav-admin-dashboard", "nav-users", "nav-audit", "nav-datasets",
               "nav-system-settings"]),
])
def test_the_sidebar_links_carry_tour_anchors(app, role, expected):
    _user(role=role)
    body = _login(app).get("/settings").get_data(as_text=True)
    nav = body[body.index('class="dss-nav"'):body.index("dss-sidebar-footer")]
    for name in expected:
        assert f'data-tour="{name}"' in nav, name
    assert 'data-tour="nav-settings"' not in nav
    assert 'data-tour="nav-settings"' in _sidebar_footer(body)


def test_the_tour_needs_no_cdn(app):
    """Vanilla JS and CSS, no libraries: nothing in the tour's own files
    fetches from anywhere else."""
    for name in ("js/tour.js", "js/tour_steps.js", "css/tour.css"):
        with open(os.path.join(ROOT, "app", "static", name), encoding="utf-8") as handle:
            source = handle.read()
        assert "://" not in source, f"{name} references an external URL"


# =====================================================================
# 4. The step lists
# =====================================================================

def _read_steps_source():
    with open(os.path.join(STATIC_JS, "tour_steps.js"), encoding="utf-8") as handle:
        return handle.read()


def _array_block(source, const_name):
    """The text of `const NAME = [ ... ];`, bracket-matched while skipping
    strings and comments -- enough to read the step list without Node."""
    start = source.index(f"const {const_name} = [") + len(f"const {const_name} = ")
    depth = 0
    i = start
    quote = None
    while i < len(source):
        ch = source[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif source.startswith("//", i):
            i = source.index("\n", i)
            continue
        elif source.startswith("/*", i):
            i = source.index("*/", i) + 2
            continue
        elif ch in "\"'`":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1
    raise AssertionError(f"unterminated {const_name} array")


def _steps(const_name):
    block = _array_block(_read_steps_source(), const_name)
    ids = re.findall(r'^\s{6}id: "([^"]+)"', block, re.M)
    pages = re.findall(r'^\s{6}page: "([^"]+)"', block, re.M)
    assert len(ids) == len(pages), f"{const_name}: every step must name its page"
    return list(zip(ids, pages))


def test_tour_steps_defines_a_tour_for_each_role():
    source = _read_steps_source()
    # Keyed exactly as User.role is stored, since that is what tour.js
    # looks the list up by.
    assert "const STEPS = { SME: SME, LGU: LGU, Admin: ADMIN };" in source
    sme, lgu, admin = _steps("SME"), _steps("LGU"), _steps("ADMIN")
    assert len(sme) >= 14, "the SME walkthrough should be thorough"
    assert len(lgu) >= 8
    assert len(admin) >= 8
    for steps in (sme, lgu, admin):
        ids = [step_id for step_id, _ in steps]
        assert len(ids) == len(set(ids)), "step ids must be unique"


def _route_for(app, path):
    """(endpoint) for a GET of `path`, failing on a 404, a redirect (e.g.
    a missing trailing slash) or a POST-only route."""
    adapter = app.url_map.bind("localhost")
    endpoint, _args = adapter.match(path, method="GET")
    return endpoint


@pytest.mark.parametrize("const_name, role", [("SME", "SME"), ("LGU", "LGU"), ("ADMIN", "Admin")])
def test_every_step_page_is_a_real_route_that_role_can_open(app, const_name, role):
    _user(role=role, state="touring")
    client = _login(app)
    for page in sorted({page for _id, page in _steps(const_name)}):
        _route_for(app, page)  # raises on anything that is not a plain GET route
        response = client.get(page)
        assert response.status_code == 200, f"{role} tour sends them to {page}, which returns {response.status_code}"


def test_every_sme_step_page_is_a_real_route(app):
    """The requirement as stated, on its own: no SME step names a page
    that does not exist."""
    for step_id, page in _steps("SME"):
        assert _route_for(app, page), step_id


# =====================================================================
# 5. The JavaScript
# =====================================================================

@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_the_javascript_logic_tests_pass():
    result = subprocess.run(
        ["node", os.path.join(ROOT, "tests", "tour_logic_test.js")],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
