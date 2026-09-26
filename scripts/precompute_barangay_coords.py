"""
precompute_barangay_coords.py
--------------------------------
Small utility to look up a real coordinate for a location NAME that
ISN'T one of the 76 official Tarlac City barangays already shipped in
app/static/data/barangay_coords.json.

You should not normally need this script at all: every barangay the app
ships with (BARANGAY_NAMES in app/ml/seed_data.py) already has a real,
PhilAtlas-sourced coordinate baked into that JSON file, resolved once and
verified against Tarlac City's bounding box -- see that file's own
"_note"/"source" fields. This script exists only for the rare case of a
NEW location name (e.g. a future LGU dataset upload introduces a
barangay that isn't one of the current 76, or a typo needs checking).

    python precompute_barangay_coords.py "Some New Barangay"

WHERE THE COORDINATE COMES FROM (first success wins)
  1. Google Geocoding API      -- needs "Geocoding API" enabled
  2. Google Places API (New)   -- needs "Places API (New)" enabled
     Both use GOOGLE_PLACES_API_KEY / GOOGLE_GEOCODING_API_KEY from your
     .env, via app/services/geocoding_service.py -- the SAME code path
     the running app uses, so this script can't drift from it.

EVERY RESULT IS BOUNDS-CHECKED against Tarlac City's rectangle before
being accepted (geocoding_service._is_in_tarlac_city_bounds). A lookup
that lands somewhere else -- the wrong "San Isidro", a province
centroid -- is rejected rather than printed as a usable answer.

This does NOT write to app/static/data/barangay_coords.json (that file
is curated, sourced data, not something a script should silently
rewrite) -- it just prints the coordinate so you can add it there
yourself, with its own source note, the same way every other entry in
that file was added.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(
        description="Look up a real coordinate for a Tarlac City location name not already in "
        "app/static/data/barangay_coords.json."
    )
    parser.add_argument("name", help='Location name, e.g. "Some New Barangay"')
    args = parser.parse_args()

    from app import create_app
    from app.services import geocoding_service as geo

    app = create_app()
    with app.app_context():
        api_key = (app.config.get("GOOGLE_PLACES_API_KEY") or "").strip()
        geocoding_key = (app.config.get("GOOGLE_GEOCODING_API_KEY") or "").strip()

        seed = geo.load_seed_coords(app)
        if args.name in seed:
            point = seed[args.name]
            print(f'"{args.name}" is already shipped: {point["lat"]}, {point["lng"]}')
            return

        if not (api_key or geocoding_key):
            print("No GOOGLE_PLACES_API_KEY / GOOGLE_GEOCODING_API_KEY in .env -- nothing to look up with.")
            return

        coords, source, status = geo.geocode_barangay_verbose(args.name, api_key, geocoding_key=geocoding_key)
        if coords:
            print(f'"{args.name}": {coords["lat"]}, {coords["lng"]}  (source: {source})')
            print("Add this to app/static/data/barangay_coords.json's \"barangays\" object if you want it to stick.")
        else:
            print(f'"{args.name}": not resolved -- {status}')


if __name__ == "__main__":
    main()
