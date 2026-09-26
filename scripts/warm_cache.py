"""
scripts/warm_cache.py
----------------------
Builds the city-wide AI sweep once, up front, and writes it to disk so no
visitor ever pays for it.

Run it in a console on the host, once, right after deploying and after
any change to the market data:

    python scripts/warm_cache.py

WHY THIS MATTERS ON A CPU-METERED HOST
The Trend Reports page needs an AI score for every (industry, barangay)
pair it charts -- 500 Random Forest predictions. Measured cost: about 14
CPU-seconds. PythonAnywhere's free plan allows 100 CPU-seconds PER DAY,
so roughly five cold page loads would exhaust the day's allowance and the
site would be throttled into a slow queue.

The sweep is cached in two places (see the caching notes at the top of
app/services/trend_analytics_service.py): in the worker process, and on
disk keyed by a fingerprint of `market_data`. The disk copy survives
worker restarts, which is what makes the difference -- with it warm, a
full trend report costs about 0.4 CPU-seconds instead of 14.

This script just builds that disk copy deliberately instead of making the
first visitor do it. It is pure optimisation: the app works fine without
it, the first request simply pays the 14 seconds.

WHAT IT IS NOT
It is not a substitute for the model. Every number written here comes
from the same compute_scores() call the app uses at request time; this
runs it now rather than later. The file is keyed to the current
market_data, so if the data changes the app ignores the file and
recomputes -- there is no way for this to serve numbers that disagree
with the database.

Safe to run repeatedly. Run it again after importing new market data.
"""

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()


def main():
    from app import create_app
    from app.services import trend_analytics_service as trends

    # The production config validates SECRET_KEY, which is right for a
    # server and pointless here, so warm under the development config --
    # it reads the same DATABASE_URL and produces the same scores.
    app = create_app("development")

    with app.app_context():
        from app.models import MarketData

        market_rows = MarketData.query.count()
        print(f"  Database   : {app.config['SQLALCHEMY_DATABASE_URI'].split('@')[-1]}")
        print(f"  market_data: {market_rows} rows")

        if not market_rows:
            print(
                "\n  market_data is empty, so there is nothing meaningful to warm.\n"
                "  Import your data first -- DEPLOY_PYTHONANYWHERE.md step 4.\n"
            )
            return

        # Drop any existing file so this genuinely recomputes rather than
        # reporting a cache hit as work done.
        trends.clear_trend_caches()

        started_cpu, started_wall = time.process_time(), time.monotonic()
        rows = trends._sweep_baseline()
        cpu_used = time.process_time() - started_cpu
        wall = time.monotonic() - started_wall

        path = trends._sweep_disk_cache_path()
        if not path or not os.path.exists(path):
            print(
                "\n  WARNING: the sweep ran but no cache file was written. The instance/\n"
                "  folder may not be writable. The app still works -- every request just\n"
                "  recomputes the sweep, which on a CPU-metered host will be slow.\n"
            )
            return

        size_kb = os.path.getsize(path) / 1024
        print(f"  Swept      : {len(rows)} AI scores in {wall:.1f}s ({cpu_used:.1f} CPU-seconds)")
        print(f"  Written    : {path} ({size_kb:.0f} KB)")

        # Prove it reads back, rather than assuming.
        trends._SWEEP_CACHE.clear()
        verify_cpu = time.process_time()
        again = trends._sweep_baseline()
        verify_used = time.process_time() - verify_cpu

        if again == rows:
            print(f"  Verified   : re-read from disk in {verify_used:.2f} CPU-seconds, identical scores")
            print(f"\n  Saves about {cpu_used - verify_used:.0f} CPU-seconds on every worker restart.\n")
        else:
            print(
                "\n  WARNING: the re-read did not match what was computed. The cache file\n"
                "  has been left in place but something is wrong -- delete\n"
                f"  {path} and report this.\n"
            )


if __name__ == "__main__":
    main()
