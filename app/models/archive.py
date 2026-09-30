"""
app/models/archive.py
-----------------------
ARCHIVE, NEVER DELETE.

The Admin module used to hard-delete user accounts and dataset rows.
Deleting a market_data or lgu_data row was worse than it looked:
forecast_result.market_id / lgu_id cascade on delete, so removing one
data row silently deleted every forecast that had ever been built on it
-- an SME's saved analysis could vanish because an administrator tidied
a table. And a deleted account takes its history with it, which is the
opposite of what an audit trail is for.

So nothing in the Admin module deletes any more. A row is ARCHIVED:
stamped with when, by whom and why, and hidden from everything that
reads live data. It can be restored exactly as it was.

TWO MIXINS, BECAUSE "ARCHIVED" MEANS TWO DIFFERENT THINGS

  ArchivableMixin      -- the three columns and the archive()/restore()
                          methods. Used by User, LguData and MarketData.

  HiddenWhenArchived   -- a marker. Any model carrying it has its
                          archived rows removed from EVERY ORM query,
                          automatically, by install_archive_filter().
                          Used by LguData and MarketData only.

User carries the first and deliberately not the second. An archived
account must stop being able to sign in and must drop out of the
active-user lists, but it must still RESOLVE: the audit trail names it,
uploaded datasets name it as their uploader, and a forum post still has
an author. Hiding archived users from every query would turn all of
those into "unknown user". So for users, "archived" is enforced where it
matters -- is_active and the admin list -- not globally.

WHY A GLOBAL FILTER RATHER THAN A WHERE CLAUSE AT EACH QUERY

market_data and lgu_data are read in well over twenty places: the
scoring engine's greatest-n-per-group lookups, the cross-source
reconciliation, trend analytics, the choropleth, the alert detector, the
cold-start check. An archived row that one of those forgot to exclude
would keep quietly influencing recommendations -- the exact silent
failure this project spends most of its tests guarding against. One
filter installed on the session covers every one of them, including
queries written after today.

The escape hatch is explicit and greppable: a query that genuinely needs
archived rows (the Admin datasets page, a restore) passes
.execution_options(include_archived=True).

Relationship loads are deliberately NOT filtered. A historical forecast
built on a row that has since been archived must still be able to show
the snapshot it was computed from; filtering its lazy load would turn
forecast.market_data into None and break the page that displays it.
"""

from datetime import datetime

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from app.extensions import db

INCLUDE_ARCHIVED = "include_archived"


class ArchivableMixin:
    """archived_at / archived_by / archive_reason, plus the two
    operations. archived_by is a plain integer rather than a foreign key
    so archiving an administrator's account can never be blocked by the
    rows that administrator archived."""

    archived_at = db.Column(db.DateTime, nullable=True)
    archived_by = db.Column(db.Integer, nullable=True)
    archive_reason = db.Column(db.String(255), nullable=True)

    @property
    def is_archived(self):
        return self.archived_at is not None

    def archive(self, by_user_id, reason):
        self.archived_at = datetime.utcnow()
        self.archived_by = by_user_id
        self.archive_reason = (reason or "").strip()[:255] or None

    def restore(self):
        self.archived_at = None
        self.archived_by = None
        self.archive_reason = None


class HiddenWhenArchived(ArchivableMixin):
    """Archivable AND invisible when archived: archived rows of any model
    carrying this are removed from ordinary ORM queries by
    install_archive_filter().

    It subclasses ArchivableMixin rather than being a bare marker because
    with_loader_criteria() evaluates its lambda against THIS class, so
    the class itself has to have an archived_at attribute. User carries
    ArchivableMixin alone, which is exactly what keeps it out of the
    global filter."""


_installed = False


def install_archive_filter():
    """Attach the filter once per process. Idempotent, because
    create_app() runs once per test as well as once per worker."""
    global _installed
    if _installed:
        return
    _installed = True

    @event.listens_for(Session, "do_orm_execute")
    def _hide_archived_rows(state):
        if not state.is_select:
            return
        # Lazy/relationship loads and deferred-column loads are left
        # alone -- see the module docstring for why a historical
        # forecast must still see the row it was built from.
        if state.is_relationship_load or state.is_column_load:
            return
        if state.execution_options.get(INCLUDE_ARCHIVED, False):
            return
        state.statement = state.statement.options(
            with_loader_criteria(
                HiddenWhenArchived,
                lambda cls: cls.archived_at.is_(None),
                include_aliases=True,
                # Without this the criteria are stored on every object the
                # query loads and re-applied to ITS lazy relationship loads
                # -- so a ForecastResult fetched normally would then load
                # forecast.market_data as None the moment that row was
                # archived. The is_relationship_load check above cannot
                # catch that case, because the option arrives already
                # attached to the lazy load. Found by
                # test_a_forecast_survives_its_market_and_lgu_rows_being_archived.
                propagate_to_loaders=False,
            )
        )


def get_including_archived(model, pk):
    """Primary-key lookup that can see an archived row -- for restore
    actions and the admin views. Returns None if no such row exists."""
    return db.session.get(model, pk, execution_options={INCLUDE_ARCHIVED: True})
