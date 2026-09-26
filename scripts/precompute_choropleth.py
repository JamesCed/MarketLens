"""
scripts/precompute_choropleth.py
-----------------------------------
Builds the Saturation Map's barangay cells once and writes them to
app/static/data/barangay_cells.json, which IS committed to the repo.

    python scripts/precompute_choropleth.py

WHY THIS IS A BUILD STEP AND NOT A REQUEST
The cells are a pure function of the 76 barangay coordinates in
app/static/data/barangay_coords.json, and that file is static and
committed. The output therefore never varies -- every deployment was
computing the identical answer from the identical input.

That is not merely wasteful. The computation rasterizes a 900x720 grid
and is the single largest thing this app ever does; measured peak
resident memory was 880 MB before the nearest-neighbour step was
chunked, and 186 MB after. A free Render instance has 512 MB and the
app already holds about 210 MB. So the work sat close enough to the
ceiling to fail there, and when it failed the map drew Tarlac City's
outline with nothing inside it -- with no error the visitor could see,
because the front end treats a failed fetch as "no cells".

Reading a committed file cannot fail that way.

WHEN TO RE-RUN IT
Only when the geometry's inputs change:

  * app/static/data/barangay_coords.json  (a coordinate corrected, a
    barangay added)
  * app/static/data/tarlac_city_boundary.json  (a new city outline)
  * the cell algorithm itself in app/services/choropleth_service.py

You do not need to re-run it for new market data, a new industry, or
anything a user does -- none of that touches the geometry. The file
records the fingerprint of the coordinates it was built from, and the
app recomputes from scratch if the two ever disagree, so forgetting to
re-run it makes the app slower, never wrong.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app import create_app  # noqa: E402
from app.services.choropleth_service import (  # noqa: E402
    PRECOMPUTED_CELLS_NAME,
    _fingerprint,
    compute_choropleth_geojson,
)
from app.services.geocoding_service import load_seed_coords  # noqa: E402


def main():
    app = create_app(os.environ.get("FLASK_CONFIG", "development"))
    with app.app_context():
        coords = load_seed_coords(app)
        if not coords:
            sys.exit("No barangay coordinates found -- is app/static/data/barangay_coords.json there?")

        print(f"Barangays          : {len(coords)}")
        print("Computing cells... (this is the slow part, once)")

        started = time.time()
        # force=True so it never reads back the file it is about to write.
        geojson = compute_choropleth_geojson(coords, app=app, force=True)
        elapsed = time.time() - started

        payload = {
            "_comment": (
                "GENERATED FILE -- do not hand-edit. Rebuild with "
                "python scripts/precompute_choropleth.py. See that script for when to."
            ),
            "fingerprint": _fingerprint(coords),
            "barangay_count": len(coords),
            "geojson": geojson,
        }

        path = os.path.join(app.root_path, "static", "data", PRECOMPUTED_CELLS_NAME)
        temp = f"{path}.tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        os.replace(temp, path)

        size_kb = os.path.getsize(path) / 1024
        print(f"Cells built        : {len(geojson['features'])} in {elapsed:.1f}s")
        print(f"Fingerprint        : {payload['fingerprint']}")
        print(f"Written            : {path} ({size_kb:.0f} KB)")
        print("\nCommit this file. The app will now serve it instead of recomputing.")


if __name__ == "__main__":
    main()
