"""
tests/test_forum.py
---------------------
The Community forum and its moderation.

What is worth pinning down here, in order of how badly it would hurt
to get wrong:

  1. Nothing a member should not see reaches them. Pending, rejected and
     removed content is invisible to everyone but its author and the
     moderators -- and "invisible" means 404, not a hidden div.
  2. The filter blocks what it must (including the usual disguises) and
     does NOT block ordinary words that happen to contain a bad one.
     A false block on "reputation" would be a daily embarrassment.
  3. The AI can only ever publish clean text. It never rejects, and when
     it is off, down or talking nonsense, posts wait for a person.
  4. Blocked text is never stored -- not as a post, and not in the audit
     trail either.
  5. Moderation decisions carry a reason, reach the author, and are
     logged.
  6. Member text is escaped wherever it is shown.

No test calls a real LLM: an autouse fixture replaces every generator
with one that fails, and the AI tests install their own.
"""

import pytest

from app import create_app
from app.extensions import db
from app.models import AuditLog, Notification, SystemSetting, User
from app.models.forum import (
    ForumChannel,
    ForumComment,
    ForumHelpful,
    ForumPost,
    ForumReport,
)
from app.services import forum_moderation as fm
from app.services import llm_service


# =====================================================================
# Fixtures
# =====================================================================
@pytest.fixture
def app():
    app = create_app("testing")

    # Several people sign in during one test, all inside one pushed app
    # context -- so flask.g (where Flask-Login caches the user) would
    # carry one client's identity into the next client's request. Same
    # fix as tests/test_admin_archive_and_audit.py.
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


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """Every provider fails unless a test installs its own. Also records
    whether the AI was asked at all."""
    monkeypatch.delenv("USE_LLM_RECOMMENDATIONS", raising=False)
    calls = []

    def failing(prompt):
        calls.append(prompt)
        return None

    monkeypatch.setattr(llm_service, "_GENERATORS", {"openai": failing, "anthropic": failing, "gemini": failing})
    return calls


def _ai_answers(monkeypatch, raw_text):
    """Make the configured provider answer with raw_text; returns the
    list of prompts it was given."""
    calls = []

    def answer(prompt):
        calls.append(prompt)
        return raw_text

    monkeypatch.setattr(llm_service, "_GENERATORS", {"openai": answer, "anthropic": answer, "gemini": answer})
    return calls


def _user(email, role="SME", name=None):
    user = User(name=name or f"{role} Person", email=email, role=role)
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def _login(app, email):
    client = app.test_client()
    client.post("/login", data={"email": email, "password": "password123"})
    return client


@pytest.fixture
def sme(app):
    return _user("sme@forum.test", name="Sam Sari-Sari")


@pytest.fixture
def other(app):
    return _user("other@forum.test", name="Olive Other")


@pytest.fixture
def admin(app):
    return _user("admin@forum.test", role="Admin", name="Ada Admin")


@pytest.fixture
def sme_client(app, sme):
    return _login(app, sme.email)


@pytest.fixture
def other_client(app, other):
    return _login(app, other.email)


@pytest.fixture
def admin_client(app, admin):
    return _login(app, admin.email)


def _no_ai():
    SystemSetting.set(fm.AI_SETTING_KEY, "false")


def _submit(client, title="Where to buy food packaging?", body="Saan po maganda bumili ng packaging dito sa Tarlac?",
            channel="general"):
    return client.post("/community/new", data={"channel": channel, "title": title, "body": body})


def _latest_post():
    db.session.expire_all()
    return ForumPost.query.order_by(ForumPost.id.desc()).first()


def _approved_post(author, title="Tips for a new carinderia", body="Here is what worked for us in our first year."):
    from app.models.forum import ensure_default_channels

    ensure_default_channels()
    channel = ForumChannel.query.filter_by(slug="general").first()
    post = ForumPost(channel_id=channel.id, author_id=author.user_id, title=title, body=body, status="approved")
    db.session.add(post)
    db.session.commit()
    return post


def _audit(action):
    db.session.expire_all()
    return AuditLog.query.filter_by(action=action).all()


def _notes_for(user):
    db.session.expire_all()
    return Notification.query.filter_by(user_id=user.user_id).all()


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


# =====================================================================
# 2. Channels and pages
# =====================================================================
def test_channels_are_seeded_on_first_visit(app, sme_client):
    assert ForumChannel.query.count() == 0
    response = sme_client.get("/community/")
    assert response.status_code == 200
    names = [c.name for c in ForumChannel.query.order_by(ForumChannel.sort_order)]
    assert names == ["General Discussion", "Food & Beverage", "Retail & Trade", "Services",
                     "Permits & Government", "Tips & Success Stories", "Ask the Community"]
    body = response.get_data(as_text=True)
    assert "Food &amp; Beverage" in body


def test_every_role_can_read_and_post(app, sme, admin):
    lgu = _user("lgu@forum.test", role="LGU")
    _no_ai()
    for email in (sme.email, lgu.email, admin.email):
        client = _login(app, email)
        assert client.get("/community/").status_code == 200
        assert client.get("/community/new").status_code == 200
        assert _submit(client, title=f"Hello from {email}").status_code == 302


def test_anonymous_visitors_are_sent_to_login(app):
    response = app.test_client().get("/community/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_new_post_form_shows_the_guidelines(app, sme_client):
    body = sme_client.get("/community/new").get_data(as_text=True)
    assert "Community guidelines" in body
    assert "No scams or selling schemes" in body
    assert "checked before they appear" in body


def test_channel_page_and_search_filter_the_feed(app, sme, sme_client):
    from app.models.forum import ensure_default_channels

    ensure_default_channels()
    food = ForumChannel.query.filter_by(slug="food-beverage").first()
    general = ForumChannel.query.filter_by(slug="general").first()
    db.session.add_all([
        ForumPost(channel_id=food.id, author_id=sme.user_id, title="Best siopao supplier", body="Looking for one.",
                  status="approved"),
        ForumPost(channel_id=general.id, author_id=sme.user_id, title="Permit renewal question", body="When is it?",
                  status="approved"),
    ])
    db.session.commit()

    food_page = sme_client.get("/community/c/food-beverage").get_data(as_text=True)
    assert "Best siopao supplier" in food_page and "Permit renewal question" not in food_page

    search = sme_client.get("/community/?q=permit").get_data(as_text=True)
    assert "Permit renewal question" in search and "Best siopao supplier" not in search

    assert sme_client.get("/community/c/no-such-channel").status_code == 404


# =====================================================================
# 3. Posting and visibility
# =====================================================================
def test_clean_post_waits_for_approval_when_ai_is_off(app, sme_client):
    _no_ai()
    response = _submit(sme_client)
    assert response.status_code == 302
    post = _latest_post()
    assert post.status == "pending"
    assert post.ai_verdict is None
    assert len(_audit("forum_post_created")) == 1


def test_pending_post_is_visible_only_to_its_author(app, sme_client, other_client):
    _no_ai()
    _submit(sme_client, title="My pending question")
    post = _latest_post()
    assert post.status == "pending"

    mine = sme_client.get("/community/").get_data(as_text=True)
    assert "My pending question" in mine
    assert "Waiting for approval" in mine
    assert "Only you can see this" in mine

    theirs = other_client.get("/community/").get_data(as_text=True)
    assert "My pending question" not in theirs
    assert other_client.get(f"/community/post/{post.id}").status_code == 404
    assert "My pending question" not in other_client.get("/community/?q=pending").get_data(as_text=True)

    assert sme_client.get(f"/community/post/{post.id}").status_code == 200


def test_validation_errors_keep_the_text(app, sme_client):
    response = _submit(sme_client, title="Hi", body="A body that is long enough to pass.")
    assert response.status_code == 422
    body = response.get_data(as_text=True)
    assert "between 5 and 120" in body
    assert "A body that is long enough to pass." in body
    assert ForumPost.query.count() == 0


def test_blocked_post_is_not_stored_and_its_text_is_not_logged(app, sme_client):
    response = _submit(sme_client, title="Bakit ganito", body="Putangina ng supplier na ito, ang bagal!")
    assert response.status_code == 422
    body = response.get_data(as_text=True)

    # Told what to change, and their text is handed back to edit.
    assert "We couldn&#39;t post this yet" in body or "We couldn't post this yet" in body
    assert "swear word" in body
    assert "ang bagal!" in body

    assert ForumPost.query.count() == 0

    rows = _audit("forum_content_blocked")
    assert len(rows) == 1
    logged = " ".join(filter(None, [rows[0].details, rows[0].target_label, rows[0].reason])).lower()
    assert "profanity" in logged
    assert "putangina" not in logged and "supplier" not in logged and "bakit" not in logged


def test_admin_posts_are_published_but_still_filtered(app, admin_client, no_real_llm):
    _submit(admin_client, title="Welcome to the Community", body="Please read the guidelines before posting.")
    post = _latest_post()
    assert post.status == "approved"
    # The admin's own post needs no AI opinion.
    assert no_real_llm == []

    response = _submit(admin_client, title="Announcement", body="This is bullshit, sorry.")
    assert response.status_code == 422
    assert ForumPost.query.count() == 1


# The limits are the agreed numbers written out, not read back from the
# module: a test that loops range(fm.MAX_POSTS_PER_WINDOW) keeps passing
# when someone raises the limit to 500, which is the change it exists
# to catch.
def test_post_rate_limit(app, sme_client):
    _no_ai()
    for i in range(5):
        assert _submit(sme_client, title=f"Question number {i}").status_code == 302

    response = _submit(sme_client, title="One more question", body="This one should be refused for now.")
    assert response.status_code == 429
    body = response.get_data(as_text=True)
    assert "try again in" in body
    assert "This one should be refused for now." in body
    assert ForumPost.query.count() == 5


def test_rate_limit_window_expires(app, sme, sme_client):
    from datetime import datetime, timedelta

    _no_ai()
    for i in range(5):
        _submit(sme_client, title=f"Old question {i}")
    for post in ForumPost.query.all():
        post.created_at = datetime.utcnow() - timedelta(hours=2)
    db.session.commit()
    assert _submit(sme_client, title="A new hour, a new question").status_code == 302


def test_comment_rate_limit(app, sme, other_client):
    _no_ai()
    post = _approved_post(sme)
    for i in range(15):
        response = other_client.post(f"/community/post/{post.id}/comment", data={"body": f"Salamat po {i}"})
        assert response.status_code == 302

    response = other_client.post(f"/community/post/{post.id}/comment", data={"body": "Isa pa po"})
    assert response.status_code == 429
    assert "Isa pa po" in response.get_data(as_text=True)
    assert ForumComment.query.count() == 15


# =====================================================================
# 4. The AI pre-screen
# =====================================================================
def test_ai_approval_publishes_a_clean_post(app, sme_client, monkeypatch):
    calls = _ai_answers(monkeypatch, '{"verdict": "approve", "reason": "A normal business question."}')
    _submit(sme_client)
    post = _latest_post()
    assert post.status == "approved"
    assert post.ai_verdict == "approve"
    assert post.ai_reason == "A normal business question."
    # The prompt tells the model where it is and treats the text as data.
    assert "Tarlac City, Philippines" in calls[0] and "Taglish" in calls[0]
    assert "<submission>" in calls[0] and "Saan po maganda" in calls[0]


def test_ai_is_not_asked_about_flagged_text_and_cannot_publish_it(app, sme_client, monkeypatch):
    calls = _ai_answers(monkeypatch, '{"verdict": "approve", "reason": "Fine."}')
    _submit(sme_client, body="Order po kayo, text 09171234567 for details.")
    post = _latest_post()
    assert post.status == "pending"
    assert "phone_number" in post.flag_list
    assert calls == []


def test_ai_failure_falls_back_to_a_moderator(app, sme_client, no_real_llm):
    _submit(sme_client)
    post = _latest_post()
    assert no_real_llm, "the AI should have been asked"
    assert post.status == "pending"
    assert post.ai_verdict is None


def test_ai_exception_and_nonsense_fall_back_to_a_moderator(app, sme_client, monkeypatch):
    def boom(prompt):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm_service, "_GENERATORS", {"openai": boom, "anthropic": lambda p: "no idea",
                                                     "gemini": lambda p: '{"verdict": "sure"}'})
    assert _submit(sme_client).status_code == 302
    assert _latest_post().status == "pending"


def test_ai_reject_goes_to_the_queue_not_to_rejected(app, sme_client, admin_client, monkeypatch):
    _ai_answers(monkeypatch, '{"verdict": "reject", "reason": "Looks like a sales pitch."}')
    _submit(sme_client, title="Visit my store this weekend", body="We have a sale on school supplies po.")
    post = _latest_post()
    assert post.status == "pending"
    assert post.ai_verdict == "reject"
    assert "ai_flagged" in post.flag_list

    queue = admin_client.get("/community/moderation").get_data(as_text=True)
    assert "Visit my store this weekend" in queue
    assert "Looks like a sales pitch." in queue


def test_ai_switch_off_means_the_ai_is_never_asked(app, sme_client, admin_client, monkeypatch):
    calls = _ai_answers(monkeypatch, '{"verdict": "approve", "reason": "Fine."}')
    response = admin_client.post("/community/moderation/ai", data={"enabled": "0"})
    assert response.status_code == 302
    assert SystemSetting.get_bool(fm.AI_SETTING_KEY, True) is False

    _submit(sme_client)
    assert calls == []
    assert _latest_post().status == "pending"


def test_app_wide_llm_switch_also_turns_the_forum_ai_off(app, sme_client, monkeypatch):
    calls = _ai_answers(monkeypatch, '{"verdict": "approve", "reason": "Fine."}')
    SystemSetting.set("use_llm_recommendations", "false")
    _submit(sme_client)
    assert calls == []
    assert _latest_post().status == "pending"


# =====================================================================
# 5. Moderation
# =====================================================================
def test_non_admins_get_403_on_moderation(app, sme, sme_client):
    lgu = _user("lgu2@forum.test", role="LGU")
    post = _approved_post(sme)
    for client in (sme_client, _login(app, lgu.email)):
        assert client.get("/community/moderation").status_code == 403
        assert client.post(f"/community/moderation/post/{post.id}/remove",
                           data={"reason": "Trying my luck"}).status_code == 403
        assert client.post("/community/moderation/ai", data={"enabled": "0"}).status_code == 403
    db.session.expire_all()
    assert db.session.get(ForumPost, post.id).status == "approved"


def test_approving_publishes_and_notifies_the_author(app, sme, sme_client, other_client, admin_client):
    _no_ai()
    _submit(sme_client, title="Approved soon")
    post = _latest_post()

    response = admin_client.post(f"/community/moderation/post/{post.id}/approve")
    assert response.status_code == 302

    db.session.expire_all()
    post = db.session.get(ForumPost, post.id)
    assert post.status == "approved"
    assert post.reviewed_by is not None and post.reviewed_at is not None
    assert "Approved soon" in other_client.get("/community/").get_data(as_text=True)

    notes = _notes_for(sme)
    assert len(notes) == 1 and notes[0].type == "info" and "approved" in notes[0].message

    rows = _audit("forum_post_approved")
    assert len(rows) == 1 and rows[0].target_type == "ForumPost" and rows[0].target_id == str(post.id)


def test_rejecting_requires_a_reason_and_tells_the_author(app, sme, sme_client, admin_client):
    _no_ai()
    _submit(sme_client, title="Needs changes")
    post = _latest_post()

    admin_client.post(f"/community/moderation/post/{post.id}/reject", data={"reason": "  "})
    db.session.expire_all()
    assert db.session.get(ForumPost, post.id).status == "pending"
    assert _notes_for(sme) == []
    assert _audit("forum_post_rejected") == []

    admin_client.post(f"/community/moderation/post/{post.id}/reject",
                      data={"reason": "Please remove the phone number"})
    db.session.expire_all()
    post = db.session.get(ForumPost, post.id)
    assert post.status == "rejected"
    assert post.moderation_note == "Please remove the phone number"

    notes = _notes_for(sme)
    assert len(notes) == 1 and "Please remove the phone number" in notes[0].message

    rows = _audit("forum_post_rejected")
    assert len(rows) == 1 and rows[0].reason == "Please remove the phone number"

    # The author can see why on their own post; nobody else can see it.
    assert "Please remove the phone number" in sme_client.get(f"/community/post/{post.id}").get_data(as_text=True)


def test_removing_published_content(app, sme, other_client, admin_client):
    post = _approved_post(sme, title="Soon to be removed")
    assert admin_client.post(f"/community/moderation/post/{post.id}/remove", data={}).status_code == 302
    db.session.expire_all()
    assert db.session.get(ForumPost, post.id).status == "approved"   # no reason, no removal

    admin_client.post(f"/community/moderation/post/{post.id}/remove", data={"reason": "Off-topic advertising"})
    db.session.expire_all()
    assert db.session.get(ForumPost, post.id).status == "removed"
    assert other_client.get(f"/community/post/{post.id}").status_code == 404
    assert "Soon to be removed" not in other_client.get("/community/").get_data(as_text=True)
    assert any("Off-topic advertising" in n.message for n in _notes_for(sme))
    assert _audit("forum_post_removed")[0].reason == "Off-topic advertising"


def test_comment_moderation(app, sme, other, other_client, admin_client, sme_client):
    _no_ai()
    post = _approved_post(sme)
    other_client.post(f"/community/post/{post.id}/comment", data={"body": "Try the supplier in San Roque po."})
    comment = ForumComment.query.one()
    assert comment.status == "pending"

    assert "Try the supplier in San Roque" in other_client.get(f"/community/post/{post.id}").get_data(as_text=True)
    assert "Try the supplier in San Roque" not in sme_client.get(f"/community/post/{post.id}").get_data(as_text=True)

    queue = admin_client.get("/community/moderation?tab=comments").get_data(as_text=True)
    assert "Try the supplier in San Roque" in queue

    admin_client.post(f"/community/moderation/comment/{comment.id}/approve")
    assert "Try the supplier in San Roque" in sme_client.get(f"/community/post/{post.id}").get_data(as_text=True)
    assert len(_audit("forum_comment_approved")) == 1
    assert len(_notes_for(other)) == 1

    admin_client.post(f"/community/moderation/comment/{comment.id}/remove", data={"reason": "Duplicate answer"})
    assert "Try the supplier in San Roque" not in sme_client.get(f"/community/post/{post.id}").get_data(as_text=True)
    assert _audit("forum_comment_removed")[0].reason == "Duplicate answer"


def test_blocked_comment_is_not_stored(app, sme, other_client):
    post = _approved_post(sme)
    response = other_client.post(f"/community/post/{post.id}/comment", data={"body": "gago ka talaga"})
    assert response.status_code == 422
    assert "gago ka talaga" in response.get_data(as_text=True)   # handed back to edit
    assert ForumComment.query.count() == 0
    row = _audit("forum_content_blocked")[0]
    assert "gago" not in (row.details or "").lower() + (row.target_label or "").lower()


def test_no_comments_on_a_pending_post(app, sme_client):
    _no_ai()
    _submit(sme_client)
    post = _latest_post()
    sme_client.post(f"/community/post/{post.id}/comment", data={"body": "Bump po"})
    assert ForumComment.query.count() == 0


def test_mutations_are_post_only(app, sme, admin_client):
    post = _approved_post(sme)
    for url in (f"/community/moderation/post/{post.id}/approve", f"/community/post/{post.id}/report",
                f"/community/post/{post.id}/helpful", f"/community/post/{post.id}/comment", "/community/moderation/ai"):
        assert admin_client.get(url).status_code == 405, url


def test_moderation_redirect_stays_inside_the_forum(app, sme, sme_client, admin_client):
    _no_ai()
    _submit(sme_client)
    post = _latest_post()
    response = admin_client.post(f"/community/moderation/post/{post.id}/approve",
                                 data={"next": "https://evil.example/phish"})
    assert response.headers["Location"].startswith("/community/moderation")


# =====================================================================
# 6. Reports
# =====================================================================
def test_three_reports_hide_content_until_a_moderator_decides(app, sme, admin_client):
    post = _approved_post(sme, title="Contested post")
    reporters = [_user(f"r{i}@forum.test") for i in range(3)]

    for i, reporter in enumerate(reporters):
        client = _login(app, reporter.email)
        response = client.post(f"/community/post/{post.id}/report", data={"reason": "spam"})
        assert response.status_code == 302
        db.session.expire_all()
        expected = "approved" if i < 2 else "pending"
        assert db.session.get(ForumPost, post.id).status == expected, f"after {i + 1} reports"

    post = db.session.get(ForumPost, post.id)
    assert "reported" in post.flag_list
    assert any("hidden" in n.message for n in _notes_for(sme))
    assert len(_audit("forum_content_reported")) == 3

    # Hidden from members...
    bystander = _login(app, _user("bystander@forum.test").email)
    assert bystander.get(f"/community/post/{post.id}").status_code == 404

    # ...and in the Reported queue, with what members said.
    queue = admin_client.get("/community/moderation?tab=reported").get_data(as_text=True)
    assert "Contested post" in queue and "Spam or scam" in queue

    # Keeping it restores it and clears the reports, so the same three
    # cannot hide it again.
    admin_client.post(f"/community/moderation/post/{post.id}/approve")
    db.session.expire_all()
    assert db.session.get(ForumPost, post.id).status == "approved"
    assert ForumReport.query.filter_by(resolved_at=None).count() == 0
    assert "Contested post" not in admin_client.get("/community/moderation?tab=reported").get_data(as_text=True)


def test_one_report_per_member(app, sme, other_client):
    post = _approved_post(sme)
    for _ in range(3):
        other_client.post(f"/community/post/{post.id}/report", data={"reason": "offensive"})
    assert ForumReport.query.count() == 1
    db.session.expire_all()
    assert db.session.get(ForumPost, post.id).status == "approved"


def test_report_rules(app, sme, sme_client, other_client):
    post = _approved_post(sme)
    # Not your own post.
    sme_client.post(f"/community/post/{post.id}/report", data={"reason": "spam"})
    # A reason from the list, and "Other" needs a note.
    other_client.post(f"/community/post/{post.id}/report", data={"reason": "because"})
    other_client.post(f"/community/post/{post.id}/report", data={"reason": "other", "note": ""})
    assert ForumReport.query.count() == 0

    other_client.post(f"/community/post/{post.id}/report", data={"reason": "other", "note": "Wrong permit fees"})
    assert ForumReport.query.count() == 1


def test_pending_content_cannot_be_reported(app, sme_client, other_client):
    _no_ai()
    _submit(sme_client)
    post = _latest_post()
    assert other_client.post(f"/community/post/{post.id}/report", data={"reason": "spam"}).status_code == 404
    assert ForumReport.query.count() == 0


# =====================================================================
# 7. Helpful
# =====================================================================
def test_helpful_is_one_per_member_and_toggles(app, sme, sme_client, other_client):
    post = _approved_post(sme)
    url = f"/community/post/{post.id}/helpful"

    data = other_client.post(url, headers={"Accept": "application/json"}).get_json()
    assert data == {"ok": True, "marked": True, "count": 1, "message": None}
    data = other_client.post(url, headers={"Accept": "application/json"}).get_json()
    assert data["marked"] is False and data["count"] == 0

    other_client.post(url)
    assert ForumHelpful.query.count() == 1

    # Not on your own post.
    assert sme_client.post(url, headers={"Accept": "application/json"}).status_code == 400
    db.session.expire_all()
    assert db.session.get(ForumPost, post.id).helpful_count == 1


# =====================================================================
# 8. Escaping
# =====================================================================
def test_member_text_is_escaped_everywhere(app, sme, sme_client, other_client, admin_client):
    _no_ai()
    _submit(sme_client, title="<script>alert('t')</script> hello",
            body="Line one <img src=x onerror=alert(1)>\nLine two")
    post = _latest_post()

    for client, url in ((sme_client, f"/community/post/{post.id}"), (sme_client, "/community/"),
                        (admin_client, "/community/moderation")):
        body = client.get(url).get_data(as_text=True)
        assert "<script>alert(" not in body, url
        assert "<img src=x" not in body, url
        assert "&lt;script&gt;" in body, url

    # Line breaks survive as <br>, and only as <br>.
    page = sme_client.get(f"/community/post/{post.id}").get_data(as_text=True)
    assert "&lt;img src=x onerror=alert(1)&gt;<br>" in page

    # The notification bell renders messages as HTML (static/js/main.js),
    # so no angle bracket from a title may reach a notification.
    admin_client.post(f"/community/moderation/post/{post.id}/approve")
    message = _notes_for(sme)[0].message
    assert "<" not in message and ">" not in message
