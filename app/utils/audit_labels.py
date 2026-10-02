"""
app/utils/audit_labels.py
---------------------------
Human-readable WHAT and default WHY for every audit action.

The action names stored in audit_logs are identifiers ("admin_toggle_
active"). The audit trail page shows people, not identifiers, so each
one maps to a label and to the purpose it serves -- the purpose being
what the WHY column shows when the person acting was not asked for a
reason (signing in, running a forecast). Administrative actions that
change someone else's access or data always carry an explicit reason
instead; see app/controllers/admin_controller.py.

An action missing from this table still renders: its identifier is
humanised and the purpose reads "General system activity". So a new
action can never break the audit page, it just looks less polished
until it is added here.

The other two helpers here are read-side too: target_type_label() turns
a stored model name ("LguData") into words, and describe_device() turns
a raw user-agent string into "Chrome on Windows". Both exist for the
same reason as the table above -- the audit page is read by an
administrator, not by the developer who wrote the log line.
"""

import re

# action -> (label, purpose)
ACTION_META = {
    # --- account & authentication
    "register": ("Created an account", "Account registration"),
    "login": ("Signed in", "User authentication"),
    "login_failed": ("Failed sign-in attempt", "Account security"),
    "logout": ("Signed out", "User authentication"),
    "password_reset_requested": ("Requested a password reset", "Account recovery"),
    "password_reset_abandoned": ("Password reset cancelled (too many wrong codes)", "Account security"),
    "password_reset_completed": ("Reset their password", "Account recovery"),
    "change_password": ("Changed their password", "Account security"),
    "update_profile": ("Updated their profile", "Profile maintenance"),
    "update_profile_picture": ("Changed their profile picture", "Profile maintenance"),
    "remove_profile_picture": ("Removed their profile picture", "Profile maintenance"),
    "update_theme": ("Changed the display theme", "Personal preference"),
    "update_notification_prefs": ("Changed notification preferences", "Personal preference"),
    "clear_notifications": ("Cleared read notifications", "Personal preference"),
    "onboarding_started": ("Started the first-time walkthrough", "User onboarding"),
    "onboarding_completed": ("Finished the first-time walkthrough", "User onboarding"),
    "onboarding_skipped": ("Skipped the first-time walkthrough", "User onboarding"),

    # --- business planning
    "create_plan": ("Created a business plan", "Business planning"),
    "run_forecast": ("Ran an AI forecast", "Business planning"),
    "update_plan": ("Edited a business plan", "Business planning"),
    # Plans are never deleted any more -- removing one moves it to Trash.
    # delete_plan is kept so rows written before that still read well.
    "trash_plan": ("Moved a business plan to Trash", "Business planning"),
    "restore_plan": ("Restored a business plan from Trash", "Business planning"),
    "delete_plan": ("Deleted a business plan (legacy)", "Business planning"),
    "select_plan": ("Switched the plan being viewed", "Business planning"),
    "save_recommended_location": ("Saved a recommended location as a plan", "Business planning"),

    # --- LGU data
    "dataset_upload": ("Uploaded a government dataset", "Data maintenance"),

    # --- administration
    "admin_create_user": ("Created a user account", "User administration"),
    "admin_toggle_active": ("Changed an account's access (suspend/activate)", "User administration"),
    "admin_delete_user": ("Deleted a user account (legacy)", "User administration"),
    "admin_archive_user": ("Archived a user account", "User administration"),
    "admin_restore_user": ("Restored an archived user account", "User administration"),
    "admin_delete_lgu_data": ("Deleted an LGU data row (legacy)", "Data maintenance"),
    "admin_delete_market_data": ("Deleted a market data row (legacy)", "Data maintenance"),
    "admin_archive_lgu_data": ("Archived an LGU data row", "Data maintenance"),
    "admin_restore_lgu_data": ("Restored an archived LGU data row", "Data maintenance"),
    "admin_archive_market_data": ("Archived a market data row", "Data maintenance"),
    "admin_restore_market_data": ("Restored an archived market data row", "Data maintenance"),
    "admin_update_settings": ("Changed system settings", "System configuration"),
    "admin_llm_probe": ("Tested the AI provider connection", "System diagnostics"),
    "admin_email_probe": ("Tested the email transport", "System diagnostics"),
    "export_barangay_seed_csv": ("Downloaded the barangay seed dataset", "Data export"),
    "export_audit_log": ("Exported the audit trail", "Audit review"),

    # --- community forum (retired: the community moved to Discord; kept
    #     so audit rows written while the forum existed still read well)
    "forum_post_created": ("Posted in the community forum", "Community participation"),
    "forum_comment_created": ("Commented in the community forum", "Community participation"),
    "forum_post_approved": ("Approved a community post", "Content moderation"),
    "forum_post_rejected": ("Rejected a community post", "Content moderation"),
    "forum_post_removed": ("Removed a community post", "Content moderation"),
    "forum_comment_approved": ("Approved a community comment", "Content moderation"),
    "forum_comment_rejected": ("Rejected a community comment", "Content moderation"),
    "forum_comment_removed": ("Removed a community comment", "Content moderation"),
    "forum_content_reported": ("Reported community content", "Community safety"),
    "forum_content_blocked": ("Submission blocked by the content filter", "Community safety"),
}

GENERIC_PURPOSE = "General system activity"


def label_for(action):
    meta = ACTION_META.get(action)
    if meta:
        return meta[0]
    return (action or "").replace("_", " ").strip().capitalize() or "Unknown action"


def purpose_for(action):
    meta = ACTION_META.get(action)
    return meta[1] if meta else GENERIC_PURPOSE


def actions_matching(text):
    """Every known action whose LABEL contains `text` -- so searching the
    audit page for "signed in" finds the rows stored as "login", which a
    plain search of the stored identifier never would."""
    needle = (text or "").strip().lower()
    if not needle:
        return []
    return [action for action, (label, _purpose) in ACTION_META.items() if needle in label.lower()]


# ------------------------------------------------------------ targets
# target_type is stored as the model's class name, because that is what
# log_action() can know without every caller spelling it out. These are
# the words shown for it.
TARGET_TYPE_LABELS = {
    "User": "Account",
    "LguData": "LGU data row",
    "MarketData": "Market data row",
    "SmeProfile": "Business plan",
    "ForecastResult": "Forecast",
    "SystemSetting": "System setting",
    "ForumPost": "Forum post",
    "ForumComment": "Forum comment",
    "Notification": "Notification",
}


def target_type_label(target_type):
    """"LguData" -> "LGU data row"; an unknown CamelCase name is split
    into words rather than shown raw."""
    if not target_type:
        return None
    if target_type in TARGET_TYPE_LABELS:
        return TARGET_TYPE_LABELS[target_type]
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(target_type)).replace("_", " ")
    return words.strip().capitalize()


# ------------------------------------------------------------ devices
# A deliberately small user-agent reader. The audit page needs "which
# kind of device was this", not a full browser-capability database, and
# a new dependency for one column is not worth its upkeep.
#
# ORDER IS THE WHOLE TRICK. Almost every browser's user agent claims to
# be several others for compatibility -- Edge says "Chrome" and
# "Safari", Chrome says "Safari", Opera says "Chrome" -- so the most
# specific token has to be tested first and the most generic last.
_BROWSERS = (
    ("Edg/", "Edge"), ("EdgA/", "Edge"), ("EdgiOS/", "Edge"),
    ("OPR/", "Opera"), ("Opera", "Opera"),
    ("SamsungBrowser/", "Samsung Internet"),
    ("FBAN", "Facebook app"), ("FBAV", "Facebook app"),
    ("Firefox/", "Firefox"), ("FxiOS/", "Firefox"),
    ("CriOS/", "Chrome"), ("Chrome/", "Chrome"), ("Chromium/", "Chromium"),
    ("Version/", "Safari"),   # Safari is the one that says "Version/x Safari/y"
    ("MSIE ", "Internet Explorer"), ("Trident/", "Internet Explorer"),
)

# Same rule: iPhone and iPad user agents also say "Mac OS X", and an
# Android one also says "Linux", so the specific ones come first.
_SYSTEMS = (
    ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
    ("CrOS", "ChromeOS"), ("Windows", "Windows"),
    ("Macintosh", "macOS"), ("Mac OS X", "macOS"), ("Linux", "Linux"),
)

# Not people. Named plainly so an unusual row explains itself.
_TOOLS = (
    ("Werkzeug/", "Test client"), ("curl/", "curl (command line)"),
    ("python-requests/", "Python script"), ("python-urllib", "Python script"),
    ("PostmanRuntime/", "Postman"),
)
# Crawlers are checked BEFORE browsers, because Googlebot and friends
# now carry a full Chrome token and would otherwise read as a person
# using Chrome.
_BOT = re.compile(r"(bot|crawler|spider)[/;\s)+]", re.IGNORECASE)


def describe_device(user_agent):
    """"Chrome on Windows", "Safari on iPhone", "Test client" -- or None
    when nothing was recorded (a background job, or a row written
    before the WHERE columns existed)."""
    agent = (user_agent or "").strip()
    if not agent:
        return None
    if _BOT.search(agent + " "):
        return "Automated bot"

    browser = next((name for token, name in _BROWSERS if token in agent), None)
    system = next((name for token, name in _SYSTEMS if token in agent), None)

    if browser is None:
        tool = next((name for token, name in _TOOLS if token in agent), None)
        if tool:
            return tool
    if browser and system:
        return f"{browser} on {system}"
    if browser or system:
        return browser or system
    # Something we have never seen: its product name is still more
    # useful than "Unknown", and never longer than a word or two.
    return agent.split("/")[0].split(" ")[0][:40] or "Unknown device"
