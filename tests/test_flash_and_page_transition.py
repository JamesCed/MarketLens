"""
tests/test_flash_and_page_transition.py
------------------------------------------
The top-centre flash toasts and the page-entry animation.

WHY THIS FILE EXISTS AT ALL

Adding the page-entry animation broke two unrelated things, twice, for
the same reason, and both were invisible in the CSS itself:

  An element with a `transform` becomes the CONTAINING BLOCK for its
  `position: fixed` descendants. They stop measuring themselves against
  the viewport and start measuring themselves against it.

First the toast. It was rendered inside .dss-main, .dss-main got the
animation, and the toast -- `position: fixed; top: 0; left: 50%` --
landed 1190px across and 272px down instead of centred at the top.

Then the landing backdrop, .ml-bg, which is fixed and full-screen and
lives inside .ml-landing. Animating .ml-landing stretched it to the full
2,681px scroll height of the page and made it scroll away with the
content.

And it would have been the modals next: `both` as a fill mode leaves
`transform: translateY(0)` applied forever, and translateY(0) is still a
transform -- only `none` is not. The avatar picker in Settings and
Create Account in admin/users are authored inside {% block content %},
so they render inside .dss-main.

These tests read the stylesheet and the templates as text. That cannot
measure a layout -- the real proof was a browser, where .ml-bg comes
back as [0,0,1280,800] before and after scrolling 1,500px, and
.dss-main's computed transform comes back `none`. What text CAN do is
guard the three specific decisions that keep it that way, so the next
person to touch this animation finds out from a test rather than from a
screenshot of a toast in the wrong corner.
"""

import os
import re

import pytest

from app import create_app
from app.extensions import db
from app.models import SystemSetting
from app.models.user import User

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def _strip_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _rule_declaring(css, animation_name):
    """The selector list and declaration block of the rule that starts
    `animation_name`, with comments removed so a mention in prose cannot
    be mistaken for a declaration."""
    body = _strip_comments(css)
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", body):
        selectors, declarations = match.group(1), match.group(2)
        if re.search(rf"animation\s*:[^;]*\b{animation_name}\b", declarations):
            return selectors.strip(), declarations.strip()
    return None, None


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


# ---------------------------------------------------------------------
# 1. The toast is not inside anything that can be transformed
# ---------------------------------------------------------------------

def test_the_flash_include_is_a_direct_child_of_body():
    """Not tidiness. Inside .dss-main the toast is positioned against
    .dss-main; out here it is positioned against the viewport, and no
    animation added to page content later can move it again."""
    base = _read("app", "templates", "base.html")

    include = base.index('include "shared/_flash.html"')
    main_open = base.index("<main")
    content_open = base.index('<div class="dss-content"')

    assert include < content_open < main_open, (
        "the flash include has moved back inside the page content; a "
        "position: fixed toast in there is positioned against whichever "
        "ancestor carries a transform, not against the screen"
    )


def test_the_toast_wrapper_is_pinned_to_the_top_centre_with_no_offset():
    css = _strip_comments(_read("app", "static", "css", "style.css"))
    wrap = re.search(r"\.dss-flash-wrap\s*\{([^}]*)\}", css)
    assert wrap, ".dss-flash-wrap rule is gone"
    declarations = wrap.group(1)

    assert re.search(r"position\s*:\s*fixed", declarations)
    assert re.search(r"top\s*:\s*0", declarations), "asked for flush to the top edge"
    assert re.search(r"left\s*:\s*50%", declarations), "asked for top CENTRE"
    assert re.search(r"margin\s*:\s*0", declarations), "asked for 0 margin"
    assert re.search(r"padding\s*:\s*0", declarations), "asked for 0 padding"
    # The wrapper is wider than the toast inside it, so without this the
    # transparent strip either side swallows clicks aimed at the page.
    assert re.search(r"pointer-events\s*:\s*none", declarations)


# ---------------------------------------------------------------------
# 2. The page animation leaves no transform behind
# ---------------------------------------------------------------------

def test_the_page_transition_does_not_keep_a_transform_after_it_plays():
    """`both` or `forwards` would hold translateY(0) on .dss-main for the
    life of the page, and translateY(0) is still a transform. Every
    modal authored inside {% block content %} would then be sized and
    centred against .dss-main instead of the screen."""
    css = _read("app", "static", "css", "style.css")
    selectors, declarations = _rule_declaring(css, "dss-page-in")

    assert declarations, "the dss-page-in animation declaration is gone"
    assert not re.search(r"\b(both|forwards)\b", declarations), (
        f"page-in declares a persisting fill mode ({declarations!r}); the "
        f"transform then outlives the animation and becomes the containing "
        f"block for every fixed element inside it"
    )


def test_the_landing_backdrop_is_left_out_of_the_animated_box():
    """.ml-bg is fixed and full-screen and lives inside .ml-landing.
    Animating the parent stretched it to the page's full scroll height
    and made it scroll away."""
    css = _read("app", "static", "css", "style.css")
    selectors, _ = _rule_declaring(css, "dss-page-in")

    assert selectors, "the dss-page-in animation declaration is gone"
    assert re.search(r"\.ml-landing\s*>", selectors), (
        f"the landing animates itself again ({selectors!r}); animate its "
        f"children instead so .ml-bg keeps the viewport as its containing block"
    )
    assert "ml-bg" in selectors, (
        f".ml-bg is no longer excluded from the page animation ({selectors!r})"
    )


def test_the_fixed_landing_backdrop_is_still_a_child_of_the_landing():
    """The test above is only meaningful while this is true. If .ml-bg is
    ever moved out to <body>, the exclusion becomes dead weight and this
    says so rather than letting it rot."""
    for template in ("login.html", "register.html", "verify_email.html"):
        markup = _read("app", "templates", "auth", template)
        # verify_email carries `class="ml-landing ml-landing-short"`, so
        # match the class rather than the whole attribute.
        shell = re.search(r'class="ml-landing[ "]', markup)
        backdrop = re.search(r'class="ml-bg[ "]', markup)
        assert shell, f"{template} no longer opens with the landing shell"
        assert backdrop, f"{template} lost its backdrop"
        assert shell.start() < backdrop.start(), (
            f"{template} moved .ml-bg out of .ml-landing; the exclusion in the "
            f"page-in selector is now doing nothing"
        )


# ---------------------------------------------------------------------
# 3. Colour carries the meaning the user asked for
# ---------------------------------------------------------------------

def test_a_failed_login_is_flashed_as_an_error(app):
    """Red for incorrect or invalid. The tone comes from the category the
    controller passes, so this checks the controller, and the mapping
    below checks the template."""
    client = app.test_client()
    user = User(name="Juan", email="juan@example.com", role="SME")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()

    page = client.post(
        "/login",
        data={"email": "juan@example.com", "password": "wrong-password"},
        follow_redirects=True,
    ).get_data(as_text=True)

    assert "dss-flash-error" in page, "a rejected login must read as an error"
    assert "dss-flash-success" not in page


def test_signing_in_is_flashed_as_a_success(app):
    """Green for a successful login."""
    client = app.test_client()
    user = User(name="Juan", email="juan@example.com", role="SME")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()

    page = client.post(
        "/login",
        data={"email": "juan@example.com", "password": "password123"},
        follow_redirects=True,
    ).get_data(as_text=True)

    assert "dss-flash-success" in page
    assert "dss-flash-error" not in page


def test_every_tone_gets_its_own_gradient_built_from_the_project_palette(app):
    """The gradients had to be added WITHOUT introducing a second palette
    -- the base stop of each one is a variable this project already
    defines, so changing the palette still changes the toasts."""
    css = _strip_comments(_read("app", "static", "css", "style.css"))

    expected = {
        "dss-flash-success": "--dss-green",
        "dss-flash-error": "--dss-deep-red",
        "dss-flash-warn": "--dss-orange",
        "dss-flash-info": "--dss-blue",
    }
    for tone, variable in expected.items():
        rule = re.search(rf"\.{tone}\s*\{{([^}}]*)\}}", css)
        assert rule, f".{tone} has no rule"
        declarations = rule.group(1)
        assert "linear-gradient" in declarations, f".{tone} is not a gradient"
        assert variable in declarations, (
            f".{tone} no longer references {variable}; the toasts have drifted "
            f"away from the prototype's colours into a palette of their own"
        )


def test_an_error_toast_is_not_put_on_a_countdown(app):
    """A confirmation you missed costs nothing. A "that password was
    wrong" that vanished before you looked up costs you the reason the
    login failed. main.js removes the bar and the timer for errors, so
    the class it keys on has to stay on the element."""
    template = _read("app", "templates", "shared", "_flash.html")
    javascript = _read("app", "static", "js", "main.js")

    assert "dss-flash-{{ tone }}" in template
    assert "dss-flash-error" in javascript, (
        "the dismiss script no longer recognises an error toast, so errors "
        "will auto-dismiss with everything else"
    )
