"""
tests/test_contact_us.py
---------------------------
The Contact Us dialog, on every role's dashboard.

WHAT IT WAS. A plain <span> reading "Contact Us" in the footer of the
SME Home page. It was not a link, it had no address behind it, and it
appeared on exactly one of the three dashboards -- so an LGU officer or
an administrator had no way to reach anyone from the page they land on.

WHAT IT IS. One dialog, included from base.html for every signed-in
page, opened from a footer bar that all three dashboards now share. The
address lives in config (SUPPORT_EMAIL) and is read from there in all
three places it appears, because an address that is right in two of
them is worse than one that is wrong in all three: nobody notices the
stale one.
"""

import pytest

from app import create_app
from app.extensions import db
from app.models import SystemSetting
from app.models.user import User

EXPECTED_EMAIL = "smesystem2026@gmail.com"

ROLES = [
    ("SME", "sme@contact.test", "/home"),
    ("LGU", "lgu@contact.test", "/lgu/dashboard"),
    ("Admin", "admin@contact.test", "/admin/dashboard"),
]


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        for role, email, _path in ROLES:
            user = User(name=role.title(), email=email, role=role)
            user.set_password("password123")
            db.session.add(user)
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


def _signed_in(app, email):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": "password123"},
                follow_redirects=True)
    return client


# ---------------------------------------------------------------------
# 1. Every role can reach it
# ---------------------------------------------------------------------

@pytest.mark.parametrize("role,email,path", ROLES)
def test_every_dashboard_has_a_working_contact_us(app, role, email, path):
    """The whole point of the change: it used to exist on one of these
    three."""
    page = _signed_in(app, email).get(path).get_data(as_text=True)

    assert "Contact Us" in page, f"{role} has no Contact Us on {path}"
    assert 'data-bs-target="#contactModal"' in page, (
        f"{role}'s Contact Us is not wired to the dialog -- it is decoration again"
    )
    assert 'id="contactModal"' in page, f"the dialog itself is missing on {path}"


@pytest.mark.parametrize("role,email,path", ROLES)
def test_the_address_is_on_every_dashboard(app, role, email, path):
    page = _signed_in(app, email).get(path).get_data(as_text=True)

    assert EXPECTED_EMAIL in page, f"{role} cannot see the support address on {path}"
    assert f"mailto:{EXPECTED_EMAIL}" in page, (
        "the address is shown but not clickable"
    )


@pytest.mark.parametrize("role,email,path", ROLES)
def test_the_dialog_explains_what_to_write_about(app, role, email, path):
    """An address on its own leaves the two questions people actually
    have unanswered: who is on the other end, and what should I say."""
    page = _signed_in(app, email).get(path).get_data(as_text=True)

    assert "A figure looks wrong" in page
    assert "Access problems" in page
    assert "LGU data uploads" in page
    assert "account email" in page


# ---------------------------------------------------------------------
# 2. One address, written down once
# ---------------------------------------------------------------------

def test_the_address_comes_from_config_not_the_templates(app):
    """It appears in the contact dialog, the Support dialog and a
    mailto: link. Three hardcoded copies is how one of them goes
    stale."""
    import re
    from pathlib import Path

    templates = Path(app.root_path) / "templates"
    offenders = []
    for path in templates.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        # The literal address must not be typed into any template --
        # every occurrence should be {{ SUPPORT_EMAIL }}.
        if re.search(re.escape(EXPECTED_EMAIL), text):
            offenders.append(str(path.relative_to(templates)))

    assert not offenders, (
        f"the support address is hardcoded in {offenders}; use SUPPORT_EMAIL so "
        f"there is one copy to change"
    )


def test_changing_the_configured_address_changes_every_page(app):
    """The test above says it is not hardcoded. This one says the
    config value is actually what renders."""
    app.config["SUPPORT_EMAIL"] = "someone-else@example.test"

    for _role, email, path in ROLES:
        page = _signed_in(app, email).get(path).get_data(as_text=True)
        assert "someone-else@example.test" in page
        assert EXPECTED_EMAIL not in page


def test_the_support_dialog_gives_the_same_address(app):
    """Support and Contact Us are two doors to the same team. A person
    who opened Support looking for help should not have to find a
    second dialog to get an address."""
    page = _signed_in(app, "sme@contact.test").get("/home").get_data(as_text=True)

    support = page.index('id="supportModal"')
    assert page.count(f"mailto:{EXPECTED_EMAIL}") >= 2, (
        "only one dialog offers the address"
    )
    assert EXPECTED_EMAIL in page[support:support + 4000]


# ---------------------------------------------------------------------
# 3. It is a real control, not a styled span
# ---------------------------------------------------------------------

def test_contact_us_is_a_button_not_a_span(app):
    """The old one was a <span>: not focusable, not reachable by
    keyboard, and it did nothing when clicked."""
    page = _signed_in(app, "sme@contact.test").get("/home").get_data(as_text=True)

    assert "<span>📞 Contact Us</span>" not in page
    assert 'class="dss-footer-link"' in page


def test_the_footer_is_one_shared_partial(app):
    """Three copies of a footer is how the LGU dashboard ends up
    missing whatever the SME one gained last month."""
    from pathlib import Path

    templates = Path(app.root_path) / "templates"
    inline_footers = [
        str(path.relative_to(templates))
        for path in templates.rglob("*.html")
        if "dss-home-footer" in path.read_text(encoding="utf-8")
        and path.name != "_footer.html"
    ]

    assert not inline_footers, (
        f"{inline_footers} still build the footer inline instead of including "
        f"shared/_footer.html"
    )
