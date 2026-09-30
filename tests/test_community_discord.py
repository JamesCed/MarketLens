"""
tests/test_community_discord.py
---------------------------------
The community is the MarketLens Discord server, not a page in the app:

  * no Community (or Moderation) entry in any role's menu;
  * the footer's "Community Forum" link opens the Discord invite in a
    new tab, through /community;
  * /community and anything under it redirect to the invite, so old
    bookmarks still land in the right place;
  * the invite is an Admin setting, and only a Discord invite is ever
    redirected to (no open redirect).
"""

import pytest

from app import create_app
from app.controllers.community_controller import DEFAULT_INVITE_URL, community_invite_url, is_valid_invite
from app.extensions import db
from app.models import SystemSetting, User


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _login(app, role="SME", email=None):
    email = email or f"{role.lower()}@community.test"
    with app.app_context():
        user = User(name=f"{role} person", email=email, role=role)
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()
    client = app.test_client()
    client.post("/login", data={"email": email, "password": "password123"})
    return client


def test_the_default_invite_is_the_projects_server():
    assert DEFAULT_INVITE_URL == "https://discord.gg/4EBtST2Bk"


@pytest.mark.parametrize("path", ["/community", "/community/", "/community/c/general", "/community/new",
                                  "/community/moderation", "/community/post/12"])
def test_every_community_url_redirects_to_discord(app, path):
    response = app.test_client().get(path)
    assert response.status_code == 302
    assert response.headers["Location"] == DEFAULT_INVITE_URL


@pytest.mark.parametrize("role", ["SME", "LGU", "Admin"])
def test_no_community_or_moderation_in_the_menu(app, role):
    body = _login(app, role).get("/settings").get_data(as_text=True)
    nav = body[body.index('class="dss-nav"'):body.index("dss-sidebar-footer")]
    assert "Community" not in nav.replace("{# Community is not a page", "")
    assert "Moderation" not in nav
    assert 'data-tour="nav-community"' not in nav


@pytest.mark.parametrize("role,page", [("SME", "/home"), ("LGU", "/lgu/dashboard"), ("Admin", "/admin/dashboard")])
def test_the_footer_link_opens_discord_in_a_new_tab(app, role, page):
    body = _login(app, role).get(page).get_data(as_text=True)
    footer = body[body.index('class="dss-home-footer'):]
    link = footer[footer.index('href="/community"') - 3:footer.index("Community Forum")]
    assert 'target="_blank"' in link and 'rel="noopener"' in link
    assert 'data-tour="community-link"' in link


def test_an_admin_can_change_the_invite(app):
    client = _login(app, "Admin")
    client.post("/admin/settings", data={"community_invite_url": "https://discord.com/invite/NewCode1"})
    with app.app_context():
        assert community_invite_url() == "https://discord.com/invite/NewCode1"
    assert app.test_client().get("/community").headers["Location"] == "https://discord.com/invite/NewCode1"


@pytest.mark.parametrize("bad", ["https://evil.example/phish", "javascript:alert(1)",
                                 "https://discord.gg.evil.example/x", "http://discord.gg/abc"])
def test_only_a_discord_invite_is_accepted(app, bad):
    client = _login(app, "Admin")
    page = client.post("/admin/settings", data={"community_invite_url": bad}, follow_redirects=True)
    assert "must be a Discord invite" in page.get_data(as_text=True)
    assert app.test_client().get("/community").headers["Location"] == DEFAULT_INVITE_URL


def test_a_bad_stored_value_falls_back_to_the_default(app):
    with app.app_context():
        SystemSetting.set("community_invite_url", "https://evil.example/x")
        assert community_invite_url() == DEFAULT_INVITE_URL


@pytest.mark.parametrize("url,ok", [
    ("https://discord.gg/4EBtST2Bk", True),
    ("https://discord.com/invite/abc-123", True),
    ("https://www.discord.com/invite/abc", True),
    ("https://discord.gg/", False),
    ("https://discord.gg/abc?next=https://evil", False),
])
def test_is_valid_invite(url, ok):
    assert is_valid_invite(url) is ok


def test_the_settings_page_has_the_invite_field(app):
    body = _login(app, "Admin").get("/admin/settings").get_data(as_text=True)
    assert 'name="community_invite_url"' in body
    assert DEFAULT_INVITE_URL in body
