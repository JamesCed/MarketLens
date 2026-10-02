"""
tests/test_forum.py
---------------------
The community content filter (app/services/forum_moderation.py).

WHAT IS NO LONGER TESTED HERE, AND WHY

This file used to drive the in-app forum's pages: /community, posting,
the AI pre-screen, the moderation queue, reports, "helpful" votes and
escaping. Those pages are gone. The community moved to the project's
Discord server (see app/controllers/community_controller.py), the forum
blueprint is no longer registered, and /community and everything under
it now redirects to the Discord invite -- which
tests/test_community_discord.py pins down. A test asserting that
/community/new stores a post and a test asserting that it redirects to
Discord cannot both pass; the second describes the app that ships.

What still ships is the filter itself, so its behaviour stays pinned:

  * it blocks what it must (including the usual disguises) and does NOT
    block ordinary words that happen to contain a bad one -- a false
    block on "reputation" would be a daily embarrassment;
  * scams, outside links and personal contact details are held for a
    person rather than published;
  * the AI can never publish text the filter flagged, and never rejects
    on its own;
  * the AI's reply is parsed tolerantly but strictly about the verdict.

These are pure functions: no app, no database, no LLM call.
"""

import pytest

from app.services import forum_moderation as fm


# =====================================================================
# 1. The content filter
# =====================================================================
@pytest.mark.parametrize("text", [
    "puta", "puuuuta ka", "P.U.T.A", "p-u-t-a", "p u t a n g i n a", "Putang ina", "Tanginamo talaga",
    "put4ngin4", "putá", "GAGO!", "okay,gago", "ulol", "Ukinnam",
    "fuck", "fuuuuck", "f*ck this", "fu.ck", "5h1t", "sh!t happens", "bullsh1t", "a$$hole",
])
def test_profanity_is_blocked_including_disguises(text):
    result = fm.check_text(text)
    assert result.verdict == fm.BLOCK, f"{text!r} was not blocked"
    assert "profanity" in result.flags
    # The author is told what to change, not just "no".
    assert result.messages and "swear" in result.messages[0].lower()


@pytest.mark.parametrize("text", [
    "My computer repair shop has a good reputation in Pasig and Tarlac.",
    "Selling shiitake and shitake mushrooms, plus cocktail drinks.",
    "Leche flan and puto for sale at the kanto.",
    "Pakyawan ng gulay sa palengke, magkano po?",
    "Assessment of our classic sari-sari store in Scunthorpe style.",
    "Petite dresses, title transfer, and a hayop farm.",
    "Importing coffee from Niger and Nigeria.",
    "Open 8 a.m. to 5 p.m., e.g. Monday to Saturday.",
    "Congratulations sa lahat ng bagong negosyo!",
])
def test_ordinary_words_are_not_blocked(text):
    """The Scunthorpe problem: a bad word inside an ordinary one."""
    result = fm.check_text(text)
    assert result.verdict == fm.CLEAN, f"{text!r} was flagged: {result.flags}"


def test_mild_insults_are_held_not_blocked():
    """"huwag maging tanga sa pera" is advice. A person decides."""
    result = fm.check_text("Huwag maging tanga sa pera, mag-ipon muna.")
    assert result.verdict == fm.REVIEW
    assert result.flags == ["rude_language"]


@pytest.mark.parametrize("text", [
    "Double your money in 7 days!",
    "Guaranteed profit po ito, promise.",
    "Sure kita dito mga ka-negosyo",
    "Join our investment scheme today",
    "Send gcash po sa akin para ma-reserve",
    "Earn 10k daily from home!",
    "Bitcoin doubling in 24 hours, legit",
    "DM me for details about this opportunity",
    "Join my team and be your own boss",
    "20% monthly returns on your capital",
])
def test_scam_and_solicitation_phrases_are_held(text):
    result = fm.check_text(text)
    assert result.verdict == fm.REVIEW
    assert "scam_phrase" in result.flags


def test_paluwagan_is_only_flagged_when_it_is_a_pitch():
    assert fm.check_text("May paluwagan kami sa barangay, okay ba ito?").is_clean
    assert "scam_phrase" in fm.check_text("Sali na sa paluwagan, may tubo 20% kada buwan").flags


def test_government_links_are_allowed():
    result = fm.check_text("See https://www.dti.gov.ph/negosyo-center and bir.gov.ph, psa.gov.ph, sec.gov.ph "
                           "and https://tarlaccity.gov.ph/permits for the forms.")
    assert result.is_clean, result.flags


def test_other_links_are_held():
    result = fm.check_text("Order at https://example.com/shop or shopee.ph/mystore")
    assert result.verdict == fm.REVIEW
    assert "external_link" in result.flags


@pytest.mark.parametrize("text", ["bit.ly/abc123", "tinyurl.com/xyz", "https://t.co/q", "bit . ly/abc", "cutt.ly/x"])
def test_link_shorteners_are_blocked(text):
    result = fm.check_text("Check this " + text)
    assert result.verdict == fm.BLOCK
    assert "link_shortener" in result.flags
    assert "full website address" in " ".join(result.messages)


@pytest.mark.parametrize("text, flag", [
    ("Call me at 09171234567", "phone_number"),
    ("Text +63 917 123 4567 po", "phone_number"),
    ("Tawag lang sa 0917-123-4567", "phone_number"),
    ("Email me at juan.delacruz@example.com", "email_address"),
])
def test_personal_contact_details_are_held(text, flag):
    result = fm.check_text(text)
    assert result.verdict == fm.REVIEW
    assert flag in result.flags
    # An email's domain is not also reported as a link.
    assert "external_link" not in result.flags


def test_prices_and_years_are_not_phone_numbers():
    assert fm.check_text("Rent is 15,000 pesos a month since 2024, about 500 per day.").is_clean


def test_spam_heuristics_hold_content():
    assert "all_caps" in fm.check_text("THIS IS A VERY LOUD MESSAGE ABOUT MY NEW STORE").flags
    assert "repeated_text" in fm.check_text("Sooooooooooooo good!").flags
    assert "repeated_text" in fm.check_text("buy buy buy buy now").flags
    many = " ".join(f"https://www.dti.gov.ph/page{i}" for i in range(6))
    assert fm.check_text(many).flags == ["too_many_links"]
    # Short shouting and divider lines are not spam.
    assert fm.check_text("DTI OK NA PO").is_clean
    assert fm.check_text("Menu\n----------\nAdobo").is_clean


@pytest.mark.parametrize("verdict, ai, is_admin, expected", [
    (fm.CLEAN, "approve", False, "approved"),
    (fm.CLEAN, "review", False, "pending"),
    (fm.CLEAN, "reject", False, "pending"),     # the AI never rejects
    (fm.CLEAN, None, False, "pending"),         # AI off or failed -> a person approves
    (fm.REVIEW, "approve", False, "pending"),   # the AI cannot overrule the filter
    (fm.REVIEW, None, True, "approved"),        # admins are the moderators
])
def test_initial_status_policy(verdict, ai, is_admin, expected):
    result = fm.FilterResult(verdict, ["x"] if verdict != fm.CLEAN else [], [])
    assert fm.initial_status(result, ai, is_admin) == expected


def test_ai_reply_parsing_is_tolerant_but_strict_about_the_verdict():
    assert fm._parse_ai_reply('```json\n{"verdict": "approve", "reason": "Fine"}\n```') == ("approve", "Fine")
    assert fm._parse_ai_reply('Sure! {"verdict": "Rejected", "reason": "Scam"} Hope that helps')[0] == "reject"
    assert fm._parse_ai_reply('{"verdict": "maybe"}') is None
    assert fm._parse_ai_reply("not json at all") is None
