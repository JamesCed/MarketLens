"""
app/ml/seed_data.py
---------------------
Reference / demo data for all 76 barangays of Tarlac City.

IMPORTANT -- READ THIS BEFORE YOUR DEFENSE:
Your dss_db schema has no barangays table -- `location`/`barangay` are
just free-text columns on sme_profile/market_data/lgu_data. This file
exists only to (a) give the AI training script (train_model.py)
realistic-looking variation to learn from, (b) give seed.py a starting
set of example market_data/lgu_data rows so the app isn't completely
empty on first run, and (c) populate the location `<datalist>`
suggestions and the SME dashboard search bar.

WHAT IS REAL vs. ESTIMATED in the table below (full detail, including
every source URL and every disagreement found between sources, is in
Reference/DATASETS.md -- read that file, this is the short version):

  - name                REAL. All 76 official barangays of Tarlac City,
                        per the PSA's Philippine Standard Geographic
                        Code (PSGC).
  - population_density  REAL for 73/76 barangays, a dataset-average
                        ESTIMATE for the other 3. This is now a TRUE
                        population density (people/km2) = 2024 PSA
                        population divided by each barangay's actual
                        land area in hectares, as published on that
                        barangay's own page on Tarlac City's official
                        government website (tarlaccity.gov.ph). Land
                        area could not be found for Matatalaib, San
                        Isidro, and San Miguel (page unreachable) --
                        those 3 use the dataset's average land area
                        (367.61 ha) as a stand-in, flagged inline below.
  - urban                REAL, but genuinely CONTESTED -- see below.
  - foot_traffic_index, average_rent, business_density,
    historical_success_rate
                        ESTIMATED / SYNTHETIC. No public dataset (PSA's
                        Census of Philippine Business and Industry is
                        public only at REGIONAL granularity; barangay-
                        level business/rent data isn't published
                        anywhere -- see Reference/DATASETS.md) has real
                        per-barangay figures for these. Each is derived
                        with a simple, deterministic formula from the
                        real population_density + urban flag above, just
                        so the AI model has plausible, internally-
                        consistent variation to train on.

THE URBAN/RURAL COLUMN IS GENUINELY CONTESTED -- PLEASE READ:
Three different classification attempts were made for this project and
they do NOT agree with each other:
  1. PSA/PSGC's own official classification (census density/activity
     criteria): 36 urban / 40 rural. Complete for all 76, single
     coherent source, no missing data. THIS is what `urban` below uses.
  2. Tarlac City's own government website's individual barangay pages
     each carry a classification sentence. Taken at face value: only
     16 barangays are explicitly confirmed "Urban Barangay" (2 more --
     San Miguel, San Pascual -- couldn't be confirmed either way). This
     is the closest match to your "19 urban" figure of anything
     verifiable online, but it is NOT a clean match, and the site's own
     text is inconsistent (many pages open with boilerplate "classified
     as Rural Barangay" and then have a separate, later sentence for
     the true urban ones reading "one of the 19 barangays on Tarlac
     City classified as an urban barangay" -- both sentences appear
     verbatim on Maligaya's and Paraiso's pages, for example).
  3. The Comprehensive Land Use Plan (CLUP) figure you originally gave
     me (19 urban / 57 rural) -- I could not locate an official CLUP
     document hosted on tarlaccity.gov.ph or any other .gov.ph domain
     to verify this against a primary source directly.
  PSA vs. the city's own website disagree on 23 of the 76 barangays.
  I've shipped PSA's classification below because it's the one
  complete, single-source, internally-consistent option -- but it is
  NOT the "19 urban" figure you asked for. If you have your own copy of
  Tarlac City's CLUP, or can get the City Planning & Development
  Office's definitive current list, that should override this column.
  See Reference/DATASETS.md for the full 76-row comparison table.

Sources:
  - PSA PSGC, Barangays of Tarlac City (2024 population + PSA's own
    Urban/Rural classification): psa.gov.ph
  - Tarlac City official government website, individual barangay
    profile pages (land area in hectares, and each page's own stated
    classification): tarlaccity.gov.ph
"""

# name, population_density (REAL people/km2 for 73/76 -- see docstring),
# foot_traffic_index (0-100, ESTIMATED), average_rent (PHP/month, ESTIMATED),
# business_density (LGU-style density figure, ESTIMATED),
# historical_success_rate (0-1, ESTIMATED), urban(True)/rural(False) per PSA
BARANGAY_PROFILES = [
    ("Aguso", 2123.2, 30, 17500, 2.7, 0.72, True),  # pop=8493 (PSA 2024), land=400.00 ha (tarlaccity.gov.ph)
    ("Alvindia Segundo", 1089.8, 10, 6500, 0.9, 0.78, False),  # pop=1960 (PSA 2024), land=179.85 ha (tarlaccity.gov.ph)
    ("Amucao", 863.8, 9, 6000, 0.8, 0.79, False),  # pop=2696 (PSA 2024), land=312.12 ha (tarlaccity.gov.ph)
    ("Armenia", 425.5, 21, 15500, 1.7, 0.75, True),  # pop=5852 (PSA 2024), land=1375.27 ha (tarlaccity.gov.ph)
    ("Asturias", 624.3, 7, 5500, 0.6, 0.79, False),  # pop=2037 (PSA 2024), land=326.27 ha (tarlaccity.gov.ph)
    ("Atioc", 2314.8, 16, 8000, 1.7, 0.76, False),  # pop=2712 (PSA 2024), land=117.16 ha (tarlaccity.gov.ph)
    ("Balanti", 229.6, 5, 5000, 0.4, 0.80, False),  # pop=2012 (PSA 2024), land=876.18 ha (tarlaccity.gov.ph)
    ("Balete", 990.1, 24, 16000, 2.0, 0.74, True),  # pop=5756 (PSA 2024), land=581.36 ha (tarlaccity.gov.ph)
    ("Balibago I", 906.7, 9, 6000, 0.8, 0.79, False),  # pop=2148 (PSA 2024), land=236.90 ha (tarlaccity.gov.ph)
    ("Balibago II", 934.8, 9, 6000, 0.8, 0.79, False),  # pop=4212 (PSA 2024), land=450.56 ha (tarlaccity.gov.ph)
    ("Balingcanaway", 1718.6, 28, 17000, 2.5, 0.72, True),  # pop=6886 (PSA 2024), land=400.68 ha (tarlaccity.gov.ph)
    ("Banaba", 580.5, 7, 5500, 0.6, 0.79, False),  # pop=1314 (PSA 2024), land=226.37 ha (tarlaccity.gov.ph)
    ("Bantog", 479.5, 7, 5500, 0.5, 0.79, False),  # pop=2777 (PSA 2024), land=579.09 ha (tarlaccity.gov.ph)
    ("Baras-baras", 1752.3, 28, 17000, 2.5, 0.72, True),  # pop=5736 (PSA 2024), land=327.35 ha (tarlaccity.gov.ph)
    ("Batang-batang", 374.6, 6, 5500, 0.4, 0.80, False),  # pop=2341 (PSA 2024), land=624.90 ha (tarlaccity.gov.ph)
    ("Binauganan", 3388.3, 36, 19500, 3.5, 0.69, True),  # pop=6163 (PSA 2024), land=181.89 ha (tarlaccity.gov.ph)
    ("Bora", 1114.9, 10, 6500, 0.9, 0.78, False),  # pop=2134 (PSA 2024), land=191.40 ha (tarlaccity.gov.ph)
    ("Buenavista", 322.3, 6, 5500, 0.4, 0.80, False),  # pop=1593 (PSA 2024), land=494.24 ha (tarlaccity.gov.ph)
    ("Buhilit", 886.8, 9, 6000, 0.8, 0.79, False),  # pop=2209 (PSA 2024), land=249.10 ha (tarlaccity.gov.ph)
    ("Burot", 1780.6, 28, 17500, 2.5, 0.72, True),  # pop=8367 (PSA 2024), land=469.90 ha (tarlaccity.gov.ph)
    ("Calingcuan", 3796.4, 23, 10000, 2.6, 0.74, False),  # pop=3848 (PSA 2024), land=101.36 ha (tarlaccity.gov.ph)
    ("Capehan", 959.0, 9, 6000, 0.8, 0.79, False),  # pop=2339 (PSA 2024), land=243.91 ha (tarlaccity.gov.ph)
    ("Carangian", 7816.6, 57, 25500, 6.3, 0.62, True),  # pop=10443 (PSA 2024), land=133.60 ha (tarlaccity.gov.ph)
    ("Care", 618.7, 22, 15500, 1.8, 0.74, True),  # pop=5379 (PSA 2024), land=869.46 ha (tarlaccity.gov.ph)
    ("Central", 579.2, 22, 15500, 1.8, 0.74, True),  # pop=4131 (PSA 2024), land=713.19 ha (tarlaccity.gov.ph)
    ("Culipat", 1019.8, 9, 6000, 0.8, 0.78, False),  # pop=2943 (PSA 2024), land=288.60 ha (tarlaccity.gov.ph)
    ("Cut-cut I", 7591.1, 56, 25500, 6.1, 0.62, True),  # pop=479 (PSA 2024), land=6.31 ha (tarlaccity.gov.ph)
    ("Cut-cut II", 1382.6, 26, 16500, 2.3, 0.73, True),  # pop=8554 (PSA 2024), land=618.67 ha (tarlaccity.gov.ph)
    ("Dalayap", 1171.9, 10, 6500, 0.9, 0.78, False),  # pop=4057 (PSA 2024), land=346.18 ha (tarlaccity.gov.ph)
    ("Dela Paz", 1300.5, 11, 6500, 1.0, 0.78, False),  # pop=3762 (PSA 2024), land=289.27 ha (tarlaccity.gov.ph)
    ("Dolores", 596.4, 7, 5500, 0.6, 0.79, False),  # pop=3984 (PSA 2024), land=667.99 ha (tarlaccity.gov.ph)
    ("Laoang", 540.6, 7, 5500, 0.5, 0.79, False),  # pop=3487 (PSA 2024), land=645.02 ha (tarlaccity.gov.ph)
    ("Ligtasan", 5244.0, 45, 22000, 4.7, 0.66, True),  # pop=2257 (PSA 2024), land=43.04 ha (tarlaccity.gov.ph)
    ("Lourdes", 439.2, 6, 5500, 0.5, 0.79, False),  # pop=2934 (PSA 2024), land=667.99 ha (tarlaccity.gov.ph) -- NOTE: the source page shows the exact same land-area figure as Dolores; likely a copy/paste error on the LGU's own page, flagged for you to spot-check
    ("Mabini", 6413.7, 50, 23500, 5.4, 0.64, True),  # pop=769 (PSA 2024), land=11.99 ha (tarlaccity.gov.ph)
    ("Maligaya", 14623.5, 90, 35000, 10.5, 0.50, True),  # pop=4409 (PSA 2024), land=30.15 ha (tarlaccity.gov.ph)
    ("Maliwalo", 2445.8, 31, 18000, 2.9, 0.71, True),  # pop=17606 (PSA 2024), land=719.84 ha (tarlaccity.gov.ph)
    ("Mapalacsiao", 3152.9, 35, 19000, 3.4, 0.70, True),  # pop=5960 (PSA 2024), land=189.03 ha (tarlaccity.gov.ph)
    ("Mapalad", 138.9, 5, 5000, 0.3, 0.80, False),  # pop=750 (PSA 2024), land=540.01 ha (tarlaccity.gov.ph)
    ("Matadero", 5394.5, 45, 22500, 4.8, 0.66, True),  # pop=2099 (PSA 2024), land=38.91 ha (tarlaccity.gov.ph). Officially also known as "San Juan Bautista" -- see Reference/DATASETS.md re: the San Juan de Bautista / San Juan de Mata confusion in an earlier version of this file.
    ("Matatalaib", 7006.7, 53, 24500, 5.8, 0.63, True),  # pop=25757 (PSA 2024), land=~367.61 ha ESTIMATED (dataset average -- real figure not published on the barangay's page)
    ("Paraiso", 12913.1, 67, 22500, 8.2, 0.58, False),  # pop=3431 (PSA 2024), land=26.57 ha (tarlaccity.gov.ph). PSA says Rural; the barangay's own page explicitly says "one of the 19 barangays ... classified as an urban barangay" -- one of several PSA-vs-LGU-page disagreements, see Reference/DATASETS.md
    ("Poblacion", 1751.5, 28, 17000, 2.5, 0.72, True),  # pop=344 (PSA 2024), land=19.64 ha (tarlaccity.gov.ph)
    ("Salapungan", 2819.2, 18, 8500, 2.0, 0.75, False),  # pop=2104 (PSA 2024), land=74.63 ha (tarlaccity.gov.ph)
    ("San Carlos", 888.8, 9, 6000, 0.8, 0.79, False),  # pop=1824 (PSA 2024), land=205.23 ha (tarlaccity.gov.ph)
    ("San Francisco", 1026.3, 9, 6000, 0.9, 0.78, False),  # pop=2929 (PSA 2024), land=285.40 ha (tarlaccity.gov.ph)
    ("San Isidro", 4029.3, 39, 20500, 3.9, 0.68, True),  # pop=14812 (PSA 2024), land=~367.61 ha ESTIMATED (dataset average -- the barangay's page gives no total land-area figure)
    ("San Jose", 2021.8, 29, 17500, 2.7, 0.72, True),  # pop=10254 (PSA 2024), land=507.16 ha (tarlaccity.gov.ph)
    ("San Jose de Urquico", 1201.3, 10, 6500, 1.0, 0.78, False),  # pop=2729 (PSA 2024), land=227.17 ha (tarlaccity.gov.ph)
    ("San Juan de Mata", 476.3, 7, 5500, 0.5, 0.79, False),  # pop=4727 (PSA 2024), land=992.54 ha (tarlaccity.gov.ph) -- a real, DIFFERENT barangay from "Matadero" (a.k.a. San Juan Bautista) above; see Reference/DATASETS.md
    ("San Luis", 1726.2, 13, 7000, 1.3, 0.77, False),  # pop=4363 (PSA 2024), land=252.75 ha (tarlaccity.gov.ph)
    ("San Manuel", 1191.9, 25, 16500, 2.2, 0.73, True),  # pop=8455 (PSA 2024), land=709.38 ha (tarlaccity.gov.ph)
    ("San Miguel", 2411.6, 31, 18000, 2.9, 0.71, True),  # pop=8865 (PSA 2024), land=~367.61 ha ESTIMATED (dataset average -- this barangay's page could not be fetched at all, persistent server error)
    ("San Nicolas", 7192.8, 54, 24500, 5.9, 0.63, True),  # pop=6936 (PSA 2024), land=96.43 ha (tarlaccity.gov.ph)
    ("San Pablo", 10671.0, 71, 29500, 8.0, 0.57, True),  # pop=4755 (PSA 2024), land=44.56 ha (tarlaccity.gov.ph)
    ("San Pascual", 1480.2, 11, 7000, 1.1, 0.78, False),  # pop=3844 (PSA 2024), land=259.70 ha (tarlaccity.gov.ph); the barangay's own page has no classification sentence at all
    ("San Rafael", 4851.0, 43, 21500, 4.4, 0.67, True),  # pop=22111 (PSA 2024), land=455.80 ha (tarlaccity.gov.ph)
    ("San Roque", 5995.2, 48, 23000, 5.1, 0.65, True),  # pop=5786 (PSA 2024), land=96.51 ha (tarlaccity.gov.ph)
    ("San Sebastian", 5858.6, 48, 23000, 5.1, 0.65, True),  # pop=4292 (PSA 2024), land=73.26 ha (tarlaccity.gov.ph)
    ("San Vicente", 6841.9, 52, 24500, 5.7, 0.63, True),  # pop=18473 (PSA 2024), land=270.00 ha (tarlaccity.gov.ph, stated as "approximately")
    ("Santa Cruz", 1288.1, 11, 6500, 1.0, 0.78, False),  # pop=4463 (PSA 2024), land=346.49 ha (tarlaccity.gov.ph)
    ("Santa Maria", 809.8, 8, 6000, 0.7, 0.79, False),  # pop=1289 (PSA 2024), land=159.18 ha (tarlaccity.gov.ph)
    ("Santo Cristo", 7667.1, 56, 25500, 6.2, 0.62, True),  # pop=2902 (PSA 2024), land=37.85 ha (tarlaccity.gov.ph)
    ("Santo Domingo", 492.0, 7, 5500, 0.5, 0.79, False),  # pop=1357 (PSA 2024), land=275.79 ha (tarlaccity.gov.ph)
    ("Santo Nino", 308.8, 6, 5000, 0.4, 0.80, False),  # pop=867 (PSA 2024), land=280.77 ha (tarlaccity.gov.ph)
    ("Sapang Maragul", 913.2, 24, 16000, 2.0, 0.74, True),  # pop=13287 (PSA 2024), land=1455.00 ha (tarlaccity.gov.ph)
    ("Sapang Tagalog", 1552.9, 27, 17000, 2.4, 0.73, True),  # pop=4960 (PSA 2024), land=319.41 ha (tarlaccity.gov.ph)
    ("Sepung Calzada", 11695.9, 76, 31000, 8.7, 0.55, True),  # pop=4869 (PSA 2024), land=41.63 ha (tarlaccity.gov.ph)
    ("Sinait", 601.1, 7, 5500, 0.6, 0.79, False),  # pop=2547 (PSA 2024), land=423.73 ha (tarlaccity.gov.ph)
    ("Suizo", 2096.5, 14, 7500, 1.5, 0.77, False),  # pop=3914 (PSA 2024), land=186.69 ha (tarlaccity.gov.ph). PSA says Rural; the barangay's own page explicitly says "Urban Barangay" -- another PSA-vs-LGU-page disagreement, see Reference/DATASETS.md
    ("Tariji", 1217.5, 10, 6500, 1.0, 0.78, False),  # pop=3191 (PSA 2024), land=262.09 ha (tarlaccity.gov.ph)
    ("Tibag", 2320.0, 31, 18000, 2.9, 0.71, True),  # pop=17936 (PSA 2024), land=773.12 ha (tarlaccity.gov.ph)
    ("Tibagan", 919.0, 24, 16000, 2.0, 0.74, True),  # pop=7025 (PSA 2024), land=764.38 ha (tarlaccity.gov.ph)
    ("Trinidad", 583.3, 7, 5500, 0.6, 0.79, False),  # pop=2148 (PSA 2024), land=368.27 ha (tarlaccity.gov.ph)
    ("Ungot", 897.4, 9, 6000, 0.8, 0.79, False),  # pop=4077 (PSA 2024), land=454.30 ha (tarlaccity.gov.ph)
    ("Villa Bacolor", 825.7, 8, 6000, 0.7, 0.79, False),  # pop=2681 (PSA 2024), land=324.68 ha (tarlaccity.gov.ph)
]

assert len(BARANGAY_PROFILES) == 76
_urban_count = sum(1 for row in BARANGAY_PROFILES if row[6])
assert _urban_count == 36, _urban_count  # PSA/PSGC's own classification -- see docstring re: the contested urban/rural column

BARANGAY_NAMES = [row[0] for row in BARANGAY_PROFILES]


def get_real_population(name):
    """Returns the REAL 2024 PSA population headcount for a barangay
    (an int), or None if the name isn't recognized / not parseable.
    Reuses _parse_source_comments() (below) -- the same inline
    "# pop=... " comments already parsed for the CSV export -- so the
    Saturation Map's detail panel can show a real "Population: 12,450"
    figure without duplicating that data anywhere else."""
    notes = _parse_source_comments().get(name)
    if not notes or not notes.get("population_2024_psa"):
        return None
    try:
        return int(notes["population_2024_psa"])
    except (TypeError, ValueError):
        return None


def get_barangay_profile(name):
    for row in BARANGAY_PROFILES:
        if row[0].lower() == str(name).lower():
            return {
                "name": row[0],
                "population_density": row[1],
                "foot_traffic_index": row[2],
                "average_rent": row[3],
                "business_density": row[4],
                "historical_success_rate": row[5],
                "urban": row[6],
            }
    return None


# ---------------------------------------------------------------------
# CSV export -- "give me a copy of the 76-barangay seed dataset".
#
# Every field actually seeded into the AI training data (train_model.py)
# comes straight from BARANGAY_PROFILES above (name, population_density,
# foot_traffic_index, average_rent, business_density,
# historical_success_rate, urban). This module also re-parses its OWN
# source file to pull the 2024 PSA population and land area figures that
# are documented in each row's trailing "# pop=... land=... ha" comment
# (see the docstring at the top of this file) -- so the export carries
# every number this project actually sourced for each barangay, not just
# the subset the ML model consumes, with nothing left out ("no missing").
# ---------------------------------------------------------------------

import csv
import re

_SOURCE_LINE_RE = re.compile(r'^\s*\(\s*"([^"]+)".*?\)\s*,\s*#\s*(.*)$')
_POP_RE = re.compile(r"pop=(\d+)")
_LAND_RE = re.compile(r"land=(~?[\d.]+)\s*ha")

CSV_COLUMNS = [
    "name",
    "population_density_per_km2",
    "foot_traffic_index",
    "average_rent_php",
    "business_density",
    "historical_success_rate",
    "urban",
    "population_2024_psa",
    "land_area_ha",
    "land_area_estimated",
    "source_notes",
]


def _parse_source_comments():
    """Returns {barangay_name: {"population_2024_psa", "land_area_ha",
    "land_area_estimated", "source_notes"}} by re-reading this file's own
    source text -- the pop=/land= figures only exist as inline comments
    next to BARANGAY_PROFILES, not as separate Python data."""
    notes_by_name = {}
    try:
        with open(__file__, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return notes_by_name

    for line in lines:
        match = _SOURCE_LINE_RE.match(line)
        if not match:
            continue
        name, comment = match.group(1), match.group(2).strip()
        pop_match = _POP_RE.search(comment)
        land_match = _LAND_RE.search(comment)
        land_value = land_match.group(1) if land_match else ""
        notes_by_name[name] = {
            "population_2024_psa": pop_match.group(1) if pop_match else "",
            "land_area_ha": land_value.lstrip("~"),
            "land_area_estimated": "TRUE" if ("ESTIMATED" in comment or land_value.startswith("~")) else "FALSE",
            "source_notes": comment,
        }
    return notes_by_name


def export_seed_data_rows():
    """Returns one dict per barangay (all 76), merging the structured
    BARANGAY_PROFILES fields with the sourced pop/land-area figures
    parsed from this file's own comments -- ready to hand to
    csv.DictWriter."""
    notes_by_name = _parse_source_comments()
    rows = []
    for row in BARANGAY_PROFILES:
        name = row[0]
        notes = notes_by_name.get(name, {})
        rows.append({
            "name": name,
            "population_density_per_km2": row[1],
            "foot_traffic_index": row[2],
            "average_rent_php": row[3],
            "business_density": row[4],
            "historical_success_rate": row[5],
            "urban": row[6],
            "population_2024_psa": notes.get("population_2024_psa", ""),
            "land_area_ha": notes.get("land_area_ha", ""),
            "land_area_estimated": notes.get("land_area_estimated", ""),
            "source_notes": notes.get("source_notes", ""),
        })
    return rows


def write_seed_data_csv(destination):
    """Writes the full 76-row export to `destination` -- a file path
    (str) or any writable text file-like object. Returns the row count."""
    rows = export_seed_data_rows()

    def _write(fileobj):
        writer = csv.DictWriter(fileobj, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    if hasattr(destination, "write"):
        _write(destination)
    else:
        with open(destination, "w", newline="", encoding="utf-8") as f:
            _write(f)
    return len(rows)
