"""
tests/test_industry_slider.py
-------------------------------
The Home page's industry slider (round 2):

  * all twenty PSIC sections are cards now, not the eight featured
    ones, in a horizontal scroll-snap row with icon-only Previous/Next
    buttons, a focusable track and a "Showing 1-4 of 20" count;
  * the chosen plan's industry comes first, ringed and aria-current,
    then the featured sections, then the rest in list order;
  * every card's icon is a Bootstrap Icon in a tinted tile -- no emoji;
  * all twenty are still scored in ONE compute_scores_batch call;
  * and the small leftover from the Settings move: the sidebar footer's
    Settings link is white when it is the page you are on.

The slider's scrolling, buttons and keys are browser behaviour; what is
pinned here is the markup they depend on, and the slider script's pure
arithmetic when a JavaScript runtime is available.
"""

import json
import os
import re
import shutil
import subprocess

import pytest

from app import create_app
from app.extensions import db
from app.ml.constants import (
    BUSINESS_TYPES,
    DEFAULT_INDUSTRY_DISPLAY,
    FEATURED_BUSINESS_TYPES,
    INDUSTRY_DISPLAY,
)
from app.ml.seed_data import BARANGAY_NAMES
from app.models import SmeProfile, SystemSetting, User

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDUCATION = "Education"                      # not one of the featured eight
RETAIL = "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles"  # featured, 2nd

# Every `bi` name below was checked against bootstrap-icons 1.11.3 -- the
# version base.html loads -- by finding `.bi-<name>::before` in that
# release's CSS (fetched from cdn.jsdelivr.net). A test cannot fetch the
# CDN, so the checked set is written down here: changing an icon in
# INDUSTRY_DISPLAY means checking the new name the same way and adding it.
ICONS_VERIFIED_IN_1_11_3 = frozenset({
    "bi-tree-fill", "bi-minecart-loaded", "bi-gear-wide-connected", "bi-lightning-charge-fill",
    "bi-droplet-fill", "bi-cone-striped", "bi-shop", "bi-truck", "bi-building", "bi-cup-hot-fill",
    "bi-pc-display", "bi-bank", "bi-house-door-fill", "bi-briefcase-fill", "bi-clipboard-check-fill",
    "bi-mortarboard-fill", "bi-heart-pulse-fill", "bi-palette-fill", "bi-scissors", "bi-house-heart-fill",
    "bi-bar-chart-fill", "bi-chevron-left", "bi-chevron-right",
})

# Emoji and pictographs, plus the variation selector that turns a plain
# symbol into an emoji ("⚙" + U+FE0F).
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        SystemSetting.ensure_defaults()
        yield app
        db.session.remove()
        db.drop_all()


def _user(email="owner@slider.test"):
    user = User(name="Owner", email=email, role="SME")
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def _plan(user, name, industry, location):
    profile = SmeProfile(user_id=user.user_id, business_name=name, industry_type=industry,
                         location=location, business_stage="startup", startup_capital=150000)
    db.session.add(profile)
    db.session.commit()
    return profile


def _client(app, email="owner@slider.test"):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": "password123"}, follow_redirects=True)
    return client


def _slider(page):
    """The slider's own markup: from its wrapper to the caption under it."""
    start = page.index('data-tour="industry-cards"')
    return page[page.rindex("<div", 0, start):page.index("Scores above are for", start)]


def _cards(page):
    return re.findall(r'<li class="dss-industry-item">(.*?)</li>', _slider(page), re.S)


def _industry(card):
    return re.search(r'data-industry="([^"]+)"', card).group(1)


def _expected_order(current=None):
    order = [current] if current else []
    for name in list(FEATURED_BUSINESS_TYPES) + list(BUSINESS_TYPES):
        if name not in order:
            order.append(name)
    return order


@pytest.fixture
def education_plan(app):
    with app.app_context():
        user = _user()
        return _plan(user, "Tibag Review Center", EDUCATION, "Tibag").sme_id


# ---------------------------------------------------------------------
# What is on the slider, and in what order
# ---------------------------------------------------------------------

def test_all_twenty_industries_are_cards_once_each(app, education_plan):
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    names = [_industry(card) for card in _cards(page)]
    assert len(names) == len(BUSINESS_TYPES) == 20
    assert sorted(names) == sorted(BUSINESS_TYPES), "every section exactly once"


def test_the_plans_industry_leads_then_featured_then_the_rest(app, education_plan):
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    cards = _cards(page)
    assert [_industry(card) for card in cards] == _expected_order(EDUCATION)

    # Only the plan's own card is current -- marked for a screen reader
    # (aria-current) and for the eye (the ring and the "Your plan" tag).
    current = [card for card in cards if 'aria-current="true"' in card]
    assert len(current) == 1 and _industry(current[0]) == EDUCATION
    assert "dss-card-current" in current[0] and "Your plan" in current[0]
    assert all("dss-card-current" not in card for card in cards[1:])


def test_a_featured_industry_is_not_listed_twice_when_it_leads(app):
    with app.app_context():
        user = _user()
        _plan(user, "Balibago Gulong", RETAIL, "Balibago I")
        page = _client(app).get("/home").get_data(as_text=True)
    names = [_industry(card) for card in _cards(page)]
    assert names == _expected_order(RETAIL)
    assert names.count(RETAIL) == 1


def test_switching_plans_moves_the_new_plans_industry_to_the_front(app, education_plan):
    with app.app_context():
        user = User.query.filter_by(email="owner@slider.test").one()
        retail = _plan(user, "Balibago Gulong", RETAIL, "Balibago I")
        client = _client(app)
        page = client.get(f"/home?plan={retail.sme_id}").get_data(as_text=True)
        assert _industry(_cards(page)[0]) == RETAIL
        page = client.get(f"/home?plan={education_plan}").get_data(as_text=True)
        assert _industry(_cards(page)[0]) == EDUCATION


def test_each_card_opens_the_map_for_its_industry_and_the_plans_barangay(app, education_plan):
    from urllib.parse import parse_qs, urlparse
    from html import unescape

    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    for card in _cards(page):
        href = unescape(re.search(r'href="([^"]+)"', card).group(1))
        url = urlparse(href)
        assert url.path == "/saturation-map"
        query = parse_qs(url.query)
        assert query["industry_type"] == [_industry(card)]
        assert query["location"] == ["Tibag"]


def test_the_page_still_renders_with_no_plans(app):
    with app.app_context():
        _user()
        response = _client(app).get("/home")
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    cards = _cards(page)
    assert [_industry(card) for card in cards] == _expected_order()
    # No plan, so nothing is "your plan's industry".
    assert not any('aria-current="true"' in card or "dss-card-current" in card for card in cards)
    # The scores are for the first barangay, and the caption says so.
    assert f"Scores above are for <strong>{BARANGAY_NAMES[0]}</strong>" in page


# ---------------------------------------------------------------------
# The slider's controls
# ---------------------------------------------------------------------

def test_the_track_is_a_focusable_labelled_region(app, education_plan):
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    slider = _slider(page)
    track = re.search(r'<div class="dss-industry-track"([^>]*)>', slider)
    assert track, "the scroll-snap track is missing"
    attrs = track.group(1)
    for attr in ('id="industryTrack"', 'tabindex="0"', 'role="region"', 'aria-label="Industries"'):
        assert attr in attrs, attr
    # The tour step still finds the slider.
    assert 'data-tour="industry-cards"' in page
    # The position count is a polite live region.
    assert re.search(r'id="industrySliderStatus"[^>]*aria-live="polite"', slider)
    assert "js/industry_slider.js" in page


@pytest.mark.parametrize("which, icon, label", [
    ("prev", "bi-chevron-left", "Previous industries"),
    ("next", "bi-chevron-right", "Next industries"),
])
def test_previous_and_next_are_icon_only_and_labelled(app, education_plan, which, icon, label):
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    slider = _slider(page)
    button = re.search(rf'<button([^>]*data-industry-slider-{which}[^>]*)>(.*?)</button>', slider, re.S)
    assert button, f"no {which} button"
    attrs, inner = button.groups()
    assert 'type="button"' in attrs
    assert f'aria-label="{label}"' in attrs and f'title="{label}"' in attrs
    assert 'aria-controls="industryTrack"' in attrs
    assert re.search(rf'<i class="bi {icon}" aria-hidden="true"></i>', inner)
    # Icon only: nothing but the icon inside the button.
    assert re.sub(r"<[^>]+>", "", inner).strip() == ""


def test_the_buttons_are_hidden_until_the_script_runs(app, education_plan):
    """Without scripting the row still scrolls by itself, and a button
    that would do nothing must not be shown."""
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    assert re.search(r'<div class="dss-industry-slider-nav" data-industry-slider-nav hidden>', _slider(page))


# ---------------------------------------------------------------------
# Icons and scores on the cards
# ---------------------------------------------------------------------

def test_every_card_has_a_bootstrap_icon_and_no_emoji(app, education_plan):
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    slider = _slider(page)
    assert not EMOJI.search(slider), f"emoji left in the slider: {EMOJI.findall(slider)}"
    assert "dss-industry-icon" not in slider, "the old blue emoji circle is gone from Home"
    for card in _cards(page):
        name = _industry(card)
        display = INDUSTRY_DISPLAY[name]
        tile = re.search(
            r'<span class="dss-industry-tile dss-hue-([a-z]+)" aria-hidden="true"><i class="bi (bi-[a-z0-9-]+)"></i></span>',
            card)
        assert tile, f"{name}: no icon tile"
        assert tile.groups() == (display["hue"], display["bi"])
        # The short label is the visible name; the full one is in the tooltip.
        assert f'<span class="dss-industry-name">{display["short"].replace("&", "&amp;")}</span>' in card


def test_the_arrow_and_score_colour_are_described_in_words(app, education_plan, monkeypatch):
    """Arrow and pill colour are read off the SAME band -- the plan
    chips' cuts: >= 6.5 good (up), >= 4 fair (level), else low (down) --
    so a yellow "fair" pill never sits beside a red down arrow. Both are
    drawn, so both are said: a screen reader hears what the arrow means."""
    from app.controllers import sme_controller

    plan_scores = [7.2, 6.5, 5.0, 4.5, 4.0, 3.9]

    def fake_batch(pairs, *args, **kwargs):
        pairs = list(pairs)
        return [{"viability_score": plan_scores[i % len(plan_scores)]} for i in range(len(pairs))]

    monkeypatch.setattr(sme_controller, "compute_scores_batch", fake_batch)
    with app.app_context():
        page = _client(app).get("/home").get_data(as_text=True)
    expected = {
        7.2: ("dss-pill-low", "bi-arrow-up-circle-fill", "a good chance"),
        6.5: ("dss-pill-low", "bi-arrow-up-circle-fill", "a good chance"),
        5.0: ("dss-pill-moderate", "bi-dash-circle", "a fair chance"),
        4.5: ("dss-pill-moderate", "bi-dash-circle", "a fair chance"),
        4.0: ("dss-pill-moderate", "bi-dash-circle", "a fair chance"),
        3.9: ("dss-pill-saturated", "bi-arrow-down-circle-fill", "hard to compete"),
    }
    for i, card in enumerate(_cards(page)):
        score = plan_scores[i % len(plan_scores)]
        pill, arrow, words = expected[score]
        assert re.search(rf'<strong class="dss-stat-pill {pill}">{score:.1f}</strong>', card), (i, score)
        assert re.search(rf'<i class="bi {arrow} [^"]*" aria-hidden="true"></i>', card), (i, score)
        assert f'<span class="visually-hidden">({words})</span>' in card
        # The arrow encodes the score's level, not a change over time,
        # so nothing on the card claims it is "rising" or "falling".
        assert "rising" not in card and "falling" not in card


def test_all_twenty_are_scored_in_one_batch_call(app, education_plan, monkeypatch):
    from app.controllers import sme_controller

    calls = []
    real = sme_controller.compute_scores_batch

    def spy(pairs, *args, **kwargs):
        pairs = list(pairs)
        calls.append(pairs)
        return real(pairs, *args, **kwargs)

    with app.app_context():
        # Signed in first: signing in lands on Home, which is a visit of
        # its own and not the one being counted.
        client = _client(app)
        monkeypatch.setattr(sme_controller, "compute_scores_batch", spy)
        page = client.get("/home").get_data(as_text=True)
    assert len(calls) == 1, f"Home scored its industry cards in {len(calls)} calls"
    pairs = calls[0]
    assert [industry for industry, _location in pairs] == _expected_order(EDUCATION)
    assert {location for _industry, location in pairs} == {"Tibag"}
    assert len(_cards(page)) == len(pairs)


# ---------------------------------------------------------------------
# The display table behind the icons
# ---------------------------------------------------------------------

def test_every_section_has_its_own_icon_and_hue():
    icons = [INDUSTRY_DISPLAY[name]["bi"] for name in BUSINESS_TYPES]
    hues = [INDUSTRY_DISPLAY[name]["hue"] for name in BUSINESS_TYPES]
    assert len(set(icons)) == len(BUSINESS_TYPES), "two sections share an icon"
    assert len(set(hues)) == len(BUSINESS_TYPES), "two sections share a colour"
    # The emoji stays for the pages that still use it (the LGU dashboard).
    assert all(INDUSTRY_DISPLAY[name]["icon"] for name in BUSINESS_TYPES)
    assert DEFAULT_INDUSTRY_DISPLAY["bi"] and DEFAULT_INDUSTRY_DISPLAY["hue"] not in hues


def test_every_icon_was_checked_against_the_loaded_bootstrap_icons():
    with open(os.path.join(ROOT, "app", "templates", "base.html"), encoding="utf-8") as handle:
        base = handle.read()
    assert "bootstrap-icons@1.11.3/" in base, (
        "base.html loads a different bootstrap-icons version now -- re-check every INDUSTRY_DISPLAY "
        "'bi' name against that version's CSS and update ICONS_VERIFIED_IN_1_11_3"
    )
    used = {entry["bi"] for entry in INDUSTRY_DISPLAY.values()} | {DEFAULT_INDUSTRY_DISPLAY["bi"]}
    assert used <= ICONS_VERIFIED_IN_1_11_3, f"not verified: {sorted(used - ICONS_VERIFIED_IN_1_11_3)}"


def _css():
    with open(os.path.join(ROOT, "app", "static", "css", "style.css"), encoding="utf-8") as handle:
        return handle.read()


def test_every_hue_has_a_light_and_a_dark_colour_pair():
    css = _css()
    light = css[css.index("HOME: THE INDUSTRY SLIDER"):]
    light_root = re.search(r":root\s*\{([^}]+)\}", light).group(1)
    dark_root = re.search(r'\[data-bs-theme="dark"\]\s*\{([^}]+)\}', light).group(1)
    hues = {entry["hue"] for entry in INDUSTRY_DISPLAY.values()} | {DEFAULT_INDUSTRY_DISPLAY["hue"]}
    for hue in hues:
        for part in ("bg", "fg"):
            assert f"--dss-hue-{hue}-{part}:" in light_root, f"{hue} {part} (light)"
            assert f"--dss-hue-{hue}-{part}:" in dark_root, f"{hue} {part} (dark)"
        assert re.search(rf"\.dss-hue-{hue}\s*\{{[^}}]*--dss-tile-bg: var\(--dss-hue-{hue}-bg\)", css), hue


def test_the_track_snaps_scrolls_on_its_own_and_respects_reduced_motion():
    css = _css()
    track = re.search(r"\.dss-industry-track\s*\{([^}]+)\}", css).group(1)
    assert "overflow-x: auto" in track
    assert "scroll-snap-type: x mandatory" in track
    assert re.search(r"\.dss-industry-item\s*\{[^}]*scroll-snap-align: start", css)
    # 4 / 3 / 2 cards from 1200 / 992 / 576px, and one and a bit below.
    assert "flex: 0 0 calc((100% - 1rem) / 1.2)" in css
    for width, basis in (("576px", "(100% - 1rem) / 2"), ("992px", "(100% - 2rem) / 3"),
                         ("1200px", "(100% - 3rem) / 4")):
        assert re.search(rf"@media \(min-width: {width}\)\s*\{{\s*\.dss-industry-item \{{ flex-basis: calc\({re.escape(basis)}\);",
                         css), width
    assert re.search(r"\.dss-slider-btn\s*\{[^}]*width: 36px; height: 36px", css)
    assert re.search(r"prefers-reduced-motion: reduce\)\s*\{\s*\.dss-industry-track \{ scroll-behavior: auto; \}", css)
    with open(os.path.join(ROOT, "app", "static", "js", "industry_slider.js"), encoding="utf-8") as handle:
        script = handle.read()
    assert "prefers-reduced-motion: reduce" in script
    assert '"ArrowRight"' in script and '"ArrowLeft"' in script


def test_the_sidebar_footer_settings_link_is_white_when_active():
    """The footer's dimmer link colour out-ranked .active, so Settings
    stayed grey on the Settings page."""
    css = _css()
    skin = css.split("INTERIOR SKIN (body.dss-skin)", 1)[1].split("END INTERIOR SKIN", 1)[0]
    rule = re.search(
        r"body\.dss-skin \.dss-sidebar-footer \.dss-nav-link:hover,\s*"
        r"body\.dss-skin \.dss-sidebar-footer \.dss-nav-link\.active \{ color: #fff; \}", skin)
    assert rule
    # ...and it comes AFTER the dimmer footer colour it has to beat.
    assert skin.index(rule.group(0)) > skin.index(
        "body.dss-skin .dss-sidebar-footer .dss-nav-link { color: rgba(226,236,246,.72); }")


# ---------------------------------------------------------------------
# The script's pure arithmetic (when a JavaScript runtime is available)
# ---------------------------------------------------------------------

def _js_runtime():
    """`node`, or VS Code's Electron run as Node -- whichever exists."""
    node = shutil.which("node")
    if node:
        return [node], {}
    code = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Microsoft VS Code", "Code.exe")
    if os.path.isfile(code):
        return [code], {"ELECTRON_RUN_AS_NODE": "1"}
    return None, None


def test_the_slider_arithmetic():
    command, env = _js_runtime()
    if command is None:
        pytest.skip("no JavaScript runtime (node) installed")
    script = os.path.join(ROOT, "app", "static", "js", "industry_slider.js").replace("\\", "/")
    probe = r"""
const s = require(%s);
const items = Array.from({length: 20}, (_, i) => ({left: i * 110, right: i * 110 + 100}));
const out = {
  four: s.visibleRange({left: 0, right: 430}, items),
  peek: s.visibleRange({left: 0, right: 120}, items),
  none: s.visibleRange({left: 0, right: 0}, items),
  label4: s.rangeLabel({first: 0, last: 3}, 20),
  label1: s.rangeLabel({first: 2, last: 2}, 20),
  labelNone: s.rangeLabel(null, 20),
  next: s.pageTarget({first: 0, last: 3}, 20, 1),
  prev: s.pageTarget({first: 4, last: 7}, 20, -1),
  nextEnd: s.pageTarget({first: 16, last: 19}, 20, 1),
  prevStart: s.pageTarget({first: 1, last: 4}, 20, -1),
};
console.log(JSON.stringify(out));
""" % json.dumps(script)
    result = subprocess.run(command + ["-e", probe], capture_output=True, text=True, encoding="utf-8",
                            timeout=60, env={**os.environ, **env})
    assert result.returncode == 0, result.stderr[-600:]
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["four"] == {"first": 0, "last": 3}
    # A 20px sliver of the second card is a hint, not "showing" it.
    assert out["peek"] == {"first": 0, "last": 0}
    assert out["none"] is None
    assert out["label4"] == "Showing 1–4 of 20"
    assert out["label1"] == "Showing 3 of 20"
    assert out["labelNone"] == "20 industries"
    assert (out["next"], out["prev"], out["nextEnd"], out["prevStart"]) == (4, 0, 19, 0)
