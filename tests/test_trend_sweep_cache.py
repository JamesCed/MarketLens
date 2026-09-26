"""
tests/test_trend_sweep_cache.py
----------------------------------
Covers the ON-DISK half of the trend cache -- the reason "Building
city-wide trend report..." is fast the second time.

WHY THIS FILE EXISTS SEPARATELY. Every other test in this project runs
against sqlite:///:memory:, and the disk cache deliberately refuses to
write for an in-memory database (a file that outlives the database it
describes can only ever be wrong). So the rest of the suite exercises
the in-process memo and never touches this code at all. These tests
build a real file-backed database so the disk layer actually runs.

What is pinned here:

  1. It is a CACHE OF THE MODEL, not a replacement for it. The rows
     reloaded from disk are the rows compute_scores() produced, with
     their numeric types intact -- the failure this guards against is
     json turning 73.4 into "73.4" and every average built on it
     silently breaking.
  2. It saves the work it claims to. A second process reading the file
     must not call compute_scores() at all.
  3. It never serves stale numbers. One new market_data row moves the
     fingerprint, and the file must be ignored.
  4. It never writes when the database is in memory.
"""

import glob
import os
import tempfile
from datetime import date

import pytest

from app import create_app
from app.config import TestingConfig
from app.extensions import db
from app.models import MarketData, SystemSetting
from app.ml.seed_data import BARANGAY_NAMES
from app.services import trend_analytics_service as trends


@pytest.fixture
def disk_app():
    """An app on a real SQLite FILE, so the disk cache is live, with its
    own instance/ directory so nothing lands in the project's.

    The URI is overridden on the config CLASS rather than through the
    TEST_DATABASE_URL environment variable, because that variable is
    read once when app/config.py is first imported -- long before any
    fixture runs -- so setting it here would silently have no effect and
    every test below would quietly pass against :memory:.
    """
    folder = tempfile.mkdtemp(prefix="dss_sweep_cache_")
    db_path = os.path.join(folder, "test.db")
    previous = TestingConfig.SQLALCHEMY_DATABASE_URI
    TestingConfig.SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
    try:
        app = create_app("testing")
        app.instance_path = os.path.join(folder, "instance")
        assert ":memory:" not in app.config["SQLALCHEMY_DATABASE_URI"]
        with app.app_context():
            db.create_all()
            SystemSetting.ensure_defaults()
            trends.clear_trend_caches()
            yield app
            db.session.remove()
            db.drop_all()
            trends.clear_trend_caches()
    finally:
        TestingConfig.SQLALCHEMY_DATABASE_URI = previous


def _seed(rows=6, count=120):
    for i, barangay in enumerate(BARANGAY_NAMES[:rows]):
        db.session.add(MarketData(
            industry_type="Food and Beverage", location=barangay, competitor_count=count + i,
            population_density=1000, historical_success_rate=0.5, foot_traffic_index=50,
            average_rent=20000, source="Google Places API", date_recorded=date.today(),
        ))
    db.session.commit()


def _cache_files(app):
    return glob.glob(os.path.join(app.instance_path, "trend_sweep_cache*"))


def test_the_sweep_is_written_to_disk(disk_app):
    with disk_app.app_context():
        _seed()
        assert _cache_files(disk_app) == [], "nothing should exist before the first sweep"
        trends._sweep_baseline("Food and Beverage")
        assert len(_cache_files(disk_app)) == 1, "the finished sweep should be on disk"


def test_a_second_process_reads_the_file_instead_of_recomputing(disk_app, monkeypatch):
    """The whole point. After a restart the in-process memo is gone; if
    the file is not read, the visitor pays the full ~14 CPU-second sweep
    again and the progress bar is back."""
    with disk_app.app_context():
        _seed()
        first = trends._sweep_baseline("Food and Beverage")

        # Simulate a fresh worker: the memo is empty, the file is not.
        trends.clear_trend_caches(drop_disk=False)
        assert len(_cache_files(disk_app)) == 1

        calls = []
        real = trends.compute_scores

        def counted(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(trends, "compute_scores", counted)
        second = trends._sweep_baseline("Food and Beverage")

        assert calls == [], "a warm disk cache must not re-run the model"
        assert second == first, "the reloaded sweep must be the same sweep"


def test_the_reloaded_numbers_are_still_numbers(disk_app):
    """The corruption this guards against is subtle: numpy floats are
    not JSON-writable, and the lazy fix (default=str) would reload every
    saturation_index as a STRING. The page would then average strings,
    or crash, long after the cache looked like it worked."""
    with disk_app.app_context():
        _seed()
        trends._sweep_baseline("Food and Beverage")
        trends.clear_trend_caches(drop_disk=False)
        rows = trends._sweep_baseline("Food and Beverage")

        assert rows, "the sweep should not be empty"
        for row in rows:
            assert isinstance(row["saturation_index"], (int, float))
            assert not isinstance(row["saturation_index"], bool)
            assert isinstance(row["viability_score"], (int, float))
            assert isinstance(row["confidence_level"], (int, float))
            assert isinstance(row["location"], str)


def test_one_new_market_row_invalidates_the_file(disk_app):
    """The fingerprint is the real invalidator, not the timer. If the
    data moves, the cached sweep must be ignored even though the file is
    seconds old."""
    with disk_app.app_context():
        _seed()
        trends._sweep_baseline("Food and Beverage")
        trends.clear_trend_caches(drop_disk=False)

        db.session.add(MarketData(
            industry_type="Food and Beverage", location=BARANGAY_NAMES[40],
            competitor_count=999, population_density=1000, historical_success_rate=0.5,
            foot_traffic_index=50, average_rent=20000, source="Google Places API",
            date_recorded=date.today(),
        ))
        db.session.commit()

        stale = trends._load_sweep_from_disk(("sweep", "Food and Beverage"), trends._data_fingerprint())
        assert stale is None, "a moved fingerprint must not match the file"


def test_a_corrupt_file_just_means_recompute(disk_app):
    """A truncated or hand-edited cache must never be an error page."""
    with disk_app.app_context():
        _seed()
        trends._sweep_baseline("Food and Beverage")
        path = _cache_files(disk_app)[0]
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json at all")

        trends.clear_trend_caches(drop_disk=False)
        rows = trends._sweep_baseline("Food and Beverage")
        assert rows, "a corrupt file should fall back to computing the sweep"


def test_clearing_the_caches_removes_the_file(disk_app):
    """After an explicit "forget everything" -- what the bulk Places
    refresh calls -- nothing cached may survive, because the caller is
    telling us the data changed in a way the fingerprint cannot see."""
    with disk_app.app_context():
        _seed()
        trends._sweep_baseline("Food and Beverage")
        assert _cache_files(disk_app)

        trends.clear_trend_caches()
        assert _cache_files(disk_app) == []


def test_an_in_memory_database_writes_nothing(tmp_path):
    """The rest of the suite runs this way. A file describing a database
    that dies with the process could only ever be wrong, so there must
    not be one -- and the project's instance/ must stay clean when the
    tests run."""
    previous = TestingConfig.SQLALCHEMY_DATABASE_URI
    TestingConfig.SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    try:
        app = create_app("testing")
        app.instance_path = str(tmp_path / "instance")
        with app.app_context():
            db.create_all()
            SystemSetting.ensure_defaults()
            trends.clear_trend_caches()
            assert trends._sweep_disk_cache_path() is None
            _seed(rows=3)
            trends._sweep_baseline("Food and Beverage")
            assert not os.path.isdir(app.instance_path) or not glob.glob(
                os.path.join(app.instance_path, "trend_sweep_cache*")
            )
            db.session.remove()
            db.drop_all()
            trends.clear_trend_caches()
    finally:
        TestingConfig.SQLALCHEMY_DATABASE_URI = previous
