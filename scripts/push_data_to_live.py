"""
scripts/push_data_to_live.py
-------------------------------
Copies the collected market data from your LOCAL MySQL straight into
the LIVE database, in one command.

    python scripts/push_data_to_live.py --target "PASTE_AIVEN_SERVICE_URI"

Paste Aiven's Service URI exactly as the console prints it, quotes and
all -- `mysql://`, `?ssl-mode=REQUIRED` and everything. It is repaired
on the way in by app/config.normalise_database_url(), the same function
the app itself uses.

WHY THIS EXISTS INSTEAD OF "just use Workbench"
MySQL Workbench's Export/Import wizards do work, but they make you get
four separate things right -- structure-vs-data-only, the target
schema, duplicate keys on rows the live database already has, and a
foreign key that cannot survive the trip (see uploaded_by below). Each
one fails differently and none of them fails clearly. This script knows
about all four.

WHAT IT COPIES

    market_data    the competitor counts, including every real Google
                   Places lookup you have collected. This is the table
                   that makes the live site show real figures instead
                   of simulated estimates.
    lgu_data       government data uploaded through the app.
    system_settings  ONLY with --include-settings. seed.py already
                   wrote the defaults on the live database, so this
                   just overwrites them with your tuned values.

WHAT IT DELIBERATELY DOES NOT COPY

    user, sme_profile, forecast_result, notifications, plan_saves,
    audit_logs.

Those hold your local test accounts and their password hashes, which
have no business on a public site, and forecasts that the live app
regenerates for itself from its own data anyway.

THE FOREIGN KEY THAT CANNOT TRAVEL
lgu_data.uploaded_by points at user.user_id, and the live database has
an entirely different user table -- your local user #3 is not the live
user #3, and probably is not anybody. Copying the number across would
either fail on the constraint or, worse, silently attribute an upload
to a stranger. So every copied lgu_data row is re-attributed to the
live system@dss.local account, which seed.py creates for exactly this
kind of ownerless data. The DATA is preserved; only the "who uploaded
it" pointer is rewritten, and this script says so when it does it.

SAFETY
It refuses to write into a table that already has rows unless you pass
--replace, so running it twice by accident cannot double your data.
--replace deletes the rows in the tables it is about to copy (only
those tables) and asks you to confirm first; pass --yes to skip the
prompt when you already know what you are doing.

    python scripts/push_data_to_live.py --target "..." --dry-run
    python scripts/push_data_to_live.py --target "..." --replace
    python scripts/push_data_to_live.py --target "..." --include-settings
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import create_engine, delete, func, insert, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import Config, normalise_database_url  # noqa: E402
from app.models import LguData, MarketData, SystemSetting  # noqa: E402
from app.models.user import User  # noqa: E402

SYSTEM_USER_EMAIL = "system@dss.local"
BATCH = 500


def _source_url(explicit):
    if explicit:
        return normalise_database_url(explicit)
    # Same five variables the app reads, so "it works locally" and
    # "this script can read it" are never out of step.
    return Config.SQLALCHEMY_DATABASE_URI


def _rows(session, model):
    columns = [c.name for c in model.__table__.columns]
    for row in session.execute(select(model.__table__)).mappings():
        yield {c: row[c] for c in columns}


def _count(session, model):
    return session.execute(select(func.count()).select_from(model.__table__)).scalar_one()


def _copy(model, src, dst, args, transform=None, label=None):
    label = label or model.__tablename__
    before = _count(dst, model)
    source_rows = list(_rows(src, model))

    print(f"\n{label}")
    print(f"  local : {len(source_rows):,} rows")
    print(f"  live  : {before:,} rows")

    if not source_rows:
        print("  -> nothing to copy.")
        return 0

    if before and not args.replace:
        print(f"  -> SKIPPED: the live table already has {before:,} rows.")
        print("     Re-run with --replace to delete those and copy yours in,")
        print("     or leave it alone if the data is already there.")
        return 0

    if args.dry_run:
        print(f"  -> would copy {len(source_rows):,} rows (dry run, nothing written).")
        return 0

    if before and args.replace:
        dst.execute(delete(model.__table__))
        print(f"  -> deleted {before:,} existing rows.")

    payload = [transform(dict(r)) for r in source_rows] if transform else source_rows
    payload = [p for p in payload if p is not None]

    for start in range(0, len(payload), BATCH):
        dst.execute(insert(model.__table__), payload[start:start + BATCH])
    dst.commit()

    after = _count(dst, model)
    print(f"  -> copied {len(payload):,}. Live now has {after:,} rows.")
    return len(payload)


def _upsert_settings(src, dst, args):
    print("\nsystem_settings")
    source_rows = list(_rows(src, SystemSetting))
    if args.dry_run:
        print(f"  -> would update/insert {len(source_rows)} settings (dry run).")
        return 0

    existing = {r["setting_key"] for r in _rows(dst, SystemSetting)}
    updated = inserted = 0
    for row in source_rows:
        if row["setting_key"] in existing:
            dst.execute(
                SystemSetting.__table__.update()
                .where(SystemSetting.__table__.c.setting_key == row["setting_key"])
                .values(setting_value=row["setting_value"])
            )
            updated += 1
        else:
            dst.execute(insert(SystemSetting.__table__), [row])
            inserted += 1
    dst.commit()
    print(f"  -> {updated} updated, {inserted} inserted.")
    return updated + inserted


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[3].strip())
    parser.add_argument("--target", required=True,
                        help="the LIVE database URL (paste Aiven's Service URI as-is)")
    parser.add_argument("--source", default=None,
                        help="the local database URL (default: the DB_* values from .env)")
    parser.add_argument("--replace", action="store_true",
                        help="delete rows already in the live tables before copying")
    parser.add_argument("--include-settings", action="store_true",
                        help="also push system_settings (overwrites the live defaults)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen and write nothing")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt for --replace")
    args = parser.parse_args()

    source_url = _source_url(args.source)
    target_url = normalise_database_url(args.target)

    def safe(url):
        """Never print a password, not even into a terminal someone
        might screenshot into a chat."""
        if "@" not in url:
            return url
        head, tail = url.rsplit("@", 1)
        if ":" in head:
            head = head[:head.rindex(":")] + ":****"
        return f"{head}@{tail}"

    print("FROM (local):", safe(source_url))
    print("TO   (live) :", safe(target_url))
    if args.dry_run:
        print("\n*** DRY RUN -- nothing will be written ***")

    if source_url == target_url:
        sys.exit("\nRefusing to run: source and target are the same database.")

    if args.replace and not args.dry_run and not args.yes:
        print("\n--replace will DELETE the rows already in the live market_data"
              "\nand lgu_data tables before copying yours in.")
        if input('Type "replace" to continue: ').strip().lower() != "replace":
            sys.exit("Cancelled. Nothing was written.")

    src_engine = create_engine(source_url, pool_pre_ping=True)
    dst_engine = create_engine(target_url, pool_pre_ping=True)

    with Session(src_engine) as src, Session(dst_engine) as dst:
        _copy(MarketData, src, dst, args,
              label="market_data  (competitor counts, incl. real Google Places lookups)")

        # See "THE FOREIGN KEY THAT CANNOT TRAVEL" at the top.
        system_user = dst.execute(
            select(User.__table__.c.user_id).where(User.__table__.c.email == SYSTEM_USER_EMAIL)
        ).scalar_one_or_none()

        if system_user is None:
            print("\nlgu_data\n  -> SKIPPED: the live database has no", SYSTEM_USER_EMAIL,
                  "account\n     to attribute the rows to. Run seed.py against it first.")
        else:
            def reattribute(row):
                row["uploaded_by"] = system_user
                return row

            _copy(LguData, src, dst, args, transform=reattribute,
                  label=f"lgu_data     (re-attributed to {SYSTEM_USER_EMAIL}, id {system_user})")

        if args.include_settings:
            _upsert_settings(src, dst, args)
        else:
            print("\nsystem_settings\n  -> skipped (pass --include-settings to push your tuned values).")

        print("\nLive totals now:")
        for model in (MarketData, LguData):
            print(f"  {model.__tablename__:<16} {_count(dst, model):,} rows")

        real = dst.execute(
            select(func.count()).select_from(MarketData.__table__)
            .where(MarketData.__table__.c.source == "Google Places API")
        ).scalar_one()
        print(f"  of which real Google Places rows: {real:,}")

    print("\nDone." if not args.dry_run else "\nDry run complete -- nothing was written.")


if __name__ == "__main__":
    main()
