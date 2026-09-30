"""
app/controllers/community_controller.py
------------------------------------------
The MarketLens community lives on Discord.

The in-app forum that briefly lived at /community has been replaced by
the project's Discord server, which already provides what the forum was
built to imitate -- channels by topic, moderators, reporting and
automatic filtering (Discord AutoMod). Community is therefore not a page
in the menu any more: the "Community Forum" link at the foot of each
dashboard opens the Discord invite in a new tab.

/community (and anything under it) still answers, as a redirect to the
same invite, so an old bookmark or a link in an earlier email lands in
the right place rather than on a 404.

The invite link is an Admin setting (System Settings -> Community), so a
replaced or expired invite is fixed without a redeploy. Only a
discord.gg / discord.com invite is ever redirected to: the setting is
edited in a browser, and a free-form URL there would turn this route
into an open redirect.
"""

import re

from flask import Blueprint, redirect

from app.models import SystemSetting

community_bp = Blueprint("community", __name__)

DEFAULT_INVITE_URL = "https://discord.gg/4EBtST2Bk"
SETTING_KEY = "community_invite_url"

_INVITE_PATTERN = re.compile(
    r"^https://(?:discord\.gg|(?:www\.)?discord\.com/invite)/[A-Za-z0-9-]{2,64}/?$"
)


def is_valid_invite(url):
    return bool(_INVITE_PATTERN.match(str(url or "").strip()))


def community_invite_url():
    """The configured invite, or the built-in one if the setting is
    empty or not a Discord invite."""
    try:
        value = (SystemSetting.get(SETTING_KEY, DEFAULT_INVITE_URL) or "").strip()
    except Exception:  # noqa: BLE001 -- a settings read must never break the link
        value = ""
    return value if is_valid_invite(value) else DEFAULT_INVITE_URL


@community_bp.route("/community", strict_slashes=False)
@community_bp.route("/community/<path:_rest>")
def discord(_rest=None):
    return redirect(community_invite_url(), code=302)
