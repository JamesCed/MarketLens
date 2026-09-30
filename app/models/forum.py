"""
app/models/forum.py
---------------------
Community forum tables: channels, posts, comments, reports and the
one-per-member "Helpful" mark. ADDITIVE -- they only reference
user.user_id and each other, and change nothing in the given schema.
startup_migrations creates them on an existing database at boot.

STATUS IS A PLAIN STRING, NOT AN ENUM, for the same reason User.theme
is: MySQL bakes an ENUM's values into the column definition, so adding
a state later would need an ALTER TABLE on a live database. The four
values in use are:

    pending   waiting for a moderator (or hidden again after reports)
    approved  visible to every signed-in member
    rejected  a moderator declined it before it was ever shown
    removed   it WAS shown, and a moderator took it down

rejected and removed are kept apart because they mean different things
to the author -- "this never went up" versus "this was up and was taken
down" -- and the moderation history should say which happened.

NOTHING IS EVER DELETED. A rejected or removed row stays, with who
decided, when and why. That is the record a moderator needs when the
same account does it again, and the one an author is owed if they ask
why their post disappeared.
"""

from datetime import datetime

from app.extensions import db

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_REMOVED = "removed"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_REMOVED)

# The short list a member picks from when reporting. Short on purpose:
# a non-technical user should recognise their reason at a glance, and
# "Other" with a note covers everything else.
REPORT_REASONS = {
    "spam": "Spam or scam",
    "offensive": "Offensive or harassment",
    "false_info": "False information",
    "off_topic": "Off-topic",
    "other": "Other",
}


class ForumChannel(db.Model):
    """A topic, like a subreddit -- but only a handful of them, created
    by the app rather than by members, so nobody has to decide where a
    question belongs among dozens."""

    __tablename__ = "forum_channels"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(60), unique=True, nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(255))
    # A Bootstrap Icons name, e.g. "cup-hot".
    icon = db.Column(db.String(40), nullable=False, default="chat-dots")
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<ForumChannel {self.slug}>"


class _ModeratedMixin:
    """Status and the moderation record, shared by posts and comments so
    the moderation page can treat both the same way."""

    status = db.Column(db.String(20), nullable=False, default=STATUS_PENDING, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    reviewed_at = db.Column(db.DateTime, nullable=True)
    # The moderator's reason, shown to the author when content is
    # rejected or removed.
    moderation_note = db.Column(db.String(255), nullable=True)
    # Comma-separated identifiers from forum_moderation.FLAG_LABELS.
    filter_flags = db.Column(db.String(255), nullable=True)
    # approve | review | reject, or NULL when the AI was not asked.
    ai_verdict = db.Column(db.String(20), nullable=True)
    ai_reason = db.Column(db.String(255), nullable=True)

    @property
    def is_approved(self):
        return self.status == STATUS_APPROVED

    @property
    def is_pending(self):
        return self.status == STATUS_PENDING

    @property
    def flag_list(self):
        return [f for f in (self.filter_flags or "").split(",") if f]

    def visible_to(self, user):
        """Approved content is for everyone signed in; anything else only
        for its author and for administrators."""
        if self.status == STATUS_APPROVED:
            return True
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        return user.user_id == self.author_id or user.is_admin()


class ForumPost(_ModeratedMixin, db.Model):
    __tablename__ = "forum_posts"

    id = db.Column(db.Integer, primary_key=True)
    channel_id = db.Column(db.Integer, db.ForeignKey("forum_channels.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    body = db.Column(db.Text, nullable=False)
    # Denormalised count of ForumHelpful rows, so the feed can show it
    # without a query per post. Only ever changed together with the
    # rows it counts (see forum_controller.helpful).
    helpful_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    reviewed_by = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="SET NULL"), nullable=True)

    channel = db.relationship("ForumChannel", lazy="joined")
    author = db.relationship("User", foreign_keys=[author_id], lazy="joined")
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
    comments = db.relationship("ForumComment", back_populates="post", lazy="dynamic",
                               cascade="all, delete-orphan")

    def __repr__(self):
        return f"<ForumPost {self.id} {self.status}>"


class ForumComment(_ModeratedMixin, db.Model):
    __tablename__ = "forum_comments"

    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("forum_posts.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="CASCADE"), nullable=False, index=True)
    body = db.Column(db.Text, nullable=False)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="SET NULL"), nullable=True)

    post = db.relationship("ForumPost", back_populates="comments")
    author = db.relationship("User", foreign_keys=[author_id], lazy="joined")
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])

    @property
    def title(self):
        """What the audit trail shows as this comment's label. A comment
        has no title of its own, and its text is the one thing that
        should not be copied into a permanent log."""
        post_title = self.post.title if self.post is not None else "a post"
        return f'Comment on "{post_title}"'

    def __repr__(self):
        return f"<ForumComment {self.id} {self.status}>"


class ForumReport(db.Model):
    """One member's report of one post or comment.

    The target is (content_type, content_id) rather than two nullable
    foreign keys, because a UNIQUE constraint over nullable columns does
    not stop duplicates in MySQL or SQLite -- NULL never equals NULL --
    and "one report per member per item" is exactly what has to hold.
    Content is never deleted (see the module docstring), so the missing
    foreign key cannot leave a report pointing at nothing.
    """

    __tablename__ = "forum_reports"
    __table_args__ = (
        db.UniqueConstraint("reporter_id", "content_type", "content_id", name="uq_forum_report_once"),
    )

    id = db.Column(db.Integer, primary_key=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="CASCADE"), nullable=False)
    content_type = db.Column(db.String(10), nullable=False)   # "post" | "comment"
    content_id = db.Column(db.Integer, nullable=False, index=True)
    reason = db.Column(db.String(20), nullable=False)
    note = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Set when a moderator acts on the item. Resolved reports stop
    # counting towards the auto-hide threshold, so content a moderator
    # has looked at and kept is not hidden again by the same pile.
    resolved_at = db.Column(db.DateTime, nullable=True)
    resolved_by = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="SET NULL"), nullable=True)

    reporter = db.relationship("User", foreign_keys=[reporter_id], lazy="joined")

    @property
    def reason_label(self):
        return REPORT_REASONS.get(self.reason, self.reason)


class ForumHelpful(db.Model):
    """A member marking a post as helpful. One per member per post --
    the only "voting" the forum has, and deliberately a single positive
    signal: there is no downvote to pile on with."""

    __tablename__ = "forum_helpful"
    __table_args__ = (db.UniqueConstraint("user_id", "post_id", name="uq_forum_helpful_once"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.user_id", ondelete="CASCADE"), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey("forum_posts.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# The channels a fresh install starts with: (slug, name, description, icon).
DEFAULT_CHANNELS = [
    ("general", "General Discussion",
     "Say hello and talk about anything to do with running a business in Tarlac City.", "chat-dots"),
    ("food-beverage", "Food & Beverage",
     "Carinderias, cafés, bakeries, food carts and catering.", "cup-hot"),
    ("retail-trade", "Retail & Trade",
     "Sari-sari stores, shops, wholesale and finding suppliers.", "shop"),
    ("services", "Services",
     "Salons, repair shops, laundry, printing and other services.", "tools"),
    ("permits-government", "Permits & Government",
     "Business permits, DTI, BIR, barangay clearances and LGU programs.", "bank"),
    ("tips-success", "Tips & Success Stories",
     "Share what worked for your business so others can learn from it.", "trophy"),
    ("ask-community", "Ask the Community",
     "Not sure about something? Ask here. No question is too small.", "question-circle"),
]


def ensure_default_channels():
    """Create the default channels if the table is empty. Called lazily
    by the forum pages, so an existing database gets them on the first
    visit without a seed step. Returns True if it created any."""
    if db.session.query(ForumChannel.id).first() is not None:
        return False
    for order, (slug, name, description, icon) in enumerate(DEFAULT_CHANNELS):
        db.session.add(ForumChannel(slug=slug, name=name, description=description, icon=icon, sort_order=order))
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001 -- two workers seeding at once; the other one won
        db.session.rollback()
        return False
    return True
