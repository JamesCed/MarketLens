# SME Business Planning & Market Entry DSS — Flask Web App

A web-based Decision Support System (DSS) with **three account types**:

| Role | Who | What they see |
|-------|-----|----------------|
| `SME` | Entrepreneurs / business owners | Home (AI forecast), Saturation Map, Trend Reports, Recommendations, Profile Settings |
| `LGU` | LGU officials | LGU Dashboard, **Gov't Data Upload** (LGU-only), Saturation Map, Trend Reports, Profile Settings |
| `Admin` | System administrator | Manage Users, Audit Trail, Datasets, System Settings, everything else |

The **Gov't Data Upload** page is enforced as LGU/Admin-only both in the
sidebar (link is hidden) and on the server (`@role_required("LGU", "Admin")`
in `app/controllers/lgu_controller.py` — an SME account hitting the URL
directly gets HTTP 403).

This app implements the architecture described in the capstone paper:
Flask (Python) backend in an MVC-style layered structure, MySQL database,
Bootstrap 5 + Chart.js front-end, Google Places API for competitor data, and
an AI forecasting engine built around a **Random Forest Regressor** (with a
K-Means clustering pass run during training, per the paper's methodology) to
compute a Market Saturation Index and viability score.

> **IMPORTANT — this codebase was realigned to your real, already-live
> `dss_db` schema.** See **section 0** below before touching anything —
> it explains exactly what changed from the original storyboard/paper draft
> and why, so nothing here surprises you at your defense.

---

## 0. This was rebuilt around YOUR real database — read this first

You sent us `dss_db.zip`, a real export of your live MySQL database (already
containing one real registered user). That export is the **source of
truth** now — every model, service, controller, and template in this
project was rewritten to match it exactly, not the other way around. Here's
what that means in practice:

### Your 5 real tables, unchanged
`user`, `sme_profile`, `market_data`, `lgu_data`, `forecast_result` — see
`sql/schema.sql` for the exact column-by-column definitions (transcribed
directly from your `dss_db_*.sql` dump) and each file under `app/models/`
for the SQLAlchemy version of the same thing. **Nothing about these 5
tables was changed.** Your existing user account still works — same email,
same password hash format (`pbkdf2:sha256...`, which `werkzeug.security`
reads natively).

### What we ADDED (and why it's safe)
Your schema has no table for notifications, saved plans, an audit trail, or
tunable AI settings — but the paper's Admin Module and "AI early warning"
features need somewhere to store that. So we added 4 small tables:
`notifications`, `plan_saves`, `audit_logs`, `system_settings`. Each one
only has a foreign key pointing INTO your tables (`user.user_id` /
`forecast_result.forecast_id`) — none of them are referenced BY your 5
tables, so they can be added or even dropped later without touching your
real data. `python seed.py` creates them automatically (`db.create_all()`
only creates tables that don't exist yet — it will never touch your 5 real
ones).

*(An earlier version of this project also added a 5th table,
`competitor_snapshots`, for individually-named competitor businesses. It
has since been removed — real competitor data now goes straight into your
existing `market_data.competitor_count` column instead, city-wide, via the
Google Places API (New); see section 5.1. If your live database still has
a leftover `competitor_snapshots` table from an older run, it's safe to
drop: `DROP TABLE IF EXISTS competitor_snapshots;`.)*

We also added ONE column to your existing `user` table: `profile_picture`
(a `LONGTEXT`, storing a base64 `data:` URI — see `app/models/user.py`).
This one DOES touch a given table (a column, not a new table), because a
profile photo is inherently a per-user attribute, not its own concept
worth a whole table. `db.create_all()` adds it automatically on a brand
new database; if your MySQL database already existed before this feature,
run once: `ALTER TABLE user ADD COLUMN profile_picture LONGTEXT NULL;`

### What we had to DESIGN AROUND
Your schema is leaner than an earlier draft of this system assumed, which
forced some real design decisions — all deliberate, all explainable:

1. **No `barangays` table.** `sme_profile.location`, `market_data.location`,
   and `lgu_data.barangay` are all plain free-text `VARCHAR` columns — there
   is no table of barangays with stored population/income/lat-lng. So:
   - The AI engine reads/writes **reference data** keyed by location NAME
     from `app/ml/seed_data.py` (all 76 official Tarlac City barangays,
     with real 2024 PSA population figures, a real true population
     density computed from real land area for 73/76 of them, and PSA's
     own official urban/rural classification — see
     `Reference/DATASETS.md` for the full picture, including a genuine
     three-way disagreement on the urban/rural column that's still
     unresolved; the foot-traffic/rent/business-density/success-rate
     columns are still estimated placeholders) instead of a database
     table.
   - The Saturation Map geocodes each location name **client-side**, on the
     fly, using the Google Maps JavaScript Geocoding service
     (`app/static/js/map.js`) — there's no stored lat/lng to read.
   - Google Places lookups switched from coordinate-based "Nearby Search"
     to free-text **Text Search** (`"retail store in <location name>"`) —
     see `app/services/places_service.py`.

2. **`forecast_result.sme_id` / `market_id` / `lgu_id` are all `NOT NULL`.**
   There's no "anonymous" or "aggregate" forecast row — every single
   forecast must point at one real `SmeProfile`, one real `MarketData`
   snapshot, and one real `LguData` row. But the Saturation Map and Trend
   Reports pages need to show scores for locations that aren't tied to any
   one SME's plan. We solved this by splitting the AI engine into two
   entry points in `app/services/forecasting_service.py`:
   - `compute_scores(industry_type, location)` — **ephemeral**, no
     `forecast_result` row written. Powers the map/trend pages.
   - `generate_forecast_for_profile(sme_profile)` — calls the above, then
     **persists** a `forecast_result` row satisfying the NOT NULL
     constraints (auto-creating a `MarketData`/`LguData` snapshot if none
     exists yet for that location — see next point). Powers the Home page.

3. **`lgu_data.uploaded_by` is `NOT NULL`.** So when the AI engine needs an
   `LguData` row for a location an LGU hasn't uploaded real data for yet, it
   auto-creates a clearly-labeled placeholder row (`zoning_info: "Auto-
   generated placeholder..."`) and has to attribute it to a REAL user. We
   created a dedicated `system@dss.local` account (Admin role, random
   unguessable password, created by `seed.py`) for exactly this — see
   `SYSTEM_USER_EMAIL` in `forecasting_service.py`. If that account is
   missing and the database has no users at all, `_get_system_user_id()`
   now **creates it on demand** rather than returning `None`: returning
   `None` produced a raw
   `IntegrityError: NOT NULL constraint failed: lgu_data.uploaded_by`
   the first time anything asked for a score on an unseeded database
   (this was the failing `tests/test_app.py::test_forecasting_service_compute_scores`).
   Scoring a location is read-shaped and shouldn't explode because
   `seed.py` hasn't been run. It's not meant to be
   logged into; it only exists as a placeholder-data owner. (This also
   means: **an Admin can never delete this account** — `admin_controller.py`
   blocks it — and deleting any OTHER user who has uploaded real LGU data
   will fail with a clear message instead of a raw database error, because
   `lgu_data.uploaded_by` is `ON DELETE RESTRICT`.)

4. **No stored `cluster_label` column.** `forecast_result` has no ENUM
   column for Low/Moderate/High/Saturated. Instead, `cluster_label` is a
   **Python `@property`** on the `ForecastResult` model
   (`app/models/forecast_result.py`) computed live from `saturation_index`
   using fixed threshold cut points (`CLUSTER_THRESHOLDS` in
   `app/ml/constants.py`: ≤25 Low, ≤50 Moderate, ≤75 High, else Saturated).
   `train_model.py` still runs an actual K-Means pass (per the paper's
   stated methodology) and reports its own cluster mapping in
   `training_report.json` for your methodology chapter — but that
   K-Means model is NOT what labels a live forecast; the fixed thresholds
   are, since they're simpler and keep every cluster boundary on the same
   0–100 scale as `saturation_alert_threshold`.

5. **Only one recommendation TEXT column**, not separate "reasons" and
   "risks" JSON columns. `app/services/recommendation_service.py` builds a
   structured `{headline, opportunity_type, summary, reasons, risks,
   generated_by}` dict — grounded in the SME's own input parameters
   (sub-category, what they sell, what makes them different, price list,
   capital, employees, stage) compared against the real
   businesses on file for that industry/location — and JSON-serializes it
   into that single column. `parse_recommendation()` reads it back out
   again on every page that shows it (Recommendations, Home, personal
   Trend Reports), and also understands the OLD plain-text format this
   column used to hold, so rows written before this change still render
   correctly instead of showing raw text.

6. **`user` has no location/business-type/notification-preference
   columns** — just `name`, `email`, `password`, `role`, `contact_number`,
   `status`, `created_at`. Profile Settings (`shared/profile_settings.html`)
   was simplified to match: just those fields, plus a password-change form.
   An SME's business details (industry, location, capital, etc.) live on
   `SmeProfile`, collected from the Home page instead.

7. **No file-metadata table for LGU uploads.** `lgu_data` stores the
   *parsed government record itself* (permits, zoning, closures, business
   density) — not a filename/size/status row. So the Gov't Data Upload page
   now asks the LGU user to pick a **target table** (`lgu_data` or
   `market_data`) and inserts parsed rows directly into it
   (`app/services/data_import_service.py`); the uploaded file itself is a
   temporary scratch file, deleted right after parsing.

None of this changes what the system DOES for the paper's objectives — SME
forecasting, the LGU-only upload page, the satellite map, Admin oversight
are all still fully implemented — it changes HOW the data is shaped
underneath, to match the database you're actually grading against.

---

## 0.1 Revisions round (September 2026) — what changed

From the panel's CHANGES-REVISIONS list, plus the audit trail, archive and
revenue requests. Everything below is applied to an existing database
automatically at start-up (`app/services/startup_migrations.py`); the same
changes as plain MySQL, for the ERD, are in `sql/2026-09_revisions.sql`.

| Change | Where |
|---|---|
| **Plan choice bar** on Home: every saved plan with its own market score, one click to switch (`/home?plan=<id>`, remembered per session, audited as `select_plan`) | `sme_controller.home`, `sme/home.html` |
| **Mini map follows the chosen plan** (its industry, centred on its barangay), bigger and filling its card; popups stay inside the map | `sme/home.html`, `static/js/map.js` |
| **Broader business parameters**: industry → sub-category, what you sell, "what makes you different" (read by the AI), optional menu / price list. One parser for sign-up, Add New Plan and Settings | `app/ml/subcategories.py`, `app/services/plan_params.py`, `shared/_plan_fields.html`, `static/js/plan_form.js` |
| **Direct competition**: the score is adjusted by how dense the plan's sub-category is (measured by Places or the LGU permit register); with no measurement it is left exactly as the industry score and labelled "estimated" | `app/services/subcategory_service.py`, table `subcategory_market_data` |
| **Monthly revenue removed** from every form; the ROI window is now built from the model alone (column kept, no longer read) | `location_opportunity_service.estimate_roi_timeframe` |
| **First-time walkthrough** (asks once; interactive, plain-language steps per role; replay from the sidebar) | `onboarding_controller.py`, `static/js/tour.js`, `tour_steps.js` |
| **Community forum** with keyword filter + moderator/AI approval, reports, moderation queue | `forum_controller.py`, `services/forum_moderation.py` |
| **Interior look matches sign-in** (navy/cyan frame; prototype content colours unchanged; remove `dss-skin` from `<body>` in `base.html` to revert) | `static/css/style.css` (INTERIOR SKIN block) |
| **Location by map** on Home (Pick on map), visible Industry type box, **Clear** | `static/js/location_picker.js`, `static/js/sme_search.js` |
| **Saturation map**: cleaner text; barangay panel shows the top 3 industries there | `static/js/map.js`, `api_controller.barangay_detail` |
| **Official barangay boundaries** (PSA/NAMRIA 2023) replace the computed cells; "Baras" folded into Baras-baras | `static/data/barangay_boundaries.json`, `choropleth_service.py`, `seed_data.canonical_barangay` |
| **Month timeline** on the map: history, current and predicted saturation, any month Jan 2020 → a year ahead | `saturation_timeline_service.py`, `static/js/map_timeline.js` |
| Map controls on the right; map fills to the side panel's height; side panel can be hidden | `sme/saturation_map.html`, `static/css/map.css` |
| **Admin: archive, never delete** users and datasets (with a required reason; restore available); archived datasets drop out of every score | `admin_controller.py`, `app/models/archive.py` |
| **Audit trail with the 5 W's** — who, what, when, where, why — with filters and CSV export | `app/utils/audit.py`, `admin/audit_log.html` |

## 1. Quick start

```bash
# 1. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment variables
cp .env .env
# open .env and fill in / confirm your MySQL credentials and (optionally)
# your Google Maps / Places API keys — see section 4 below.

# 4. Make sure MySQL is running and reachable at the host/port/user/password
#    in your .env (defaults match the project brief: 127.0.0.1:3306, root/root),
#    and that your EXISTING dss_db database (from dss_db.zip) is restored there.
#    Do NOT run sql/schema.sql against it -- see the warning at the top of
#    that file. Just run step 5 below; it never touches your 5 real tables.

# 5. Create any missing (additive) tables, seed settings/demo accounts, and
#    train the AI model (one command)
python seed.py

# 6. Run the app
python app.py
```

Open **http://127.0.0.1:5000** in your browser. Your real existing account
(from the database you sent us) logs in as-is. `seed.py` also creates demo
accounts for quick testing (change/delete these before any real deployment):

| Email | Password | Role |
|-------|----------|------|
| admin@dss.local | admin123 | Administrator |
| sme@dss.local | sme12345 | SME / Entrepreneur |
| lgu@dss.local | lgu12345 | LGU Official |

`system@dss.local` is also created, with a random password nobody knows —
it's not a login account, see section 0.4 above.

---

## 2. Folder structure (layered architecture)

```
flask_webapp_dss/
├── app.py                     # entry point — `python app.py` starts the server
├── seed.py                    # one-time/idempotent setup: tables, settings, demo accounts, train AI
├── precompute_barangay_coords.py # OPTIONAL: replaces placeholder map coordinates with real OpenStreetMap-geocoded ones (see section 5.2)
├── export_seed_data.py        # exports the 76-barangay reference dataset to CSV (no DB/Flask needed)
├── requirements.txt           # all pip packages needed (see section 3)
├── .env.example                # template for your local environment variables
├── .gitignore
├── sql/
│   └── schema.sql             # raw-SQL mirror of your 5 real tables + the 4 additive ones
│                               # (⚠ DROPs tables — do not run against your live DB, see file header)
├── tests/
│   └── test_app.py            # pytest smoke tests (auth, RBAC, forecasting engine)
├── instance/
│   └── uploads/                # Gov't Data Upload's temp scratch space (files are deleted after parsing)
└── app/                        # ---------- the actual application ----------
    ├── __init__.py             # application factory: create_app() wires everything together
    ├── config.py               # ALL environment-dependent settings (DB, API keys, AI weights)
    ├── extensions.py           # Flask extension instances (db, login_manager, csrf)
    │
    ├── models/                 # ---- DATA LAYER (SQLAlchemy ORM = your real ERD tables) ----
    │   ├── user.py              # your `user` table — login + RBAC role (Admin/SME/LGU)
    │   ├── sme_profile.py       # your `sme_profile` table — a business plan/scenario to analyze
    │   ├── market_data.py       # your `market_data` table — competitor/demand snapshot per industry+location
    │   ├── lgu_data.py          # your `lgu_data` table — parsed government records per barangay
    │   ├── forecast_result.py   # your `forecast_result` table — AI engine output (the important one!)
    │   ├── notification.py      # ADDITIVE: AI early-warning + info notifications
    │   ├── plan_save.py         # ADDITIVE: "Save to My Plans" button backing table
    │   ├── audit_log.py         # ADDITIVE: Admin Module audit trail
    │   └── system_setting.py    # ADDITIVE: Admin-tunable AI engine settings (key/value store)
    │
    ├── controllers/             # ---- APPLICATION LAYER (Flask blueprints = routes) ----
    │   ├── auth_controller.py    # register / login / logout
    │   ├── sme_controller.py     # Home, Saturation Map, Trend Reports, Recommendations
    │   ├── lgu_controller.py     # LGU Dashboard + Gov't Data Upload (LGU/Admin only!)
    │   ├── admin_controller.py   # Admin Module: users, audit trail, datasets, settings
    │   ├── profile_controller.py # Profile Settings (shared by every role)
    │   └── api_controller.py     # JSON API used by the map/chart JavaScript (see section 5)
    │
    ├── services/                 # ---- the "AI Forecasting Engine" + integrations ----
    │   ├── forecasting_service.py  # THE core AI pipeline (see section 6)
    │   ├── places_service.py       # Google Places API (New) Text Search wrapper, Tarlac-City-restricted (+ simulated fallback)
    │   ├── trend_analytics_service.py # LGU/Admin aggregate report + SME personal/scoped report (see section 6b)
    │   ├── recommendation_service.py # turns scores into one recommendation text block
    │   ├── llm_service.py          # OPTIONAL GPT (OpenAI) or Claude (Anthropic) call for nicer recommendation text
    │   ├── email_service.py        # OPTIONAL Gmail SMTP call for the registration verification code
    │   └── data_import_service.py  # parses LGU-uploaded CSV/Excel straight into lgu_data/market_data
    │
    ├── ml/                        # ---- model training (offline, run via seed.py) ----
    │   ├── constants.py            # shared encodings (industry types, feature order, cluster thresholds)
    │   ├── seed_data.py            # all 76 Tarlac City barangays (real population + urban/rural, estimated market figures)
    │   ├── train_model.py          # trains + saves the Random Forest (+ evaluates K-Means)
    │   └── model_store/             # rf_model.pkl, training_report.json
    │
    │   (app/static/data/barangay_coords.json holds the map's barangay
    │   coordinates -- see section 5.2)
    │
    ├── utils/
    │   ├── decorators.py           # @role_required(...) — the RBAC enforcement
    │   ├── audit.py                # log_action() helper -> writes to audit_logs
    │   └── helpers.py              # small shared file-upload helpers
    │
    ├── static/                    # ---- PRESENTATION LAYER (front-end assets) ----
    │   ├── css/style.css           # hand-written styling on top of Bootstrap 5
    │   └── js/
    │       ├── main.js             # notification bell dropdown (every page)
    │       ├── map.js              # Google Maps JS API saturation map (search bar, 4-tier zones, detail panel)
    │       └── sme_search.js       # SME Home page search bar -> GET /api/forecast, deep-links into map.js
    │
    └── templates/                 # ---- PRESENTATION LAYER (Jinja2 + Bootstrap 5) ----
        ├── base.html               # master layout (sidebar + topbar + flash messages)
        ├── auth/                   # login.html, register.html
        ├── sme/                    # home.html, saturation_map.html, trend_reports.html (shared by SME/LGU/Admin), recommendations.html
        ├── lgu/                    # dashboard.html, government_upload.html
        ├── admin/                  # dashboard.html, users.html, audit_log.html, datasets.html, settings.html
        ├── shared/                 # _sidebar.html, _topbar.html, _flash.html, profile_settings.html
        └── errors/                 # 403.html, 404.html, 500.html
```

This mirrors the paper's "Unified MVC Architecture" almost 1:1:
**models/** = Data Layer, **controllers/** + **services/** = Application
Layer (AI Engine & Controller), **templates/** + **static/** = Presentation
Layer (UI).

---

## 3. Packages needed (requirements.txt)

| Package | Why |
|---|---|
| Flask, Werkzeug | the web framework itself |
| Flask-SQLAlchemy | ORM — turns Python classes in `app/models/` into MySQL tables |
| PyMySQL, cryptography | pure-Python MySQL driver (no compiler/build tools needed) |
| Flask-Login | session-based login, `current_user`, `@login_required` |
| Flask-WTF | CSRF protection on every form |
| python-dotenv | loads `.env` into environment variables |
| numpy, pandas, scikit-learn, joblib | the AI engine: Random Forest, K-Means (training-time evaluation), data wrangling, saving/loading the trained model |
| contourpy | traces the Saturation Map's choropleth cells (each barangay's nearest-neighbor region, rasterized then clipped to Tarlac City's real boundary) back into vector polygons (`app/services/choropleth_service.py`) |
| openpyxl | reading uploaded `.xlsx` files (`pandas.read_excel`) |
| requests | calling the Google Places API |
| openai, anthropic | powers the AI-Powered Recommendation when `USE_LLM_RECOMMENDATIONS=true` and a key is configured (pick a provider via `LLM_PROVIDER`; the `openai` package also reaches OpenRouter — see section 7); with no key configured, or the flag off, the app quietly uses the built-in rule-based recommendation instead |
| pytest | running the test suite (`tests/`) |

Email verification (section 5.3b) uses Python's built-in `smtplib`/`email`
modules — no extra package needed. Profile pictures (Profile Settings) are
stored as base64 text directly in the database — also no extra package.

Install them all with `pip install -r requirements.txt` inside your venv.

---

## 4. Database connection

Configured in `.env` (copy from `.env.example`) and read by `app/config.py`:

```
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=root
DB_PASSWORD=root
DB_NAME=dss_db
```

These are exactly the credentials you gave us. `app/config.py` turns them
into a SQLAlchemy URI:
`mysql+pymysql://root:root@127.0.0.1:3306/dss_db?charset=utf8mb4`

You never need to edit the connection string by hand — just make sure your
**existing** `dss_db` database (restored from `dss_db.zip`) is running and
matches the credentials in `.env`, then run `python seed.py` (see section 0
and 1 — it is safe against your real data; `sql/schema.sql` is NOT, for a
fresh/empty database only).

---

## 5. API connections (preparation)

### 5.0 Where the keys live — `.env` and `.env.example`

This project now uses **three separate Google keys**, because Google
restricts a key per-API: a key enabled only for Places is rejected by the
Geocoding endpoint, and vice versa.

| `.env` variable | Google API to enable | Used for |
|---|---|---|
| `GOOGLE_PLACES_API_KEY` | Places API (New) | competitor counts per barangay (5.1) |
| `GOOGLE_GEOCODING_API_KEY` | Geocoding API | barangay name → real lat/lng (5.2). Optional — falls back to the Places key |
| `GOOGLE_MAPS_JS_API_KEY` | Maps JavaScript API | drawing the map in the browser (5.2) |

Only **`.env`** is actually read (`app.py` loads it with python-dotenv).
The project also ships **`.env.example`** holding the same values: a bare
`.env` is treated as hidden by most tools and kept vanishing from the
project folder after unzipping, so the `.example` copy is what survives.
After extracting, copy it once:

```
copy .env.example .env      REM Windows
cp .env.example .env        # macOS / Linux
```

> **`.env.example` contains real keys**, so it is listed in `.gitignore`
> alongside `.env`. An `.example` file is normally committed — this one
> must not be, or the keys get published. Blank them out first if you
> ever need to commit it.

### 5.1 Google Places API (NEW) — server-side, real city-wide competitor data
Used by `app/services/places_service.py` to fetch real competitor
businesses the way the paper describes ("The backend programmatically
retrieves competitor data ... by calling the Google Places API using the
Python requests package"). This calls the **Places API (New) Text Search**
endpoint (`POST https://places.googleapis.com/v1/places:searchText`), with
every request **hard-restricted to a Tarlac City bounding box**
(`locationRestriction`, not just a soft bias) so results are always
"Tarlac City only", per the current requirement.

1. Go to https://console.cloud.google.com/google/maps-apis
2. Create a project, enable **"Places API (New)"**, enable billing (Google
   gives a recurring free monthly credit that comfortably covers a school
   project).
3. Create an API key, restrict it to Places API (New).
4. **Put it in your `.env` file, on the line that already reads
   `GOOGLE_PLACES_API_KEY=`** — that's the ONE file/variable to paste this
   key into. Leave **no space after the `=`**: a leading space can be
   read as part of the key and Google then rejects it as invalid. `app/config.py` reads it from there automatically; nothing
   else needs to change.

**Result counts are no longer capped at 20.** `PLACES_MAX_RESULTS` (and
the matching Admin → System Settings field) now defaults to **0 =
unlimited**: the app pages through Text Search until Google stops
returning a `nextPageToken`, so a dense barangay reports the businesses
it really has. Previously only the first page was requested, which is
why somewhere like Matatalaib sat at exactly 20 for every industry.

Be aware of Google's own ceiling when reading the numbers: Text Search
returns 20 places per page and issues no page token past the 3rd, so
**60 results per (industry × barangay) query is a hard API limit**. A
count sitting at exactly 60 should be read as "60 or more". Existing
installs that still carry the old default of `20` are migrated to `0`
automatically on startup (`SystemSetting._DEFAULT_UPGRADES`) — a value
you deliberately chose yourself is never overwritten.

**Without a key, nothing breaks** — `places_service.py` automatically
returns a clearly-labeled simulated competitor list instead (matching the
paper's own "AI-driven simulation" fallback for when real-time data isn't
available).

**No separate seeding script — data is fetched live, on demand.** Once a
key is set, `market_data.competitor_count` is populated automatically the
moment the AI engine actually needs a given industry+barangay combo:
opening the Saturation Map for an industry, generating/editing a business
plan, or opening Trend Reports all trigger real, live Places API lookups
for whatever combos they touch, with each result cached for 30 days (see
`find_or_create_market_data()` in `app/services/forecasting_service.py`).
There is deliberately no bulk "seed the whole city" script — real
competitor data is meant to come from Google's live database exactly when
it's asked for, not from a batch job run ahead of time. The only thing you
need to do is add the key to `.env`; everything else already wired to call
`find_or_create_market_data()` will start returning real numbers instead
of simulated ones on its very next lookup.

**20 industry categories -- the PSIC top-level sections.** The industry
axis is now the Philippine Standard Industrial Classification's own
top-level sections (Agriculture/Forestry/Fishing, Mining and Quarrying,
Manufacturing, ... Other Service Activities, Activities of Households as
Employers) -- the same sections PSA publishes its business and
employment statistics under. That matters for your defense: a claim like
"Accommodation and Food Service is saturated in this barangay" is now
checkable against a published PSA figure, because both sides of the
comparison use the same classification. Each section has an SME-scale
Google Places search term in `SEARCH_TERM_MAP`
(`app/services/places_service.py`) and a short display label for tight UI
(`short_industry_label()` in `app/ml/constants.py` -- "Wholesale and
Retail Trade; Repair of Motor Vehicles and Motorcycles" is 68 characters
and would break a card).

**Micro businesses are deliberately excluded -- this is an SME system.**
Under the Philippine MSME definition (RA 9501/DTI, by asset size) a
MICRO enterprise holds up to PHP 3,000,000 in assets: the sari-sari
store in a front window, the carinderia with four tables, the market
stall, the food cart, the piso-wifi box on a wall. Real businesses, but
not the competition an SME planning a PHP 500k-5M entry is sizing itself
against -- and counting them made every barangay look saturated for
reasons that had nothing to do with SME-scale competition. Two
mechanisms enforce that scope:

- **A live filter on Places results.** `MICRO_BUSINESS_PATTERNS` in
  `app/services/places_service.py` drops micro establishments out of
  every Google Places response before they are counted. It matches on
  the place's own NAME (sari-sari, carinderia, food cart, tiangge, piso
  wifi, vulcanizing, backyard...), because Google has no "micro
  enterprise" flag and no reliable place TYPE for it either -- a
  sari-sari store and a 7-Eleven both come back as `convenience_store`,
  so filtering on that type would throw away genuine SME retailers. The
  pattern list is deliberately CONSERVATIVE: a pattern only goes in if a
  match is near-certain to be micro, so some micro businesses with a
  generic name ("JM Store") are still counted, but a real SME is
  essentially never dropped by mistake. Every exclusion is returned
  alongside the results (`excluded_micro` from
  `search_competitors_detailed()`, surfaced by `/api/places/nearby`), so
  you can show a panel exactly which establishments were left out rather
  than asserting the filter works.
- **A one-time migration of the data already on file.**
  `app/services/industry_migration.py` runs once on startup and moves
  every stored `industry_type` onto the PSIC sections: non-micro rows
  are remapped ("Retail" -> "Wholesale and Retail Trade; Repair of Motor
  Vehicles and Motorcycles", "Health & Wellness" -> "Human Health and
  Social Work Activities"), micro-only rows are deleted, and duplicates
  the remap creates (two old categories collapsing into one section for
  the same barangay and date) are collapsed so nothing double-counts.
  SME plans and forecast history are only ever remapped, never deleted
  -- that is a person's saved work. Every touched row is copied first
  into the `industry_migration_log` table, full original row included as
  JSON, and `industry_migration.undo()` puts all of it back. Remapped
  rows also get one free Places re-fetch (their old count came from the
  old category's search term), via the same watermark mechanism used
  when the result cap was lifted.

  Five of the former micro-era categories are remapped rather than
  deleted -- bakeshops, laundry shops, samgyupsal/grill restaurants,
  hardware & construction supply, and agri-supply stores are ordinarily
  SME-scale registered establishments. That split is a judgment call and
  it lives in one editable place: move a name between `OLD_TO_PSIC` and
  `MICRO_ONLY` in that file and re-run to change it.

**Only `competitor_count` ever comes from Google.** The other four
columns on the same `market_data` row — `population_density`,
`foot_traffic_index`, `average_rent`, `historical_success_rate` — are the
separate, static reference dataset described in `app/ml/seed_data.py`
(mostly-real population figures, synthetic socio-demographic estimates
for the rest — see that file's own docstring). Adding a Places API key
never changes those four; it only makes `competitor_count` real.

*(An earlier version of this project also had a separate
`competitor_snapshots` table with individually-named businesses, seeded by
`seed_competitors.py`/`export_competitors_csv.py`. That table, model, and
both scripts have been removed — real competitor data now lives directly
in the real `market_data.competitor_count` column, city-wide, via the
Places API (New). If your live database still has a `competitor_snapshots`
table from an older run of this project, it's no longer used anywhere and
can be dropped: `DROP TABLE IF EXISTS competitor_snapshots;`.)*

### 5.2 The Saturation Map — OpenStreetMap + Leaflet
The Saturation Map (`app/templates/sme/saturation_map.html` +
`app/static/js/map.js`, also shown as a mini preview on the Home page)
renders on **OpenStreetMap tiles via Leaflet** (`L.map` + `L.polygon`
for the color-coded saturation zones, drawn as a real **choropleth map**
-- see "From circles to a choropleth, plus click-to-isolate" below).

**There is nothing to configure.** Leaflet's CSS/JS load from a CDN in
the two templates, and OpenStreetMap tiles need no API key, no billing
account and no quota. This replaced the Google Maps JavaScript API, so
`GOOGLE_MAPS_JS_API_KEY` is no longer used for anything and the "Map
preview is unavailable" placeholder that used to appear without it is
gone. (`GOOGLE_PLACES_API_KEY` is a *different* key and is still what
fetches the competitor data in 5.1.)

Two things improved for free in the switch:

- **Scroll-to-zoom just works.** Leaflet zooms on an ordinary wheel or
  trackpad scroll. Google Maps' default "cooperative" gesture mode
  required ctrl+scroll and showed a "use ctrl+scroll to zoom the map"
  overlay on a page that also scrolls — and that overlay is what used to
  swallow the first click on a barangay, which is why isolating a
  barangay seemed to need two clicks.
- **No key, no billing, no quota** for the basemap.

#### Attribution — and one honest caveat you should know before your defense

Two separate obligations apply to this map, and they are not the same
thing:

1. **OpenStreetMap (satisfied).** The tiles are ODbL-licensed, so the
   tile layer carries the required "© OpenStreetMap contributors"
   credit, and the page repeats it under the map for the city-boundary
   data.
2. **Google Places (attribution satisfied; see the caveat).** The
   competitor counts drawn on this map come from the Google Places API.
   Google's Places policy requires Places-derived content to carry
   Google attribution "near the top or bottom of the content and in the
   same visual element", so `addGoogleAttributionControl()` in `map.js`
   pins a **"Powered by Google"** mark inside the map's own top-left
   corner.

**The caveat.** Google's Maps Platform Terms of Service **section 10.5**
says: *"You must not use the Content in a Maps API Implementation that
contains a non-Google map."* This page is an OpenStreetMap basemap
displaying Google-Places-derived competitor counts, so the "Powered by
Google" mark satisfies the ATTRIBUTION requirement but does **not**
resolve that separate restriction. This was a deliberate project
decision — the OSM basemap was wanted, and the Places data is what makes
the competitor counts real — and it is written down here rather than
hidden so you are not caught out if a panel member asks. If you would
rather be strictly compliant, there are two clean ways out, and neither
requires touching anything else in the app:

- **Go back to a Google basemap** for the Places data (revert this
  section: load the Maps JavaScript API and swap `L.map`/`L.polygon` for
  `google.maps.Map`/`google.maps.Polygon` in `map.js`), or
- **Replace the competitor data source** with a non-Google one —
  OpenStreetMap's own POI data through the Overpass API is the natural
  match for an OSM basemap, and `places_service.py` is the only file
  that would change, since everything downstream just reads
  `market_data.competitor_count`. Expect thinner coverage than Google's
  in Tarlac City.

#### Barangay positions are REAL, sourced once, and no longer depend on live geocoding

Each barangay's saturation zone is drawn at its own real coordinate —
see `app/services/geocoding_service.py` and
`app/static/data/barangay_coords.json`.

This replaced a genuine accuracy bug, and the bug took **three rounds**
to actually fix, which is worth being honest about:

1. The app originally plotted every barangay using
   `app/static/data/barangay_coords.json`, a file of *placeholder*
   points spread across the city's bounding box by a spiral formula.
   They looked plausible but were not real, so a zone labelled "Tibag"
   could be drawn on top of a completely different barangay.
2. The next two rounds tried to fix this with **live Google geocoding**
   (Geocoding API, then a Places API fallback, then a browser-side
   `google.maps.Geocoder` fallback with rate-limit backoff). This
   improved the *logic*, but a barangay whose coordinate had already
   been cached — right or wrong, from any earlier round — was never
   looked up again, because the cache was designed to never be
   overwritten. A bad answer written before a fix could (and did)
   survive the fix indefinitely. That is almost certainly why "Laoang"
   kept rendering next to "Sapang Maragul" even after the geocoding
   logic itself had been corrected: two placeholder points that
   happened to land near each other, cached long before NAME_MISMATCH
   detection or bounds-checking existed to catch it.
3. This round replaces the *data*, not just the logic. Every one of the
   76 official Tarlac City barangays now ships with a real coordinate
   in `app/static/data/barangay_coords.json`, individually looked up
   from **PhilAtlas** (philatlas.com), a Philippine geographic reference
   site that compiles official PSA/NAMRIA location data one page per
   barangay (see that file's own `_note`/`source` fields for the
   citation). "Laoang" is `15.5465, 120.5452`; "Sapang Maragul" is
   `15.5051, 120.5492` — about 4.6 km apart, not the same spot.

How it works now:

1. `GET /api/barangay-coords` answers the 76 shipped barangays straight
   from that static file — no network call, no API quota, no dependency
   on which Google API happens to be enabled on your key. This is why
   the map is accurate **even with no API key configured at all**.
2. Live Google geocoding (Geocoding API, falling back to Places API
   (New) Text Search, both bounds-checked against Tarlac City and
   cached in `instance/barangay_coords_cache.json`) is kept **only** as
   a fallback for a location name that ISN'T one of the 76 — a typo, or
   a barangay/subdivision name a future LGU upload or SME plan
   introduces. That is genuinely rare, so this path has no throttling,
   retry-backoff, or diagnostic UI anymore — the complexity that used to
   exist for "resolve all 76 from scratch" simply isn't needed once the
   76 are always already known.
3. A geocode is still **rejected unless it falls inside Tarlac City's
   rectangle** — a wrong coordinate is worse than a missing one, so bad
   fallback geocodes are discarded rather than plotted.

If you ever need to look up a name outside the shipped 76 by hand:
```
python precompute_barangay_coords.py "Some New Location"
```
It prints the resolved coordinate (same bounds-checked code path the
app uses) for you to add to `barangay_coords.json` yourself, with its
own source note — that file is curated data, not something a script
should silently rewrite.

#### From circles to a choropleth, plus click-to-isolate

The Saturation Map used to draw each barangay as two soft, translucent
circles centered on its coordinate — it read as a heatmap, not a map of
actual areas. It's now a real **choropleth**: every barangay is a
filled polygon **cell**, computed by `app/services/choropleth_service.py`
and served from `GET /api/barangay-choropleth`.

**The map's outer shape is now Tarlac City's REAL, surveyed border** —
`app/static/data/tarlac_city_boundary.json`, OpenStreetMap's own
administrative boundary polygon for Tarlac City (relation `15585454`,
317 vertices, fetched via Nominatim; ODbL-licensed, which is why the
map credits **"(c) OpenStreetMap contributors"** near the legend — see
`app/services/choropleth_service.py`'s module docstring for the exact
citation and how the file was validated: every one of the 76 real,
PhilAtlas-sourced barangay coordinates falls inside it). This directly
replaces an earlier version of the choropleth that clipped to
`places_service.TARLAC_CITY_BOUNDS`, a plain bounding **rectangle** —
which is why the map used to shade a big square that visibly bled into
Victoria, Gerona, San Jose, La Paz, and Concepcion well past the city's
real limits. The real outline is also drawn on its own, as a bold
outline layer (`GET /api/tarlac-city-boundary`), so the city's actual
border is visible regardless of which barangay cells are shown.

> **Update (revisions round):** the map now draws the **official PSA/NAMRIA
> barangay boundaries** (2023, `phl_admbnda_adm4_psa_namria_20231106`,
> distributed by UN OCHA on HDX), extracted unsimplified for Tarlac City's
> 76 barangays into `app/static/data/barangay_boundaries.json`. All 76
> PhilAtlas points fall inside their own official polygon, and the
> polygons' outer edge matches the OpenStreetMap city outline. The
> computed cells described below are kept only as a fallback if that file
> is missing. Names written differently in the data ("Baras" for
> Baras-baras) are folded into the official barangay at start-up
> (`seed_data.canonical_barangay`, `startup_migrations._merge_barangay_aliases`).
>
> The map also has a **month timeline** (January 2020 → a year ahead):
> history uses counts on file for that month, or back-projects along the
> PSA/DTI national MSME series from the first count on file; the future
> is the Random Forest's prediction from each barangay's own recorded
> trend (national series otherwise), with confidence falling by horizon.
> See `app/services/saturation_timeline_service.py`.

The paragraphs below describe the earlier computed cells: at the time, no
per-barangay **boundary** dataset for Tarlac City had been found — only
the real point coordinates covered above. So while the map's OUTER edge is now the real city
border, the internal lines dividing one barangay's cell from its
neighbor's are still a computed nearest-neighbor (Voronoi) partition:
the cell for a barangay is "every point inside the real city boundary
that is closer to that barangay's real coordinate than to any other
barangay's." That's a precise mathematical property, not a guess —
every cell is verified (see `tests/test_choropleth.py`) to contain its
own barangay's real point and no other's, and the cells' areas sum to
Tarlac City's own real area almost exactly (>99.99%, the rest being
ordinary raster-grid rounding — see "how the cells are computed"
below). What it does **not** claim is that a cell's INTERNAL edge
traces a real administrative line (a river bend, a road) — the map's
legend caption says so directly, the same way the household income gap
below is disclosed rather than papered over. If a real per-barangay
boundary dataset becomes available, only `choropleth_service.py` needs
to change.

**How the cells are computed:** a fine grid (900x720 points) is laid
over the city, each grid point is tested against the real city polygon
and assigned to whichever barangay's real coordinate is nearest, and
the resulting raster region for each barangay is traced back into a
smooth vector polygon with marching squares (`contourpy` — the same
contouring engine matplotlib uses). This avoids needing a general
polygon-clipping library (e.g. `shapely`) just to intersect a Voronoi
cell against a real, non-rectangular (concave) boundary — a computation
this project's own dev sandbox couldn't even install `shapely` to
verify against. A barangay whose cell the real, concave city outline
happens to split into more than one piece (this does happen — e.g.
Balanti, Burot) is served as a GeoJSON `MultiPolygon` rather than
silently dropping a piece; `map.js` renders it as one `L.polygon` with
every piece as its own ring group. (Nesting depth matters here: Leaflet
reads a two-level ring array as one polygon with a HOLE, so
`geometryToPaths()` normalizes every geometry to the three-level
multipolygon form — otherwise Burot's second piece would be punched out
of its first.)

**Click a barangay — on the map, in the "All Barangays" list, or by
searching its name — and every other barangay's cell and list row
disappears**, leaving only the one you picked (the detail panel updates
to match). This happens on a **single click**: Leaflet puts no gesture
overlay on top of the map, so nothing intercepts the first click the way
Google Maps' "use ctrl+scroll to zoom" overlay used to — that overlay is
why isolating used to seem to need two clicks even though
`isolateAndSelect()` itself was always single-click by design. **Double-click that same barangay again** — on the map or in
the list — **or click the "Show All" banner/button, and every barangay
comes back.** Searching the same name a second time toggles the same
way (isolate, then search again to restore). All four entry points
(map click, list click, list double-click, search bar) share one piece
of state (`dssIsolatedLocation` in `map.js`), so the map and the "All
Barangays" list can never disagree about which barangay, if any, is
isolated. Isolating overrides the legend's tier filter and the "Hide
Saturation" toggle — it's an explicit "show me just this one," so it
always wins. The page's default view (on first load, or after switching
industry) still shows all 76 barangays; isolating only ever happens from
an explicit click, double-click, or search.

**Map controls.** Leaflet zooms on an ordinary wheel/trackpad scroll
with no modifier key and no interstitial overlay, so both the
"ctrl+scroll to zoom" annoyance and the two-clicks-to-isolate symptom it
caused are gone by construction — see section 5.2 above.

#### Sociodemographic overlay: population density (real), household spending/demand (real, PSA FIES) — and why there's still no household income layer

The LGU module's saturation-map objective calls for a sociodemographic
overlay of "population density and household income." The map's
**Overlay** selector switches the shaded zones between **Market
Saturation** (the AI cluster tiers) and **Population Density**, coloured
from the same real 2024 PSA population ÷ land-area figures used
everywhere else in the app (`population_density` in
`app/ml/seed_data.py` — real for 73 of 76 barangays, dataset-average
estimated for the 3 whose land area isn't published; see that file's own
docstring).

**Every barangay's hover tooltip and detail panel also now shows "Est.
Household Demand"** — real PSA Family Income and Expenditure Survey
(FIES) figures for what a typical Tarlac household spends per year on
food, utilities (housing/water/electricity/gas), transportation,
recreation, and education (`app/services/socio_demographic_service.py`).
Two real numbers are combined to produce it: Tarlac province's own real
2023 average annual family expenditure (₱299,670, from PSA Region III's
FIES special release) multiplied by the PSA's own real NATIONAL
percent-share breakdown by category (food 40.9%, housing/utilities
22.9%, transportation 10.7%, education 3.0% — from PSA's 2023 FIES
Infographics release; recreation's ~2.0% share is carried over from a
separate 2021 PSA/CPBRD factsheet, since the 2023 release folds
recreation into a combined "Other" bucket, and is flagged with a `*`
everywhere it appears). The resulting peso amount is therefore an
**estimate** — Tarlac's real total spend localized by the national
spending pattern, since PSA does not publish a Tarlac- or
barangay-specific category breakdown anywhere — and, like population
density's honest limitations, this is stated directly in the tooltip
and detail panel text, not left implicit. Because no barangay-level
household spending dataset exists either, this is **one figure for the
whole city**, identical on every barangay's tooltip (unlike population
density, which genuinely varies barangay-to-barangay) — the tooltip
says "Tarlac City avg." for exactly this reason. Full sourcing,
including every URL checked and why each was or wasn't used, is in
`Reference/DATASETS.md` section 7.

Household INCOME (as opposed to spending) is deliberately **still not**
shown as an overlay: no dataset publishes real household income at the
barangay level anywhere in the Philippines (PSA's Family Income and
Expenditure Survey stops at region/province level for income, same as
for the spending-category breakdown above) — this was researched and
documented in `Reference/DATASETS.md` earlier in this project. Rather
than invent a number for 76 barangays and label it as data, the map
says so directly in the small print under the legend and leans on the
two sociodemographic figures it CAN back with a real, sourced number
(population density and household spending/demand, both above). See
`Reference/CAPSTONE_ALIGNMENT.md` for the full objective-by-objective
comparison against the capstone paper.

The map shows 4 color-coded saturation tiers (Low="High Opportunity"
`#22c55e` green, Moderate="Low-Moderate" `#fde047` **light yellow**,
High="Moderate" `#f97316` **bright orange**, Saturated="High Saturation"
`#b91c1c` **deep red** — same underlying AI cluster thresholds as
everywhere else in the app, see `app/ml/constants.py`).

Getting these apart took two passes. First `#eab308` / `#f59e0b`
(yellow vs amber) were nearly identical as translucent circles; then
`#ea580c` / `#ef4444` (orange vs red) were too — OKLab lightness 0.646
vs 0.637, a 0.009 gap, which is why they still read as one colour on the
map. The current orange and red sit at 0.705 vs 0.505: a normal-vision
ΔE of 21, against a readability floor of 15.

**Hovering a zone** names the barangay, its overlay-appropriate reading
(saturation tier or population density), the industry being viewed, the
number of businesses, that barangay's real 2024 PSA population/density
and its saturation %. A status line under the map confirms all 76
positions are real, surveyed locations (and would name any exception, if
a non-standard location name couldn't be placed at all).

**The legend is also a filter.** Each tier is a button showing how many
barangays currently fall in it; click it to hide/show that tier's zones,
with Show All / Hide All alongside. A tier that is filtered off greys out
and is struck through, so its state doesn't depend on colour either.
Filtering changes only what is drawn — the barangay list, the detail
panel and every figure stay exactly as they are.

Change the colours in one place —
`CLUSTER_COLORS` in `app/static/js/map.js`, mirrored by `--dss-yellow` /
`--dss-orange` in `app/static/css/style.css` and the three legend blocks
(`sme/saturation_map.html`, `sme/home.html`, `lgu/dashboard.html`).

There is also a search bar for
any barangay + industry combination, a "Hide Saturation" toggle, a detail
panel (Total Businesses / the three largest industries in the barangay / real 2024 PSA
Population / Density Score / Est. Household Demand / Recommended Actions),
and a scrollable "All Barangays" list. Business/competitor data plotted
on the map still comes
from the real, live Google Places API (New) calls described in 5.1 — the
Maps JavaScript API key in this section only controls the map *tiles*
themselves.

### 5.2b Proving the Places API is really being used (TEMPORARY panel)

The LGU **Trend Reports** page ends with a yellow-bordered
**"Google Places API — Fetched Data"** card listing every
(industry × barangay) lookup whose result was stored with
`source = "Google Places API"`, plus a running count of how many combos
are still simulated. It exists so you can *see* real Google data
arriving rather than trusting that it is.

Only real Tarlac City barangays are listed. Location is free text
everywhere in this schema, so an upload or a typed plan can introduce a
name that isn't a real barangay — a stray "Baras" beside the actual
"Baras-baras", a misspelling, a subdivision name. Those have no PSA
population, no land area and no reference profile behind them, so the
dashboard tables (this one and the LGU Dashboard's "All Barangays") leave
them out instead of showing rows of blanks. Nothing is deleted — the
underlying `market_data` is untouched and still feeds the totals and the
AI engine's own averages; see `_has_real_population()` in
`trend_analytics_service.py`.

**It is meant to be deleted.** Remove these three things together:

1. the `<div class="dss-card border-warning" id="placesProofCard">` block
   and the `loadPlacesProof()` function in
   `app/templates/sme/trend_reports.html`
2. the `/api/places-fetched` route in `app/controllers/api_controller.py`
3. `get_places_api_rows()` in `app/services/trend_analytics_service.py`

#### Fetching everything at once

Ordinary page loads only upgrade a handful of rows each, which is right
for a dashboard but a slow way to fill a database. The panel has a
**"Fetch all business data from Google"** button that pulls the whole
grid in stoppable batches with a progress bar
(`/api/places-refresh`, `app/services/market_refresh_service.py`).

> **This costs real money.** A full sweep is 20 industries × 76
> barangays = **1,520 lookups**, and with paging on, each can be up to 3
> Text Search requests — roughly **4,560 billable Places calls** against
> the billing account attached to your key. It runs in batches, you can
> stop at any point, and the most populous barangays and most common
> industries are fetched first, so stopping early still leaves the map
> and charts right where anyone will actually look.

#### Two bugs that made the cap look unfixable

**1. The migration never ran.** `places_max_results` shipped as `0`
(unlimited), but `SystemSetting.ensure_defaults()` was only ever called
by `seed.py`, the Admin settings save, and the tests — *never by the
running app*. So an existing database kept the `"20"` row it was seeded
with months earlier, and every lookup stayed capped no matter what the
code default said. The symptom was a verification table where literally
every row read exactly "20 businesses found". `create_app()` now runs
`app/services/startup_migrations.py` at boot, which applies defaults
whose meaning changed — without overwriting a value an Admin chose
deliberately.

**2. Rows already fetched under the cap never refreshed.** A row holding
real Google data was cached for the full 30 days, so the ~1,800 combos
captured at 20 would have stayed at 20 for a month. The migration
records a **watermark** (the highest `market_id` at upgrade time); any
Google row at or below it is treated as stale-by-content and gets one
re-fetch, after which its replacement sits above the watermark and is
cached normally.

#### Why your data may still say "simulated"

`find_or_create_market_data()` caches each snapshot for 30 days. Before
this version, a row cached *while no API key was configured* was stored
as simulated (`source = "Manual"`) and then reused for the full 30 days
— so adding your key to `.env` appeared to change nothing, because no
live lookup was ever attempted again.

Now a cached row is **refreshed early when it is simulated and a real
key is configured**. To keep that from turning one Trend Reports load
(8 industries × 76 barangays = 608 combos) into 608 sequential HTTP
calls, each web request upgrades at most
`_MAX_LIVE_REFRESH_PER_REQUEST` (8) rows. Your database therefore
converts to real Google data progressively as you use the app, rather
than stalling on one enormous page load. Rows that already hold real
Google data are never re-fetched early.

### 5.3 GPT / Claude API (optional — the AI-Powered Recommendation)
See section 7 below ("Which AI should we use?").

#### The Recommendations page: 15+ real location opportunities

The storyboard's recommendations page ends with "our AI has identified
15+ additional opportunities". That list is now real, and it answers one
specific question: **"where else in Tarlac City could I open this same
business?"**

For the industry on your own business plan, `rank_location_opportunities()`
(`app/services/location_opportunity_service.py`) scores **every** barangay
in the city and returns the best 18 as storyboard-shaped cards — a
viability score, key metrics, "Why This Works" and "Considerations".
Nothing on a card is invented:

| What the card shows | Where it comes from |
|---|---|
| Competitor count | `market_data.competitor_count` for that industry+barangay — a live Google Places count when a key is set, a clearly-flagged simulated estimate when it isn't. Micro businesses are already excluded (5.1). |
| Population | That barangay's real 2024 PSA population. |
| Population density | Real people/km² (2024 PSA population ÷ the barangay's own published land area). |
| Viability / saturation / confidence | The Random Forest's own output for that industry+barangay. |
| Residents per business | Population ÷ competitor count. |
| Your Capital / Est. Break-even | Your own plan's inputs — not a made-up "investment range". |

**Every reason and consideration is a sentence about one of those
numbers, usually compared against the city median for the same
industry.** "Only 3 competitors" means nothing on its own; *"3
competitors against a city median of 13, with 2,851 residents each
versus a median of 341"* is a finding. That comparison is why the engine
scores all 76 barangays before ranking any of them. Where the data is
weak, the card says so — a barangay whose count is simulated rather than
live carries that as a Consideration.

**How the list is ordered (and why it isn't just "best viability
first").** Ranking on the model's viability alone puts the city's
smallest rural barangays on top: almost nobody competes in a barangay of
800 people, so it scores as gloriously uncontested — a true statement
about competition and a useless recommendation about where to open a
business. The order instead blends three real measures, each as that
barangay's percentile among all those scored:

- **50% viability score** — the model's own output: how uncontested.
- **30% residents per existing business** — market depth.
- **20% population** — absolute market size, which is what stops a tiny
  barangay from topping the list on emptiness alone.

Those weights are a **stated editorial choice**, not something derived
from the data, and they live in one place (`RANK_WEIGHTS`) so they can
be defended, argued with, or changed. Each card prints its own three
percentiles so a reader can see exactly why it placed where it did, and
the viability score shown is always the model's own unmodified number —
the blend affects the ORDER, never a displayed figure.


### 5.3b Gmail App Password (optional — SME/LGU registration email verification)
When set, `auth_controller.py` emails a 6-digit code to anyone registering
as an SME or LGU before their account is actually created (see
`app/services/email_service.py`). **You cannot use your normal Gmail
password for this** — Google requires an "App Password" instead:

1. Go to https://myaccount.google.com/security and turn on **2-Step
   Verification** if it isn't already on (App Passwords don't exist
   without it).
2. Go to https://myaccount.google.com/apppasswords (search "App
   Passwords" in your Google Account if that link doesn't work directly).
3. Create one — name it anything (e.g. "SME DSS") — and Google shows you a
   16-character password ONCE. Copy it immediately.
4. In `.env`, set:
   ```
   GMAIL_ADDRESS=your.address@gmail.com
   GMAIL_APP_PASSWORD=the16charpasswordnospaces
   ```

**Without this configured**, registration works exactly as it did before
this feature — the account is created immediately, no code, no email
sent. Nothing breaks either way; this is purely additive.

### 5.4 Internal JSON API (`app/controllers/api_controller.py`)
This is the "API connections (preparation)" layer the front-end JavaScript
talks to. It's already fully implemented — nothing to configure:

| Endpoint | Used by | Returns |
|---|---|---|
| `GET /api/locations` | (available for future use) | every location name the app has any data for |
| `GET /api/locations-forecast?industry_type=...` | map.js | one ephemeral AI score per known location, for that industry |
| `GET /api/forecast?industry_type=&location=` | sme_search.js (SME Home search bar), map.js (deep links / unknown pins) | one ephemeral forecast, no DB write |
| `GET /api/places/nearby?location=&industry_type=` | map.js | real (or simulated) competitor list for the detail panel |
| `GET /api/barangay-detail?location=&industry_type=` | map.js (Saturation Map detail panel) | Total Businesses, the top 3 industries by business count, real population, density score, recommended actions for one barangay |
| `GET /api/barangay-coords` | map.js | `{name: {lat, lng, source}}` for every known location -- the real, static PhilAtlas coordinates plus any live-geocoded fallback (section 5.2) |
| `GET /api/barangay-choropleth` | map.js (Saturation Map choropleth) | a GeoJSON `FeatureCollection`, one `Polygon`/`MultiPolygon` cell per known barangay, clipped to Tarlac City's REAL boundary (see section 5.2's "From circles to a choropleth" and `app/services/choropleth_service.py`) |
| `GET /api/tarlac-city-boundary` | map.js (Saturation Map choropleth) | Tarlac City's own real outline, as a single-feature GeoJSON `FeatureCollection` (OpenStreetMap, ODbL) |
| `GET /api/trend-data?industry_type=&period=` | trend_reports.html (shared by SME/LGU/Admin) | blended chart data -- the 76-barangay baseline + every real forecast/plan on file; `period` (6m/12m/24m/36m) is the Select Period control (see section 6b) |
| `GET /api/notifications` | main.js | the current user's notification bell items |
| `POST /api/notifications/<id>/read` | main.js | marks one notification read |

---

## 6. The AI Forecasting Engine (how a score is calculated)

`app/services/forecasting_service.py` has two entry points (see section 0.2
for why): `compute_scores(industry_type, location)` (ephemeral) and
`generate_forecast_for_profile(sme_profile)` (persists a `forecast_result`
row). Pipeline:

1. **Market snapshot** — `find_or_create_market_data()` gets/creates a
   `MarketData` row: `competitor_count` from `places_service.py` (Google
   Places Text Search, or simulated fallback), plus
   `population_density`/`foot_traffic_index`/`average_rent`/
   `historical_success_rate` from the reference barangay profile
   (`app/ml/seed_data.py`) or the dataset's own running average.
2. **LGU snapshot** — `find_or_create_lgu_data()` gets/creates an `LguData`
   row for `business_density`, preferring a REAL LGU upload if one exists,
   else an auto-generated placeholder (see section 0.3).
3. **Feature vector** (8 features, `app/ml/constants.py` `FEATURE_NAMES`):
   `[competitor_count, population_density, foot_traffic_index,
   average_rent, historical_success_rate, business_density,
   years_in_operation, industry_type_encoded]` — every single one is a real
   column from `market_data`/`lgu_data`, plus `years_in_operation` derived
   from `sme_profile.registration_date`.
4. **Saturation Index (0–100)** — predicted by a trained
   **RandomForestRegressor** (100 trees). If the model hasn't been trained
   yet, falls back to the transparent weighted formula
   `saturation = w1*CD + w2*DT + w3*SD` so the app never breaks.
5. **Confidence level (0–100)** — NEW, and only possible because it's a
   Random Forest: computed from how much the individual trees agree with
   each other (low disagreement across `rf_model.estimators_` predictions =
   high confidence).
6. **Cluster label** (Low/Moderate/High/Saturated) — derived live from
   `saturation_index` via fixed thresholds (see section 0.4) — NOT stored.
7. **Viability Score (0–10)** — derived from the saturation index, shown to
   users.
8. **AI-Powered Recommendation** — `recommendation_service.py` builds the
   comparison: the SME's OWN input parameters from this step's
   `sme_profile` (sub-category, offering, innovation idea, price list,
   capital, employees, business stage -- monthly revenue is no longer collected)
   against a real competitor sample for this industry+location (from the
   `market_data` snapshot above, upgraded with real Google Places names
   when that snapshot isn't a simulated fallback) and this barangay's real
   PSA population, into a structured headline / opportunity type / summary
   / "Why This Works" reasons / "Considerations" risks dict — rewritten by
   GPT-4o-mini (or Claude) when `use_llm_recommendations` is on, otherwise
   a deterministic rule-based version of the same shape — JSON-serialized
   into the single `recommendation` TEXT column (see section 0.5).
9. **Early warning** — only for `generate_forecast_for_profile()`: if
   `saturation_index` crosses the alert threshold (default 75 on the 0–100
   scale, tunable in Admin > System Settings), a `Notification` row is
   created for that SME's profile owner.

### Training the model
```bash
python -m app.ml.train_model
```
This is also run automatically once by `seed.py`. It trains on a
**synthetic dataset** derived from the paper's own MSI formula (see the
big comment at the top of `app/ml/train_model.py` for exactly why and how)
because no real historical SME survival data has been collected yet. It
also runs a K-Means (K=4) pass for methodology-chapter evidence (see
section 0.4). It prints and saves real **MAE / RMSE / accuracy%** numbers
to `app/ml/model_store/training_report.json` — verified during development
to reach **~96.6% accuracy** (`100 - MAE`, both on the 0–100 saturation
scale) on the synthetic test split, comfortably above the paper's 85%
target. **Re-run this command after you upload real DTI/PSA/LGU historical
data** so the model learns from reality instead of the synthetic bootstrap
— see `Reference/DATASETS.md` for where to get that real data. It was
also deliberately re-run after `BUSINESS_TYPES` grew from 24 to 46
categories (section 5.1) so the model's industry vocabulary covers the
new ones too — the synthetic generator samples `industry_type` uniformly
and doesn't hand-craft per-industry logic, so this is a safe, ordinary
re-run, not a special case.

### 6a. The LGU Dashboard

`GET /lgu/dashboard` (`lgu_controller.dashboard()`,
`app/templates/lgu/dashboard.html`) is the LGU's city-wide counterpart
to the SME Home page, and now uses the **same layout**: a hero with a
city-wide search bar, a row of live industry cards, then the analysis
panels — while keeping every LGU planning stat the page already had
(Barangays On File, Forecasts Generated, Saturated Zones, Opportunity
Zones, Diversification Guidance).

Two things are worth knowing:

- **Industry cards are sampled, not swept.** Scoring 8 industries ×
  76 barangays would be 608 model calls on a dashboard, so each
  industry's Market Score is averaged across the city's 6 most populous
  barangays — a small, fixed, deterministic sample of the built-up core
  where competition actually concentrates. The full all-barangay sweep
  still happens on Trend Reports, which is the page built for it. The
  page states which barangays it sampled.
- **"All Barangays" replaces the old name badges.** Every barangay is
  listed with how many businesses are on file, how many of those came
  back from a live Google Places lookup (`live` badge), how many
  industries have been surveyed there, and its real 2024 PSA
  population. It is filterable, and clicking a row opens that barangay
  on the Saturation Map. Built by
  `trend_analytics_service.get_barangay_business_table()` — pure
  database reads, no AI sweep and no HTTP, so it stays cheap.

### 6b. Trend Reports & Analytics -- one shared dashboard

ONE city-wide dashboard, shared by SME, LGU and Admin
(`sme_controller.trend_reports()`), laid out to the paper's Figure 4 /
4.1:

- **The city-wide report** (`GET /api/trend-data`,
  `app/services/trend_analytics_service.py: build_trend_report()`).
  Unchanged from before: a full sweep of the 76-barangay reference
  dataset blended with every real `ForecastResult`/`SmeProfile` on
  file, so the page is never empty on a fresh install and gets more
  "real" as usage accumulates. **Total Businesses / Industry
  Distribution** are real counts: the freshest
  `market_data.competitor_count` for **every** (industry, barangay)
  combo on file -- Google Places rows *and* PSA/DTI/reference rows --
  plus every registered `SmeProfile`.

  > **Fixed in this version.** Both figures previously counted only
  > rows whose `source` was exactly `"Google Places API"` and discarded
  > everything else. On a database with thousands of market_data rows
  > but no live lookup yet, that made **Total Businesses** read `1` and
  > collapsed the **Industry Distribution** pie to a single 100% slice
  > (whichever industry an SME happened to register a plan in). Every
  > source is counted now, and each slice still carries its own
  > `from_places_api` / `from_market_data` / `registered_plans` split,
  > so the KPI card can state exactly where its number came from.
  > Regression tests: `tests/test_analytics_and_geocoding.py`.

  **Every industry gets its own slice** — there is no grouped "Other"
  bucket. (An earlier version collapsed everything past the top 8, which
  turned ~16 real industries into one dominant grey wedge and hid the
  very distribution the chart exists to show.) **Monthly Industry
  Trends** charts that same full list, so the two panels agree.

  Past roughly 8 categories colour stops being a reliable identifier,
  and no choice of 24 hues fixes that. So the palette extends 8
  colour-vision-checked base hues with a darker and a lighter step of
  each, ordered so neighbouring slices are always different hues — and
  **identity is carried by text**: every industry is listed by name with
  its count and share beneath the chart, slices carry a 2px separating
  border, and hovering names the slice and breaks down where its
  businesses came from. Both panels share one colour map, so an industry
  looks the same in the pie and in the trend lines.

  **Quarterly Performance & Growth Rate** is now about the MARKET, not
  about registered plans. It used to sum `monthly_revenue_est` across
  every `SmeProfile`, which on a real deployment is one or two plans —
  so the line sat flat at a fraction of a million pesos and said
  nothing. Each quarter now reports the real number of businesses on
  file at that quarter's end (the freshest `market_data` snapshot dated
  on or before its last day — `market_data` keeps one row per refresh,
  so that is genuine history), plus the quarter-over-quarter change.
  An **industry dropdown** picks which market to review; it calls the
  lightweight `/api/quarterly-performance` endpoint, which is pure
  database reads, so switching industry is instant and does not re-run
  the city-wide AI sweep. Quarters older than the first snapshot on file
  can't be measured, so they are back-projected along the published
  national establishment series, **flagged `projected`, and drawn
  hollow**, with the caption saying how many quarters are measured
  versus projected.

  > That projection used to be a flat **8% per quarter** — 36% a year,
  > compounding, in every quarter of every year. No pandemic, no
  > rebound, just a smooth exponential nobody measured; it is where this
  > chart's "made-up" feel came from. It now follows the real series, so
  > the growth rate reported for mid-2020 is **negative**, because that
  > is what actually happened to Philippine establishments
  > (`test_quarterly_growth_is_negative_through_the_2020_contraction`).
  > And picking a 2020-2025 month no longer draws a flat line of zeros
  > on the floor: when every quarter in the window predates the first
  > snapshot, the whole window is projected from today's real total
  > rather than returned as nothing.

  A **loading bar** covers the first city-wide sweep, which takes a few
  seconds; the cards and canvases used to sit visibly blank until it
  finished.
**Every role sees the same page.** SME, LGU and Admin all open the same
city-wide Trend Reports & Analytics dashboard.

An earlier version gave SME accounts a separate per-plan "My Trend
Report" instead, built from their own saved plans. That was removed, and
the reason is worth stating plainly for your defense: a chart drawn from
one user's one or two saved plans is not a market trend, it is a
restatement of their own inputs. The paper's own Figure 4 / 4.1
describes this page as market indicators, quarterly performance and
industry growth distribution — all city-wide — so that is what every
role now gets. An SME's plan-specific numbers still live on Home and on
Recommendations, where they belong.

**Laid out to the paper's Figure 4 / 4.1:** summary market indicators,
Monthly Industry Trends, Industry Distribution, Quarterly Performance &
Growth Rate, and Top Performing Industries.

**Quarterly Performance & Growth Rate is one chart, not two.** Figure
4.1 shows both series together and that pairing is the useful part:
demand still climbing while the growth rate flattens is a market
maturing, which two charts side by side made much harder to read. So it
is a dual-axis combo — businesses on file as a filled area on the left
(a count) and quarter-over-quarter growth on the right (a percentage).
The two axes are labelled and coloured to their series, because a shared
scale between a count and a percentage would be meaningless. Quarters
with no real snapshot behind them are drawn with hollow points and named
in the caption.

#### Select Period, and where the history actually comes from

**Select Period is a real month picker** — `<input type="month">`, so
the browser's own calendar offers **year and month only, no day grid**,
which is exactly what "as of this month" means. It runs from **January
2020 to the current month**. Every series ENDS at the month you pick, so
choosing June 2021 reviews the market as it stood in June 2021: the four
KPI cards, the monthly trend lines and the quarterly chart all move
together.

**Google Places cannot supply history, and it is important to say so
out loud.** The Places API answers "what is there now". There is no
parameter, endpoint or field anywhere in it that returns the businesses
that existed in a past year, and no commercial API does — that is simply
not what Places is. Claiming otherwise on defense day is a claim that
would not survive one follow-up question.

So history that predates this app's own first data collection is
**modelled**, and the model is grounded in real published figures rather
than invented. `app/services/historical_baseline_service.py` holds the
Philippines' own MSME establishment counts, from DTI's *Philippine MSME
Statistics* (built on PSA's List of Establishments):

| Year | Establishments | Change |
|------|---------------:|-------:|
| 2019 | 995,741        |        |
| 2020 | 952,969        | −4.3%  |
| 2021 | 1,076,279      | +12.9% |
| 2022 | 1,105,143      | +2.7%  |
| 2023 | 1,241,733      | +12.4% |

A Tarlac City figure for, say, June 2021 is therefore not a number
anyone chose. It is **this app's own real current count**, scaled back
along that curve to where June 2021 sat on it. The level is real
(today's measured count), the trajectory is real (published national
growth), and only the attribution of that trajectory to one barangay is
modelled. The annual figures are anchored at **mid-year** rather than
January, so the series falls through the first half of 2020 and rebounds
through 2021 — the pandemic contraction is visible in the chart instead
of being hidden at a year boundary, which is exactly the feature a
plausible-looking invented curve would smooth away.

What is still an assumption, stated plainly:

1. Tarlac City is assumed to have moved with the national MSME trend. A
   city-level establishment series for Tarlac is not published (see
   `Reference/DATASETS.md` §4), so the national series is the closest
   real proxy available.
2. 2024 onward is not yet in the published series, so those years
   continue at a conservative `POST_RECOVERY_GROWTH = 0.03` — not the
   12%+ of the rebound years.
3. Each (industry, barangay) line carries a small **deterministic**
   variation, seeded from its own name, so the chart is not 20
   identically-shaped lines. It never changes between page loads.

**Nothing is written to the database.** The back-projection is computed
on the fly, every time. Inserting thousands of invented `market_data`
rows would corrupt the one table in this system that holds real
measurements: it would inflate Total Businesses, it would be
indistinguishable from a real PSA/DTI upload afterwards, and it would be
very hard to undo. There is a test for this
(`test_nothing_is_written_to_the_database`).

**Measured always beats projected.** A month with real dated rows is
computed from those rows and nothing else. Only months with none are
projected, and every one of them is flagged — `measured: false` on the
card, `projected: true` on the quarterly points, "(estimated)" on screen,
and a note under the cards naming the source and the national figure
behind that month. As the app accumulates its own snapshots, projected
points are replaced by measured ones automatically.

#### Why the cards used to say "no comparison available"

Every KPI figure came from a dated row, and this app only has rows from
the day it was first run. So the current month had a value, the month
before it had nothing, the delta was `None`, and **all four cards
printed "no comparison available" for every month anyone could pick.**
That is fixed by the grounded history above. Two details mattered:

- **New Startups** is market entry, not this app's sign-up log. It counts
  the businesses the city gained that month, plus any SME plan first
  forecast that month. Counting only registered plans gave a number with
  no history behind it at all (`sme_profile` has no `created_at`), so
  that card could never show a change for any month, ever.
- It reports an **absolute** change, not a percentage. Net new entries
  legitimately hits zero — for six months straight in the first half of
  2020, because the country was losing establishments — and a percentage
  change has no answer from a base of zero. "3 fewer than last month"
  has an answer whatever the two figures are.

The honest blank survives: on a database with nothing measured anywhere
there is nothing to scale, so the cards go back to saying so
(`test_an_empty_database_still_says_no_comparison`).

#### Changing the month is fast

Picking a month used to **reload the whole page**, which re-ran the
city-wide AI sweep (600 `compute_scores()` calls), rebuilt the Industry
Distribution pie and the Google Places verification table, and
re-downloaded every asset — to change four numbers and three charts.
None of that depends on the chosen month. Three changes:

1. **`GET /api/trend-period`** returns only the month-dependent half:
   the KPI cards, the monthly lines, the quarterly chart and the industry
   ranking. The pie and the Places table are not rebuilt at all.
2. **The sweep and the `market_data` scan are memoised** on a
   *fingerprint* of `market_data` (row count, newest id, newest date,
   total competitor count), not on a timer. One inserted row — or an
   in-place edit, which leaves the count untouched — invalidates it
   immediately, so there is no window in which the page can show a total
   the database no longer agrees with. A 5-minute TTL is a second safety
   net, not the primary invalidation.
3. **The page updates in place** instead of navigating. Months already
   looked at are answered from an in-page cache with no network call;
   a slow reply for an abandoned month is dropped rather than painting
   over a newer one; the monthly chart is updated rather than rebuilt,
   so the legend's show/hide state survives; and `?as_of=` is kept in the
   address bar via `history.replaceState`, so the month still survives a
   refresh or a shared link.

Measured end to end in a browser: a date change went from a full page
reload plus a ~17s sweep to **0.03-0.09s**. Tests:
`test_changing_the_month_does_not_re_run_the_city_wide_sweep` and
`test_the_cache_lets_go_the_moment_market_data_changes`.


The Home page's **"Forecast & Recommendations" bar chart** (Current vs.
Projected Demand & Viability) works the same way it always did: Q1 is
real (this forecast's own AI score + that barangay's real foot-traffic
figure), Q2-Q4 are an explicitly-labelled illustrative projection, not
a second model run (see `project_quarterly_outlook()`, reused by both
the Home page and the Forecast panel).

The **Saturation Map** (and the Home page's map preview, which reuses
the same map) shades each barangay in one of **4** color-coded zones --
High Saturation (red) / Moderate (orange) / Low-Moderate (yellow) /
High Opportunity (green) -- matching the AI's own 4-level Low/Moderate/
High/Saturated cluster one-for-one (see `CLUSTER_DISPLAY_LABELS` in
`app/static/js/map.js`). There's no per-barangay polygon boundary in
the given schema, so each "shaded area" is a real **choropleth cell**
-- a nearest-neighbor partition of each barangay's real coordinate,
clipped to Tarlac City's REAL, OpenStreetMap-sourced outline rather
than a bounding rectangle (see `app/services/choropleth_service.py`
and section 5.2's "From circles to a choropleth" writeup) -- rather
than a traced per-barangay administrative outline. Clicking a barangay
isolates it (hiding every other cell and list row) until it's
double-clicked, searched again, or "Show All" is clicked.

---

## 7. Which AI should we use? (plugin/AI recommendation)

Two different AI needs, two different tools — don't use one AI for both:

**A. The actual forecasting math (viability score, saturation index) —
scikit-learn (already built in, nothing to sign up for).**
This is what the paper specifies (Random Forest Regressor, with K-Means
for the clustering methodology), and it needs to be reproducible,
explainable, and gradeable — an LLM would be the wrong tool here (numbers
must come from real math you can defend in front of a panel, not from a
language model's guess). This is 100% free, runs entirely offline, and is
already fully implemented in `app/services/forecasting_service.py` +
`app/ml/train_model.py`.

**B. Turning those numbers — PLUS the SME's own input parameters and a
real comparison against the market — into the structured "AI-Powered
Recommendation" — optionally, GPT via OpenAI/OpenRouter, or Claude via
Anthropic (`app/services/llm_service.py`).** Matches the paper's 1.2
Purpose and Description (SME Module: "AI-driven business recommendations").
This is genuinely the most beginner-friendly way to "add AI" to a school
project:

- Pick a provider with `LLM_PROVIDER=openai` (default) or
  `LLM_PROVIDER=anthropic` in `.env`. You only need ONE API key — whichever
  provider's key is set is the one used; if your chosen provider's key is
  blank, the other one is tried automatically before falling back to the
  rule-based recommendation.
- **Getting an OpenAI (GPT) API key directly:**
  1. Sign up / log in at https://platform.openai.com
  2. Add a payment method under **Settings > Billing** (the API is
     pay-per-use, not covered by a ChatGPT Plus subscription — a handful
     of recommendations costs a small fraction of a cent with
     `gpt-4o-mini`).
  3. Go to https://platform.openai.com/api-keys → **Create new secret
     key** → copy it immediately (it's shown once — starts with `sk-` or
     `sk-proj-`).
  4. In `.env`: `OPENAI_API_KEY=sk-...`, leave `OPENAI_BASE_URL=` blank,
     and `OPENAI_MODEL=gpt-4o-mini`.
- **Or route the SAME `openai` package call through OpenRouter
  (https://openrouter.ai) instead** — handy if you already have an
  OpenRouter key (it looks like `sk-or-v1-...`, not a native OpenAI key) or
  want one dashboard/one bill across multiple model providers. OpenRouter
  is intentionally OpenAI-SDK-compatible, so nothing in `llm_service.py`
  changes — only `.env`:
  ```
  OPENAI_API_KEY=sk-or-v1-...your OpenRouter key...
  OPENAI_BASE_URL=https://openrouter.ai/api/v1
  OPENAI_MODEL=openai/gpt-4o-mini
  ```
  Note the `openai/` prefix on `OPENAI_MODEL` — OpenRouter routes by
  provider-prefixed model id, so `gpt-4o-mini` alone (no prefix) would be
  rejected once `OPENAI_BASE_URL` points at OpenRouter.
- **Getting an Anthropic (Claude) API key** instead: sign up at
  https://console.anthropic.com, add billing, create a key under
  **Settings > API Keys**, put it in `.env` as `ANTHROPIC_API_KEY=`.
- Either way: `pip install -r requirements.txt` already installs both
  `openai` and `anthropic` packages, and the call for each — plus the
  grounding prompt that hands the model the SME's own inputs and the real
  competitor/population data — is already written for you in
  `llm_service.py` and `recommendation_service.py`; nothing else to code.
- **On by default** (`USE_LLM_RECOMMENDATIONS=true` in `.env`, or the
  toggle in Admin > System Settings) as long as an API key is actually
  configured. If no key is set, or the API call ever fails for any reason
  (no internet, quota, malformed JSON back), `generate_recommendation_json()`
  returns `None` and the app silently falls back to the free, deterministic,
  rule-based recommendation (`recommendation_service.py`) — a demo never
  breaks because of this, and every recommendation — LLM-written or
  rule-based — comes back in the exact same `{headline, opportunity_type,
  summary, reasons, risks}` shape, so the page never has to know which one
  produced it (a small "AI-generated" badge on the Recommendations page is
  the only visible difference).
- We recommend the smallest/cheapest model of whichever provider you pick
  (`gpt-4o-mini` / `openai/gpt-4o-mini` via OpenRouter, or
  `claude-haiku-4-5`, all already the defaults) since this task (turn some
  numbers + real comparison data into a short recommendation) doesn't need
  a large model.

You do **not** need any other "AI plugin," framework, or vector database
for this project — that would be over-engineering for what the paper
actually asks for.

---

## 8. Things you should double-check before your defense

1. **Real historical data** doesn't exist yet — the paper's own Limitations
   section acknowledges this and prescribes "data imputation" + "AI-driven
   simulation" as the fallback, which is exactly what this codebase does
   (`data_import_service.py`'s mean-imputation, `places_service.py`'s
   simulated competitor list, `train_model.py`'s synthetic training data).
   Your accuracy/MAE/RMSE numbers for the defense should be **re-generated
   after importing real data** (see `Reference/DATASETS.md`), not quoted
   from the synthetic run.
2. **`app/ml/seed_data.py` now covers all 76 official Tarlac City
   barangays** with **real** 2024 PSA population figures, a **real
   true population density** (people/km²) computed from real land area
   for 73 of the 76 (the other 3 use a dataset-average estimate,
   flagged inline — their land area wasn't published anywhere
   reachable), and PSA's own official urban/rural classification.
   **Important:** the urban/rural column is genuinely contested — PSA
   says 36 urban / 40 rural, but Tarlac City's own barangay web pages
   (taken carefully, including a hidden override sentence some pages
   have) support at most ~16–18 urban, and neither cleanly matches the
   "19 urban / 57 rural" CLUP figure originally used. See
   `Reference/DATASETS.md` section 2 for the full three-way comparison
   and why only your City Planning & Development Office can give a
   truly definitive answer here. The **rent, foot-traffic,
   business-density, and historical-success-rate** columns are still
   **estimated placeholders** — confirmed during research that no
   public dataset has real per-barangay figures for those (PSA's
   business census is public only at regional granularity; see
   `Reference/DATASETS.md` section 4 for exactly what was checked and
   the concrete next step). Replace any of this either by editing
   `seed_data.py` and re-running `python seed.py`, or (recommended, and
   now fully wired up) by logging in as an LGU account and using the
   **Gov't Data Upload** page, which writes real rows straight into
   `lgu_data`/`market_data`.
3. **Deleting a `market_data` or `lgu_data` row in Admin > Datasets cascades
   to `forecast_result`** — this is real behavior from the schema you gave
   us (`ON DELETE CASCADE` on both foreign keys), not something we added.
   The delete buttons warn about this every time; just be aware a "clean
   up bad data" click there can also silently remove SME forecast history
   built on top of it.
4. **MAE/RMSE→accuracy%**: the paper states "target ≥85% accuracy" without
   defining exactly how "accuracy %" is computed from a continuous
   regression's MAE (accuracy is normally an ML classification term, not
   great for regression like this). We used the common capstone convention
   `accuracy% = 100 - MAE` (both already on the 0–100 saturation scale);
   state this explicitly in your methodology chapter so your panel knows
   the definition you used.
5. **Admin accounts** aren't mentioned in your instructions to us (you said
   "accounts for entrepreneurs/business owners, and one for LGU"), but the
   paper's own Objectives, RBAC table, and ERD all require a full **Admin
   Module** (user management, audit trail, dataset management, system
   config) as a third role, and your real `user.role` ENUM even includes
   `'Admin'` as a value — so it's built in. Log in as `admin@dss.local` to
   see it.
6. **Saturation Map objective coverage** — see
   `Reference/CAPSTONE_ALIGNMENT.md` for a line-by-line comparison of the
   paper's LGU Dashboard / Saturation Map bullet points (sector filter,
   competitor density overlay, sociodemographic overlay, etc.) against
   what's actually implemented, including the one honest gap (household
   income) and why it's a documented data-availability limitation rather
   than a missing feature.

---

## 9. Running the tests

```bash
pytest
```
127 tests across 7 files, all using an in-memory SQLite database (never
touches your real MySQL `dss_db`) and never making a real network call:
- `tests/test_app.py` — login/registration work, an SME account gets HTTP
  403 on the LGU-only upload page, and both AI engine entry points
  (`compute_scores`, `generate_forecast_for_profile`) return sane values.
- `tests/test_analytics_and_geocoding.py` — Trend Reports counting parity
  (including the SME-personal vs. LGU pie chart fix), Saturation Map
  coordinate accuracy, and the Places API result-cap migration.
- `tests/test_recommendation_llm.py` — the AI-Powered Recommendation's
  JSON-validation logic (`_coerce_llm_payload`), the
  serialize/parse round trip and backward compatibility with the OLD
  plain-text format, the rule-based generator's shape, and that
  `build_recommendation()` correctly falls back to it whenever the LLM is
  off, unconfigured, or fails.
- `tests/test_choropleth.py` — the real OpenStreetMap city-boundary file
  and the Voronoi-choropleth computation (every cell contains its own
  barangay's real point and no other's, cell vertices stay inside the
  real city border, areas sum correctly, MultiPolygon handling), plus
  both choropleth/boundary API endpoints.
- `tests/test_business_taxonomy_and_demand.py` — the 20-section PSIC
  `BUSINESS_TYPES` list (exactly the PSIC sections, no micro or retired
  category still offered, every section has a display entry, a short
  label and a Places search term, and no search term asks Google for
  micro establishments), the micro-business filter itself (micro names
  caught, SME names never dropped), the PSA FIES-derived demand
  breakdown (exactly the 5 requested categories, amounts derived
  correctly from the real Tarlac total, recreation flagged as the softer
  estimate), the OpenStreetMap/Leaflet basemap (Leaflet in use, Google
  Maps SDK calls gone, both attributions present, correct multipolygon
  nesting), and that the Saturation Map/Home pages actually expose
  `window.DSS_DEMAND_SUMMARY`.

- `tests/test_industry_migration_and_opportunities.py` — the one-time
  PSIC migration (micro rows deleted, SME-scale rows remapped,
  duplicates collapsed, unknown LGU-uploaded categories left alone, SME
  plans remapped and never deleted, runs only once, every change logged,
  and `undo()` restoring everything), plus the Recommendations page's
  opportunity engine (15+ ranked cards, ranking that weighs market size
  rather than emptiness, every card carrying the real figures its
  reasons quote, and simulated counts disclosed as a consideration).

- `tests/test_trends_and_roi.py` — the map tooltip losing its demand
  block (and the detail panel keeping it), the AI-derived ROI Timeframe
  (margin applied, saturation moving the number, always a range), the
  Recommendations page's city-wide counts and "Explore All" view, and
  Trend Reports being one shared dashboard whose Select Period control
  genuinely changes the window without inventing history.

There is also a **Leaflet API-contract test** that is deliberately NOT
part of the `pytest` suite, because it runs under `node`, not Python:
it loads `app/static/js/map.js` against a strict stub of the Leaflet API
and fails on any wrong method name, wrong LatLng shape or wrong polygon
nesting depth — the exact class of mistake a Google-Maps-to-Leaflet port
can make. It also asserts the isolate/restore behavior, the hover popup,
the tier filter and both required attributions. Treat a quick manual
click-through of the live map as part of your own pre-defense checklist
too, since neither test renders real tiles.

Extend these files as you add features — this is also your starting point
for the paper's "Functionality Testing" chapter.

---

## 10. Running in production / deploying to a live URL

**For a step-by-step walkthrough of putting this on a public HTTPS URL
via GitHub, see [DEPLOYMENT.md](DEPLOYMENT.md).** It covers the whole
path — GitHub, Render for the app, Aiven for MySQL, moving your collected
Google data across, and the security holes a public URL opens — and the
repo already contains everything it needs (`wsgi.py`, `Procfile`,
`render.yaml`, `.python-version`, `.env.sample`).

Start with the thing most people expect and which is not true: **GitHub
cannot host this app.** GitHub Pages serves static files only — no Python,
no Flask, no MySQL. GitHub holds the code; a separate host runs it.

The short version of what production needs:

- **A real WSGI server.** `python app.py` uses Flask's built-in
  development server, which handles one request at a time and says itself
  not to use it in production. The repo ships `wsgi.py` plus a `Procfile`
  for `gunicorn`; `waitress-serve --port=8080 wsgi:app` is the
  Windows-friendly equivalent.
- **`FLASK_CONFIG=production` and a strong random `SECRET_KEY`.** The
  production config now *refuses to boot* without one, deliberately:
  Flask signs the login session cookie with that key, the development
  default is printed in `app/config.py`, and `app/config.py` is in your
  repo — so anyone who reads it could forge an Admin session.
- **HTTPS, and let the app know.** The production config applies
  `ProxyFix` so Flask sees the real scheme and host behind a proxy, and
  marks the session cookie Secure/HttpOnly/SameSite=Lax.
- **`PLACES_LIVE_FETCH=false` on any public deployment.** It serves the
  competitor data already in `market_data` and calls Google not at all. A
  full sweep is ~4,560 billable Places calls, and the button that starts
  one is reachable by anyone who can log in. DEPLOYMENT.md §9.
- **`seed.py` is safe to run against a live database, and turns its own
  demo accounts off.** Those three logins (`admin@dss.local / admin123`
  and friends) have passwords published in this repo, so on
  `FLASK_CONFIG=production` it creates none of them and creates a single
  administrator from `ADMIN_EMAIL`/`ADMIN_PASSWORD` instead.
- **Never commit your real `.env`** — or `.env.example`, which in this
  project holds real keys. Both are already in `.gitignore`; verify with
  `git status` before your first push.
