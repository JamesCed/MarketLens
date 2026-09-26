# Deploying this app to a live (www) URL via GitHub

Start with the thing that trips most people up:

> **GitHub cannot run this app.** GitHub Pages serves static files only —
> HTML, CSS, JavaScript. It has no Python, no Flask, no MySQL. There is no
> setting that changes this.

So GitHub holds the **code**, and a separate **host** pulls that code and
runs it. That is still "deploying with GitHub": you push a commit, the
host notices, rebuilds, and your live URL updates. Three pieces:

| Piece | Where it lives | Why |
|---|---|---|
| Code | GitHub | version control, and what the host builds from |
| App (Flask + the AI engine) | Render | runs Python; gives you an HTTPS URL |
| Database (MySQL) | Aiven | Render's managed database is PostgreSQL; this app is MySQL |

You end up with `https://your-app-name.onrender.com`, on HTTPS, free, and
redeploying on every `git push`.

**Everything below assumes the read-only setup you chose:** the live site
serves the Google Places data you have *already* collected and never calls
Google itself. Section 4 moves that data across; §9 explains why.

---

## What the free tiers actually give you

Checked September 2026 — worth re-reading before you rely on them.

**Render free web service:** 512 MB RAM, less than 1 CPU, 750 instance
hours per month per workspace, custom domains and managed TLS included.
It **spins down after 15 minutes with no traffic**, and waking up takes
**about a minute**.

**Aiven free MySQL:** 1 CPU, 1 GB RAM, 1 GB disk, `max_connections` 76,
backups included, **no credit card**, no expiry. Aiven may power off a
free service with no "continuative activity", with a notification first;
you can power it back on.

Two consequences worth planning around rather than discovering:

1. **Cold starts and your defence.** A sleeping Render service plus this
   app's first-request AI sweep means an unlucky first visitor waits over
   a minute. **Open your URL five minutes before you present** and it
   stays warm for as long as people keep clicking. §8 has a way to keep it
   awake.
2. **Nothing here is private by default.** A public URL is public. §5 and
   §9 close the two holes that matter.

---

## 1. Get the code onto GitHub

### 1a. First, check what you are about to publish

This is the step to slow down on. Your project folder contains **real API
keys**.

`.gitignore` already excludes `.env` **and** `.env.example` — that second
one is unusual and deliberate: in this project `.env.example` holds real
keys, so publishing it would publish them. Verify that the ignore rules
are working *before* your first push:

```bash
cd flask_webapp_dss
git init
git add -A
git status --short | findstr /I ".env"      # Windows
# git status --short | grep -i "\.env"      # macOS/Linux
```

**That must print nothing.** If either `.env` file appears, stop and fix
`.gitignore` first. A key pushed to GitHub is compromised the moment it
lands — deleting it in a later commit does not help, because it stays in
the history, and bots scan public repos for exactly this within minutes.

While you are here, confirm `venv/` is also excluded (it is 320 MB of
Windows-specific binaries, useless to a Linux host):

```bash
git status --short | findstr /I "venv"      # also expect nothing
```

**`.env.sample` IS meant to be published.** It is the template — every
variable name, no real values — so anyone cloning the repo (including
you, on another machine) knows what to fill in. Do not confuse it with
`.env.example`, which in this project holds the real keys and stays
ignored.

### 1c. What the repo should look like

```
flask_webapp_dss/
├── app.py                  <- python app.py  (LOCAL dev server)
├── wsgi.py                 <- gunicorn wsgi:app  (what Render runs)
├── seed.py                 <- prepares the database + trains the model
├── requirements.txt
├── render.yaml             <- optional: lets Render configure itself
├── Procfile                <- same start command, for other hosts
├── .python-version         <- 3.14
├── .env.sample             <- committed template, no real values
├── .gitignore
├── DEPLOYMENT.md           <- this file
├── README.md
├── app/                    <- the application package
│   ├── controllers/  models/  services/  ml/  static/  templates/
│   └── config.py
├── scripts/                <- one-off helpers, not needed to run the app
│   ├── check_apis.py               is the AI / Places key working?
│   ├── warm_cache.py               pre-build the trend sweep
│   ├── export_seed_data.py         the 76-barangay reference data to CSV
│   └── precompute_barangay_coords.py
├── sql/
└── tests/
```

Not in the repo, by design: `venv/`, `.env`, `.env.example`,
`instance/`, `dumps/`, `__pycache__/`, `.idea/`, and the 9 MB trained
model under `app/ml/model_store/` — the model is rebuilt on the host,
which also guarantees it matches the scikit-learn version installed
there.

### 1b. Commit and push

```bash
git add -A
git commit -m "SME Market Saturation DSS - initial deploy"
git branch -M main
```

Create an empty repository on GitHub — **no** README, `.gitignore`, or
licence, so nothing conflicts — then:

```bash
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

**Private or public?** Private is the safer default and Render deploys
from private repos fine. Make it public only if your panel wants to browse
the code.

---

## 2. Create the MySQL database (Aiven)

1. Sign up at <https://aiven.io/free-mysql-database> — no card needed.
2. **Create service → MySQL → Free plan.** Pick the region closest to the
   Philippines (Singapore if offered).
3. Wait for the status to go from *Rebuilding* to **Running** (a few
   minutes).
4. On the service **Overview**, copy the **Service URI**. It looks like:

   ```
   mysql://avnadmin:PASSWORD@mysql-xxxx-yyyy.a.aivencloud.com:12345/defaultdb
   ```

5. **Convert it for this app.** SQLAlchemy needs the driver named and the
   charset set. Change `mysql://` to `mysql+pymysql://` and append
   `?charset=utf8mb4`:

   ```
   mysql+pymysql://avnadmin:PASSWORD@mysql-xxxx-yyyy.a.aivencloud.com:12345/defaultdb?charset=utf8mb4
   ```

   Keep this string somewhere safe — it is your `DATABASE_URL`, and it
   contains the database password, so it never goes in the repo.

Aiven requires TLS, and PyMySQL negotiates that automatically as long as
the `cryptography` package is installed — it is already in
`requirements.txt`. If you ever do hit an SSL error, download Aiven's
`ca.pem` from the same Overview page and append
`&ssl_ca=/path/to/ca.pem`.

---

## 3. Prepare the live database

`seed.py` does this. It creates the tables, writes the AI engine's
default settings, creates the internal `system@dss.local` row, trains the
model if one is not already on disk, and creates **one** administrator —
the one whose email and password you supply.

It is safe to run again at any time: every step checks before it writes,
and it never drops or alters an existing table.

### The demo accounts turn themselves off — this is the part to understand

On localhost `seed.py` creates three fixed logins:

```
admin@dss.local / admin123
sme@dss.local   / sme12345
lgu@dss.local   / lgu12345
```

Those passwords are written in plain text **inside `seed.py`**, which is
in your public repository. On localhost that is a convenience. On a
public URL `admin123` is not a demo account — it is an administrator
login that anyone who opens your repo already has.

So when `FLASK_CONFIG=production`, `seed.py` creates **none of them**. It
creates a single administrator from `ADMIN_EMAIL` and `ADMIN_PASSWORD`
instead, and if you do not set those it creates no login account at all
and tells you so. You do not have to remember this; it is the default.

### Running it from your own machine (optional)

You can let Render run it during the build (§5, and that is the simpler
path). If you would rather do it yourself first, so you can watch it:

```powershell
# Windows PowerShell — from the flask_webapp_dss folder
$env:FLASK_CONFIG   = "production"
$env:SECRET_KEY     = "any-long-random-string-for-this-one-run"
$env:DATABASE_URL   = "mysql+pymysql://avnadmin:PASSWORD@HOST:PORT/defaultdb?charset=utf8mb4"
$env:ADMIN_EMAIL    = "you@example.com"
$env:ADMIN_PASSWORD = "a-long-password-you-choose"
python seed.py
```

```bash
# macOS / Linux
FLASK_CONFIG=production SECRET_KEY="any-long-random-string" \
DATABASE_URL="mysql+pymysql://..." ADMIN_EMAIL="you@example.com" \
ADMIN_PASSWORD="a-long-password-you-choose" python seed.py
```

`SECRET_KEY` is required even here: the production config refuses to
build the app without one, deliberately.

---

## 4. Move your collected data across (MySQL Workbench)

Your local `dss_db` holds the competitor counts already fetched from
Google Places — 2,972 businesses, 86 real lookups. That data is the whole
reason the live site needs no API key. You are copying **three tables**:

| Table | What it is |
|---|---|
| `market_data` | the competitor counts fetched from Google Places |
| `lgu_data` | any government data uploaded through the app |
| `system_settings` | your tuned engine values |

**Do not copy `user`, `sme_profile`, `forecast_result`, `notifications`,
`plan_saves` or `audit_logs`.** Those hold your local test accounts and
their password hashes, and forecasts that will be regenerated anyway.
`seed.py` already created every table, so nothing is missing.

### 4a. Add the Aiven database as a connection in MySQL Workbench

1. Workbench → **Database → Manage Connections → New**.
2. Fill in:
   - **Connection Name:** `aiven-dss`
   - **Hostname:** `mysql-38bdb7d4-project-dss.h.aivencloud.com`
   - **Port:** `23964` (**not** 3306 — this catches people)
   - **Username:** `avnadmin`
   - **Password:** *Store in Vault…* → paste your Aiven password
   - **Default Schema:** `defaultdb`
3. Open the **SSL** tab → **Use SSL: Require**, and set **SSL CA File**
   to the `certs/aiven-ca.pem` in your project folder. Aiven refuses
   plaintext connections, and this is the step people miss.
4. **Test Connection.** Fix it here, not later.

### 4b. Export the three tables from your local database

1. Open your **local** connection.
2. **Server → Data Export**.
3. Tick `dss_db`, then in the right-hand pane tick **only**
   `market_data`, `lgu_data`, `system_settings`.
4. Choose **Export to Self-Contained File**, and set the path to
   something like `dumps\dss_data.sql`.
5. Under *Advanced Options*, make sure **Create Schema** is off if you
   used §3 (the tables already exist) — or leave it on and let it skip.
6. **Start Export.**

`dumps/` is in `.gitignore`. Keep it there: that file is your entire
dataset.

### 4c. Import it into Aiven

1. Open the **`aiven-dss`** connection.
2. **Server → Data Import**.
3. **Import from Self-Contained File** → pick `dumps\dss_data.sql`.
4. **Default Target Schema:** `defaultdb`.
5. **Start Import**, and watch for errors rather than assuming.

### 4d. Check it landed

In a Workbench query tab on `aiven-dss`:

```sql
SELECT COUNT(*) AS rows_total,
       SUM(source = 'Google Places API') AS real_google_rows
FROM market_data;
```

The second number is your real Google data. If it is 0, the import did
not bring `market_data` across.

> **Command line instead**, if you prefer it:
>
> ```bash
> mysqldump -u root -p --no-tablespaces --skip-add-locks --single-transaction \
>   dss_db market_data lgu_data system_settings > dumps/dss_data.sql
>
> mysql --host=mysql-xxxx-yyyy.a.aivencloud.com --port=12345 \
>       --user=avnadmin --password=PASSWORD \
>       --ssl-mode=REQUIRED defaultdb < dumps/dss_data.sql
> ```

> **1 GB disk on the Aiven free plan.** This dataset is a few MB. Fine.

---

## 5. Deploy on Render — filling in the form by hand

Render can read `render.yaml` and set everything up itself
(**New + → Blueprint**), but the manual path is below, field by field.

1. Sign up at <https://render.com> with your GitHub account.
2. **New + → Web Service.**
3. **Connect a repository** → pick your repo → **Connect**.

Then fill in each field:

| Field | What to put |
|---|---|
| **Name** | `sme-market-saturation-dss` — this becomes `https://sme-market-saturation-dss.onrender.com` |
| **Language** / Runtime | `Python 3` |
| **Branch** | `main` |
| **Region** | **Singapore** — closest to the Philippines |
| **Root Directory** | *leave blank* (the repo root already holds `app.py` and `requirements.txt`) |
| **Build Command** | `pip install --upgrade pip && pip install -r requirements.txt && python seed.py` |
| **Start Command** | `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --preload --access-logfile - wsgi:app` |
| **Instance Type** | `Free` |

Then open **Advanced** and add the environment variables:

| Key | Value | Why |
|---|---|---|
| `SECRET_KEY` | a long random string | Flask signs the login cookie with it. Generate one: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. **The app refuses to boot without it.** |
| `FLASK_CONFIG` | `production` | HTTPS cookies, debug off, and the safety checks on |
| `DATABASE_URL` | your Aiven string, starting `mysql+pymysql://` | see the note below |
| `PLACES_LIVE_FETCH` | `false` | the live site never calls Google |
| `PYTHON_VERSION` | `3.14` | matches what your pinned numpy/pandas/scikit-learn were installed against |
| `ADMIN_EMAIL` | your email | the one administrator `seed.py` creates |
| `ADMIN_PASSWORD` | a long password | not one that appears in any repository |

Leave `OPENAI_API_KEY`, `GMAIL_ADDRESS` and the Google keys **unset**.
Nothing breaks: the app falls back to its rule-based recommendations, and
registration completes without an email code.

> ### Paste Aiven's Service URI exactly as Aiven gives it to you
>
> Aiven's connection string is **not** usable by PyMySQL as written, in
> three separate ways — so `app/config.py` repairs it for you
> (`normalise_database_url`). Copy the Service URI straight out of the
> Aiven console into `DATABASE_URL` and it will work:
>
> ```
> mysql://avnadmin:YOUR_PASSWORD@mysql-38bdb7d4-project-dss.h.aivencloud.com:23964/defaultdb?ssl-mode=REQUIRED
> ```
>
> What gets fixed, and when each fault would otherwise have bitten:
>
> | In Aiven's URI | Problem | When it breaks |
> |---|---|---|
> | `mysql://` | names no driver, so SQLAlchemy reaches for MySQLdb, which is not installed | at import — `Can't load plugin: sqlalchemy.dialects:mysql` |
> | `ssl-mode=REQUIRED` | SQLAlchemy forwards unknown query parameters to the driver as keyword arguments, and PyMySQL has no `ssl-mode` (it is not even a legal Python name) | at the **first query**, after the app has booted looking healthy |
> | no CA certificate | just deleting `ssl-mode` would silently downgrade a connection Aiven requires to be encrypted | never visibly — which is worse |
>
> It becomes `mysql+pymysql://…?charset=utf8mb4&ssl_ca=<repo>/certs/aiven-ca.pem`,
> which is encrypted **and** verifies the server really is your Aiven
> service. `certs/aiven-ca.pem` is committed on purpose: a CA
> certificate is public, it identifies the server rather than
> authenticating you. Your **password** is the secret, and it belongs
> only in Render's Environment tab.
>
> Your service, for reference:
>
> | | |
> |---|---|
> | Host | `mysql-38bdb7d4-project-dss.h.aivencloud.com` |
> | Port | `23964` (**not** 3306) |
> | User | `avnadmin` |
> | Database | `defaultdb` |
> | SSL | REQUIRED — handled by the committed CA |

Click **Deploy Web Service**. The first build takes **5–10 minutes** —
numpy, pandas and scikit-learn are large.

### Running seed.py and the app "at the same time"

You asked for one command that does both. There are two ways, and the
difference matters more than it looks.

**The one you want — seeding in the Build Command:**

```
pip install --upgrade pip && pip install -r requirements.txt && python seed.py
```

with the start command left as gunicorn alone. `&&` means "only if the
previous one succeeded", so a failed seed stops the deploy instead of
starting a broken app.

**The literal one-liner, if you insist on it in the Start Command:**

```
python seed.py && gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --preload --access-logfile - wsgi:app
```

This works. It is just worse, and here is exactly why:

A free Render service **spins down after 15 minutes of no traffic** and
starts again on the next visit. Anything the build wrote is part of the
image and survives that; anything written at *start* time does not. So
with seeding in the Start Command, every single wake-up reconnects to
Aiven, re-checks every table, and — because the model trained at start
time is gone with the old container — **retrains the Random Forest**,
adding roughly a minute on top of Render's own wake-up. Your examiner
clicks the link and waits two minutes.

Put it in the Build Command and it runs once per deploy, the trained
model is baked into the image, and waking up is just Render's own ~1
minute.

(`seed.py` now skips training when a model is already on disk, so the
one-liner is not catastrophic — but on free-tier Render the disk is new
every wake-up, so the skip rarely gets a chance to help.)

### Why the start command looks like that

**`--workers 1`.** Not arbitrary. This app's memory footprint, measured
with everything resident — Flask, numpy, pandas, scikit-learn, and the
trained Random Forest — is about **210 MB**. A free instance has 512 MB.
Two workers would sit at ~420 MB before serving a request and get
OOM-killed under any load. Threads give concurrency without a second copy
of the model.

**`--timeout 120`.** The first request after a cold start can run the
full city-wide AI sweep, roughly 15 seconds. Gunicorn's 30-second default
is close enough to that to be a risk, and a timeout here means a 502 in
front of an audience.

**`--preload`.** Loads the app once in the master process before forking,
so the 9 MB model is read from disk a single time.

**`wsgi:app`, not `app:app`.** `app` is the name of the package (`app/`),
and Python resolves that name to the package every time — a file called
`app.py` next to it can never be imported by name. `gunicorn app:app`
therefore fails with *"Failed to find attribute 'app' in 'app'"*.
`wsgi.py` has no such clash. The full explanation is in the banner
comment at the top of `app.py`.

When the log says your service is live, open the URL and log in with the
`ADMIN_EMAIL` / `ADMIN_PASSWORD` you set.

---

## 6. Redeploying

```bash
git add -A
git commit -m "describe your change"
git push
```

Render rebuilds automatically. Roll back from **Deploys → any earlier
deploy → Redeploy**.

---

## 7. If something goes wrong

| What you see | What it is |
|---|---|
| Build fails on `pip install`, numpy/pandas/scikit-learn | Python version mismatch. Render's default is 3.14.3 and `.python-version` pins `3.14`, matching what these packages were installed against locally. If a wheel is genuinely missing, try `3.13` in `.python-version`. |
| `RuntimeError: SECRET_KEY is unset or still the development default` | Working as intended. Set `SECRET_KEY` in Render → Environment. |
| `Can't connect to MySQL server` | `DATABASE_URL` wrong. Check you changed `mysql://` to `mysql+pymysql://`, kept `?charset=utf8mb4`, and that the Aiven service is **Running** not powered off. |
| `Table 'defaultdb.user' doesn't exist` | §3 was skipped, or run against the wrong database. |
| **400 Bad Request: The referrer header is missing** on login | Already handled — `WTF_CSRF_SSL_STRICT` is off in `ProductionConfig` for exactly this. If you see it, something re-enabled it. See §10. |
| Login redirects back to the login page, no error | The session cookie is being dropped. You are on plain http with `SESSION_COOKIE_SECURE=True`. Use the https URL. |
| First visit takes ~1 minute | Render free spun down after 15 idle minutes. §8. |
| Trend Reports empty; map has no shading | No `market_data` in the live database. §4. |
| Charts missing, page otherwise fine | Chart.js and Leaflet load from a CDN; check the browser console for blocked requests. |
| `502 Bad Gateway` under a few users | Out of memory. Confirm `--workers 1`. |
| **`Failed to find attribute 'app' in 'app'`** | Your Start Command says `app:app`. It cannot work: `app` is the package (`app/`), and Python resolves that name to the package, never to `app.py`. Use `wsgi:app`. |
| Build log: `No module named 'seed'` or seed.py errors | The Build Command runs from the repo root. If you set **Root Directory** to anything, clear it. |
| Seeding succeeds but you cannot log in | On `FLASK_CONFIG=production`, `seed.py` creates no demo accounts on purpose (§3). Set `ADMIN_EMAIL` and `ADMIN_PASSWORD` and redeploy, or register through the sign-up page. |
| Build hangs or is OOM-killed during `seed.py` | Training the Random Forest is the memory-heavy step. Re-run the deploy; if it repeats, train locally and temporarily un-ignore `app/ml/model_store/rf_model.pkl` so the build can skip training. |

Render's **Logs** tab is the first place to look — `--access-logfile -`
sends request logs there.

---

## 8. Keeping it awake for your defence

Simplest and most reliable: **open the site yourself 5 minutes before you
present**, and leave the tab open. While anyone is clicking, it stays warm.

A free uptime pinger (UptimeRobot, cron-job.org) hitting your URL every 10
minutes also works, but note the honest arithmetic: 750 free instance
hours per month is about 31 days for **one** service, so a constant pinger
uses your whole month's allowance on a single app. That is fine if this is
the only thing you run on Render.

Do not bother optimising the sweep further — it is already cached, and
after the first request a date change is under 0.1 s.

---

## 9. What you are exposing, and what is already closed

A public URL means anyone can reach every page behind a login, and can
register their own account. Two things were closed for you:

**Google Places billing.** A full city sweep is 20 industries × 76
barangays, up to 3 billable Places requests each — 1,520 combinations and up to about 4,560 billable calls on
whoever owns the key. The "Fetch remaining" button that starts one sits on
the Trend Reports page, reachable by anyone who can log in. With
`PLACES_LIVE_FETCH=false`:

- every Places and Geocoding call is blocked at a single gate in
  `places_service.live_fetch_enabled()`;
- `/api/places-refresh` returns **403 server-side**, not merely a hidden
  button — hiding a button stops an honest user, not a POST;
- the verification panel says live fetching is off instead of claiming
  your imported counts are simulated;
- and an old real count is **never replaced by a fresh estimate**. That
  last one is subtle and matters: `market_data` rows go stale after 30
  days, at which point the app normally refetches. With fetching off that
  "refetch" would produce a *simulated* number and overwrite real Google
  data — a site that looks right for a month, then quietly degrades
  barangay by barangay. An old real measurement beats a fresh invented
  one.

Recommended: **do not put your Google keys on Render at all.** Nothing can
spend them there, and the safest place for a billable key is off the
public host entirely. The map needs no key — it is OpenStreetMap via
Leaflet, and all 76 barangay coordinates are precomputed and committed.

**Session forgery.** Covered in §5: production refuses to start on the
default `SECRET_KEY`.

Still your job:

- **Check for `@dss.local` accounts** in the live database. On a
  production config `seed.py` creates none, but one can arrive from an
  imported dump or an earlier run — Admin → Manage Users lists them.
- **Registration is open** to anyone with the URL. Fine for a defence;
  check Admin → Users afterwards.
- If you later switch live fetching back on, set a **budget alert** in
  Google Cloud Console → Billing → Budgets & alerts first.

---

## 10. What changed in the code for deployment

Nothing about how the app behaves locally. `python app.py` is unchanged,
live Places fetching still defaults **on** for development, and the test
suite covers both modes.

| File | Why |
|---|---|
| `wsgi.py` *(new)* | What gunicorn imports. Defaults to the production config. `load_dotenv()` runs **before** `from app import create_app` — `app/config.py` reads `os.environ` at class-definition time, so the reverse order would silently ignore every setting. |
| `Procfile` *(new)* | The start command, in the repo rather than only in a dashboard. |
| `render.yaml` *(new)* | The whole service as code: build command, start command, env vars, region. |
| `.python-version` *(new)* | Pins 3.14, matching what the dependency versions were installed against. |
| `requirements.txt` | Added `gunicorn` (a `>=` floor, not an exact pin — 23.0.0 predates Python 3.14) and `waitress` as a fallback. |
| `.gitignore` | Added `.idea/`, `.vscode/`, `.pytest_cache/` and database dumps. |
| `app/config.py` | `PLACES_LIVE_FETCH`; production now sets Secure/HttpOnly/SameSite cookies, `PREFERRED_URL_SCHEME=https`, relaxes the CSRF referrer check (reasoning in the file), and **refuses to boot** on the default `SECRET_KEY`. |
| `app/__init__.py` | `ProxyFix` under the production config, so Flask sees the real https scheme and host behind Render's proxy. Never applied locally, where those headers would be client-controlled. |
| `app/services/places_service.py` | `live_fetch_enabled()` — the single gate every outbound Places call passes through. |
| `app/services/forecasting_service.py` | Read-only deployments serve an existing `market_data` row however old, instead of replacing it with an estimate. |
| `app/services/geocoding_service.py` | Honours the same switch; geocoding is a second billable Google API. |
| `app/controllers/api_controller.py` | `/api/places-refresh` returns 403 when fetching is off; the two status endpoints report `live_fetch_disabled`. |
| `app/templates/sme/trend_reports.html` | Hides the bulk-fetch box and stops claiming your imported data is simulated. |
| `seed.py` | Creates **no** demo accounts when `FLASK_CONFIG=production` — their passwords are published in the file — and creates one administrator from `ADMIN_EMAIL`/`ADMIN_PASSWORD` instead. Also skips retraining when a model is already on disk, so it is cheap to re-run. |
| `run.py` → **`app.py`** | Renamed as you asked. `python app.py` is unchanged. Note the banner at the top of it: `app` as an import name always means the *package*, so gunicorn must use `wsgi:app`. |
| `scripts/` | `check_apis.py`, `export_seed_data.py` and `precompute_barangay_coords.py` moved off the repo root; `warm_cache.py` added. None of them are needed to run the app. |
| `.env.sample` *(new)* | A committed template with no real values, so a fresh clone knows what to fill in. `.env.example` still holds your real keys and stays ignored. |
| `tests/test_deployment_readonly.py` *(new)* | 16 tests over the above, including the stale-row regression, a real HTTPS login without a Referer header, and an assertion that `app` resolves to the package rather than to `app.py`. |

### One finding worth knowing about

The CSRF referrer problem in §7 was not found by reading the code. It only
appears once ProxyFix correctly reports https: Flask-WTF then demands a
`Referer` header on every POST and returns **400 Bad Request: The referrer
header is missing** without one. Browsers omit `Referer` for ordinary
privacy reasons, and this never happens on `http://localhost` — so the
first thing to break on the live site would have been **logging in**, in
front of your panel.

It is off now. The CSRF token is still verified and `SameSite=Lax` means a
cross-site POST carries no session cookie at all, so two layers of CSRF
protection remain — the referrer check predates SameSite and was the third.
`app/config.py` spells this out at the setting, and
`test_production_does_not_require_a_referrer_header` fails if anything
weakens the two that remain.

---

## Appendix: a custom domain, later

Render free supports custom domains and issues TLS certificates for them.

1. Buy a domain (Namecheap, Cloudflare, Hostinger — roughly ₱600–900/yr).
2. Render → your service → **Settings → Custom Domains → Add**.
3. At your registrar, add the records Render shows you — usually a `CNAME`
   for `www` pointing at `your-app.onrender.com`, plus Render's
   instructions for the apex (`example.com`).
4. Wait for DNS (minutes to a few hours) and for Render to report the
   certificate issued.

No code change is needed: `ProxyFix` already takes the hostname from the
proxy, so links and cookies follow the new domain on their own.
