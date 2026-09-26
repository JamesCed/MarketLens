"""
tests/test_flash_and_page_transition.py
------------------------------------------
The top-centre flash toasts, and the absence of any page transition.

WHY THIS FILE EXISTS AT ALL

There was briefly a rise-and-fade played on the page containers --
.dss-main and the landing shell -- on every navigation. It has been
removed at the user's request, and these tests keep it removed, because
it broke three unrelated things for one reason that is invisible in the
CSS itself:

  An element with a `transform` becomes the CONTAINING BLOCK for its
  `position: fixed` descendants. They stop measuring themselves against
  the viewport and start measuring themselves against it.

First the toast. It was rendered inside .dss-main, .dss-main got the
animation, and the toast -- `position: fixed; top: 0; left: 50%` --
landed 1190px across and 272px down instead of centred at the top.

Then the landing backdrop, .ml-bg, which is fixed and full-screen and
lives inside .ml-landing. Animating .ml-landing stretched it to the
page's full 2,681px scroll height and made it scroll away with the
content.

And the modals were next, though that one never shipped: `both` as a
fill mode leaves `transform: translateY(0)` applied forever, and
translateY(0) is still a transform -- only `none` is not. The avatar
picker in Settings and Create Account in admin/users are authored
inside {% block content %}, so they render inside .dss-main.

So the rule these tests enforce is narrow and specific: the toast may
animate, because it is a fixed element in its own right with nothing
fixed inside it, but page CONTAINERS may not. Animate a thing, not a
container.

These tests read the stylesheet and the templates as text, which cannot
measure a layout -- the real proof was a browser, where .ml-bg comes
back as [0,0,1280,800] both at rest and after scrolling 1,500px, and
.dss-main's computed transform comes back `none`. What text CAN do is
fail the moment someone reintroduces the shape of the bug, so they find
out here rather than from a screenshot of a toast in the wrong corner.
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
# 2. Nothing animates the page containers
# ---------------------------------------------------------------------

# The two shells every page is built out of: .dss-main wraps
# {% block content %} for a signed-in user, .ml-landing is the whole
# public page. Both hold fixed descendants.
PAGE_CONTAINERS = (".dss-main", ".ml-landing")


def _rules_mentioning(css, needles):
    """(selectors, declarations) for every rule whose selector list
    mentions one of `needles`. Comments are stripped first so the long
    note explaining why this is forbidden is not itself read as code."""
    body = _strip_comments(css)
    found = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", body):
        selectors, declarations = match.group(1).strip(), match.group(2)
        if any(needle in selectors for needle in needles):
            found.append((selectors, " ".join(declarations.split())))
    return found


def test_no_page_container_is_animated_or_transformed():
    """The page transition is gone and stays gone.

    Deliberately broad: it catches the container itself
    (`.dss-main { animation: ... }`), which broke the toast, AND the
    children workaround (`.ml-landing > :not(.ml-bg)`), which was only
    ever a way to keep an animation the user has since asked to remove.
    A transform counts as much as an animation -- a static
    `transform: translateZ(0)` for "GPU acceleration" creates exactly
    the same containing block, silently, with nothing moving to hint at
    it.
    """
    css = _read("app", "static", "css", "style.css")

    offenders = [
        (selectors, declarations)
        for selectors, declarations in _rules_mentioning(css, PAGE_CONTAINERS)
        if re.search(r"(^|[;{\s])(animation|transform)\s*:", declarations)
    ]

    assert not offenders, (
        "a page container is being animated or transformed again:\n  "
        + "\n  ".join(f"{s} {{ {d} }}" for s, d in offenders)
        + "\n\nA transform on either of these makes it the containing block "
        "for every position:fixed element inside it -- the flash toast, the "
        "landing backdrop, and the modals authored in {% block content %}."
    )


def test_no_page_entry_keyframes_are_left_lying_around():
    """Not pedantry: dead @keyframes are exactly what someone reaches
    for when re-adding the effect, and a rule that merely LOOKS unused
    is the easiest thing in a 63 KB stylesheet to wire back up by
    accident."""
    css = _strip_comments(_read("app", "static", "css", "style.css"))
    assert "dss-page-in" not in css, (
        "the page-entry keyframes are back in the stylesheet"
    )


def test_the_fixed_landing_backdrop_is_still_a_child_of_the_landing():
    """Why .ml-landing must stay transform-free, stated as a test. The
    backdrop is fixed and full-screen and lives inside the landing
    shell; if it is ever lifted out to <body>, the constraint above
    stops applying to .ml-landing and this test is the place that
    should fail and say so."""
    for template in ("login.html", "register.html", "verify_email.html"):
        markup = _read("app", "templates", "auth", template)
        # verify_email carries `class="ml-landing ml-landing-short"`, so
        # match the class rather than the whole attribute.
        shell = re.search(r'class="ml-landing[ "]', markup)
        backdrop = re.search(r'class="ml-bg[ "]', markup)
        assert shell, f"{template} no longer opens with the landing shell"
        assert backdrop, f"{template} lost its backdrop"
        assert shell.start() < backdrop.start(), (
            f"{template} moved .ml-bg out of .ml-landing"
        )


def test_the_toast_itself_still_animates():
    """The point of removing the page transition was the page, not the
    toast. The toast is a fixed element in its own right with nothing
    fixed inside it, so its entry animation cannot do what the page
    animation did -- and the user asked to keep it."""
    css = _strip_comments(_read("app", "static", "css", "style.css"))

    rule = re.search(r"\.dss-flash\s*\{([^}]*)\}", css)
    assert rule, ".dss-flash rule is gone"
    assert re.search(r"animation\s*:[^;]*dss-flash-in", rule.group(1)), (
        "the toast lost its entry animation along with the page transition"
    )
    for name in ("dss-flash-in", "dss-flash-out", "dss-flash-drain"):
        assert re.search(rf"@keyframes\s+{name}\b", css), f"@keyframes {name} is gone"


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
