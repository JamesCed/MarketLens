"""
app/models/industry_migration_log.py
---------------------------------------
Backup + audit trail for the one-time industry taxonomy migration (see
app/services/industry_migration.py).

WHY A TABLE AND NOT JUST A LOG LINE
That migration rewrites `industry_type` on real rows and DELETES the
micro-business rows outright. Neither is something to do to a real
database on faith. Every row it touches is copied here first --
including the full original row as JSON -- so the change is:

  - auditable: "show me exactly which rows were changed or removed, and
    what they were before" is one SELECT, which is the answer a capstone
    panel (or your own future self) will want; and
  - reversible: `industry_migration.undo()` reads this table back and
    restores every row it recorded.

This is an ADDITIVE table. It does not touch or alter any of the five
real tables from dss_db -- it only records what was done to them. It is
created on demand by the migration itself (CREATE TABLE IF NOT EXISTS
semantics via SQLAlchemy's checkfirst), so an existing install does not
need `seed.py` re-run to pick it up.
"""

from datetime import datetime

from app.extensions import db


class IndustryMigrationLog(db.Model):
    __tablename__ = "industry_migration_log"

    id = db.Column(db.Integer, primary_key=True)
    # Which real table the affected row lives in: "market_data",
    # "sme_profile" or "forecast_result".
    table_name = db.Column(db.String(64), nullable=False)
    row_id = db.Column(db.Integer, nullable=False)
    old_industry_type = db.Column(db.String(150), nullable=False)
    # NULL for a deleted row -- there is no "new" value for one.
    new_industry_type = db.Column(db.String(150))
    # "remapped", "deleted_micro", or "deleted_duplicate".
    action = db.Column(db.String(32), nullable=False)
    location = db.Column(db.String(150))
    # Full original row as JSON, so a deleted row can be put back
    # exactly as it was.
    row_json = db.Column(db.Text)
    migrated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<IndustryMigrationLog {self.action} {self.table_name}#{self.row_id} {self.old_industry_type}>"
