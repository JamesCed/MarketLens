"""
app/controllers/forum_controller.py
--------------------------------------
The Community forum: a small, moderated place for business owners and
LGU staff to ask each other questions. Think a Facebook group with a
handful of fixed topics, not a full Reddit.

    /community/                      feed of approved posts (+ your own pending ones)
    /community/c/<slug>              the same feed, one channel
    /community/post/<id>             a post, its comments, Helpful and Report
    /community/new                   write a post
    /community/moderation            Admin: pending posts / comments / reported

HOW A SUBMISSION IS DECIDED (the policy lives in services/forum_moderation.py)

  1. Length and rate limits. Refused with a friendly message; nothing saved.
  2. The deterministic filter. BLOCK -> not saved, the author is told what
     to change and gets their text back. The audit row records WHICH
     rules fired, never the text itself -- an audit trail that quotes
     every slur ever typed at the forum is its own moderation problem.
  3. Saved. It is published straight away only if the filter found
     nothing AND the AI pre-screen said "approve" (or the author is an
     administrator). Everything else waits for a moderator, exactly like
     a Facebook group with post approval turned on.

WHAT MEMBERS CAN SEE

Approved content, plus their own pending items (badged "Waiting for
approval"), plus -- on their own post's page -- why a rejected or
removed post came down. Anything else answers 404, not 403: a 403 would
confirm to a curious member that post 57 exists and is being hidden.

EVERY STATE CHANGE IS A POST. Approve, reject, remove, report and
Helpful all change data, so none of them can be triggered by a link, an
<img> tag or a prefetching browser. Flask-WTF's CSRF token covers each.
"""

from datetime import datetime, timedelta

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from markupsafe import Markup, escape
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from app.extensions import db
from app.models import Notification, SystemSetting
from app.models.forum import (
    REPORT_REASONS,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_REMOVED,
    ForumChannel,
    ForumComment,
    ForumHelpful,
    ForumPost,
    ForumReport,
    ensure_default_channels,
)
from app.services import forum_moderation as fm
from app.utils.audit import log_action
from app.utils.decorators import role_required

forum_bp = Blueprint("forum", __name__, url_prefix="/community")

# ---------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------
TITLE_MIN, TITLE_MAX = 5, 120
BODY_MIN, BODY_MAX = 10, 5000
COMMENT_MIN, COMMENT_MAX = 2, 2000

# Three different members, not three clicks: one person cannot hide a
# post they merely disagree with, but a real problem that several
# people notice comes down before a moderator is even online.
REPORTS_TO_HIDE = 3

# Same floor as the admin pages (admin_controller.MIN_REASON_LENGTH):
# enough to stop an empty box or a stray "x", and the author reads it,
# so it has to say something.
MIN_REASON_LENGTH = 5

PER_PAGE = 15

# Shown in Philippine time, as on the audit trail. Stored in UTC.
DISPLAY_UTC_OFFSET = timedelta(hours=8)


# ---------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------
def render_text(value):
    """User text with its line breaks kept, and nothing else. It is
    escaped FIRST and only then are the newlines turned into <br>, so
    the only markup that can reach the page is the <br> added here."""
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    return Markup(str(escape(text)).replace("\n", "<br>\n"))


def local_time(dt):
    if dt is None:
        return ""
    return (dt + DISPLAY_UTC_OFFSET).strftime("%b %d, %Y %I:%M %p") + " PHT"


def time_ago(dt):
    """"5 minutes ago" for recent things, a date for older ones. Friendlier
    than a timestamp for people who are not reading this as data."""
    if dt is None:
        return ""
    seconds = (datetime.utcnow() - dt).total_seconds()
    if seconds < 60:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = int(seconds // size)
            if unit == "day" and count > 7:
                break
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return (dt + DISPLAY_UTC_OFFSET).strftime("%b %d, %Y")


@forum_bp.context_processor
def _forum_template_helpers():
    # Scoped to this blueprint's templates, so nothing here leaks into
    # (or collides with) the rest of the app's template globals.
    return {
        "forum_text": render_text,
        "forum_time": local_time,
        "forum_ago": time_ago,
        "flag_label": fm.flag_label,
        "report_reasons": REPORT_REASONS,
        "min_reason_length": MIN_REASON_LENGTH,
        "limits": {
            "title_min": TITLE_MIN, "title_max": TITLE_MAX,
            "body_min": BODY_MIN, "body_max": BODY_MAX,
            "comment_min": COMMENT_MIN, "comment_max": COMMENT_MAX,
        },
    }


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------
def _is_admin():
    return current_user.is_authenticated and current_user.is_admin()


def _clean_title(value):
    # A title is one line; runs of spaces and stray newlines are noise.
    return " ".join((value or "").split())


def _clean_body(value):
    # Line breaks are kept -- they are how people lay out a question --
    # but trailing spaces and runs of blank lines are not.
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    cleaned, blank = [], 0
    for line in lines:
        blank = blank + 1 if not line.strip() else 0
        if blank <= 1:
            cleaned.append(line)
    return "\n".join(cleaned).strip()


def _reason_from_form():
    return " ".join((request.form.get("reason") or "").split())[:255]


def _safe_next(default):
    """Only ever redirect back into the forum. A `next` pointing anywhere
    else -- another site, or a protocol-relative //evil.example -- is
    ignored rather than followed."""
    target = request.form.get("next") or request.args.get("next") or ""
    if target.startswith("/community") and not target.startswith("//"):
        return target
    return default


def _wants_json():
    return request.accept_mimetypes.best == "application/json"


def _notify(user_id, message):
    """An in-app notification for the author.

    Angle brackets are taken out of the message: the notification bell
    (static/js/main.js) inserts messages as HTML, and a post title is
    member-written text. Stripping < and > here means no title can ever
    become markup there, whatever the bell does."""
    safe = message.replace("<", "").replace(">", "")
    if len(safe) > 500:
        safe = safe[:497] + "..."
    db.session.add(Notification(user_id=user_id, type="info", message=safe))


def _short(text, limit=60):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _visible_post_or_404(post_id):
    post = db.session.get(ForumPost, post_id)
    if post is None or not post.visible_to(current_user):
        abort(404)
    return post


def _channels():
    ensure_default_channels()
    return ForumChannel.query.order_by(ForumChannel.sort_order, ForumChannel.id).all()


def _unresolved_reports(kind, item_id):
    return ForumReport.query.filter_by(content_type=kind, content_id=item_id, resolved_at=None)


def _resolve_reports(kind, item_id):
    now = datetime.utcnow()
    for report in _unresolved_reports(kind, item_id).all():
        report.resolved_at = now
        report.resolved_by = current_user.user_id


# ---------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------
def _feed_filter():
    """Approved posts, plus the viewer's own posts still waiting."""
    return or_(
        ForumPost.status == STATUS_APPROVED,
        and_(ForumPost.author_id == current_user.user_id, ForumPost.status == STATUS_PENDING),
    )


def _approved_comment_counts(post_ids):
    if not post_ids:
        return {}
    rows = (
        db.session.query(ForumComment.post_id, func.count(ForumComment.id))
        .filter(ForumComment.post_id.in_(post_ids), ForumComment.status == STATUS_APPROVED)
        .group_by(ForumComment.post_id)
        .all()
    )
    return dict(rows)


def _moderation_queue_size():
    posts = ForumPost.query.filter_by(status=STATUS_PENDING).count()
    comments = ForumComment.query.filter_by(status=STATUS_PENDING).count()
    return posts + comments


def _render_feed(channel=None):
    channels = _channels()
    search = (request.args.get("q") or "").strip()[:100]
    page = max(1, request.args.get("page", 1, type=int) or 1)

    query = ForumPost.query.filter(_feed_filter())
    if channel is not None:
        query = query.filter(ForumPost.channel_id == channel.id)
    if search:
        needle = search.lower()
        # autoescape so a search for "50%" means the characters 50%, not
        # "50 followed by anything".
        query = query.filter(or_(
            func.lower(ForumPost.title).contains(needle, autoescape=True),
            func.lower(ForumPost.body).contains(needle, autoescape=True),
        ))

    rows = (
        query.order_by(ForumPost.created_at.desc(), ForumPost.id.desc())
        .offset((page - 1) * PER_PAGE)
        .limit(PER_PAGE + 1)
        .all()
    )
    posts = rows[:PER_PAGE]

    return render_template(
        "forum/index.html",
        channels=channels,
        channel=channel,
        posts=posts,
        search=search,
        page=page,
        has_more=len(rows) > PER_PAGE,
        comment_counts=_approved_comment_counts([p.id for p in posts]),
        queue_size=_moderation_queue_size() if _is_admin() else 0,
    )


@forum_bp.route("/")
@login_required
def index():
    return _render_feed()


@forum_bp.route("/c/<slug>")
@login_required
def channel(slug):
    _channels()
    found = ForumChannel.query.filter_by(slug=slug).first()
    if found is None:
        abort(404)
    return _render_feed(found)


# ---------------------------------------------------------------------
# A post
# ---------------------------------------------------------------------
def _render_post(post, comment_text="", comment_messages=None, status=200):
    visible = or_(
        ForumComment.status == STATUS_APPROVED,
        and_(ForumComment.author_id == current_user.user_id, ForumComment.status == STATUS_PENDING),
    )
    if _is_admin():
        visible = or_(visible, ForumComment.status == STATUS_PENDING)
    comments = (
        ForumComment.query.filter(ForumComment.post_id == post.id, visible)
        .order_by(ForumComment.created_at.asc(), ForumComment.id.asc())
        .all()
    )

    marked_helpful = (
        ForumHelpful.query.filter_by(user_id=current_user.user_id, post_id=post.id).first() is not None
    )
    mine = ForumReport.query.filter_by(reporter_id=current_user.user_id).filter(
        or_(
            and_(ForumReport.content_type == "post", ForumReport.content_id == post.id),
            and_(ForumReport.content_type == "comment", ForumReport.content_id.in_([c.id for c in comments] or [0])),
        )
    ).all()
    reported = {(r.content_type, r.content_id) for r in mine}

    return render_template(
        "forum/post.html",
        post=post,
        comments=comments,
        marked_helpful=marked_helpful,
        reported=reported,
        comment_text=comment_text,
        comment_messages=comment_messages or [],
        review_messages=_review_messages(post),
    ), status


def _review_messages(item):
    """Why a pending item is waiting, in the author's terms."""
    return fm.review_messages(item.flag_list) if item.is_pending else []


@forum_bp.route("/post/<int:post_id>")
@login_required
def post(post_id):
    return _render_post(_visible_post_or_404(post_id))


# ---------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------
def _screen(kind, channel_name, title, body):
    """Filter, then (only for clean text from a non-admin) the AI.
    Returns (FilterResult, status, flags_text, ai_verdict, ai_reason)."""
    result = fm.check_text(title, body)
    ai = None
    if result.is_clean and not _is_admin():
        ai = fm.ai_prescreen(kind, channel_name, title, body)
    ai_verdict, ai_reason = ai if ai else (None, None)
    status = fm.initial_status(result, ai_verdict, _is_admin())

    flags = list(result.flags)
    if ai_verdict in ("review", "reject"):
        flags.append("ai_flagged")
    return result, status, (",".join(flags)[:255] or None), ai_verdict, ai_reason


def _log_blocked(kind, result, label):
    # Flag NAMES only. See the module docstring for why the text itself
    # is never written to the audit trail.
    log_action(
        "forum_content_blocked",
        details=f"{kind.capitalize()} blocked before it was saved. Rules: {', '.join(result.flags)}.",
        target_type="ForumPost" if kind == "post" else "ForumComment",
        target_label=label,
    )


@forum_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_post():
    channels = _channels()
    form = {
        "channel": request.values.get("channel", ""),
        "title": _clean_title(request.form.get("title")),
        "body": _clean_body(request.form.get("body")),
    }

    def show(errors=None, blocked=None, status=200):
        return render_template(
            "forum/new.html", channels=channels, form=form,
            errors=errors or [], blocked=blocked or [],
        ), status

    if request.method == "GET":
        return show()

    chosen = next((c for c in channels if c.slug == form["channel"]), None)
    errors = []
    if chosen is None:
        errors.append("Please choose a channel for your post.")
    if not TITLE_MIN <= len(form["title"]) <= TITLE_MAX:
        errors.append(f"The title should be between {TITLE_MIN} and {TITLE_MAX} characters.")
    if not BODY_MIN <= len(form["body"]) <= BODY_MAX:
        errors.append(f"Your post should be between {BODY_MIN} and {BODY_MAX} characters.")
    if errors:
        return show(errors, status=422)

    # Administrators are exempt: an announcement thread is theirs to write.
    if not _is_admin():
        wait = fm.rate_limit_wait(ForumPost, current_user.user_id, fm.MAX_POSTS_PER_WINDOW, fm.POST_WINDOW)
        if wait:
            return show([
                f"You've shared {fm.MAX_POSTS_PER_WINDOW} posts in the last hour. Thank you! "
                f"Please try again in {fm.friendly_wait(wait)}. Your text is still here."
            ], status=429)

    result, status, flags, ai_verdict, ai_reason = _screen("post", chosen.name, form["title"], form["body"])
    if result.is_blocked:
        _log_blocked("post", result, f"New post in {chosen.name}")
        return show(blocked=result.messages, status=422)

    new = ForumPost(
        channel_id=chosen.id, author_id=current_user.user_id, title=form["title"], body=form["body"],
        status=status, filter_flags=flags, ai_verdict=ai_verdict, ai_reason=ai_reason,
    )
    db.session.add(new)
    db.session.commit()
    log_action(
        "forum_post_created",
        details=f"Channel: {chosen.name}. Status: {status}. Flags: {flags or 'none'}. AI: {ai_verdict or 'not asked'}.",
        target=new,
    )

    if status == STATUS_APPROVED:
        flash("Your post is live. Thanks for sharing!", "success")
    else:
        flash("Thanks! Your post will appear for everyone once a moderator approves it.", "info")
    return redirect(url_for("forum.post", post_id=new.id))


@forum_bp.route("/post/<int:post_id>/comment", methods=["POST"])
@login_required
def add_comment(post_id):
    post = _visible_post_or_404(post_id)
    if not post.is_approved:
        flash("Comments open once this post is approved.", "info")
        return redirect(url_for("forum.post", post_id=post.id))

    body = _clean_body(request.form.get("body"))
    if not COMMENT_MIN <= len(body) <= COMMENT_MAX:
        return _render_post(
            post, body, [f"Your comment should be between {COMMENT_MIN} and {COMMENT_MAX} characters."], 422
        )

    if not _is_admin():
        wait = fm.rate_limit_wait(ForumComment, current_user.user_id, fm.MAX_COMMENTS_PER_WINDOW, fm.COMMENT_WINDOW)
        if wait:
            return _render_post(post, body, [
                f"You've written a lot of comments in the last few minutes. Please take a short break and "
                f"try again in {fm.friendly_wait(wait)}."
            ], 429)

    result, status, flags, ai_verdict, ai_reason = _screen("comment", post.channel.name, "", body)
    if result.is_blocked:
        _log_blocked("comment", result, f'New comment on "{_short(post.title, 200)}"')
        return _render_post(post, body, result.messages, 422)

    comment = ForumComment(
        post_id=post.id, author_id=current_user.user_id, body=body,
        status=status, filter_flags=flags, ai_verdict=ai_verdict, ai_reason=ai_reason,
    )
    db.session.add(comment)
    db.session.commit()
    log_action(
        "forum_comment_created",
        details=f"Status: {status}. Flags: {flags or 'none'}. AI: {ai_verdict or 'not asked'}.",
        target=comment,
    )

    if status == STATUS_APPROVED:
        flash("Your comment is posted.", "success")
    else:
        flash("Thanks! Your comment will appear once a moderator approves it.", "info")
    return redirect(url_for("forum.post", post_id=post.id) + f"#comment-{comment.id}")


# ---------------------------------------------------------------------
# Helpful
# ---------------------------------------------------------------------
@forum_bp.route("/post/<int:post_id>/helpful", methods=["POST"])
@login_required
def helpful(post_id):
    post = _visible_post_or_404(post_id)

    def answer(ok, message=None, code=200):
        if _wants_json():
            return jsonify({"ok": ok, "marked": marked, "count": post.helpful_count, "message": message}), code
        if message:
            flash(message, "info" if ok else "warning")
        return redirect(url_for("forum.post", post_id=post.id))

    marked = False
    if not post.is_approved:
        return answer(False, "You can mark a post helpful once it is published.", 400)
    if post.author_id == current_user.user_id:
        return answer(False, "You can't mark your own post as helpful.", 400)

    # A toggle: pressing it again takes the mark back.
    existing = ForumHelpful.query.filter_by(user_id=current_user.user_id, post_id=post.id).first()
    if existing is not None:
        db.session.delete(existing)
    else:
        db.session.add(ForumHelpful(user_id=current_user.user_id, post_id=post.id))
        marked = True
    try:
        db.session.flush()
    except (IntegrityError, StaleDataError):
        # A double-click raced itself (two inserts, or two deletes of the
        # same row); the first click already did the work. Whatever
        # state it left is read back below.
        db.session.rollback()
        marked = ForumHelpful.query.filter_by(user_id=current_user.user_id, post_id=post.id).first() is not None
    # Recounted rather than incremented, so the number can never drift
    # from the rows it summarises.
    post.helpful_count = ForumHelpful.query.filter_by(post_id=post.id).count()
    db.session.commit()
    return answer(True)


# ---------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------
def _report(kind, item, post):
    back = url_for("forum.post", post_id=post.id)
    if not item.is_approved:
        abort(404)
    if item.author_id == current_user.user_id:
        flash("You can't report your own post or comment.", "info")
        return redirect(back)

    reason = request.form.get("reason") or ""
    note = " ".join((request.form.get("note") or "").split())[:255]
    if reason not in REPORT_REASONS:
        flash("Please choose a reason for the report.", "warning")
        return redirect(back)
    if reason == "other" and len(note) < MIN_REASON_LENGTH:
        flash("Please tell us briefly what's wrong so a moderator can check.", "warning")
        return redirect(back)

    if ForumReport.query.filter_by(reporter_id=current_user.user_id, content_type=kind,
                                   content_id=item.id).first() is not None:
        flash("You've already reported this. Thank you, a moderator will look at it.", "info")
        return redirect(back)

    db.session.add(ForumReport(reporter_id=current_user.user_id, content_type=kind, content_id=item.id,
                               reason=reason, note=note or None))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("You've already reported this. Thank you, a moderator will look at it.", "info")
        return redirect(back)

    why = REPORT_REASONS[reason] + (f": {note}" if note else "")
    log_action("forum_content_reported", details=f"Reported a {kind}.", target=item, reason=why)

    # Distinct reporters, by construction: the unique constraint allows
    # each member one report per item.
    if _unresolved_reports(kind, item.id).count() >= REPORTS_TO_HIDE:
        item.status = STATUS_PENDING
        flags = item.flag_list
        if "reported" not in flags:
            item.filter_flags = ",".join(flags + ["reported"])[:255]
        what = "post" if kind == "post" else "comment"
        _notify(item.author_id,
                f'Your community {what} "{_short(post.title)}" is hidden for now because several members '
                "reported it. A moderator will review it soon.")
        db.session.commit()
        flash("Thanks for letting us know. This has been hidden until a moderator reviews it.", "success")
        if kind == "post":
            return redirect(url_for("forum.index"))
        return redirect(back)

    flash("Thanks for letting us know. A moderator will take a look.", "success")
    return redirect(back)


@forum_bp.route("/post/<int:post_id>/report", methods=["POST"])
@login_required
def report_post(post_id):
    post = _visible_post_or_404(post_id)
    return _report("post", post, post)


@forum_bp.route("/comment/<int:comment_id>/report", methods=["POST"])
@login_required
def report_comment(comment_id):
    comment = db.session.get(ForumComment, comment_id)
    if comment is None or not comment.visible_to(current_user) or not comment.post.visible_to(current_user):
        abort(404)
    return _report("comment", comment, comment.post)


# ---------------------------------------------------------------------
# Moderation (Admin)
# ---------------------------------------------------------------------
@forum_bp.route("/moderation")
@role_required("Admin")
def moderation():
    tab = request.args.get("tab", "posts")
    if tab not in ("posts", "comments", "reported"):
        tab = "posts"

    open_reports = ForumReport.query.filter_by(resolved_at=None).order_by(ForumReport.created_at.asc()).all()
    reports_by_item = {}
    for report in open_reports:
        reports_by_item.setdefault((report.content_type, report.content_id), []).append(report)
    reported_post_ids = [cid for (ctype, cid) in reports_by_item if ctype == "post"]
    reported_comment_ids = [cid for (ctype, cid) in reports_by_item if ctype == "comment"]

    # A reported item appears under Reported only, with its reports next
    # to it -- listing it twice would invite two moderators to decide it
    # twice.
    pending_posts = ForumPost.query.filter(ForumPost.status == STATUS_PENDING)
    if reported_post_ids:
        pending_posts = pending_posts.filter(ForumPost.id.notin_(reported_post_ids))
    pending_comments = ForumComment.query.filter(ForumComment.status == STATUS_PENDING)
    if reported_comment_ids:
        pending_comments = pending_comments.filter(ForumComment.id.notin_(reported_comment_ids))

    live = (STATUS_APPROVED, STATUS_PENDING)
    reported_posts = (ForumPost.query.filter(ForumPost.id.in_(reported_post_ids), ForumPost.status.in_(live)).all()
                      if reported_post_ids else [])
    reported_comments = (ForumComment.query.filter(ForumComment.id.in_(reported_comment_ids),
                                                   ForumComment.status.in_(live)).all()
                         if reported_comment_ids else [])
    reported_items = sorted(
        [("post", p) for p in reported_posts] + [("comment", c) for c in reported_comments],
        key=lambda pair: -len(reports_by_item.get((pair[0], pair[1].id), [])),
    )

    from app.services.recommendation_service import llm_recommendations_enabled

    return render_template(
        "forum/moderation.html",
        tab=tab,
        pending_posts=pending_posts.order_by(ForumPost.created_at.asc()).all(),
        pending_comments=pending_comments.order_by(ForumComment.created_at.asc()).all(),
        reported_items=reported_items,
        reports_by_item=reports_by_item,
        ai_setting=fm.ai_setting_enabled(),
        llm_enabled=llm_recommendations_enabled(),
        reports_to_hide=REPORTS_TO_HIDE,
    )


_AUTHOR_MESSAGES = {
    ("post", "approve"): 'Good news! Your community post "{title}" was approved and is now visible to everyone.',
    ("post", "restore"): 'Your community post "{title}" was reviewed by a moderator and is visible again.',
    ("post", "reject"): 'Your community post "{title}" was not approved. Reason: {reason} '
                        "You're welcome to edit it and post again.",
    ("post", "remove"): 'Your community post "{title}" was removed by a moderator. Reason: {reason}',
    ("comment", "approve"): 'Your comment on "{title}" was approved and is now visible.',
    ("comment", "restore"): 'Your comment on "{title}" was reviewed by a moderator and is visible again.',
    ("comment", "reject"): 'Your comment on "{title}" was not approved. Reason: {reason}',
    ("comment", "remove"): 'Your comment on "{title}" was removed by a moderator. Reason: {reason}',
}


@forum_bp.route("/moderation/<kind>/<int:item_id>/<action>", methods=["POST"])
@role_required("Admin")
def moderate(kind, item_id, action):
    if kind not in ("post", "comment") or action not in ("approve", "reject", "remove"):
        abort(404)
    item = db.session.get(ForumPost if kind == "post" else ForumComment, item_id)
    if item is None:
        abort(404)

    default_tab = "reported" if _unresolved_reports(kind, item.id).count() else (
        "posts" if kind == "post" else "comments")
    back = _safe_next(url_for("forum.moderation", tab=default_tab))
    reason = _reason_from_form()
    thing = "post" if kind == "post" else "comment"

    if action in ("reject", "remove") and len(reason) < MIN_REASON_LENGTH:
        flash(f"Please give a reason (at least {MIN_REASON_LENGTH} characters). The author will see it.", "warning")
        return redirect(back)

    was = item.status
    had_reports = _unresolved_reports(kind, item.id).count() > 0

    if action == "approve":
        if was not in (STATUS_PENDING, STATUS_APPROVED) or (was == STATUS_APPROVED and not had_reports):
            flash(f"This {thing} has already been {was}.", "info")
            return redirect(back)
        new_status = STATUS_APPROVED
    elif action == "reject":
        if was != STATUS_PENDING:
            flash(f"Only a {thing} waiting for approval can be rejected. Use Remove for published ones.", "info")
            return redirect(back)
        new_status = STATUS_REJECTED
    else:
        if was not in (STATUS_PENDING, STATUS_APPROVED):
            flash(f"This {thing} has already been {was}.", "info")
            return redirect(back)
        new_status = STATUS_REMOVED

    item.status = new_status
    item.reviewed_by = current_user.user_id
    item.reviewed_at = datetime.utcnow()
    item.moderation_note = reason or None
    _resolve_reports(kind, item.id)

    # Which message the author gets. "restore" is the approve that
    # follows reports: to the author it is "your post is back", not
    # "your post was approved" for something that was up all along.
    post_title = item.title if kind == "post" else item.post.title
    template_key = action
    if action == "approve" and had_reports:
        template_key = "restore"
    notify = not (action == "approve" and was == STATUS_APPROVED)   # kept, never hidden: nothing to tell
    if notify:
        _notify(item.author_id, _AUTHOR_MESSAGES[(kind, template_key)].format(
            title=_short(post_title), reason=reason.rstrip(".") + "." if reason else ""))
    db.session.commit()

    done = {"approve": "approved", "reject": "rejected", "remove": "removed"}[action]
    # forum_post_approved, forum_comment_removed, ... -- see audit_labels.
    log_action(
        f"forum_{kind}_{done}",
        details=f"{thing.capitalize()} {was} -> {new_status}" + (" (after member reports)" if had_reports else ""),
        target=item,
        reason=reason or None,
    )

    flash(f"The {thing} was {done}." + (" The author has been notified." if notify else ""), "success")
    return redirect(back)


@forum_bp.route("/moderation/ai", methods=["POST"])
@role_required("Admin")
def moderation_ai():
    """The forum's AI switch lives on the moderation page, next to the
    queue it changes, rather than in System Settings -- the person
    deciding "should the AI approve posts?" is the one looking at the
    queue."""
    enabled = request.form.get("enabled") == "1"
    row = SystemSetting.set(
        fm.AI_SETTING_KEY, "true" if enabled else "false",
        description="true = the AI may publish clean community posts without waiting for a moderator",
    )
    log_action(
        "admin_update_settings",
        details=f"Community AI pre-screen turned {'on' if enabled else 'off'}",
        target=row,
    )
    flash("AI pre-screen turned on." if enabled else
          "AI pre-screen turned off. Every new post will wait for a moderator.", "success")
    return redirect(url_for("forum.moderation"))
