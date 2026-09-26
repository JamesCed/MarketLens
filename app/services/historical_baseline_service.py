"""
app/services/historical_baseline_service.py
----------------------------------------------
Where the Trend Reports page's history comes from, for every month
before this system started collecting its own snapshots.

THE PROBLEM THIS SOLVES
The four KPI cards want a month-over-month change, and the monthly and
quarterly charts want a line going back years. This app only has
`market_data` rows from the day it was first run, so every earlier month
had nothing behind it and the cards read "no comparison available".

WHY NOT JUST FETCH THE HISTORY FROM GOOGLE PLACES
Because it does not exist. The Places API answers "what is there now" --
there is no parameter, endpoint or field anywhere in it that returns the
businesses that existed in a past year. No commercial API does; that is
not what Places is. Saying otherwise on defense day would be a claim
that cannot survive one follow-up question, so this file does not
pretend: everything before the first real snapshot is a MODELLED
back-projection, computed here, flagged as projected everywhere it is
displayed, and replaced automatically by real readings as the app
accumulates them.

WHAT MAKES IT MORE THAN A MADE-UP CURVE
The SHAPE of the history is real. It is taken from the Philippines'
own published MSME establishment counts (DTI's "Philippine MSME
Statistics", built on PSA's List of Establishments) -- including the
2020 COVID contraction and the 2021 rebound, which are exactly the
features a plausible-looking invented curve would smooth away:

    2019    995,741
    2020    952,969   -4.3%   <- pandemic contraction
    2021  1,076,279  +12.9%   <- rebound
    2022  1,105,143   +2.7%
    2023  1,241,733  +12.4%

So a Tarlac City business count for, say, June 2021 is not a number
someone chose. It is this app's OWN REAL current count for that
industry and barangay, scaled back along the national establishment
curve to where June 2021 sat on it. The level is real (today's measured
count), the trajectory is real (published national growth), and only the
attribution of that trajectory to one barangay is modelled.

WHAT IS STILL AN ASSUMPTION, STATED PLAINLY
  1. Tarlac City is assumed to have moved with the national MSME trend.
     It is a real assumption. A city-level establishment series for
     Tarlac is not published (see Reference/DATASETS.md section 4 for
     the search that established this), so the national series is the
     closest real proxy available.
  2. 2024 onward is not yet in the published series, so those years
     continue at POST_RECOVERY_GROWTH below -- a conservative figure,
     not the 12%+ of the rebound years.
  3. Each (industry, barangay) series carries a small DETERMINISTIC
     variation so the chart is not 20 identically-shaped lines. It is
     seeded from the series name, so it never changes between page
     loads, and it is bounded to a few percent.

NOTHING HERE IS WRITTEN TO THE DATABASE. The back-projection is computed
on the fly, every time. Inserting thousands of invented `market_data`
rows would corrupt the one table in this system that holds real
measurements -- it would inflate "Total Businesses", it would be
indistinguishable from a real PSA/DTI upload afterwards, and it would be
very hard to undo. The real table stays real.
"""

import hashlib
import math
from datetime import date

# Real published national MSME establishment counts -- DTI's "2023
# Philippine MSME Statistics in Brief" (from PSA's List of
# Establishments). These five years are measurements, not estimates.
NATIONAL_MSME_ESTABLISHMENTS = {
    2019: 995_741,
    2020: 952_969,    # -4.3% -- the COVID contraction
    2021: 1_076_279,  # +12.9% -- rebound
    2022: 1_105_143,  # +2.7%
    2023: 1_241_733,  # +12.4%
}

# The published series stops at 2023. Later years continue at this rate.
# Chosen conservatively -- roughly the 2022 step rather than the 12%+
# rebound years, because assuming the rebound continues indefinitely
# would overstate today's growth. Change it here if DTI publishes newer
# figures; everything downstream follows automatically.
POST_RECOVERY_GROWTH = 0.03

# The earliest month the trend page will chart. Before 2020 there is no
# pandemic-era shape to show and the projection stops being interesting.
EARLIEST_HISTORY = date(2020, 1, 1)

# How far the per-series deterministic variation can move a single
# (industry, barangay) line away from the national curve, either way.
# Small on purpose: it exists so the chart isn't N identical lines, not
# to manufacture barangay-level detail this system does not have.
SERIES_VARIATION = 0.04


def _establishments_for_year(year):
    """The national establishment count for any year, real where it is
    published and continued at POST_RECOVERY_GROWTH after that."""
    if year in NATIONAL_MSME_ESTABLISHMENTS:
        return float(NATIONAL_MSME_ESTABLISHMENTS[year])

    known_years = sorted(NATIONAL_MSME_ESTABLISHMENTS)
    if year < known_years[0]:
        # Earlier than the published series -- walk backwards at the
        # same conservative rate.
        value = float(NATIONAL_MSME_ESTABLISHMENTS[known_years[0]])
        for _ in range(known_years[0] - year):
            value /= (1.0 + POST_RECOVERY_GROWTH)
        return value

    value = float(NATIONAL_MSME_ESTABLISHMENTS[known_years[-1]])
    for _ in range(year - known_years[-1]):
        value *= (1.0 + POST_RECOVERY_GROWTH)
    return value


def _establishments_for_month(anchor):
    """The national count interpolated to a MONTH.

    The published figures are annual, and each one is treated as sitting
    at the MIDDLE of its year rather than at January. That is not a
    detail: an annual establishment count is a whole-year figure, so
    pinning it to January would push the entire 2019 -> 2020 collapse
    into a single year boundary and hide it. Anchored at mid-year, the
    monthly series falls from about 972,600 in January 2020 to about
    953,000 in July 2020 and climbs back through 2021 -- the pandemic
    contraction and rebound are actually visible in the chart, which is
    the whole reason for grounding this in real figures instead of
    drawing a smooth invented line.
    """
    position = anchor.year + (anchor.month - 6.5) / 12.0
    lower = int(math.floor(position))
    fraction = position - lower
    start = _establishments_for_year(lower)
    end = _establishments_for_year(lower + 1)
    return start + (end - start) * fraction


def _series_variation(series_key, anchor):
    """A small, DETERMINISTIC offset for one (industry, barangay) series
    in one month -- seeded from the series name and month, so it is
    identical on every page load and every machine. Never random."""
    if not series_key:
        return 1.0
    digest = hashlib.sha256(f"{series_key}|{anchor.year}-{anchor.month}".encode("utf-8")).hexdigest()
    # 0..1 from the first 8 hex digits, mapped to +/- SERIES_VARIATION.
    unit = int(digest[:8], 16) / 0xFFFFFFFF
    return 1.0 + ((unit * 2.0) - 1.0) * SERIES_VARIATION


def growth_index(anchor, reference=None, series_key=None):
    """How large the market was in `anchor`'s month RELATIVE to
    `reference`'s month (default: today), as a multiplier.

    1.0 means "the same size as the reference month". June 2021 against
    a 2026 reference returns something well below 1.0, because the
    national establishment base has grown since. This is the single
    number every back-projection below is built from.
    """
    reference = reference or date.today()
    base = _establishments_for_month(reference)
    if base <= 0:
        return 1.0
    index = _establishments_for_month(anchor) / base
    return max(0.05, index * _series_variation(series_key, anchor))


def project_businesses(current_count, anchor, reference=None, series_key=None):
    """This app's REAL current business count for one industry+barangay,
    scaled back to where `anchor`'s month sat on the national curve."""
    if not current_count:
        return 0
    return max(0, int(round(float(current_count) * growth_index(anchor, reference, series_key))))


def project_saturation(current_saturation, anchor, reference=None, series_key=None):
    """Market saturation back-projected to `anchor`'s month.

    Saturation is driven mainly by how many competitors occupy a market
    (see app/ml/train_model.py's feature weights), so a month when the
    establishment base was materially smaller was, all else equal, a
    less saturated one. The current saturation is therefore scaled by
    the same index -- damped, because saturation is a bounded 0-100
    index rather than a count, so it does not fall as steeply as the
    raw business number does.
    """
    if current_saturation is None:
        return None
    index = growth_index(anchor, reference, series_key)
    damped = 1.0 - ((1.0 - index) * 0.6)
    # Two decimals, not one. Consecutive months differ by a fraction of a
    # percentage point, so rounding here would flatten every
    # month-over-month comparison to exactly zero before the caller ever
    # saw it. Callers that DISPLAY this round it themselves.
    return round(max(0.0, min(100.0, float(current_saturation) * damped)), 2)


def project_viability(current_saturation, anchor, reference=None, series_key=None):
    """Viability follows saturation, on the same 0-10 scale the
    forecasting engine uses (viability = (100 - saturation) / 10)."""
    saturation = project_saturation(current_saturation, anchor, reference, series_key)
    if saturation is None:
        return None
    return round(max(0.0, min(10.0, (100.0 - saturation) / 10.0)), 1)


def national_context(anchor):
    """What the back-projection for this month is based on, in a form
    the UI can show a user or a panel: the national figure behind it and
    whether that figure is published or continued."""
    year = anchor.year
    return {
        "year": year,
        "national_establishments": int(round(_establishments_for_month(anchor))),
        "is_published_year": year in NATIONAL_MSME_ESTABLISHMENTS,
        "source": "DTI Philippine MSME Statistics (PSA List of Establishments)",
    }
