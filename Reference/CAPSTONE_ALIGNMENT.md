# Capstone objective alignment

This is a line-by-line check of `CAPSTONEFINALPAPER.docx`'s **Specific
Objectives** (the Admin / SME / LGU module bullet points) against what
this codebase actually does, written so you can defend every claim in
your paper with a specific file/route rather than a general "yes, it's
in there." Where the system genuinely can't do something the paper
describes, that's called out explicitly rather than glossed over —
matching the paper's own Limitations section, which already anticipates
and accepts exactly this kind of data-availability gap.

Legend: ✅ implemented and testable · ⚠️ implemented with a documented
limitation · — not applicable / out of scope for a capstone deployment.

## Admin Module

| Paper objective | Status | Where |
|---|---|---|
| Manage user accounts | ✅ | `admin.users`, `admin.create_user`, `admin.toggle_user_active`, `admin.delete_user` in `app/controllers/admin_controller.py` |
| Audit trail | ✅ | `app/models/audit_log.py` (`AuditLog`), viewed at `admin.audit_log` |
| Manage datasets and uploaded records | ✅ | `admin.datasets`, `admin.delete_lgu_data`, `admin.delete_market_data` |
| System configuration and settings management | ✅ | `admin.settings` + `app/models/system_setting.py` |
| Monitor system activities and operational logs | ✅ | Same `AuditLog` table backs this — every account/dataset/settings change is logged |

## SME Module

| Paper objective | Status | Where |
|---|---|---|
| Input and manage business parameters | ✅ | `sme.home` plan form → `SmeProfile` |
| AI-driven business recommendations and alternative industry suggestions | ✅ | Two layers, both on `sme/recommendations.html` and both matching the paper's Figma storyboard (summary stat cards, Key Metrics, two-column Why This Works / Considerations, status-badge footer), with real/derived figures throughout and no fabricated example numbers. **(1) Per-plan:** every forecast (`generate_forecast_for_profile()`) compares the SME's OWN inputs against the real businesses on file for that industry/location and the barangay's real PSA population (`app/services/recommendation_service.py`) — optionally reworded by GPT-4o-mini or Claude (`app/services/llm_service.py`), otherwise a deterministic rule-based version of the same shape. **(2) 15+ location opportunities** — the storyboard's "15+ additional opportunities", now real, with an **Explore All Recommendations** button that lifts the display cap to every barangay scored, and summary cards that count the AI's High/Moderate tiers **city-wide** rather than the user's own saved plans: for the plan's industry, `app/services/location_opportunity_service.py` scores all 76 barangays and returns the best 18 as ranked cards, each one's Why This Works / Considerations written from that barangay's own Places competitor count, real 2024 PSA population/density and model output, **compared against the city median for the same industry**. Ordering blends viability (50%), residents-per-business (30%) and population (20%) as percentiles — a stated, editable weighting, printed on every card — because ranking on viability alone just surfaces the emptiest, tiniest barangays. Each card's **ROI Timeframe** is model-derived rather than arithmetic: the SME's own capital and revenue discounted to an assumed net margin (`ASSUMED_NET_MARGIN`, since revenue is not profit), then stretched or compressed by the barangay's own AI saturation score, and reported as a range. With the LLM switched on, one batched call rewrites the cards' prose from those same figures — the numbers, ranking and ROI are all computed before the model is called, and the prompt forbids inventing any others. |
| AI early warning notifications for market decline and oversaturation | ✅ | `app/models/notification.py` (`Notification`), surfaced via `/api/notifications` |
| Forecasting: market saturation, viability scores, demand projections | ✅ | `compute_scores()` / `generate_forecast_for_profile()`, `app/ml/` (K-Means thresholds + RandomForestRegressor) |
| Dashboard: viability scores, saturation results, demand projections, competitor density, AI alerts | ✅ | `sme/home.html` (the SME's own plan figures) + `sme/trend_reports.html` (the shared city-wide analytics dashboard) |

## LGU Module

| Paper objective | Status | Where |
|---|---|---|
| Identify underserved markets/sectors | ✅ | Saturation Map's "High Opportunity" tier + industry diversification recommendations in Trend Reports |
| **Interactive map by barangay** | ✅ | `sme/saturation_map.html` + `app/static/js/map.js`, **OpenStreetMap tiles via Leaflet** (no API key, no billing, and scroll-to-zoom with no ctrl-key overlay — which is also why isolating a barangay now registers on a single click), rendered as a real **choropleth** clipped to Tarlac City's REAL, OpenStreetMap-sourced border — not a bounding rectangle (one filled `L.polygon` cell per barangay, `/api/barangay-choropleth` + `app/services/choropleth_service.py`; the border itself is also drawn as its own bold outline via `/api/tarlac-city-boundary` — see `README.md` section 5.2). Clicking a barangay — on the map, the "All Barangays" list, or the search bar — isolates it IMMEDIATELY on a single click, hiding every other cell/row until it's double-clicked, searched again, or "Show All" is clicked; all three entry points share one isolation state so they never disagree. **Attribution caveat worth knowing:** the map carries both the OSM credit and a "Powered by Google" mark for the Places-derived competitor counts, but Google's Maps ToS §10.5 bars using its Content on a non-Google map — a deliberate, documented trade-off, see `README.md` 5.2. |
| **Business category filter for sector-specific saturation view** | ✅ | The industry `<select>` on the Saturation Map re-queries `/api/locations-forecast?industry_type=...` and recolors every zone for that industry specifically — not a cosmetic filter, the underlying saturation figures themselves change. The axis is the **20 PSIC top-level sections** (`app/ml/constants.py`), the same classification PSA publishes business statistics under, so a saturation claim is checkable against a published figure. Each section has an SME-scale Google Places search term (`SEARCH_TERM_MAP`). **Micro businesses are excluded on purpose** — this system plans for SMEs, so sari-sari stores, carinderias, food carts and market stalls are filtered out of every competitor count (`MICRO_BUSINESS_PATTERNS`, with every exclusion returned for audit), and the rows already on file were migrated onto the PSIC sections with the micro-only ones deleted (`app/services/industry_migration.py`, fully logged and reversible). |
| **Competitor density overlay (registered businesses per area)** | ✅ | Each barangay's exact `competitor_count` is in the hover tooltip, the detail panel, and the "All Barangays" list (a choropleth cell's shape/position is fixed by the Voronoi tessellation, so density is communicated by these real figures rather than by scaling a shape) |
| **Sociodemographic overlay (population density, household spending/demand, and household income)** | ⚠️ | Population density: ✅ real, 2024-PSA-derived people/km² (`app/ml/seed_data.py`, real for 73/76 barangays), switchable via the map's **Overlay** selector — a genuinely FIXED per-barangay figure that never changes when the industry `<select>` changes (only `saturation_index`, an industry-dependent number, does that; the detail panel shows both, separately labeled, so the two are never confused — see `renderDetailPanel()` in `map.js` and `/api/barangay-detail`'s `population_density` field). Household spending/demand: ✅ real PSA FIES (2023) figures — Tarlac's own real average annual family expenditure (₱299,670) applied to the PSA's real national spending-category shares (food, utilities, transportation, education directly from PSA's 2023 release; recreation estimated from a 2021 PSA/CPBRD factsheet, flagged separately) — shown on every barangay's map tooltip and detail panel, explicitly labeled as a Tarlac City-wide average rather than a barangay-specific figure, since no such breakdown is published at that granularity (`app/services/socio_demographic_service.py`; full sourcing in `Reference/DATASETS.md` section 7). Household income: **not implemented, deliberately.** No dataset — PSA, DTI, or otherwise — publishes real household INCOME at the barangay level anywhere in the Philippines; PSA's Family Income and Expenditure Survey stops at region/province for income. This was researched and documented in `Reference/DATASETS.md` earlier in the project. Inventing a number for 76 barangays and presenting it as data would be worse than the honest gap the paper's own Limitations section already accepts (imputation/simulation for missing secondary data, not fabrication of primary data that was never collected). Say this plainly to your panel: population density and household spending are the two sociodemographic figures this system can back with a cited, real source; household income specifically is the one honest "no." |
| Trend Projection: historical/projected demand trends, summary stats, AI diversification recommendations | ✅ | ONE city-wide **Trend Reports & Analytics** dashboard shared by SME, LGU and Admin (`sme/trend_reports.html`), laid out to the paper's own Figure 4 / 4.1: summary market indicators, Monthly Industry Trends, Industry Distribution, **Quarterly Performance & Growth Rate** as a single dual-axis chart (demand as a filled area on the left axis, quarter-over-quarter growth on the right — reading them together is what shows a market flattening out), and Top Performing Industries. A **Select Period** control (6 / 12 / 24 months, 3 years) drives how far back the monthly and quarterly charts look; widening it never invents history — quarters with no snapshot behind them stay flagged as back-projections and are drawn with hollow points. **The per-plan "My Trend Report" for SMEs was removed on purpose:** a chart drawn from one user's one or two saved plans is a restatement of their own inputs, not a market trend. An SME's plan-specific figures live on Home and Recommendations instead. |
| Government data upload interface | ✅ | `lgu.government_upload` |
| Reports: saturation, demand trend, sociodemographic, diversification | ✅ (sociodemographic report inherits the same population-density-only scope above) | Trend Reports page's charts/tables double as this; there's no separate PDF export module |

## Testing checklist (paper section: "Functionality testing using black-box testing")

Every bullet in the paper's own testing checklist has a corresponding
automated test in `tests/`, run with `pytest` (127 tests as of this
round -- including `tests/test_business_taxonomy_and_demand.py`'s
coverage of the expanded 46-category business taxonomy, its Places API
search-term mapping, and the PSA FIES-derived demand figures -- plus
`tests/test_recommendation_llm.py`'s coverage of the
structured recommendation contract, JSON storage round trip, backward
compatibility with legacy plain-text rows, and rule-based/LLM fallback
behavior — see `README.md` section 9). The Saturation Map's specific
checklist item, **"Saturation map rendering with sector filter and
sociodemographic overlay,"** is covered by:
- `test_all_76_shipped_barangays_have_real_bounds_checked_coordinates`
  and `test_laoang_and_sapang_maragul_are_at_their_own_distinct_real_positions`
  (`tests/test_analytics_and_geocoding.py`) — the map renders every
  barangay at its real position.
- `tests/test_choropleth.py` (13 tests) — the real city boundary file
  is a genuine OpenStreetMap export and every one of the 76 real
  barangay coordinates falls inside it; the choropleth cells each
  contain their own barangay's real point and no other's; every cell
  vertex stays within the real city boundary (not a rectangle that
  overshoots it — the exact bug this round fixed); the cells' areas
  sum to the real city polygon's own area; a barangay whose cell the
  real (concave) boundary splits into more than one piece comes back
  as a valid `MultiPolygon`; and both `/api/barangay-choropleth` and
  `/api/tarlac-city-boundary`'s auth + GeoJSON response shape.
- `tests/test_industry_migration_and_opportunities.py` — the one-time
  PSIC migration (and its `undo()`), and the Recommendations page's
  15+ location-opportunity engine.
- A **Leaflet API-contract test** run under `node` (not part of the
  `pytest` suite): loads `map.js` against a strict stub of the Leaflet
  API and fails on any wrong method, LatLng shape or polygon nesting
  depth, plus isolate/restore, the hover popup, the tier filter and both
  attributions.
- The industry `<select>`, the Overlay `<select>`, and the click/double
  -click isolate-restore interaction (map, "All Barangays" list, and
  search bar) were exercised with a live, seeded Flask server under
  Playwright during this round's development (not part of the
  committed `pytest` suite — no `playwright` dependency is in
  `requirements.txt`, matching the "no `selenium`/`playwright`
  dependency was part of the brief" note this file already made); treat
  a quick manual click-through of all three as part of your own
  pre-defense checklist.

## The one honest "no" — and why it's not a hole in the system

If a panel member asks "where is household income on the map," the
accurate answer is: it isn't there, because no one publishes it at
barangay granularity, and a capstone DSS that invents socioeconomic
statistics for 76 real communities is a worse outcome than one that
says so and shows the real figure it does have. This is the same
standard of honesty the paper already sets for itself in its own
Limitations section — "relies primarily on secondary and publicly
available data... which may not always be complete" — this is simply
that principle applied to one specific overlay instead of left as a
general disclaimer.
