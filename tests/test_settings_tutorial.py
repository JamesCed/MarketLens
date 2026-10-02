"""
tests/test_settings_tutorial.py
---------------------------------
Settings moved to the sidebar footer, "Take the tour" became Settings ›
Tutorial, and Business Preferences left Settings for the Home page.
What has to hold:

  1. The sidebar. Settings is a link in the FOOTER, directly above
     Support, styled like it -- and no longer in the list of pages.
     "Take the tour" is gone from the sidebar.
  2. The Tutorial pane, for EVERY role: a tab in the rail, a pane with
     the [data-tour-replay] button static/js/tour.js binds, and the
     account's walkthrough state in words rather than the raw column
     value. ?section=tutorial opens it.
  3. Old links. ?section=plans sends an SME to Home (where the plans
     are now); any other role falls back to Profile.
  4. No "Business Preferences" anywhere an SME can see it: Settings,
     the top bar's user menu, the sidebar, the Support dialog.
  5. The first visit is untouched. A brand-new account still has
     onboarding_state NULL, so window.DSS_ONBOARDING says state null and
     the "Is this your first time here?" prompt is on its first page.

The tour's own step list is checked from Python too (Node is not
always installed to run tests/tour_logic_test.js): the Settings steps
must aim at elements the Settings page really renders.
"""

import json
import os
import re

import pytest

from app import create_app
from app.controllers.onboarding_controller import describe_walkthrough
from app.extensions import db
from app.models import SystemSetting, User

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROLES = ["SME", "LGU", "Admin"]
PROMPT = 'id="dssOnboardingPrompt"'


@pytest.fixture
def app():
    app = create_app("testing")

    # One client's sign-in must not leak into another's request (the
    # app context stays pushed for the whole test, and Flask-Login
    # caches the user on flask.g) -- same guard as test_onboarding.py.
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


def _user(role="SME", email=None, state=None):
    email = email or f"{role.lower()}@tutorial.test"
    user = User(name=f"Test {role}", email=email, role=role)
    user.set_password("password123")
    user.onboarding_state = state
    db.session.add(user)
    db.session.commit()
    return user


def _login(app, email):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": "password123"})
    return client


def _page(app, role="SME", path="/settings", state="completed"):
    """A page as `role` sees it. 'completed' by default, so the
    first-time prompt is not in the way of what is being checked."""
    user = _user(role=role, state=state)
    return _login(app, user.email).get(path).get_data(as_text=True)


def _between(body, start_marker, end_marker):
    start = body.index(start_marker)
    return body[start:body.index(end_marker, start)]


def _sidebar(body):
    return _between(body, 'class="dss-sidebar"', "</aside>")


def _nav_list(body):
    return _between(body, 'class="dss-nav"', 'class="dss-sidebar-footer"')


def _footer(body):
    return _between(body, 'class="dss-sidebar-footer"', "</aside>")


def _topbar(body):
    return _between(body, '<header class="dss-topbar"', "</header>")


def _tutorial_pane(body):
    pane = body[body.index('id="section-tutorial"'):]
    return pane[:pane.index("</section>")]


def _active_pane(body):
    for match in re.finditer(r'<section class="dss-settings-pane([^"]*)"\s+id="section-([a-z]+)"', body):
        if "active" in match.group(1):
            return match.group(2)
    return None


def _config(body):
    match = re.search(r"window\.DSS_ONBOARDING = (\{.*?\});</script>", body)
    assert match, "window.DSS_ONBOARDING is not on the page"
    return json.loads(match.group(1))


# =====================================================================
# 1. The sidebar
# =====================================================================

@pytest.mark.parametrize("role", ROLES)
def test_settings_is_in_the_sidebar_footer_directly_above_support(app, role):
    body = _page(app, role, path="/trend-reports")
    footer = _footer(body)

    link = re.search(r'<a [^>]*data-tour="nav-settings"[^>]*>(.*?)</a>', footer, re.S)
    assert link, "no Settings link in the sidebar footer"
    assert 'href="/settings"' in link.group(0)
    assert "dss-nav-link" in link.group(0) and "dss-nav-support" in link.group(0), \
        "Settings should be styled exactly like Support beside it"
    assert "bi-gear" in link.group(1) and "Settings" in link.group(1)

    # Settings first, then Support -- and nothing between them.
    support = footer.index('data-bs-target="#supportModal"')
    assert footer.index('data-tour="nav-settings"') < support
    between = footer[link.end():support]
    assert "<a " not in between and "data-tour=" not in between


@pytest.mark.parametrize("role", ROLES)
def test_settings_is_no_longer_in_the_list_of_pages(app, role):
    body = _page(app, role, path="/trend-reports")
    nav = _nav_list(body)
    assert 'href="/settings"' not in nav
    assert 'data-tour="nav-settings"' not in nav
    # One Settings link in the whole sidebar: moved, not duplicated.
    assert _sidebar(body).count('href="/settings"') == 1


def test_the_footer_settings_link_is_highlighted_on_settings_only(app):
    _user(role="SME", state="completed")
    client = _login(app, "sme@tutorial.test")

    on_settings = re.search(r'<a [^>]*data-tour="nav-settings"[^>]*>',
                            _footer(client.get("/settings").get_data(as_text=True))).group(0)
    assert re.search(r'class="[^"]*\bactive\b', on_settings)
    assert 'aria-current="page"' in on_settings

    elsewhere = re.search(r'<a [^>]*data-tour="nav-settings"[^>]*>',
                          _footer(client.get("/trend-reports").get_data(as_text=True))).group(0)
    assert not re.search(r'class="[^"]*\bactive\b', elsewhere)
    assert "aria-current" not in elsewhere


@pytest.mark.parametrize("role", ROLES)
def test_take_the_tour_is_gone_from_the_sidebar(app, role):
    sidebar = _sidebar(_page(app, role))
    assert "Take the tour" not in sidebar
    assert "data-tour-replay" not in sidebar
    assert "dss-nav-tour" not in sidebar
    assert 'data-tour="nav-tour"' not in sidebar


def test_no_page_outside_settings_carries_a_replay_button(app):
    _user(role="SME", state="completed")
    client = _login(app, "sme@tutorial.test")
    for path in ("/trend-reports", "/saturation-map", "/recommendations"):
        body = client.get(path).get_data(as_text=True)
        assert "data-tour-replay" not in body, path
        assert "Take the tour" not in body, path


def test_the_support_dialog_describes_home_plans_and_the_tutorial(app):
    support = _page(app, "SME", path="/trend-reports")
    support = support[support.index('id="supportModal"'):]
    support = " ".join(support[:support.index("</dl>")].split())
    assert "Business Preferences" not in support
    assert "Trash" in support and "edits a plan" in support
    assert "Tutorial" in support


# =====================================================================
# 2. The Tutorial pane, for every role
# =====================================================================

@pytest.mark.parametrize("role", ROLES)
def test_the_tutorial_tab_and_pane_render_for_every_role(app, role):
    body = _page(app, role)

    tab = re.search(r'<button [^>]*class="dss-settings-tab[^"]*"[^>]*data-section="tutorial"[^>]*>(.*?)</button>',
                    body, re.S)
    assert tab, f"{role}: no Tutorial tab in the Settings rail"
    assert "Tutorial" in tab.group(1) and "bi-signpost-2" in tab.group(1)
    assert 'aria-controls="section-tutorial"' in tab.group(0)

    pane = _tutorial_pane(body)
    button = re.search(r"<button [^>]*\bdata-tour-replay\b[^>]*>(.*?)</button>", pane, re.S)
    assert button, f"{role}: the Tutorial pane has no [data-tour-replay] button"
    assert 'type="button"' in button.group(0)
    assert "Start the tutorial" in button.group(1)
    assert "bi-play-circle" in button.group(1)

    words = " ".join(pane.split())
    assert "automatically the first time they sign in" in words


@pytest.mark.parametrize("role", ROLES)
def test_section_tutorial_opens_the_tutorial_pane(app, role):
    body = _page(app, role, path="/settings?section=tutorial")
    assert _active_pane(body) == "tutorial"
    tab = re.search(r'<button [^>]*data-section="tutorial"[^>]*>', body).group(0)
    assert re.search(r'class="dss-settings-tab[^"]*\bactive\b', tab)


@pytest.mark.parametrize("state, label", [
    (None, "Never started"),
    ("touring", "In progress"),
    ("completed", "Completed"),
    ("skipped", "Skipped"),
])
def test_the_pane_says_where_the_walkthrough_stands_in_words(app, state, label):
    pane = _tutorial_pane(_page(app, "SME", path="/settings?section=tutorial", state=state))
    marker = re.search(r'data-walkthrough-state="([a-z]+)"', pane)
    assert marker and marker.group(1) == (state or "never")
    assert label in pane
    # The raw column value is never what the person reads: 'touring',
    # the one that is not already a plain word, is shown as "In progress".
    if state == "touring":
        assert "touring" not in re.sub(r"<[^>]+>", " ", pane)


def test_an_unknown_walkthrough_state_reads_as_never_started():
    """A hand-edited row must not break Settings."""
    described = describe_walkthrough("something-odd")
    assert described["state"] is None
    assert described["label"] == "Never started"
    assert describe_walkthrough("")["label"] == "Never started"
    assert describe_walkthrough("completed")["label"] == "Completed"


def test_the_replay_endpoint_logs_where_the_replay_came_from(app):
    from app.models import AuditLog

    user = _user(role="LGU", state="skipped")
    response = _login(app, user.email).post("/onboarding/restart")
    assert response.get_json() == {"ok": True, "state": "touring"}
    row = AuditLog.query.filter_by(action="onboarding_started").one()
    assert row.details == "Replayed the tour from Settings › Tutorial (was skipped)"


# =====================================================================
# 3. Old ?section=plans links
# =====================================================================

@pytest.mark.parametrize("query", ["plans", "PLANS", " plans "])
def test_section_plans_redirects_an_sme_to_home(app, query):
    user = _user(role="SME", state="completed")
    response = _login(app, user.email).get("/settings", query_string={"section": query})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/home")


@pytest.mark.parametrize("role", ["LGU", "Admin"])
def test_section_plans_falls_back_to_profile_for_other_roles(app, role):
    user = _user(role=role, state="completed")
    response = _login(app, user.email).get("/settings?section=plans")
    assert response.status_code == 200
    assert _active_pane(response.get_data(as_text=True)) == "profile"


def test_the_settings_sections_are_the_same_for_every_role():
    from app.controllers.profile_controller import SETTINGS_SECTIONS, SME_ONLY_SECTIONS

    assert SETTINGS_SECTIONS == ("profile", "notifications", "appearance", "security", "tutorial")
    assert "plans" not in SETTINGS_SECTIONS
    assert SME_ONLY_SECTIONS == ()


# =====================================================================
# 4. No "Business Preferences" anywhere an SME can see it
# =====================================================================

@pytest.mark.parametrize("path", ["/settings", "/trend-reports"])
def test_no_business_preferences_for_an_sme(app, path):
    body = _page(app, "SME", path=path)
    for where, text in (("page", body), ("top bar", _topbar(body)), ("sidebar", _sidebar(body))):
        assert "Business Preferences" not in text, f"still in the {where} of {path}"
        assert "section=plans" not in text, f"a link to the old pane is still in the {where} of {path}"
    assert 'id="section-plans"' not in body


def test_the_user_menu_keeps_its_other_shortcuts(app):
    menu = _topbar(_page(app, "SME", path="/trend-reports"))
    assert 'href="/settings?section=profile"' in menu
    assert 'href="/settings?section=appearance"' in menu
    assert 'href="/settings"' in menu


# =====================================================================
# 5. The first visit is untouched
# =====================================================================

def test_a_new_account_is_never_asked_yet():
    assert User.__table__.c.onboarding_state.nullable is True
    assert User.__table__.c.onboarding_state.default is None
    assert User(name="New", email="new@tutorial.test", role="SME").onboarding_state is None


@pytest.mark.parametrize("signup", [
    {"role": "lgu"},
    {"role": "sme", "business_name": "Kape Tarlac", "industry_type": "Food and Beverage",
     "location": "Poblacion", "business_stage": "startup", "capital": "250000"},
])
def test_a_brand_new_account_gets_the_first_time_prompt(app, signup):
    """Sign up for real, then sign in: the first signed-in page carries
    window.DSS_ONBOARDING with state null and the prompt -- whatever the
    Settings page now holds."""
    email = f"brand-new-{signup['role']}@tutorial.test"
    data = {"full_name": "Brand New", "email": email,
            "password": "password123", "confirm_password": "password123", **signup}
    app.test_client().post("/register", data=data, follow_redirects=True)

    user = User.query.filter_by(email=email).one()
    assert user.onboarding_state is None

    client = _login(app, email)
    for path in ("/trend-reports", "/settings"):
        body = client.get(path).get_data(as_text=True)
        config = _config(body)
        assert config["state"] is None, path
        assert config["suppressed"] is False, path
        assert PROMPT in body, path
        assert "Is this your first time here?" in body, path


def test_tour_js_still_prompts_a_never_asked_account_on_its_own():
    """The auto-start lives in tour.js boot(): a null state shows the
    prompt; 'touring' resumes. Moving the replay button must not have
    touched either branch."""
    with open(os.path.join(ROOT, "app", "static", "js", "tour.js"), encoding="utf-8") as handle:
        source = handle.read()
    boot = source[source.index("function boot()"):source.index("function whenReady(")]
    assert 'if (config.state == null || config.state === "") setupPrompt(config, tour);' in boot
    assert 'else if (config.state === "touring") tour.resume();' in boot
    assert 'document.querySelectorAll("[data-tour-replay]")' in boot
    assert "Settings › Tutorial" in source and "Take the tour</strong> in the sidebar" not in source


# =====================================================================
# The tour's Settings steps aim at what the Settings page renders
# =====================================================================

def _steps_source():
    with open(os.path.join(ROOT, "app", "static", "js", "tour_steps.js"), encoding="utf-8") as handle:
        return handle.read()


def test_the_tour_no_longer_sends_anyone_to_business_preferences():
    source = _steps_source()
    assert "Business Preferences" not in source
    assert "section-plans" not in source
    assert 'data-section="plans"' not in source
    assert '"sme-settings-plans"' not in source
    # The end of every tour says how to replay it -- from Tutorial.
    assert re.search(r"const REPLAY_TIP =[^;]*Tutorial", source, re.S)
    assert source.count("how: REPLAY_TIP") == 3


@pytest.mark.parametrize("role", ROLES)
def test_the_settings_steps_targets_exist_on_the_settings_page(app, role):
    body = _page(app, role)
    assert 'id="settingsNav"' in body
    rail = body[body.index('id="settingsNav"'):]
    rail = rail[:rail.index("</nav>")]
    # sme-settings waits for a click on '#settingsNav [data-section="tutorial"]'.
    assert 'data-section="tutorial"' in rail
    assert '\'#settingsNav [data-section="tutorial"]\'' in _steps_source()
    # The menu steps light up the whole sidebar, footer included, because
    # they point people to Settings "at the bottom of the menu".
    assert 'id="dssSidebar"' in body
