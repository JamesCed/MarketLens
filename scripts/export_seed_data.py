"""
export_seed_data.py
----------------------
Exports the full 76-barangay reference/seed dataset (app/ml/seed_data.py
BARANGAY_PROFILES) to a CSV file -- every field this project actually
sourced or generated for each barangay, not just the subset the ML
model consumes (see app/ml/seed_data.py write_seed_data_csv() for
exactly what's included and where each column comes from).

Needs no database, no .env, and no Flask/MySQL packages installed --
app/ml/seed_data.py itself has zero dependencies. It's loaded here via
importlib straight from its file path (rather than a normal
`from app.ml.seed_data import ...`), which deliberately skips running
app/__init__.py (the Flask application factory, which DOES need
Flask/Flask-SQLAlchemy importable) -- so this script works even in an
environment that only has Python itself, no pip installs at all.

Run with:
    python export_seed_data.py                       (writes barangay_seed_data.csv here)
    python export_seed_data.py path/to/output.csv     (writes to a custom path)
"""

import importlib.util
import os
import sys

DEFAULT_OUTPUT = "barangay_seed_data.csv"


def _load_seed_data_module():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "ml", "seed_data.py")
    spec = importlib.util.spec_from_file_location("dss_seed_data", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run():
    destination = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUTPUT
    seed_data = _load_seed_data_module()
    row_count = seed_data.write_seed_data_csv(destination)
    print(f"Wrote {row_count} barangay rows to {destination}")


if __name__ == "__main__":
    run()
