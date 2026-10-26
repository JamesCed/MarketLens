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

ACTION_META = {
    "register": ("Created an account", "Account registration"),
    "login": ("Signed in", "User authentication"),
    "login_failed": ("Failed sign-in attempt", "Account security"),
    "logout": ("Signed out", "User authentication"),
    "password_reset_requested": ("Requested a password reset", "Account recovery"),
    "password_reset_abandoned": ("Password reset cancelled (too many wrong codes)", "Account security"),
    "password_reset_completed": ("Reset their password", "Account recovery"),
    "change_password": ("Changed their password", "Account security"),
    "change_password_failed": ("Failed to change their password", "Account security"),
    "export_trend_report": ("Exported the Trend Report", "Reporting"),
    "update_profile": ("Updated their profile", "Profile maintenance"),
    "update_profile_picture": ("Changed their profile picture", "Profile maintenance"),
    "remove_profile_picture": ("Removed their profile picture", "Profile maintenance"),
    "update_theme": ("Changed the display theme", "Personal preference"),
    "update_notification_prefs": ("Changed notification preferences", "Personal preference"),
    "clear_notifications": ("Cleared read notifications", "Personal preference"),
    "onboarding_started": ("Started the first-time walkthrough", "User onboarding"),
    "onboarding_completed": ("Finished the first-time walkthrough", "User onboarding"),
    "onboarding_skipped": ("Skipped the first-time walkthrough", "User onboarding"),

    "create_plan": ("Created a business plan", "Business planning"),
    "run_forecast": ("Ran an AI forecast", "Business planning"),
    "update_plan": ("Edited a business plan", "Business planning"),
    "trash_plan": ("Moved a business plan to Trash", "Business planning"),
    "restore_plan": ("Restored a business plan from Trash", "Business planning"),
    "delete_plan": ("Deleted a business plan (legacy)", "Business planning"),
    "select_plan": ("Switched the plan being viewed", "Business planning"),
    "purge_plan": ("Permanently deleted a plan after 30 days in Trash", "Trash retention"),
    "save_recommended_location": ("Saved a recommended location as a plan", "Business planning"),

    "dataset_upload": ("Uploaded a government dataset to the staging area", "Data maintenance"),
    "staging_review_approve": ("Approved a staged government dataset (Data Steward)", "Data maintenance"),
    "staging_review_reject": ("Rejected a staged government dataset (Data Steward)", "Data maintenance"),
    "staging_publish": ("Published approved government data to the live dataset", "Data maintenance"),
    "staging_admin_reject": ("Rejected an approved government dataset before publishing", "Data maintenance"),
    "view_diversification_plan": ("Opened the Diversification Plan", "City planning"),
    "export_diversification_plan": ("Exported the Diversification Plan", "City planning"),

    "admin_create_user": ("Created a user account", "User administration"),
    "admin_toggle_active": ("Changed an account's access (suspend/activate)", "User administration"),
    "admin_suspend_user": ("Suspended a user account", "User administration"),
    "admin_reactivate_user": ("Reactivated a suspended account", "User administration"),
    "suspension_expired": ("A timed suspension ended", "User administration"),
    "admin_delete_user": ("Deleted a user account (legacy)", "User administration"),
    "admin_archive_user": ("Archived a user account", "User administration"),
    "admin_restore_user": ("Restored an archived user account", "User administration"),
    "admin_set_lgu_role": ("Changed an LGU account's role (Encoder / Data Steward)", "User administration"),
    "lgu_access_requested": ("Asked for LGU access (waiting for verification)", "LGU access"),
    "admin_approve_lgu_access": ("Verified and approved an LGU access request", "LGU access"),
    "admin_reject_lgu_access": ("Declined an LGU access request", "LGU access"),
    "admin_revoke_lgu_access": ("Removed an account's LGU access", "LGU access"),
    "admin_reverify_lgu_access": ("Re-verified an LGU account (periodic check)", "LGU access"),
    "admin_delete_lgu_data": ("Deleted an LGU data row (legacy)", "Data maintenance"),
    "admin_delete_market_data": ("Deleted a market data row (legacy)", "Data maintenance"),
    "admin_archive_lgu_data": ("Archived an LGU data row", "Data maintenance"),
    "admin_restore_lgu_data": ("Restored an archived LGU data row", "Data maintenance"),
    "admin_archive_market_data": ("Archived a market data row", "Data maintenance"),
    "admin_restore_market_data": ("Restored an archived market data row", "Data maintenance"),
    "admin_update_settings": ("Changed system settings", "System configuration"),
    "retention_purge": ("Deleted data past its retention period", "Data maintenance"),
    "admin_llm_probe": ("Tested the AI provider connection", "System diagnostics"),
    "admin_email_probe": ("Tested the email transport", "System diagnostics"),
    "export_barangay_seed_csv": ("Downloaded the barangay seed dataset", "Data export"),
    "export_audit_log": ("Exported the audit trail", "Audit review"),

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

CATEGORIES = (
    ("security", "Security & sign-in"),
    ("accounts", "Account administration"),
    ("plans", "Business plans"),
    ("data", "Datasets & uploads"),
    ("settings", "System settings"),
    ("profile", "Profile & preferences"),
    ("other", "Other activity"),
)
_CATEGORY_OF = {
    "login": "security", "login_failed": "security", "logout": "security",
    "password_reset_requested": "security", "password_reset_abandoned": "security",
    "password_reset_completed": "security", "change_password": "security", "register": "security",
    "change_password_failed": "security",
    "admin_create_user": "accounts", "admin_toggle_active": "accounts", "admin_delete_user": "accounts",
    "admin_archive_user": "accounts", "admin_restore_user": "accounts", "admin_suspend_user": "accounts",
    "admin_reactivate_user": "accounts", "suspension_expired": "accounts", "admin_set_lgu_role": "accounts",
    "lgu_access_requested": "accounts", "admin_approve_lgu_access": "accounts", "admin_reject_lgu_access": "accounts",
    "admin_revoke_lgu_access": "accounts", "admin_reverify_lgu_access": "accounts",
    "create_plan": "plans", "run_forecast": "plans", "update_plan": "plans", "trash_plan": "plans",
    "restore_plan": "plans", "delete_plan": "plans", "purge_plan": "plans", "select_plan": "plans",
    "save_recommended_location": "plans",
    "dataset_upload": "data", "staging_review_approve": "data", "staging_review_reject": "data",
    "staging_publish": "data", "staging_admin_reject": "data", "admin_delete_lgu_data": "data", "admin_delete_market_data": "data",
    "admin_archive_lgu_data": "data", "admin_restore_lgu_data": "data", "admin_archive_market_data": "data",
    "admin_restore_market_data": "data", "export_barangay_seed_csv": "data", "retention_purge": "data",
    "view_diversification_plan": "data", "export_diversification_plan": "data", "export_trend_report": "data",
    "admin_update_settings": "settings", "admin_llm_probe": "settings", "admin_email_probe": "settings",
    "export_audit_log": "settings",
    "update_profile": "profile", "update_profile_picture": "profile", "remove_profile_picture": "profile",
    "update_theme": "profile", "update_notification_prefs": "profile", "clear_notifications": "profile",
    "onboarding_started": "profile", "onboarding_completed": "profile", "onboarding_skipped": "profile",
}
_HIGH = frozenset({
    "login_failed", "password_reset_abandoned", "password_reset_completed", "change_password",
    "change_password_failed", "admin_create_user", "admin_toggle_active", "admin_delete_user", "admin_archive_user",
    "admin_restore_user", "admin_suspend_user", "admin_reactivate_user", "suspension_expired",
    "dataset_upload", "admin_delete_lgu_data", "admin_delete_market_data", "admin_archive_lgu_data",
    "admin_restore_lgu_data", "admin_archive_market_data", "admin_restore_market_data",
    "admin_update_settings", "export_audit_log", "export_barangay_seed_csv", "purge_plan", "delete_plan",
    "export_diversification_plan", "retention_purge", "staging_review_approve", "staging_review_reject",
    "staging_publish", "staging_admin_reject", "admin_set_lgu_role", "lgu_access_requested",
    "admin_approve_lgu_access", "admin_reject_lgu_access", "admin_revoke_lgu_access", "admin_reverify_lgu_access",
})
_LOW = frozenset({
    "select_plan", "update_theme", "update_notification_prefs", "clear_notifications",
    "onboarding_started", "onboarding_completed", "onboarding_skipped", "view_diversification_plan",
    "admin_llm_probe", "admin_email_probe", "logout",
})
IMPORTANCE_LEVELS = (("important", "Important only"), ("high", "High importance only"), ("all", "Everything"))


def category_for(action):
    return _CATEGORY_OF.get(action, "other")


def importance_for(action):
    if action in _HIGH:
        return "high"
    if action in _LOW:
        return "low"
    return "normal"


def actions_in_category(category):
    return [a for a, c in _CATEGORY_OF.items() if c == category]


def low_importance_actions():
    return sorted(_LOW)


def high_importance_actions():
    return sorted(_HIGH)


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


_BROWSERS = (
    ("Edg/", "Edge"), ("EdgA/", "Edge"), ("EdgiOS/", "Edge"),
    ("OPR/", "Opera"), ("Opera", "Opera"),
    ("SamsungBrowser/", "Samsung Internet"),
    ("FBAN", "Facebook app"), ("FBAV", "Facebook app"),
    ("Firefox/", "Firefox"), ("FxiOS/", "Firefox"),
    ("CriOS/", "Chrome"), ("Chrome/", "Chrome"), ("Chromium/", "Chromium"),
    ("Version/", "Safari"),
    ("MSIE ", "Internet Explorer"), ("Trident/", "Internet Explorer"),
)

_SYSTEMS = (
    ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
    ("CrOS", "ChromeOS"), ("Windows", "Windows"),
    ("Macintosh", "macOS"), ("Mac OS X", "macOS"), ("Linux", "Linux"),
)

_TOOLS = (
    ("Werkzeug/", "Test client"), ("curl/", "curl (command line)"),
    ("python-requests/", "Python script"), ("python-urllib", "Python script"),
    ("PostmanRuntime/", "Postman"),
)
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
    return agent.split("/")[0].split(" ")[0][:40] or "Unknown device"
