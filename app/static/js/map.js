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
//
// PAGE SETTINGS -- every window.DSS_* value this file reads. Both pages
// that load it (sme/saturation_map.html and sme/home.html) set these in
// an inline <script> before map.js. The two pages share one file, so any
// difference between them is expressed HERE, as a setting, rather than
// as page-specific code paths nobody remembers exist.
//
//   Endpoint URLs (required -- set from url_for()):
//     DSS_LOCATIONS_FORECAST_URL    /api/locations-forecast: one AI score per barangay
//     DSS_BARANGAY_DETAIL_URL       /api/barangay-detail: detail panel / info popup
//     DSS_BARANGAY_COORDS_URL       /api/barangay-coords: real barangay positions
//     DSS_BARANGAY_CHOROPLETH_URL   /api/barangay-choropleth: the shaded cells
//     DSS_TARLAC_CITY_BOUNDARY_URL  /api/tarlac-city-boundary: the bold city outline
//
//   Optional:
//     DSS_DEFAULT_INDUSTRY    Industry to score when the page has no
//                             #businessTypeSelect -- the Home mini map
//                             passes the chosen plan's industry. Falls
//                             back to "Food and Beverage".
//     DSS_FOCUS_LOCATION      A barangay name (case-insensitive). Once the
//                             map has loaded, that barangay is outlined,
//                             centred, and its details opened -- in the
//                             #detailPanel if the page has one, otherwise
//                             in an info popup on the map. Every other
//                             barangay stays visible (this is "here is
//                             your plan's barangay", not an isolate).
//                             Unset or empty = the normal city-wide view.
//     DSS_MAP_AS_OF           "YYYY-MM": score the map for that month --
//                             history, or the model's prediction ahead
//                             (set by the Saturation Map's timeline, see
//                             map_timeline.js). Unset or empty = now.
//     DSS_SATURATION_MAP_URL  When set, the info popup ends with an
//                             "Open in full map" link to the Saturation
//                             Map, focused on that barangay and industry.
//     DSS_DEMAND_SUMMARY      PSA FIES household-spending block shown in
//                             the detail panel (see demandDetailHtml).
//
//   Set by the pages but NOT read here (other scripts use them):
//     DSS_FORECAST_URL, DSS_PLACES_URL, DSS_CSRF_TOKEN.
//
// All of these except DSS_DEMAND_SUMMARY are read when they are needed,
// not when this file loads, so a page may set them in any <script> that
// runs before DOMContentLoaded. DSS_DEMAND_SUMMARY must be set before
// map.js is loaded.
//
// PAGES WITHOUT A DETAIL PANEL (the Home mini map): clicking a barangay
// opens its details in a popup on the map instead, and the page opens
// on the whole city (or on DSS_FOCUS_LOCATION) rather than zooming into
// the alphabetically-first barangay -- that default only exists to fill
// a panel, and with no panel it just threw the view somewhere arbitrary.

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
// Two map overlays, with separate jobs (see "Hover tooltip + info popup"
// below): a hover TOOLTIP that follows the cursor, and a pinned info
// POPUP opened by a click (on pages with no detail panel) or by
// DSS_FOCUS_LOCATION.
let dssHoverTooltip = null;
let dssHoverLocation = null; // whose content the tooltip currently holds
let dssInfoPopup = null;
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
// The DSS_FOCUS_LOCATION barangay, once resolved to its real name. Drawn
// with the same bold outline as an isolated cell, but WITHOUT hiding the
// others, so the plan's barangay stays findable among its neighbours.
let dssFocusedLocation = null;
// Bumped by every selection AND by "show all". A detail request that
// comes back after either has happened is stale: it may still fill the
// panel if it is for the barangay currently selected, but it must not
// move the map or open a popup -- that would yank the view back to a
// barangay the user has already moved on from (double-click-to-restore
// used to do exactly that when the detail request was slower than the
// double-click).
let dssSelectSeq = 0;

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
  // Folded by default: the figures are the same for every barangay, so
  // they need not push the barangay's own numbers down the panel.
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
    <details class="small mt-3 dss-demand-fold">
      <summary class="fw-semibold mb-1">Est. Household Demand <span class="text-muted" style="font-weight:400;">(Tarlac City avg., not barangay-specific)</span></summary>
      ${rows}
      <div class="text-muted" style="font-size:.75rem;margin-top:.35rem;">
        Based on Tarlac's real ${dssDemandSummary.expenditure_year} PSA FIES average annual family expenditure
        (${pesoShort(dssDemandSummary.tarlac_avg_annual_family_expenditure_php)}) applied to the PSA's national
        per-category spending shares -- an estimate, since PSA does not publish a Tarlac- or barangay-specific
        category breakdown. * Recreation is carried over from a separate 2021 PSA/CPBRD factsheet (the 2023
        release folds it into a combined "Other" bucket). Same figure for every barangay.
      </div>
    </details>`;
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

// DSS_FOCUS_LOCATION, trimmed, or null when unset/blank/not a string --
// read at use time (see PAGE SETTINGS at the top of this file).
function focusLocationSetting() {
  const value = window.DSS_FOCUS_LOCATION;
  if (typeof value !== "string") return null;
  return value.trim() || null;
}

// The full Saturation Map has a side panel for a barangay's details; the
// Home mini map does not, and shows them in a popup on the map instead.
// Checked on every call rather than once, because the panel is the only
// thing that decides which of the two a page gets.
//
// A Saturation Map whose side panel has been folded away (the tab on its
// edge) counts as having none: details then open in the map popup, the
// same as on the Home page, instead of in a panel nobody can see.
function hasDetailPanel() {
  const panel = document.getElementById("detailPanel");
  if (!panel) return false;
  const folded = typeof panel.closest === "function" && panel.closest(".is-side-collapsed");
  return !folded;
}

// Industry names reach this file from the database, where industry_type
// is free text an SME can type -- so they are escaped before being put
// into HTML, rather than trusted to be one of the known PSIC sections.
function escapeHtml(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
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

// Small, muted "(est.)" after a count that is a generated placeholder
// rather than a real Google Places / LGU-DTI figure. Kept deliberately
// quiet -- it is a disclosure, not a warning -- but never left off: the
// "About this map's data" note on the Saturation Map promises it.
const ESTIMATE_MARK =
  ' <span class="text-muted" style="font-size:.72rem;font-weight:400;" ' +
  'title="Estimated: no Google Places or LGU/DTI count on file for this industry here yet">(est.)</span>';

const SUBHEADING_STYLE = "font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;";

// The barangay's top industries by business count, ranked, as detail
// rows -- shared by the Saturation Map's detail panel and the Home mini
// map's info popup so the two can never disagree. The ranking itself is
// done server-side (see api_controller._top_industries): which industry
// is "top" is a finding, not presentation.
function topIndustriesHtml(items) {
  if (!Array.isArray(items) || !items.length) {
    return '<div class="text-muted border-bottom py-1">No business counts on file yet</div>';
  }
  return items
    .map(
      (item, index) => `
      <div class="d-flex justify-content-between align-items-baseline gap-2 border-bottom py-1" title="${escapeHtml(item.industry)}">
        <span class="text-muted">${index + 1}. ${escapeHtml(item.label || item.industry)}</span>
        <span class="text-nowrap"><strong>${Number(item.count).toLocaleString()}</strong>${item.is_estimated ? ESTIMATE_MARK : ""}</span>
      </div>`
    )
    .join("");
}

// When the timeline shows another month, the panel adds that month's
// figure under today's -- the details below it (businesses, population)
// are today's, and say nothing about the chosen month.
function timelineDetailHtml(location) {
  const row = (dssLocationsCache || []).find((r) => r.location === location);
  const label = periodLabel(row);
  if (!label) return "";
  return `
    <div class="small border rounded px-2 py-1 dss-timeline-detail">
      <div class="d-flex justify-content-between align-items-start gap-2">
        <span class="text-muted">Saturation <span class="d-block" style="font-size:.72rem;">${escapeHtml(label)}</span></span>
        <strong class="text-nowrap">${Number(row.saturation_index).toFixed(1)}%</strong>
      </div>
      <div class="text-muted" style="font-size:.72rem;">${Number(row.competitor_count).toLocaleString()} businesses ${row.basis === "predicted" ? "expected" : "at the time"} &middot; ${escapeHtml(displayLabelFor(row.cluster_label))}</div>
    </div>`;
}

function renderDetailPanel(detail) {
  const panel = document.getElementById("detailPanel");
  if (!panel) return;
  const actions = (detail.recommended_actions || [])
    .map((a) => `<li>${escapeHtml(a)}</li>`)
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
          <h5 class="mb-0">${escapeHtml(detail.location)}</h5>
          <span class="dss-stat-pill ${pillClass(detail.cluster_label)}">${displayLabelFor(detail.cluster_label)}</span>
        </div>
      </div>
      ${isolateBtn}
    </div>
    <div class="small mb-3">
      <div class="d-flex justify-content-between border-bottom py-1"><span class="text-muted">Total Businesses</span><strong>${Number(detail.total_businesses || 0).toLocaleString()}</strong></div>
      <div class="text-muted pt-2 pb-1" style="${SUBHEADING_STYLE}">Top industries here</div>
      ${topIndustriesHtml(detail.top_industries)}
      <div class="d-flex justify-content-between border-bottom py-1"><span class="text-muted">Population</span><strong>${population}</strong></div>
      <div class="d-flex justify-content-between border-bottom py-1"><span class="text-muted">Population Density <span class="text-muted" style="font-weight:400;">(fixed)</span></span><strong>${density}</strong></div>
      <div class="d-flex justify-content-between align-items-start gap-2 py-1">
        <span class="text-muted">Saturation Score
          <span class="d-block" style="font-size:.72rem;">for ${escapeHtml(detail.industry_type)}</span></span>
        <strong class="text-nowrap">${Number(detail.saturation_index).toFixed(1)}%</strong>
      </div>
    </div>
    ${timelineDetailHtml(detail.location)}
    ${demandDetailHtml()}
    <div class="alert alert-light border mb-0 mt-3">
      <strong>Recommended Actions</strong>
      <ul class="mb-0 mt-1">${actions}</ul>
    </div>
  `;

  const btn = document.getElementById("detailIsolateBtn");
  if (btn) btn.addEventListener("click", () => toggleIsolate(detail.location));
}

// THE WHOLE CITY, when no barangay is picked. The side panel used to
// keep showing the last barangay clicked even after "Show All" put every
// barangay back on the map -- a panel about Dalayap next to a map of all
// of Tarlac City. With nothing selected it now summarises the city for
// the chosen industry, from the same rows the map is drawn from: how
// many businesses, the average saturation, how many barangays sit in
// each tier, and where there is the most and the least room.
function renderCityOverviewPanel() {
  const panel = document.getElementById("detailPanel");
  if (!panel) return;
  const rows = dssLocationsCache || [];
  if (!rows.length) {
    panel.innerHTML = '<div class="text-muted text-center py-4">Loading Tarlac City&hellip;</div>';
    return;
  }
  const industry = rows[0].industry_type || currentBusinessType();
  const businesses = rows.reduce((sum, r) => sum + (Number(r.competitor_count) || 0), 0);
  const population = rows.reduce((sum, r) => sum + (Number(r.population) || 0), 0);
  const avgSat = rows.reduce((sum, r) => sum + (Number(r.saturation_index) || 0), 0) / rows.length;
  const tiers = {};
  rows.forEach((r) => { tiers[r.cluster_label] = (tiers[r.cluster_label] || 0) + 1; });
  const bySat = [...rows].sort((a, b) => Number(a.saturation_index) - Number(b.saturation_index));
  const open = bySat.slice(0, 3);
  const crowded = bySat.slice(-3).reverse();
  const line = (r) => `<li><button type="button" class="btn btn-link btn-sm p-0 align-baseline dss-city-pick" data-location="${escapeHtml(r.location)}">${escapeHtml(r.location)}</button>
      <span class="text-muted">&middot; ${Number(r.saturation_index).toFixed(1)}%, ${Number(r.competitor_count).toLocaleString()} businesses</span></li>`;
  const tierRows = ["Low", "Moderate", "High", "Saturated"]
    .filter((t) => tiers[t])
    .map((t) => `<div class="d-flex justify-content-between border-bottom py-1">
        <span><span class="dss-stat-pill ${pillClass(t)}">${displayLabelFor(t)}</span></span><strong>${tiers[t]} barangay${tiers[t] === 1 ? "" : "s"}</strong></div>`)
    .join("");
  const verdict = avgSat >= 75 ? "very crowded" : avgSat >= 50 ? "crowded" : avgSat >= 25 ? "moderately busy" : "open";

  panel.innerHTML = `
    <div class="d-flex align-items-start gap-2 mb-3">
      <i class="bi bi-buildings fs-4 text-primary" aria-hidden="true"></i>
      <div>
        <h5 class="mb-0">Tarlac City</h5>
        <div class="text-muted small">All ${rows.length} barangays &middot; ${escapeHtml(industry)}</div>
      </div>
    </div>
    <div class="small mb-3">
      <div class="d-flex justify-content-between border-bottom py-1"><span class="text-muted">Businesses in this industry</span><strong>${businesses.toLocaleString()}</strong></div>
      ${population ? `<div class="d-flex justify-content-between border-bottom py-1"><span class="text-muted">Population</span><strong>${population.toLocaleString()}</strong></div>` : ""}
      <div class="d-flex justify-content-between border-bottom py-1"><span class="text-muted">Average saturation</span><strong>${avgSat.toFixed(1)}%</strong></div>
      <div class="text-muted pt-2 pb-1" style="${SUBHEADING_STYLE}">Barangays by level</div>
      ${tierRows}
    </div>
    <div class="small mb-2"><strong>Most room for a new business</strong><ul class="mb-0 ps-3">${open.map(line).join("")}</ul></div>
    <div class="small mb-2"><strong>Most crowded</strong><ul class="mb-0 ps-3">${crowded.map(line).join("")}</ul></div>
    <p class="dss-chart-note mb-0">Across the city this industry is ${verdict} on average (${avgSat.toFixed(1)}% saturated).
      Click a barangay on the map or in the list for its own details.</p>`;
  panel.querySelectorAll(".dss-city-pick").forEach((el) =>
    el.addEventListener("click", () => isolateAndSelect(el.dataset.location)));
}

async function selectLocation(location) {
  dssSelectedLocation = location;
  const seq = ++dssSelectSeq;
  const businessType = currentBusinessType();
  const usePopup = !hasDetailPanel();
  // Close the previous barangay's popup now rather than when the new one
  // opens: it is keepInView, so for as long as it stays open every map
  // move below would be pulled back towards it.
  if (usePopup) closeInfoPopup();

  let detail;
  try {
    detail = await fetch(
      `${window.DSS_BARANGAY_DETAIL_URL}?location=${encodeURIComponent(location)}&industry_type=${encodeURIComponent(businessType)}`
    ).then((r) => r.json());
  } catch (err) {
    detail = null;
  }
  const usable = detail && !detail.error && detail.location ? detail : null;

  // A different barangay was selected while this request was in flight:
  // showing this older answer now would put the wrong barangay's figures
  // on screen.
  if (dssSelectedLocation !== location) return;
  if (usable) renderDetailPanel(usable);

  // Superseded by "show all" or a newer click -- see dssSelectSeq.
  if (seq !== dssSelectSeq) return;
  if (usePopup) {
    // No animation here: the popup's auto-pan measures the map at the
    // moment it opens, and mid-way through an animated zoom Leaflet still
    // reports the OLD zoom -- so the pan would be computed for the wrong
    // view and the popup could still land half outside a small map.
    await focusMapOn(location, { animate: false });
    if (seq === dssSelectSeq) openInfoPopup(location, usable);
  } else {
    await focusMapOn(location);
  }
}

// DSS_FOCUS_LOCATION: outline the plan's barangay, centre on it and open
// its details (panel or popup) exactly as a click would -- but without
// isolating it, so the preview still shows how it compares with the
// barangays around it. Matched case-insensitively against the real
// names, since a plan's location is whatever the SME typed.
function focusBarangay(name) {
  const wanted = String(name || "").trim();
  if (!wanted) return Promise.resolve();
  const match = dssLocationsCache.find((r) => r.location.toLowerCase() === wanted.toLowerCase());
  // Not one of the scored locations: still try it as typed, the same way
  // the search bar does -- it just has no cell to outline.
  const location = match ? match.location : wanted;
  dssFocusedLocation = location;
  applyTierVisibility();
  return selectLocation(location);
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
  // Nothing is focused any more, so the panel describes the whole city
  // rather than the barangay that was isolated.
  dssSelectedLocation = null;
  // Cancel any detail request still in flight (it must not zoom back in
  // afterwards), and close the info popup BEFORE re-centring: it is
  // keepInView, and would otherwise drag the map straight back to it.
  dssSelectSeq += 1;
  closeInfoPopup();
  applyTierVisibility();
  renderLocationList(dssLocationsCache);
  updateIsolationBanner();
  renderCityOverviewPanel();
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

// `options.animate === false` jumps straight there -- see selectLocation()
// for why the info-popup path needs that.
async function focusMapOn(location, options) {
  if (!dssMap || !window.L) return;
  const coords = await loadCoords();
  const point = coords[location];
  if (!point) return;
  dssMap.setView([point.lat, point.lng], 15, options && options.animate === false ? { animate: false } : undefined);
}

// ---- Hover tooltip + info popup: always fully inside the map ---------
// The Home page's mini map is small (~420px tall), and its hover card
// used to open ABOVE the cursor with no regard for the map's edge: over
// the top half of the map the card was cut off, and the only way to read
// it was to drag the map until the barangay sat near the bottom. Both
// overlays below are now kept whole wherever the barangay is.
//
// "Inside the map" also means clear of the map's own controls, because
// Leaflet draws controls ABOVE popups and tooltips: the "Powered by
// Google" mark in the top-left corner (see addGoogleAttributionControl),
// the zoom buttons in the top-right (moved there in dssInitMap so the
// two no longer stack into one tall block), and the OpenStreetMap credit
// along the bottom. These margins keep every card out from under all
// three. [x, y] in pixels.
const MAP_CLEAR_TOP_LEFT = [16, 48];
const MAP_CLEAR_BOTTOM_RIGHT = [48, 24];
const HOVER_GAP_PX = 12; // between the cursor and the hover card
const TOOLTIP_CHROME_PX = 14; // Leaflet tooltip padding + border, both sides
// A popup's tip, content margins and border sit OUTSIDE its maxHeight
// box; subtracting them makes maxHeight mean "the whole popup fits".
const POPUP_CHROME_PX = 52;

// The hover card is never wider than half the map. Its direction is
// "auto" -- Leaflet opens it on whichever side of the cursor faces the
// map's centre -- so capped at half the width it always fits sideways,
// even on a phone-width map.
function hoverContentMaxWidth() {
  const mapWidth = dssMap ? dssMap.getSize().x : 480;
  const half = Math.floor(mapWidth / 2) - HOVER_GAP_PX - MAP_CLEAR_TOP_LEFT[0] - TOOLTIP_CHROME_PX;
  return Math.max(120, Math.min(240, half));
}

function hoverTooltipHtml(row, color, isPopulationMode) {
  const population = row.population
    ? `<div>Population: <strong>${Number(row.population).toLocaleString()}</strong></div>`
    : "";
  const density = row.population_density
    ? `<div>Density: <strong>${Number(row.population_density).toLocaleString()} /km&sup2;</strong></div>`
    : "";
  const modeLabel = isPopulationMode ? "Population density overlay" : displayLabelFor(row.cluster_label);
  // Styled inline rather than by class: home.html does not load this
  // page's stylesheet, and Leaflet's tooltip defaults to nowrap.
  return `<div style="width:max-content;max-width:${hoverContentMaxWidth()}px;white-space:normal;font-size:.8rem;line-height:1.45;">
      <div style="font-weight:600;font-size:.95rem;">${escapeHtml(row.location)}</div>
      <div style="margin:.15rem 0 .3rem;">
        <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${color};margin-right:.35rem;"></span>${modeLabel}
      </div>
      <div>Industry: <strong>${escapeHtml(row.industry_type)}</strong></div>
      <div>Businesses: <strong>${Number(row.competitor_count).toLocaleString()}</strong></div>
      ${population}
      ${density}
      <div style="color:#6c757d;font-size:.72rem;margin-top:.25rem;">Saturation ${row.saturation_index}%${periodLabel(row) ? ` (${escapeHtml(periodLabel(row))})` : ""} &middot; click to focus, double-click to show all</div>
    </div>`;
}

function showHoverTooltip(row, color, isPopulationMode, evt) {
  if (!dssMap || !dssHoverTooltip || !evt || !evt.latlng) return;
  // Rebuild the card only when the cursor enters a different barangay,
  // not on every pixel of movement inside the same one.
  if (dssHoverLocation !== row.location) {
    dssHoverTooltip.setContent(hoverTooltipHtml(row, color, isPopulationMode));
    dssHoverLocation = row.location;
  }
  dssHoverTooltip.setLatLng(evt.latlng);
  if (!dssMap.hasLayer(dssHoverTooltip)) dssHoverTooltip.openOn(dssMap);
  keepHoverTooltipInside(evt.containerPoint);
}

function hideHoverTooltip() {
  dssHoverLocation = null;
  if (dssMap && dssHoverTooltip) dssMap.closeTooltip(dssHoverTooltip);
}

// Direction "auto" handles left/right; this handles up/down. A
// left/right tooltip is centred vertically on the cursor, so near the top
// or bottom edge half of it would hang outside -- shift it back in by
// exactly the overhang. Measured from the rendered card, so it holds for
// any content length.
//
// NOT autoPan, deliberately: a card that follows the cursor and pans the
// map to fit would move the map under the mouse on every movement near
// an edge -- the map would chase the cursor. Only the pinned info popup
// (below), which opens once and stays put, auto-pans.
function keepHoverTooltipInside(cursor) {
  const el = dssHoverTooltip.getElement ? dssHoverTooltip.getElement() : null;
  if (!el || !cursor) return;
  const mapHeight = dssMap.getSize().y;
  const half = el.offsetHeight / 2;
  const top = cursor.y - half;
  const bottom = cursor.y + half;
  let dy = 0;
  if (top < MAP_CLEAR_TOP_LEFT[1]) dy = MAP_CLEAR_TOP_LEFT[1] - top;
  else if (bottom > mapHeight - MAP_CLEAR_BOTTOM_RIGHT[1]) dy = mapHeight - MAP_CLEAR_BOTTOM_RIGHT[1] - bottom;
  dy = Math.round(dy);
  const current = dssHoverTooltip.options.offset || [HOVER_GAP_PX, 0];
  if (current[1] !== dy) {
    dssHoverTooltip.options.offset = [HOVER_GAP_PX, dy];
    dssHoverTooltip.update();
  }
}

function closeInfoPopup() {
  if (dssMap && dssInfoPopup) dssMap.closePopup(dssInfoPopup);
  dssInfoPopup = null;
}

// The pinned card with a barangay's details, for pages with no detail
// panel (the Home mini map). `detail` is the /api/barangay-detail answer,
// or null if that request failed -- the card then falls back to the
// figures the map already has for this barangay.
function infoPopupHtml(location, detail, row) {
  const d = detail || {};
  const pick = (key) => {
    if (d[key] !== undefined && d[key] !== null) return d[key];
    if (row && row[key] !== undefined && row[key] !== null) return row[key];
    return null;
  };
  const cluster = pick("cluster_label");
  const industry = pick("industry_type") || currentBusinessType();
  const saturation = pick("saturation_index");
  const businesses = pick("competitor_count");
  const population = pick("population");

  const tierLine = cluster
    ? `<div style="margin:.1rem 0 .4rem;"><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${CLUSTER_COLORS[cluster] || CLUSTER_COLORS.Moderate};margin-right:.35rem;"></span>${displayLabelFor(cluster)}</div>`
    : "";
  const industryLine =
    saturation !== null
      ? `<div class="mb-1">${escapeHtml(industry)}: <strong>${Number(saturation).toFixed(1)}%</strong> saturated` +
        (businesses !== null ? ` &middot; <strong>${Number(businesses).toLocaleString()}</strong> businesses` : "") +
        "</div>"
      : "";
  const populationLine =
    population !== null
      ? `<div class="d-flex justify-content-between gap-3 border-bottom py-1"><span class="text-muted">Population</span><strong>${Number(population).toLocaleString()}</strong></div>`
      : "";
  const topSection = detail
    ? `<div class="text-muted pt-2 pb-1" style="${SUBHEADING_STYLE}">Top industries here</div>${topIndustriesHtml(detail.top_industries)}`
    : "";
  const fullMapLink = window.DSS_SATURATION_MAP_URL
    ? `<a class="d-inline-block mt-2" href="${escapeHtml(window.DSS_SATURATION_MAP_URL)}?location=${encodeURIComponent(location)}&industry_type=${encodeURIComponent(industry)}">Open in full map &rarr;</a>`
    : "";

  return `<div style="line-height:1.45;">
      <div style="font-weight:600;font-size:.95rem;">${escapeHtml(location)}</div>
      ${tierLine}
      ${industryLine}
      ${populationLine}
      ${topSection}
      ${fullMapLink}
    </div>`;
}

function openInfoPopup(location, detail) {
  if (!dssMap || !window.L) return;
  const point = dssCoordsCache[location];
  if (!point) return; // can't be placed on the map -- nothing to point at
  const row = dssLocationsCache.find((r) => r.location === location) || null;

  // Sized from the map it is opening in, so the same code suits the full
  // 500px map and the Home page's small one: never wider than the space
  // between the side margins, never taller than the space between the top
  // and bottom margins -- longer content scrolls inside the card instead.
  const size = dssMap.getSize();
  const maxWidth = Math.max(160, Math.min(260, size.x - MAP_CLEAR_TOP_LEFT[0] - MAP_CLEAR_BOTTOM_RIGHT[0] - 24));
  const maxHeight = Math.max(96, size.y - MAP_CLEAR_TOP_LEFT[1] - MAP_CLEAR_BOTTOM_RIGHT[1] - POPUP_CHROME_PX);

  hideHoverTooltip(); // a tap on a phone leaves the hover card up otherwise
  dssInfoPopup = L.popup({
    className: "dss-map-popup",
    maxWidth,
    minWidth: Math.min(200, maxWidth),
    maxHeight,
    // Pan the map just enough to show the whole card, stopping short of
    // the controls (see MAP_CLEAR_*)...
    autoPan: true,
    autoPanPaddingTopLeft: MAP_CLEAR_TOP_LEFT,
    autoPanPaddingBottomRight: MAP_CLEAR_BOTTOM_RIGHT,
    // ...and keep it whole while it is open: dragging the map can no
    // longer push half the card off the edge. Click the map (or the x)
    // to dismiss it and explore freely.
    keepInView: true,
  })
    .setLatLng([point.lat, point.lng])
    .setContent(infoPopupHtml(location, detail, row))
    .openOn(dssMap);
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

  // The cells are about to be replaced (new industry or overlay), so a
  // hover card still showing the old figures would be wrong -- and the
  // cell it belongs to won't fire a mouseout once it is removed.
  hideHoverTooltip();
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
    polygon.on("mousemove", (evt) => showHoverTooltip(row, color, isPopulationMode, evt));
    polygon.on("mouseout", hideHoverTooltip);
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
// "focused" even though it's now the only shape on the map. So does the
// DSS_FOCUS_LOCATION barangay while nothing is isolated -- the same
// "this one" signal, just with its neighbours still showing.
function applyTierVisibility() {
  if (!dssMap) return;
  dssPolygons.forEach(({ polygon, location, cluster }) => {
    const isIsolatedCell = dssIsolatedLocation !== null && location === dssIsolatedLocation;
    const isFocusCell = dssIsolatedLocation === null && dssFocusedLocation !== null && location === dssFocusedLocation;
    const outlined = isIsolatedCell || isFocusCell;
    const show = dssIsolatedLocation !== null
      ? isIsolatedCell
      : dssZonesVisible && (dssOverlayMode === "population" || dssVisibleTiers[cluster] !== false);
    if (show) {
      if (!dssMap.hasLayer(polygon)) polygon.addTo(dssMap);
    } else if (dssMap.hasLayer(polygon)) {
      dssMap.removeLayer(polygon);
    }
    polygon.setStyle({
      weight: outlined ? 3 : 1,
      color: outlined ? "#111827" : "#ffffff",
    });
    // Drawn last, so the neighbouring cells' white edges can't paint over
    // part of its bold outline.
    if (outlined && show) polygon.bringToFront();
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
    // The one-line caption under the map says what the shading means, so
    // it has to change with the overlay too -- each variant is marked
    // with the overlay it describes.
    document.querySelectorAll("[data-overlay-caption]").forEach((el) => {
      el.hidden = el.dataset.overlayCaption !== dssOverlayMode;
    });
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

// ---- Saturation Map loading state ------------------------------------
// Shown while the barangay scores and cells are being fetched. This
// runs on the FIRST load and on every industry/overlay change, because
// both re-score all 76 barangays -- the second case is the one that
// used to leave the map looking frozen with no explanation.
//
// Staged messages, but honest ones: they describe what the server is
// actually doing, and the whole thing is removed the moment the data
// lands rather than running to the end of a scripted timeline.
const MAP_LOAD_STAGES = [
  [0, "Reading the businesses on file\u2026"],
  [450, "Scoring every barangay with the AI engine\u2026"],
  [1600, "Drawing the saturation zones\u2026"],
];

let mapLoadTimer = null;

function showMapLoading(on) {
  const box = document.getElementById("mapLoading");
  const step = document.getElementById("mapLoadingStep");
  if (!box) return;

  if (mapLoadTimer) {
    clearInterval(mapLoadTimer);
    mapLoadTimer = null;
  }
  if (!on) {
    box.hidden = true;
    return;
  }

  const started = performance.now();
  if (step) step.textContent = MAP_LOAD_STAGES[0][1];
  box.hidden = false;
  mapLoadTimer = setInterval(() => {
    const elapsed = performance.now() - started;
    let message = MAP_LOAD_STAGES[0][1];
    for (const [at, text] of MAP_LOAD_STAGES) if (elapsed >= at) message = text;
    if (step) step.textContent = message;
  }, 200);
}

// Tells the page's other scripts (the timeline) what the map now shows.
function announceLocationsLoaded(rows) {
  if (typeof document.dispatchEvent !== "function" || typeof CustomEvent !== "function") return;
  document.dispatchEvent(new CustomEvent("dss:locations-loaded", { detail: { rows } }));
}

// "Mar 2027 · predicted" for a row scored for another month, or "" for
// one scored for now -- used by the hover card and the detail panel.
const BASIS_LABEL = {
  recorded: "on file",
  "back-projected": "back-projected",
  predicted: "predicted",
};
function periodLabel(row) {
  if (!row || !row.period || row.period === "current" || !row.as_of) return "";
  const [year, month] = String(row.as_of).split("-").map(Number);
  const name = new Date(year, month - 1, 1).toLocaleString(undefined, { month: "short", year: "numeric" });
  return `${name} · ${BASIS_LABEL[row.basis] || row.basis}`;
}

async function loadLocations() {
  const businessType = currentBusinessType();
  showMapLoading(true);
  try {
    return await loadLocationsInner(businessType);
  } finally {
    // finally, not after the happy path: a failed fetch must not leave
    // the overlay up forever covering a map the visitor could still
    // pan and read.
    showMapLoading(false);
  }
}

async function loadLocationsInner(businessType) {
  // Fetch the forecast rows and the real barangay coordinates in
  // parallel. Coordinates used to only load once a Google Map actually
  // initialized (focusMapOn() -> loadCoords()), which never happens
  // when no GOOGLE_MAPS_JS_API_KEY is configured -- leaving
  // dssCoordsCache empty and crashing updateCoordStatus() below.
  // /api/barangay-coords is a plain backend endpoint independent of the
  // Maps JS SDK, so fetch it here unconditionally instead.
  // DSS_MAP_AS_OF ("YYYY-MM", set by the Saturation Map's timeline):
  // score the map for another month. Unset or empty = now.
  const asOf = typeof window.DSS_MAP_AS_OF === "string" ? window.DSS_MAP_AS_OF : "";
  const asOfParam = asOf ? `&as_of=${encodeURIComponent(asOf)}` : "";
  const [rows] = await Promise.all([
    fetch(
      `${window.DSS_LOCATIONS_FORECAST_URL}?industry_type=${encodeURIComponent(businessType)}${asOfParam}`
    ).then((r) => r.json()),
    loadCoords(),
  ]);
  dssLocationsCache = rows;
  announceLocationsLoaded(rows);
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
  } else if (focusLocationSetting()) {
    // First load on a page that names a barangay to open on (the Home
    // page passes the chosen plan's) -- see focusBarangay(). Later loads
    // take the branch above, since this sets dssSelectedLocation.
    focusBarangay(focusLocationSetting());
  } else if (hasDetailPanel()) {
    // Default view on first load: ALL barangays on the map, and the
    // panel summarising the whole city -- nothing has been picked, so
    // the panel should not pretend the first barangay was.
    renderCityOverviewPanel();
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
    // Leaflet's default zoom buttons are added below, top-RIGHT -- see
    // the zoom control comment.
    zoomControl: false,
  });

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    // ODbL requires this credit on every map built from OSM tiles.
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(dssMap);

  // Hover card: a Leaflet TOOLTIP (it used to be a popup) that follows
  // the cursor. A tooltip, for two reasons:
  //   * tooltips ignore the mouse, so the card can never slide under the
  //     cursor and "steal" it -- which fired mouseout on the barangay and
  //     made the old hover popup flicker; and
  //   * Leaflet shows one popup at a time, and the pinned info popup
  //     (openInfoPopup) needs to stay open while the mouse moves -- a
  //     hover popup would have closed it on the first movement.
  // Direction "auto" + keepHoverTooltipInside() keep it whole in any
  // size of map.
  dssHoverTooltip = L.tooltip({
    direction: "auto",
    offset: [HOVER_GAP_PX, 0],
    className: "dss-map-tooltip",
    opacity: 0.96,
  });

  addGoogleAttributionControl();

  // Zoom buttons top-RIGHT rather than Leaflet's default top-left. The
  // "Powered by Google" mark has to be top-left (see
  // addGoogleAttributionControl), and the two stacked in one corner made
  // a block ~110px tall that popups and tooltips slid underneath --
  // Leaflet draws controls above both. One control per top corner keeps
  // each corner shallow enough for MAP_CLEAR_* to step around.
  L.control.zoom({ position: "topright" }).addTo(dssMap);

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
  // The Home map grows to the height of the forecast panel beside it,
  // and that panel settles only after its charts draw -- a size change
  // no window resize announces. Watching the element itself covers it.
  if (window.ResizeObserver) {
    new ResizeObserver(() => dssMap && dssMap.invalidateSize()).observe(mapEl);
  }
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

// For the timeline: re-score the map (industry, overlay and isolation
// stay as they are). Resolves once the map is redrawn.
window.dssReloadMap = function () {
  return loadLocations();
};

// The Saturation Map's side panel was folded or unfolded. Unfolding
// re-fills the panel for the barangay picked while it was hidden (those
// picks opened a popup instead); folding closes nothing -- the next pick
// simply opens a popup.
document.addEventListener("dss:side-panel", function (event) {
  const collapsed = event.detail && event.detail.collapsed;
  if (!collapsed) {
    closeInfoPopup();
    if (dssSelectedLocation) {
      selectLocation(dssSelectedLocation);
    } else {
      // Opened folded, nothing picked yet: the whole-city summary, the
      // same as a normal first load.
      renderCityOverviewPanel();
    }
  }
});

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
