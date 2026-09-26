// app/static/js/map.js
// -----------------------------------------------------------------
// Drives the Saturation Map (and the Home page's mini map preview).
//
// Map tiles: OpenStreetMap, rendered with Leaflet (L.map / L.polygon).
// No API key, no billing account and no usage quota -- the Leaflet CSS
// and JS come from a CDN <script>/<link> in saturation_map.html and
// home.html. This replaced the Google Maps JavaScript API, which needed
// GOOGLE_MAPS_JS_API_KEY in .env; that key is no longer used for the
// basemap (GOOGLE_PLACES_API_KEY, a different key, is still what fetches
// the competitor data below).
//
// Two consequences of the switch worth knowing:
//   * Scroll-to-zoom just works. Leaflet zooms on an ordinary wheel or
//     trackpad scroll, so there is no "hold ctrl to zoom" overlay of the
//     kind Google Maps shows by default -- and therefore no overlay to
//     swallow the first click on a barangay.
//   * Attribution. OpenStreetMap's ODbL requires the "(c) OpenStreetMap
//     contributors" credit, which the tile layer carries. Google's
//     Places policy separately requires Google attribution on
//     Places-derived content, which is why addGoogleAttributionControl()
//     puts a "Powered by Google" mark at the top of the map. READ the
//     long comment on that function before your defense: Google's Maps
//     Platform ToS section 10.5 ("no use with a non-Google map") is a
//     separate restriction that attribution does not resolve, and the
//     project's decision about it is documented in README.md 5.2.
//
// Business data (competitor counts / names) comes from the Google
// Places API (New) -- see app/services/places_service.py, itself
// called via Python's `requests` package straight from the backend
// (matching the paper's own "calling the Google Places API using the
// Python requests package") -- through this app's own JSON endpoints
// (/api/locations-forecast, /api/barangay-detail, /api/places/nearby).
// Those counts are SME-scale only: micro businesses (sari-sari stores,
// carinderias, food carts...) are filtered out server-side, since this
// DSS plans for SMEs. This file never calls any Google API directly.
//
// Barangay MAP COORDINATES come from a static, pre-computed JSON file
// (app/static/data/barangay_coords.json) holding REAL, PhilAtlas-sourced
// positions for all 76 official Tarlac City barangays -- see that file's
// own "_note"/"source" fields and app/services/geocoding_service.py's
// module docstring for the full history (an earlier, purely synthetic
// placeholder file used to live here, which is why barangays used to
// render in the wrong place). Live Google geocoding still exists
// server-side, but only as a fallback for a location name outside those
// 76, so this file has no client-side geocoding code at all.
//
// CHOROPLETH CELLS come from /api/barangay-choropleth
// (app/services/choropleth_service.py): a Voronoi tessellation computed
// server-side from those SAME real coordinates, clipped to Tarlac
// City's bounds -- one filled polygon "zone" per barangay, replacing
// the earlier two-soft-circles-per-point approximation. Each cell is
// guaranteed to contain its own barangay's real point and no other
// barangay's, so the map's layout stays faithful to the real, surveyed
// positions even though a cell's exact edge is a computed approximation
// rather than a traced administrative boundary line (no such dataset
// exists publicly for this city -- see that service's own docstring).
//
// ISOLATE / RESTORE: clicking a barangay -- on the map, in the "All
// Barangays" list, or via the search bar -- hides every OTHER zone so
// only that barangay's cell (and its detail panel) is shown.
// Double-clicking that same barangay again (on the map or in the list),
// or searching it again, restores every zone. dssIsolatedLocation below
// is the single source of truth for this state; applyTierVisibility()
// and renderLocationList() both read it so the map and the list panel
// never disagree about what's currently isolated.
//
// Deep linking: this page also reads ?location=&industry_type= from
// the URL (see dssDeepLinkLocation/dssDeepLinkIndustry below) so the
// SME Home page's search bar (app/static/js/sme_search.js) can link
// straight into a focused view of whatever the user just searched.

// Full 4-tier cluster palette (matches app/ml/constants.py
// CLUSTER_THRESHOLDS / CLUSTER_LABELS_ORDERED exactly -- these are the
// AI engine's real internal cluster names). CLUSTER_DISPLAY_LABELS
// below is ONLY the user-facing text shown on the map/legend/detail
// panel; the underlying cluster_label value from the API is unchanged.
// Tier colours are deliberately far apart from each other so two
// adjacent tiers are never mistaken for one another on a satellite
// basemap: green -> LIGHT yellow -> DARK orange -> red. Keep these in
// sync with the legends in sme/saturation_map.html, sme/home.html and
// lgu/dashboard.html, and with --dss-yellow / --dss-orange in
// static/css/style.css.
const CLUSTER_COLORS = {
  Low: "#22c55e", // High Opportunity  -- green
  Moderate: "#fde047", // Low-Moderate  -- light yellow
  High: "#f97316", // Moderate         -- bright orange
  Saturated: "#b91c1c", // High Saturation -- deep red
};

// Tier order used by the legend/filter UI, least to most saturated.
const CLUSTER_ORDER = ["Low", "Moderate", "High", "Saturated"];

// Sociodemographic overlay: real 2024-PSA-derived population density
// (people/km2), the one figure of the paper's "population density and
// household income" overlay objective that has real, sourced, per-
// barangay data (see Reference/DATASETS.md -- no public dataset
// publishes barangay-level household income anywhere in the
// Philippines, so this system does not invent one). Buckets chosen to
// spread Tarlac City's actual 76-barangay range roughly evenly.
const DENSITY_BANDS = [
  { max: 600, color: "#c7d2fe", label: "Low density" },
  { max: 1200, color: "#818cf8", label: "Moderate density" },
  { max: 1800, color: "#6366f1", label: "High density" },
  { max: Infinity, color: "#3730a3", label: "Very high density" },
];

function densityColorFor(value) {
  if (value === null || value === undefined) return "#9ca3af";
  const band = DENSITY_BANDS.find((b) => value <= b.max);
  return (band || DENSITY_BANDS[DENSITY_BANDS.length - 1]).color;
}

const CLUSTER_DISPLAY_LABELS = {
  Low: "High Opportunity",
  Moderate: "Low-Moderate",
  High: "Moderate",
  Saturated: "High Saturation",
};

function displayLabelFor(clusterLabel) {
  return CLUSTER_DISPLAY_LABELS[clusterLabel] || clusterLabel;
}

let dssMap = null;
let dssPolygons = []; // [{polygon, location, cluster}] -- one choropleth cell per barangay
// Tarlac City's own real outline (see choropleth_service.py's module
// docstring) drawn as a single bold, unfilled overlay -- separate from
// the barangay cells, so the real city border stays visible no matter
// which cells are shown, filtered, or isolated.
let dssCityBoundaryPolygon = null;
let dssZonesVisible = true; // global "Hide Saturation" toggle -- gates the whole choropleth layer
// Per-tier visibility, driven by the legend filter. A tier switched off
// here is hidden on the map but still counted in the barangay list, so
// the filter never changes the underlying numbers.
const dssVisibleTiers = { Low: true, Moderate: true, High: true, Saturated: true };
let dssInfoWindow = null;
let dssLocationsCache = [];
let dssCoordsCache = {}; // { "Barangay Name": {lat, lng, source}, ... }
// Whether loadCoords() has completed at least once. Kept separate from
// dssCoordsCache itself (an object, always truthy) so loadCoords() can
// tell "not fetched yet" apart from "fetched, 76 real coords in it".
let dssCoordsLoaded = false;
let dssChoroplethCache = null; // GeoJSON FeatureCollection from /api/barangay-choropleth
let dssSelectedLocation = null;
// The one barangay currently isolated (every other zone/list row
// hidden), or null when all barangays are showing. See the module
// docstring above for the full click / double-click / search contract.
let dssIsolatedLocation = null;

// Overlay mode: "saturation" (default, colours by AI saturation tier)
// or "population" (colours by real PSA population density -- the
// sociodemographic overlay called for in the LGU dashboard objectives).
let dssOverlayMode = "saturation";

// "Demand" data: real PSA FIES (2023) household spending-category
// figures -- see app/services/socio_demographic_service.py for exactly
// what's real vs. estimated and why. Set once, server-side (this figure
// is the SAME for every barangay -- no barangay-level breakdown is
// published anywhere -- so it is deliberately NOT re-fetched per
// barangay the way population/competitor counts are; see that
// service's module docstring). Absent (null) on any page that doesn't
// render window.DSS_DEMAND_SUMMARY.
const dssDemandSummary = window.DSS_DEMAND_SUMMARY || null;

function pesoShort(n) {
  return `₱${Number(n).toLocaleString()}`;
}

// Fuller block for the detail panel -- same figures, same caveats, laid
// out as its own labelled section rather than squeezed into the compact
// hover tooltip.
function demandDetailHtml() {
  if (!dssDemandSummary) return "";
  const rows = dssDemandSummary.categories
    .map(
      (c) =>
        `<div class="d-flex justify-content-between border-bottom py-1">
           <span class="text-muted">${c.label}${c.is_recreation_estimate ? " <span title=\"Estimated from a 2021 PSA/CPBRD factsheet -- the 2023 PSA release does not break recreation out separately.\">*</span>" : ""}</span>
           <strong>${pesoShort(c.estimated_annual_php)}/yr</strong>
         </div>`
    )
    .join("");
  return `
    <div class="small mt-3">
      <div class="fw-semibold mb-1">Est. Household Demand <span class="text-muted" style="font-weight:400;">(Tarlac City avg., not barangay-specific)</span></div>
      ${rows}
      <div class="text-muted" style="font-size:.75rem;margin-top:.35rem;">
        Based on Tarlac's real ${dssDemandSummary.expenditure_year} PSA FIES average annual family expenditure
        (${pesoShort(dssDemandSummary.tarlac_avg_annual_family_expenditure_php)}) applied to the PSA's national
        per-category spending shares -- an estimate, since PSA does not publish a Tarlac- or barangay-specific
        category breakdown. * Recreation is carried over from a separate 2021 PSA/CPBRD factsheet (the 2023
        release folds it into a combined "Other" bucket). Same figure for every barangay.
      </div>
    </div>`;
}

// Deep-link support: a search result on the SME Home page links here
// with ?location=&industry_type= so the map opens already focused on
// what the user just searched.
const dssUrlParams = new URLSearchParams(window.location.search);
const dssDeepLinkLocation = dssUrlParams.get("location");
const dssDeepLinkIndustry = dssUrlParams.get("industry_type");

function currentBusinessType() {
  const select = document.getElementById("businessTypeSelect");
  if (select) return select.value;
  // No industry <select> on this page (e.g. the Home page's mini map
  // preview) -- fall back to whatever industry the page itself set.
  return window.DSS_DEFAULT_INDUSTRY || "Food and Beverage";
}

function applyDeepLinkIndustry() {
  if (!dssDeepLinkIndustry) return;
  const select = document.getElementById("businessTypeSelect");
  if (!select) return;
  const match = Array.from(select.options).find(
    (opt) => opt.value.toLowerCase() === dssDeepLinkIndustry.toLowerCase()
  );
  if (match) select.value = match.value;
}

function pillClass(cluster) {
  return (
    {
      Low: "dss-pill-low",
      Moderate: "dss-pill-moderate",
      High: "dss-pill-high",
      Saturated: "dss-pill-saturated",
    }[cluster] || "dss-pill-moderate"
  );
}

// Number of requested locations that are NEITHER one of the 76 shipped,
// real-coordinate barangays NOR already cached from a prior fallback
// geocode. In normal operation this is always 0 -- see
// app/services/geocoding_service.py.
let dssCoordsPending = 0;
let dssCoordsResolving = false;

async function loadCoords(force) {
  if (dssCoordsLoaded && !force) return dssCoordsCache;
  try {
    const payload = await fetch(window.DSS_BARANGAY_COORDS_URL).then((r) => r.json());
    dssCoordsCache = payload.barangays || {};
    dssCoordsPending = payload.pending || 0;
  } catch (err) {
    dssCoordsCache = dssCoordsCache || {};
    dssCoordsPending = 0;
  }
  dssCoordsLoaded = true;
  return dssCoordsCache;
}

// The choropleth's polygon cells -- fetched once and reused across
// industry/overlay changes, since the GEOMETRY only depends on the
// barangay coordinate set (see choropleth_service.py), not on which
// industry or overlay is selected. Only the per-cell FILL COLOR changes
// when those switch, which drawChoropleth() re-applies without needing
// a new fetch.
async function loadChoropleth(force) {
  if (dssChoroplethCache && !force) return dssChoroplethCache;
  // WHY THIS REPORTS INSTEAD OF SHRUGGING.
  //
  // This used to swallow every failure into an empty FeatureCollection.
  // The map then drew Tarlac City's outline with nothing inside it and
  // the tier counters all read 0 -- which looks exactly like "there is
  // no data", and is indistinguishable from a 500, a 502 from an
  // out-of-memory worker, or a timeout. Three separate debugging
  // sessions started from that screenshot with nothing to go on.
  //
  // A missing layer is now stated, in the console and on the page.
  try {
    const response = await fetch(window.DSS_BARANGAY_CHOROPLETH_URL);
    if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText}`);
    const payload = await response.json();
    if (!payload || !Array.isArray(payload.features)) {
      throw new Error("the response carried no GeoJSON features");
    }
    dssChoroplethCache = payload;
    showChoroplethProblem(null);
  } catch (err) {
    console.error("[MarketLens] barangay cells could not be loaded:", err);
    dssChoroplethCache = { type: "FeatureCollection", features: [], error: String(err.message || err) };
    showChoroplethProblem(dssChoroplethCache.error);
  }
  return dssChoroplethCache;
}

// Puts the reason on the page, next to the map it is missing from.
function showChoroplethProblem(message) {
  const host = document.getElementById("mapIsolationBanner");
  if (!host) return;
  const existing = document.getElementById("dssChoroplethProblem");
  if (!message) {
    if (existing) existing.remove();
    return;
  }
  const html =
    '<div class="alert alert-warning py-2 small mb-2" id="dssChoroplethProblem">' +
    "<strong>The barangay shading could not be loaded.</strong> " +
    "The map below shows the city outline and the barangay list is unaffected. " +
    '<span class="text-muted d-block">' + message + "</span></div>";
  if (existing) existing.outerHTML = html;
  else host.insertAdjacentHTML("afterbegin", html);
}

// GeoJSON -> the nested {lat, lng} structure L.polygon() expects.
//
// A GeoJSON Polygon's coordinates are [ring, ...holes]; a MultiPolygon's
// are [[ring, ...holes], ...] -- one entry per disconnected piece, which
// really happens here: the real, concave city outline splits a few
// barangay cells (Balanti, Burot) into more than one piece, see
// choropleth_service.py.
//
// MIND THE NESTING DEPTH -- it is the whole reason this function is
// three levels deep rather than two. Leaflet reads
//   [ring]            as a simple polygon,
//   [ring, ring]      as ONE polygon with the second ring cut out as a
//                     HOLE, and
//   [[ring], [ring]]  as two SEPARATE polygons.
// The Google Maps version of this file returned the two-level form,
// which Google's even-odd fill rule happened to render as separate
// pieces; handing that same value to Leaflet would punch Burot's second
// piece out of its first as a hole. So every geometry is normalized to
// the three-level (multipolygon) form here, which is also exactly
// GeoJSON's own [polygon][ring][point] shape -- holes included, if a
// future boundary file ever has any.
function geometryToPaths(geometry) {
  const polygons = geometry.type === "MultiPolygon" ? geometry.coordinates : [geometry.coordinates];
  return polygons.map((poly) => poly.map((ring) => ring.map(([lng, lat]) => ({ lat, lng }))));
}

// Tarlac City's own real outline -- fetched once and drawn as a single,
// unfilled, bold overlay so the actual city border stays visible no
// matter what's happening with the barangay cells (isolated, filtered,
// hidden). Not clickable, so it never steals a click meant for a
// barangay cell drawn on top of/inside it.
async function loadAndDrawCityBoundary() {
  if (!dssMap || !window.L || dssCityBoundaryPolygon) return;
  let geo;
  try {
    geo = await fetch(window.DSS_TARLAC_CITY_BOUNDARY_URL).then((r) => r.json());
  } catch (err) {
    return;
  }
  const feature = (geo.features || [])[0];
  if (!feature) return;

  dssCityBoundaryPolygon = L.polygon(geometryToPaths(feature.geometry), {
    color: "#1d4ed8",
    opacity: 0.9,
    weight: 3,
    fill: false,
    // Not interactive, so it never steals a click meant for a barangay
    // cell drawn on top of/inside it.
    interactive: false,
  }).addTo(dssMap);
  // Keep it under the barangay cells and their click targets.
  dssCityBoundaryPolygon.bringToBack();
}

// Almost always a no-op: the 76 shipped barangays are always resolved
// instantly from the static, real coordinate file. This only has
// anything to do if a location name OUTSIDE that list shows up (a typo,
// or a barangay a future LGU upload introduces) and the server's own
// (bounded, synchronous) fallback geocode attempt didn't finish it --
// a couple of quick re-fetches covers that rare case.
async function maybeResolveMore() {
  if (dssCoordsResolving || dssCoordsPending <= 0) return;
  dssCoordsResolving = true;
  try {
    let guard = 0;
    let lastPending = Infinity;
    while (dssCoordsPending > 0 && guard < 3) {
      guard += 1;
      await loadCoords(true);
      await loadChoropleth(true); // a newly-resolved name may now have a cell too
      await drawChoropleth(dssLocationsCache);
      if (dssCoordsPending >= lastPending) break; // no progress -- stop asking
      lastPending = dssCoordsPending;
    }
  } finally {
    dssCoordsResolving = false;
    updateCoordStatus();
  }
}

// Small readout under the map confirming the map is on real positions,
// or naming the (normally zero) handful of locations that aren't.
function updateCoordStatus() {
  const el = document.getElementById("coordStatus");
  if (!el || !dssLocationsCache.length) return;
  const unresolved = dssLocationsCache.filter((r) => !dssCoordsCache[r.location]).length;
  if (unresolved === 0) {
    el.innerHTML =
      `<span class="text-success"><i class="bi bi-geo-alt-fill"></i> All ${dssLocationsCache.length} barangay positions are real, surveyed locations.</span>`;
    return;
  }
  el.innerHTML =
    `<span class="text-muted"><i class="bi bi-geo-alt"></i> ${unresolved} location${unresolved === 1 ? "" : "s"} outside the standard 76 Tarlac City barangays could not be placed on the map.</span>`;
}

// One line, shown above the map and atop the "All Barangays" list,
// telling the user a barangay is currently isolated and how to get
// back to the full map -- both the described double-click gesture and
// a plain button, since a hover-only affordance is easy to miss.
function isolationBannerHtml(extraClass) {
  if (!dssIsolatedLocation) return "";
  return (
    `<div class="alert alert-info py-2 px-3 small mb-2 d-flex justify-content-between align-items-center gap-2 ${extraClass || ""}">` +
    `<span><i class="bi bi-crosshair"></i> Showing only <strong>${dssIsolatedLocation}</strong>. Double-click it again (map or list) to show every barangay.</span>` +
    `<button type="button" class="btn btn-sm btn-outline-primary flex-shrink-0" id="dssShowAllBtn">Show All</button>` +
    `</div>`
  );
}

function wireShowAllButtons() {
  document.querySelectorAll("#dssShowAllBtn").forEach((btn) => {
    btn.addEventListener("click", restoreAllLocations);
  });
}

function renderLocationList(rows) {
  const list = document.getElementById("locationList");
  if (!list) return;
  const sorted = [...rows].sort((a, b) => a.location.localeCompare(b.location));
  const visible = dssIsolatedLocation ? sorted.filter((r) => r.location === dssIsolatedLocation) : sorted;

  const rowsHtml = visible
    .map(
      (r) => `
      <button type="button" class="list-group-item list-group-item-action d-flex justify-content-between align-items-center dss-loc-item${r.location === dssIsolatedLocation ? " active" : ""}" data-location="${r.location}" title="Click to isolate this barangay, double-click to restore all">
        <span><span class="dss-legend-swatch" style="background:${CLUSTER_COLORS[r.cluster_label]}"></span>${r.location}</span>
        <span class="text-end">
          <span class="dss-stat-pill ${pillClass(r.cluster_label)}">${displayLabelFor(r.cluster_label)}</span>
          <div class="text-muted" style="font-size:.7rem;">${r.competitor_count} businesses</div>
        </span>
      </button>`
    )
    .join("");

  list.innerHTML = isolationBannerHtml() + rowsHtml;

  list.querySelectorAll(".dss-loc-item").forEach((el) => {
    el.addEventListener("click", () => isolateAndSelect(el.dataset.location));
    el.addEventListener("dblclick", () => toggleIsolate(el.dataset.location));
  });
  wireShowAllButtons();
}

function renderLocationTable(rows) {
  const table = document.querySelector("#locationTable tbody");
  if (!table) return;
  table.innerHTML = rows
    .map(
      (r) => `<tr style="cursor:pointer" class="dss-loc-row" data-location="${r.location}">
        <td>${r.location}</td><td>${r.competitor_count}</td>
        <td><span class="dss-stat-pill ${pillClass(r.cluster_label)}">${displayLabelFor(r.cluster_label)}</span></td>
        <td>${r.saturation_index.toFixed(1)}%</td>
        <td>${r.viability_score}/10</td>
      </tr>`
    )
    .join("");
  table.querySelectorAll(".dss-loc-row").forEach((el) => {
    el.addEventListener("click", () => isolateAndSelect(el.dataset.location));
    el.addEventListener("dblclick", () => toggleIsolate(el.dataset.location));
  });
}

function renderDetailPanel(detail) {
  const panel = document.getElementById("detailPanel");
  if (!panel) return;
  const actions = (detail.recommended_actions || [])
    .map((a) => `<li>${a}</li>`)
    .join("") || "<li>No recommendation available.</li>";
  const population = detail.population !== null && detail.population !== undefined
    ? Number(detail.population).toLocaleString()
    : "Not available";
  // A FIXED, real people/km2 figure -- must read the same no matter
  // which industry is selected. Shown as its own row, separate from the
  // industry-specific Saturation Score below, so the two are never
  // mistaken for one another (an earlier version labelled the
  // industry-dependent saturation_index "Density Score", which made
  // population density look like it was changing with the industry
  // dropdown when it never actually did).
  const density = detail.population_density !== null && detail.population_density !== undefined
    ? `${Number(detail.population_density).toLocaleString()} /km²`
    : "Not available";

  const isolateBtn = detail.location === dssIsolatedLocation
    ? `<button type="button" class="btn btn-sm btn-outline-secondary" id="detailIsolateBtn"><i class="bi bi-arrows-fullscreen"></i> Show All</button>`
    : `<button type="button" class="btn btn-sm btn-outline-primary" id="detailIsolateBtn"><i class="bi bi-crosshair"></i> Isolate on Map</button>`;

  panel.innerHTML = `
    <div class="d-flex align-items-start justify-content-between gap-2 mb-3">
      <div class="d-flex align-items-start gap-2">
        <i class="bi bi-geo-alt-fill fs-4 text-primary"></i>
        <div>
          <h5 class="mb-0">${detail.location}</h5>
          <span class="dss-stat-pill ${pillClass(detail.cluster_label)}">${displayLabelFor(detail.cluster_label)}</span>
        </div>
      </div>
      ${isolateBtn}
    </div>
    <div class="small mb-3">
      <div class="d-flex justify-content-between border-bottom py-1"><span class="text-muted">Total Businesses</span><strong>${detail.total_businesses}</strong></div>
      <div class="d-flex justify-content-between border-bottom py-1"><span class="text-muted">Food Industry</span><strong>${detail.food_count}</strong></div>
      <div class="d-flex justify-content-between border-bottom py-1"><span class="text-muted">Service Industry</span><strong>${detail.service_count}</strong></div>
      <div class="d-flex justify-content-between border-bottom py-1"><span class="text-muted">Retail Industry</span><strong>${detail.retail_count}</strong></div>
      <div class="d-flex justify-content-between border-bottom py-1"><span class="text-muted">Population</span><strong>${population}</strong></div>
      <div class="d-flex justify-content-between border-bottom py-1"><span class="text-muted">Population Density <span class="text-muted" style="font-weight:400;">(fixed)</span></span><strong>${density}</strong></div>
      <div class="d-flex justify-content-between py-1"><span class="text-muted">Saturation Score <span class="text-muted" style="font-weight:400;">(for ${detail.industry_type})</span></span><strong>${detail.saturation_index.toFixed(1)}%</strong></div>
    </div>
    ${demandDetailHtml()}
    <div class="alert alert-light border mb-0 mt-3">
      <strong>Recommended Actions</strong>
      <ul class="mb-0 mt-1">${actions}</ul>
    </div>
  `;

  const btn = document.getElementById("detailIsolateBtn");
  if (btn) btn.addEventListener("click", () => toggleIsolate(detail.location));
}

async function selectLocation(location) {
  dssSelectedLocation = location;
  const businessType = currentBusinessType();

  let detail;
  try {
    detail = await fetch(
      `${window.DSS_BARANGAY_DETAIL_URL}?location=${encodeURIComponent(location)}&industry_type=${encodeURIComponent(businessType)}`
    ).then((r) => r.json());
  } catch (err) {
    detail = null;
  }
  if (detail && !detail.error) {
    renderDetailPanel(detail);
  }

  await focusMapOn(location);
}

// Explicit user action (map click, list/table click, search, or a deep
// link) -- shows this barangay's detail AND isolates it on the map/list
// (hides every other zone). Plain selectLocation() alone -- used for
// the initial "select the first row" default on page load -- never
// isolates, so the map still opens showing all 76 barangays by default.
async function isolateAndSelect(location) {
  dssIsolatedLocation = location;
  applyTierVisibility();
  renderLocationList(dssLocationsCache);
  updateIsolationBanner();
  await selectLocation(location);
}

// Double-clicking the currently isolated barangay (map or list), the
// "Show All" button, or searching the same name again all call this.
function restoreAllLocations() {
  dssIsolatedLocation = null;
  applyTierVisibility();
  renderLocationList(dssLocationsCache);
  updateIsolationBanner();
  const detailBtn = document.getElementById("detailIsolateBtn");
  if (detailBtn && dssSelectedLocation) {
    // Refresh the detail panel's own button label without a network
    // round trip -- selectLocation() already populated everything else.
    detailBtn.outerHTML = `<button type="button" class="btn btn-sm btn-outline-primary" id="detailIsolateBtn"><i class="bi bi-crosshair"></i> Isolate on Map</button>`;
    document.getElementById("detailIsolateBtn").addEventListener("click", () => toggleIsolate(dssSelectedLocation));
  }
  if (dssMap) {
    dssMap.setView([15.4869, 120.59], 13); // Tarlac City center -- see dssInitMap
  }
}

// Click the isolated barangay again (map or list) => restore; click a
// DIFFERENT barangay while one is isolated => switch focus to it;
// nothing isolated yet => isolate it. One function covers every
// "double-click"/"search it again" case described for both the search
// bar and the barangay list.
function toggleIsolate(location) {
  if (dssIsolatedLocation === location) {
    restoreAllLocations();
  } else {
    isolateAndSelect(location);
  }
}

function updateIsolationBanner() {
  const el = document.getElementById("mapIsolationBanner");
  if (!el) return;
  el.innerHTML = isolationBannerHtml();
  wireShowAllButtons();
}

async function focusMapOn(location) {
  if (!dssMap || !window.L) return;
  const coords = await loadCoords();
  const point = coords[location];
  if (!point) return;
  dssMap.setView([point.lat, point.lng], 15);
}

// Draws one choropleth polygon per barangay (see module docstring and
// app/services/choropleth_service.py). Geometry is fetched once
// (loadChoropleth caches it); only the per-cell fill color depends on
// `rows` (this industry's/overlay's data), so this redraws styling on
// every industry/overlay change without a second geometry fetch.
async function drawChoropleth(rows) {
  if (!dssMap || !window.L) return;
  const geo = await loadChoropleth();
  const cellByName = {};
  (geo.features || []).forEach((f) => {
    cellByName[f.properties.name] = f.geometry;
  });

  dssPolygons.forEach(({ polygon }) => dssMap.removeLayer(polygon));
  dssPolygons = [];
  const tierCounts = { Low: 0, Moderate: 0, High: 0, Saturated: 0 };

  for (const row of rows) {
    const geometry = cellByName[row.location];
    if (!geometry) continue; // no choropleth cell for this location -- skipped, never guessed

    const isPopulationMode = dssOverlayMode === "population";
    const color = isPopulationMode
      ? densityColorFor(row.population_density)
      : CLUSTER_COLORS[row.cluster_label] || CLUSTER_COLORS.Moderate;

    // Usually one ring (Polygon); occasionally more than one, for a
    // barangay whose cell the real, concave city outline splits into
    // disconnected pieces (MultiPolygon) -- either way, ONE L.polygon
    // holding every ring, so the cell stays a single clickable unit.
    const polygon = L.polygon(geometryToPaths(geometry), {
      color: "#ffffff",
      opacity: 0.9,
      weight: 1,
      fillColor: color,
      fillOpacity: 0.65,
    }).addTo(dssMap);

    polygon.on("click", () => isolateAndSelect(row.location));
    polygon.on("dblclick", () => toggleIsolate(row.location));
    polygon.on("mousemove", (evt) => {
      const population = row.population
        ? `<div>Population: <strong>${Number(row.population).toLocaleString()}</strong></div>`
        : "";
      const density = row.population_density
        ? `<div>Density: <strong>${Number(row.population_density).toLocaleString()} /km&sup2;</strong></div>`
        : "";
      const modeLine = isPopulationMode
        ? `<div style="margin:.15rem 0 .3rem;">
             <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${color};margin-right:.35rem;"></span>
             Population density overlay
           </div>`
        : `<div style="margin:.15rem 0 .3rem;">
             <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${color};margin-right:.35rem;"></span>
             ${displayLabelFor(row.cluster_label)}
           </div>`;
      dssInfoWindow.setContent(
        `<div style="min-width:190px;line-height:1.45;">
           <div style="font-weight:600;font-size:1rem;">${row.location}</div>
           ${modeLine}
           <div>Industry: <strong>${row.industry_type}</strong></div>
           <div>Businesses: <strong>${Number(row.competitor_count).toLocaleString()}</strong></div>
           ${population}
           ${density}
           <div style="color:#6c757d;font-size:.75rem;margin-top:.25rem;">Saturation ${row.saturation_index}% &middot; click to isolate, double-click to restore all</div>
         </div>`
      );
      dssInfoWindow.setLatLng(evt.latlng).openOn(dssMap);
    });
    polygon.on("mouseout", () => dssMap.closePopup(dssInfoWindow));
    dssPolygons.push({ polygon, location: row.location, cluster: row.cluster_label });
    if (row.cluster_label in tierCounts) tierCounts[row.cluster_label] += 1;
  }

  applyTierVisibility();
  updateTierFilterCounts(tierCounts);
}

// Show/hide each cell according to: isolation first (if a barangay is
// isolated, ONLY its cell shows, full stop -- the tier filter and the
// global "Hide Saturation" toggle are both set aside for as long as
// isolation is active, since isolating is itself an explicit "show me
// just this one" request); otherwise, both the global toggle AND the
// per-tier filter apply as before. The per-tier filter only makes sense
// in saturation mode -- in population mode every zone stays visible.
// The isolated cell also gets a bolder, dark outline so it reads as
// "focused" even though it's now the only shape on the map.
function applyTierVisibility() {
  if (!dssMap) return;
  dssPolygons.forEach(({ polygon, location, cluster }) => {
    const isIsolatedCell = dssIsolatedLocation !== null && location === dssIsolatedLocation;
    const show = dssIsolatedLocation !== null
      ? isIsolatedCell
      : dssZonesVisible && (dssOverlayMode === "population" || dssVisibleTiers[cluster] !== false);
    if (show) {
      if (!dssMap.hasLayer(polygon)) polygon.addTo(dssMap);
    } else if (dssMap.hasLayer(polygon)) {
      dssMap.removeLayer(polygon);
    }
    polygon.setStyle({
      weight: isIsolatedCell ? 3 : 1,
      color: isIsolatedCell ? "#111827" : "#ffffff",
    });
  });
}

function updateTierFilterCounts(tierCounts) {
  CLUSTER_ORDER.forEach((cluster) => {
    const el = document.querySelector(`[data-tier-count="${cluster}"]`);
    if (el) el.textContent = tierCounts[cluster] || 0;
  });
}

// Legend doubles as a filter: click a tier to show/hide those zones.
function setupTierFilter() {
  const items = document.querySelectorAll("[data-tier-filter]");
  if (!items.length) return;

  items.forEach((item) => {
    item.addEventListener("click", () => {
      const cluster = item.dataset.tierFilter;
      dssVisibleTiers[cluster] = !dssVisibleTiers[cluster];
      item.classList.toggle("dss-tier-off", !dssVisibleTiers[cluster]);
      item.setAttribute("aria-pressed", String(dssVisibleTiers[cluster]));
      applyTierVisibility();
    });
  });

  const showAll = document.getElementById("tierShowAll");
  const hideAll = document.getElementById("tierHideAll");
  const setAll = (visible) => {
    CLUSTER_ORDER.forEach((c) => {
      dssVisibleTiers[c] = visible;
    });
    items.forEach((item) => {
      item.classList.toggle("dss-tier-off", !visible);
      item.setAttribute("aria-pressed", String(visible));
    });
    applyTierVisibility();
  };
  if (showAll) showAll.addEventListener("click", () => setAll(true));
  if (hideAll) hideAll.addEventListener("click", () => setAll(false));
}

// The Saturation / Population Density overlay switch -- the LGU
// dashboard's "sociodemographic overlay" objective. Household income
// has no real, publicly-available per-barangay figure anywhere in the
// Philippines (see Reference/DATASETS.md), so this system is honest
// about only overlaying the one figure it has real data for.
function setupOverlaySwitch() {
  const select = document.getElementById("mapOverlaySelect");
  const satLegend = document.getElementById("saturationLegend");
  const densityLegend = document.getElementById("densityLegend");
  if (!select) return;
  select.addEventListener("change", () => {
    dssOverlayMode = select.value;
    if (satLegend) satLegend.hidden = dssOverlayMode !== "saturation";
    if (densityLegend) densityLegend.hidden = dssOverlayMode !== "population";
    drawChoropleth(dssLocationsCache);
  });
}

function setupSearchBar() {
  const input = document.getElementById("mapSearchInput");
  const form = document.getElementById("mapSearchForm");
  if (!input || !form) return;

  form.addEventListener("submit", (evt) => {
    evt.preventDefault();
    const typed = input.value.trim();
    if (!typed) return;
    const match = dssLocationsCache.find((r) => r.location.toLowerCase() === typed.toLowerCase());
    // Not one of the currently-listed locations -- still try it as
    // typed (compute_scores() on the backend works for ANY typed
    // location string, seeded reference or not); it just won't have a
    // choropleth cell to isolate if it can't be placed on the map.
    const matchedName = match ? match.location : typed;
    // Searching the ALREADY-isolated barangay again is this search
    // bar's equivalent of "double-click to restore" -- a text input
    // has no double-click gesture of its own, so re-submitting the
    // same name toggles back to showing every barangay instead.
    toggleIsolate(matchedName);
  });
}

function setupHideSaturationToggle() {
  const toggle = document.getElementById("hideSaturationToggle");
  if (!toggle) return;
  toggle.addEventListener("click", () => {
    dssZonesVisible = !dssZonesVisible;
    applyTierVisibility();
    toggle.innerHTML = dssZonesVisible
      ? '<i class="bi bi-eye-slash"></i> Hide Saturation'
      : '<i class="bi bi-eye"></i> Show Saturation';
  });
}

async function loadLocations() {
  const businessType = currentBusinessType();
  // Fetch the forecast rows and the real barangay coordinates in
  // parallel. Coordinates used to only load once a Google Map actually
  // initialized (focusMapOn() -> loadCoords()), which never happens
  // when no GOOGLE_MAPS_JS_API_KEY is configured -- leaving
  // dssCoordsCache empty and crashing updateCoordStatus() below.
  // /api/barangay-coords is a plain backend endpoint independent of the
  // Maps JS SDK, so fetch it here unconditionally instead.
  const [rows] = await Promise.all([
    fetch(
      `${window.DSS_LOCATIONS_FORECAST_URL}?industry_type=${encodeURIComponent(businessType)}`
    ).then((r) => r.json()),
    loadCoords(),
  ]);
  dssLocationsCache = rows;
  renderLocationList(rows);
  renderLocationTable(rows);
  await drawChoropleth(rows);

  if (dssDeepLinkLocation) {
    // Arriving via a Home-page search deep link is itself an explicit
    // "I searched for this place" action, so it isolates too.
    isolateAndSelect(dssDeepLinkLocation);
  } else if (dssIsolatedLocation) {
    // An industry/overlay change re-runs loadLocations() -- stay
    // isolated on whatever the user had already isolated, refreshed
    // for the new industry, instead of silently popping back to "all".
    isolateAndSelect(dssIsolatedLocation);
  } else if (dssSelectedLocation) {
    selectLocation(dssSelectedLocation);
  } else if (rows.length) {
    // Default view on first load: show ALL barangays, just populate
    // the detail panel with the first one -- no isolation.
    selectLocation(rows[0].location);
  }

  updateCoordStatus();
  maybeResolveMore();
}

// Builds the Leaflet map over OpenStreetMap tiles. Called on
// DOMContentLoaded (below) -- unlike the Google Maps JS API, which had
// to be handed a ?callback= because it loaded asynchronously, Leaflet
// is an ordinary synchronous <script> and needs no callback dance.
// Leaflet's JS and its CSS are two separate downloads, and the map is
// only usable if BOTH arrived. When leaflet.js loads but leaflet.css
// does not, Leaflet still runs and still creates every layer -- it just
// draws them with none of its positioning rules, so tiles scatter
// across the page and the choropleth spills out of the container over
// the whole layout. That looks like a bug in this app, but it is a
// missing stylesheet, so detect it explicitly and say so.
//
// The probe: Leaflet's CSS sets `.leaflet-pane { position: absolute }`.
// If a throwaway element with that class is still `static`, the
// stylesheet is not in effect.
function isLeafletCssLoaded() {
  try {
    const probe = document.createElement("div");
    probe.className = "leaflet-pane";
    probe.style.display = "none";
    document.body.appendChild(probe);
    const position = window.getComputedStyle(probe).position;
    document.body.removeChild(probe);
    return position === "absolute";
  } catch (err) {
    return true; // never block the map on a failed probe
  }
}

window.dssInitMap = function () {
  const mapEl = document.getElementById("dss-map");
  if (!mapEl || !window.L || dssMap) return;

  if (!isLeafletCssLoaded()) {
    mapEl.innerHTML =
      '<div class="dss-map-unavailable">' +
      "<div><strong>Map stylesheet didn't load.</strong><br>" +
      "Leaflet's CSS (leaflet.css) was blocked or unreachable, so the map can't be drawn. " +
      "Check your connection to cdnjs.cloudflare.com, then reload. Everything else on this " +
      "page — the barangay list, search and detail panel — still works.</div></div>";
    // The rest of the page is driven by the same JSON, so keep it alive.
    setupSearchBar();
    loadLocations();
    return;
  }

  dssMap = L.map(mapEl, {
    center: [15.4869, 120.59], // Tarlac City center
    zoom: 13,
    // Leaflet zooms on an ordinary wheel/trackpad scroll by default --
    // there is no "hold ctrl to zoom" interstitial of the kind Google
    // Maps shows in its default "cooperative" gesture mode, and so no
    // overlay that swallows the first click on a barangay. This is the
    // Leaflet equivalent of the gestureHandling:"greedy" fix the Google
    // map needed, and here it is simply the default behavior.
    scrollWheelZoom: true,
  });

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    // ODbL requires this credit on every map built from OSM tiles.
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(dssMap);

  // One reusable popup, positioned on hover -- the Leaflet counterpart
  // of the single google.maps.InfoWindow this file used before.
  dssInfoWindow = L.popup({
    closeButton: false,
    autoPan: false,
    className: "dss-map-popup",
    offset: [0, -4],
  });

  addGoogleAttributionControl();

  setupSearchBar();
  setupHideSaturationToggle();
  setupTierFilter();
  setupOverlaySwitch();

  loadAndDrawCityBoundary();
  loadLocations();

  // Leaflet measures the container once, at creation. This map sits in a
  // Bootstrap card inside a responsive column, so its real width is often
  // only settled a tick later (fonts, the detail panel, the sidebar) --
  // and a map measured too early renders tiles for the wrong size, which
  // leaves grey gaps or misplaced tiles. Re-measuring after layout
  // settles, and again on resize, is the standard fix.
  setTimeout(() => dssMap && dssMap.invalidateSize(), 0);
  window.addEventListener("resize", () => dssMap && dssMap.invalidateSize());
};

// REQUIRED BY GOOGLE, AND DELIBERATELY AT THE TOP OF THE MAP.
// Competitor counts on this map come from the Google Places API (New)
// -- see app/services/places_service.py -- and Google's Places policy
// requires that Places-derived content carry Google attribution "near
// the top or bottom of the content and in the same visual element" when
// it is not shown on a Google map. This control puts the "Powered by
// Google" mark in the map's own top-left corner, inside the map
// element itself, which is what that rule asks for.
//
// READ THIS BEFORE YOUR DEFENSE -- attribution is NOT the whole story.
// Google's Maps Platform Terms of Service section 10.5 says: "You must
// not use the Content in a Maps API Implementation that contains a
// non-Google map." This map is an OpenStreetMap basemap showing
// Google-Places-derived competitor counts, so the attribution below
// satisfies the ATTRIBUTION requirement but does not resolve that
// separate restriction. That trade-off was a deliberate project
// decision and is written up in README.md section 5.2 -- know the
// answer before a panel member asks it.
function addGoogleAttributionControl() {
  if (!dssMap || !window.L) return;
  const control = L.control({ position: "topleft" });
  control.onAdd = function () {
    const div = L.DomUtil.create("div", "dss-google-attribution");
    // Google's own hosted mark, with a text fallback if it can't load,
    // so the required credit is shown either way.
    div.innerHTML =
      '<img src="https://maps.gstatic.com/mapfiles/api-3/images/powered-by-google-on-white3.png" ' +
      'alt="Powered by Google" height="18" ' +
      "onerror=\"this.replaceWith(Object.assign(document.createElement('span')," +
      "{textContent:'Powered by Google',className:'dss-google-attribution-text'}))\">";
    div.title = "Competitor data from the Google Places API";
    return div;
  };
  control.addTo(dssMap);
}

document.addEventListener("DOMContentLoaded", function () {
  const select = document.getElementById("businessTypeSelect");
  if (select) select.addEventListener("change", loadLocations);
  applyDeepLinkIndustry();

  if (window.L && document.getElementById("dss-map")) {
    window.dssInitMap();
  } else {
    // Leaflet unavailable (offline, blocked CDN) -- the search bar,
    // detail panel and "All Barangays" list still work off the same
    // JSON data; isolating just filters the list, since there's no map
    // to draw cells on.
    setupSearchBar();
    loadLocations();
  }
});
