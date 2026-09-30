// Verifies map.js against a STRICT stub of the Leaflet 1.9 API surface it
// uses. The stub throws on any method name or argument shape that real
// Leaflet would reject, so this catches Google->Leaflet porting mistakes
// (wrong method, wrong latlng shape, wrong polygon nesting depth) without
// needing the CDN, which this sandbox blocks.
//
// Also covers the Home mini-map behaviour driven by window settings:
// DSS_FOCUS_LOCATION (outline + centre + info popup, no isolation), and
// hover/info cards that stay whole inside a ~420px-tall map.
//
// Run: node tests/leaflet_contract_test.js
const fs = require("fs");
const assert = require("assert");

const calls = [];
function rec(name, args) { calls.push({ name, args }); }

// The Home page's mini map after its resize: ~420px tall. The full
// Saturation Map is 500px, so anything that fits here fits there.
const MAP_SIZE = { x: 540, y: 420 };

function assertLatLng(v, where) {
  const ok = Array.isArray(v) ? v.length === 2 && v.every((n) => typeof n === "number")
                              : v && typeof v.lat === "number" && typeof v.lng === "number";
  assert(ok, `${where}: not a Leaflet LatLng (got ${JSON.stringify(v)})`);
}
function assertPoint(v, where) {
  assert(Array.isArray(v) && v.length === 2 && v.every((n) => typeof n === "number"),
    `${where}: expected an [x, y] point, got ${JSON.stringify(v)}`);
}
// Leaflet polygon nesting: [latlng...] | [[latlng...]] (rings/holes)
// | [[[latlng...]]] (multipolygon). Assert we pass the 3-level form and
// that the leaves are real latlngs.
function assertPolygonLatLngs(v, where) {
  assert(Array.isArray(v) && v.length, `${where}: empty polygon latlngs`);
  assert(Array.isArray(v[0]) && Array.isArray(v[0][0]),
    `${where}: expected 3-level [polygon][ring][point] nesting (multipolygon form), got depth < 3. ` +
    `Two-level input would make extra rings render as HOLES in Leaflet.`);
  v.forEach((poly, i) => poly.forEach((ring, j) => {
    assert(ring.length >= 4, `${where}: ring ${i}.${j} has ${ring.length} points`);
    assertLatLng(ring[0], `${where} ring ${i}.${j}[0]`);
  }));
}

class Layer {
  constructor(kind) { this._kind = kind; this._on = {}; this._style = {}; }
  addTo(map) { assert(map && map._isMap, "addTo() needs the map"); map._layers.add(this); return this; }
  on(evt, fn) {
    assert(["click", "dblclick", "mousemove", "mouseover", "mouseout"].includes(evt), `unknown layer event ${evt}`);
    assert(typeof fn === "function", "handler must be a function");
    this._on[evt] = fn; return this;
  }
  setStyle(o) {
    Object.keys(o).forEach((k) => assert(
      ["color", "weight", "opacity", "fillColor", "fillOpacity", "fill", "interactive", "dashArray"].includes(k),
      `setStyle: '${k}' is not a Leaflet Path option (Google names like strokeWeight/strokeColor won't work)`));
    Object.assign(this._style, o); return this;
  }
  bringToBack() { rec("bringToBack"); return this; }
  bringToFront() { rec("bringToFront", [this]); return this; }
}

const POPUP_OPTIONS = [
  "closeButton", "autoPan", "className", "offset", "maxWidth", "minWidth", "maxHeight",
  "autoPanPaddingTopLeft", "autoPanPaddingBottomRight", "autoPanPadding", "keepInView",
  "autoClose", "closeOnClick",
];
class Popup {
  constructor(opts) {
    Object.keys(opts || {}).forEach((k) => assert(POPUP_OPTIONS.includes(k), `L.popup: unknown option '${k}'`));
    ["autoPanPaddingTopLeft", "autoPanPaddingBottomRight", "autoPanPadding", "offset"].forEach((k) => {
      if (opts && opts[k] !== undefined) assertPoint(opts[k], `L.popup ${k}`);
    });
    ["maxWidth", "minWidth", "maxHeight"].forEach((k) => {
      if (opts && opts[k] !== undefined) assert(typeof opts[k] === "number" && opts[k] > 0, `L.popup ${k} must be a positive number`);
    });
    this.options = Object.assign({}, opts);
    this._content = null;
  }
  setContent(html) { assert(typeof html === "string", "popup content must be a string"); this._content = html; return this; }
  setLatLng(ll) { assertLatLng(ll, "popup.setLatLng"); this._ll = ll; return this; }
  openOn(map) { assert(map && map._isMap, "openOn() needs the map"); map._openPopup = this; return this; }
}

const TOOLTIP_OPTIONS = ["direction", "offset", "className", "opacity", "permanent", "sticky", "interactive", "pane"];
class Tooltip {
  constructor(opts) {
    Object.keys(opts || {}).forEach((k) => assert(TOOLTIP_OPTIONS.includes(k), `L.tooltip: unknown option '${k}'`));
    if (opts && opts.direction !== undefined) {
      assert(["right", "left", "top", "bottom", "center", "auto"].includes(opts.direction), `L.tooltip: bad direction '${opts.direction}'`);
    }
    if (opts && opts.offset !== undefined) assertPoint(opts.offset, "L.tooltip offset");
    this.options = Object.assign({}, opts);
    this._content = null;
    this._map = null;
    // Rendered height of the card, as the browser would measure it.
    this._el = { offsetHeight: 150, offsetWidth: 200 };
    this._updates = 0;
  }
  setContent(html) { assert(typeof html === "string", "tooltip content must be a string"); this._content = html; return this; }
  setLatLng(ll) { assertLatLng(ll, "tooltip.setLatLng"); this._ll = ll; return this; }
  openOn(map) {
    assert(map && map._isMap, "openOn() needs the map");
    assert(this._ll, "a standalone tooltip needs setLatLng() before it is opened");
    map._layers.add(this); map._openTooltip = this; this._map = map; return this;
  }
  getElement() { return this._map ? this._el : undefined; }
  update() { assertPoint(this.options.offset, "tooltip offset at update()"); this._updates += 1; return this; }
}

function makeControl(opts) {
  return { options: opts, addTo(map) { assert(map._isMap); map._controls.push(this); this._el = this.onAdd ? this.onAdd(map) : null; return this; } };
}

const L = {
  map(el, opts) {
    assert(el, "L.map needs an element");
    Object.keys(opts || {}).forEach((k) => assert(
      ["center", "zoom", "scrollWheelZoom", "zoomControl", "attributionControl", "maxZoom", "minZoom"].includes(k),
      `L.map: unknown option '${k}' (Google options like mapTypeId/gestureHandling are not Leaflet)`));
    if (opts && opts.center) assertLatLng(opts.center, "L.map center");
    const map = {
      _isMap: true, _layers: new Set(), _controls: [], _openPopup: null, _openTooltip: null,
      _zoomControl: !(opts && opts.zoomControl === false),
      setView(ll, z, o) {
        assertLatLng(ll, "map.setView"); assert(typeof z === "number", "setView zoom");
        if (o !== undefined) Object.keys(o).forEach((k) => assert(["animate", "duration", "pan", "zoom", "reset"].includes(k), `setView: unknown option '${k}'`));
        rec("setView", [ll, z, o]); return this;
      },
      removeLayer(l) { this._layers.delete(l); return this; },
      hasLayer(l) { return this._layers.has(l); },
      closePopup(p) { if (!p || p === this._openPopup) this._openPopup = null; return this; },
      closeTooltip(t) { assert(t instanceof Tooltip, "closeTooltip needs the tooltip"); this._layers.delete(t); t._map = null; if (this._openTooltip === t) this._openTooltip = null; return this; },
      getSize() { return { x: MAP_SIZE.x, y: MAP_SIZE.y }; },
      invalidateSize() { rec("invalidateSize"); return this; },
      addControl(c) { this._controls.push(c); return this; },
    };
    return map;
  },
  tileLayer(url, opts) {
    assert(typeof url === "string" && url.includes("{z}"), "tileLayer needs a {z}/{x}/{y} template");
    assert(opts && opts.attribution, "tileLayer MUST carry attribution (OSM ODbL requires the credit)");
    rec("tileLayer", [url, opts]);
    return new Layer("tile");
  },
  polygon(latlngs, opts) {
    assertPolygonLatLngs(latlngs, "L.polygon");
    Object.keys(opts || {}).forEach((k) => assert(
      ["color", "weight", "opacity", "fillColor", "fillOpacity", "fill", "interactive", "dashArray", "className"].includes(k),
      `L.polygon: '${k}' is not a Leaflet Path option`));
    const p = new Layer("polygon");
    p._latlngs = latlngs; p._style = Object.assign({}, opts);
    return p;
  },
  popup(opts) { return new Popup(opts); },
  tooltip(opts) { return new Tooltip(opts); },
  control(opts) { return makeControl(opts); },
  DomUtil: { create: (tag, cls) => ({ tagName: tag, className: cls, innerHTML: "", title: "" }) },
};
L.control.zoom = (opts) => {
  assert(opts && ["topleft", "topright", "bottomleft", "bottomright"].includes(opts.position), "L.control.zoom needs a valid position");
  const c = makeControl(opts); c._isZoom = true; return c;
};

// ---- minimal DOM / fetch so map.js can run headless ----
const elements = { "dss-map": { id: "dss-map" }, businessTypeSelect: null };
global.window = {
  L,
  location: { search: "" },
  DSS_TARLAC_CITY_BOUNDARY_URL: "/api/tarlac-city-boundary",
  DSS_BARANGAY_CHOROPLETH_URL: "/api/barangay-choropleth",
  DSS_LOCATIONS_FORECAST_URL: "/api/locations-forecast",
  DSS_BARANGAY_COORDS_URL: "/api/barangay-coords",
  DSS_BARANGAY_DETAIL_URL: "/api/barangay-detail",
  DSS_SATURATION_MAP_URL: "/sme/saturation-map",
  DSS_DEMAND_SUMMARY: {
    tarlac_avg_annual_family_expenditure_php: 299670, expenditure_year: 2023,
    geographic_note: "x",
    categories: [{ key: "food", label: "Food", national_share_percent: 40.9, share_source_year: 2023, estimated_annual_php: 122565, is_recreation_estimate: false }],
  },
};
global.L = L;
// Leaflet's CSS is "loaded" in this stub: .leaflet-pane resolves to
// position:absolute, which is exactly what isLeafletCssLoaded() probes.
global.window.getComputedStyle = (el) => ({
  position: el && el.className === "leaflet-pane" ? "absolute" : "static",
});
global.window.addEventListener = () => {};
global.setTimeout = setTimeout;
global.document = {
  body: { appendChild() {}, removeChild() {} },
  addEventListener: () => {},
  getElementById: (id) => elements[id] || null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (tag) => ({ tagName: tag, className: "", style: {}, innerHTML: "", classList: { add() {}, toggle() {} } }),
};
global.URLSearchParams = URLSearchParams;

// Real geometry fixtures: one simple Polygon, one 2-piece MultiPolygon.
const ringA = [[120.55, 15.45], [120.60, 15.45], [120.60, 15.50], [120.55, 15.50], [120.55, 15.45]];
const ringB = [[120.62, 15.46], [120.64, 15.46], [120.64, 15.48], [120.62, 15.48], [120.62, 15.46]];
const forecastRows = [
  { location: "Poblacion", industry_type: "Food and Beverage", cluster_label: "Low", saturation_index: 20, viability_score: 8, competitor_count: 3, population: 8000, population_density: 1200 },
  { location: "Burot", industry_type: "Food and Beverage", cluster_label: "Saturated", saturation_index: 90, viability_score: 1, competitor_count: 40, population: 5000, population_density: 800 },
];
const choropleth = {
  type: "FeatureCollection",
  features: [
    { type: "Feature", properties: { name: "Poblacion" }, geometry: { type: "Polygon", coordinates: [ringA] } },
    { type: "Feature", properties: { name: "Burot" }, geometry: { type: "MultiPolygon", coordinates: [[ringA], [ringB]] } },
  ],
};
const coords = { Poblacion: { lat: 15.48, lng: 120.59 }, Burot: { lat: 15.47, lng: 120.63 } };
function detailFor(location) {
  return {
    location, industry_type: "Food and Beverage", cluster_label: "Low", saturation_index: 20,
    viability_score: 8, competitor_count: 3, confidence_level: 80, total_businesses: 105,
    top_industries: [
      { industry: "Food and Beverage", label: "Food & Beverage", count: 60, is_estimated: false },
      { industry: "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles", label: "Wholesale & Retail Trade", count: 40, is_estimated: true },
      { industry: "Other Service Activities", label: "Other Services", count: 5, is_estimated: false },
    ],
    population: 8000, population_density: 1200, recommended_actions: ["Go"],
  };
}
// A real fetch() Response: map.js checks response.ok before parsing
// (see loadChoropleth), so the stub must carry it too.
global.fetch = async (url) => ({
  ok: true, status: 200, statusText: "OK",
  json: async () => {
    if (url.includes("tarlac-city-boundary")) {
      return { type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "Polygon", coordinates: [ringA] } }] };
    }
    if (url.includes("barangay-choropleth")) return choropleth;
    if (url.includes("barangay-coords")) return { barangays: coords, pending: 0 };
    if (url.includes("locations-forecast")) return forecastRows;
    if (url.includes("barangay-detail")) {
      return detailFor(new URL(url, "http://stub").searchParams.get("location"));
    }
    return [];
  },
});

// ---- load map.js + assertions in ONE scope ----
const mapSrc = fs.readFileSync(__dirname + "" + "/../app/static/js/map.js", "utf8");
const testBody = `(async () => {
  const flush = async () => { for (let i = 0; i < 8; i++) await new Promise((r) => setTimeout(r, 0)); };
  const lastSetView = () => calls.filter((c) => c.name === "setView").slice(-1)[0];

  window.dssInitMap();
  assert(dssMap && dssMap._isMap, "dssInitMap did not create a Leaflet map");
  assert(calls.find((c) => c.name === "tileLayer"), "no OSM tile layer added");
  const googleControl = dssMap._controls.find((c) => c._el && /Powered by Google/.test(c._el.innerHTML));
  assert(googleControl, "Google attribution control not added / missing 'Powered by Google'");
  assert(googleControl.options.position === "topleft", "Google attribution must sit at the TOP of the map");
  // Zoom buttons moved to the other top corner so they don't stack with
  // the Google mark into a block popups slide under.
  assert(!dssMap._zoomControl, "Leaflet's default (top-left) zoom control should be switched off");
  const zoom = dssMap._controls.find((c) => c._isZoom);
  assert(zoom && zoom.options.position === "topright", "zoom buttons should be top-right");

  await loadAndDrawCityBoundary();
  assert(dssCityBoundaryPolygon, "city boundary polygon not drawn");
  assert(calls.find((c) => c.name === "bringToBack"), "city boundary should be sent to back");
  assert(dssCityBoundaryPolygon._style.interactive === false, "city boundary must not steal clicks");

  // Let the background loadLocations() kicked off by dssInitMap() settle
  // first, then draw explicitly -- same rows either way.
  await flush();
  assert(!document.getElementById("dssChoroplethProblem"), "choropleth load should have succeeded");
  // No detail panel and no DSS_FOCUS_LOCATION (a bare mini map): the page
  // must open on the whole city, not zoom into the first barangay.
  assert(!calls.find((c) => c.name === "setView"), "a panel-less map must not auto-zoom to the first barangay");
  assert(!dssMap._openPopup, "no info popup should open by itself without DSS_FOCUS_LOCATION");

  const rows = forecastRows;
  dssLocationsCache = rows;
  await drawChoropleth(rows);
  assert.strictEqual(dssPolygons.length, 2, "expected one polygon per barangay");

  // The MultiPolygon barangay must be TWO separate polygons, not one with a hole.
  const burot = dssPolygons.find((p) => p.location === "Burot").polygon;
  assert.strictEqual(burot._latlngs.length, 2, "Burot's 2 pieces must be 2 separate polygons");
  assert.strictEqual(burot._latlngs[0].length, 1, "each piece should have exactly its own outer ring (no accidental hole)");

  // ---- Hover card: a tooltip, direction auto, never cut off ----
  const hoverCell = dssPolygons[0].polygon;
  assert.strictEqual(dssHoverTooltip.options.direction, "auto", "hover tooltip should use direction 'auto'");
  const cardBox = (cursorY) => {
    const h = dssHoverTooltip._el.offsetHeight;
    const dy = dssHoverTooltip.options.offset[1];
    return { top: cursorY - h / 2 + dy, bottom: cursorY + h / 2 + dy };
  };
  // Cursor right at the TOP edge: this is the case the client hit.
  hoverCell._on.mousemove({ latlng: { lat: 15.47, lng: 120.58 }, containerPoint: { x: 100, y: 5 } });
  assert(dssMap.hasLayer(dssHoverTooltip), "hover did not open the tooltip");
  assert(/Poblacion/.test(dssHoverTooltip._content), "tooltip missing barangay name");
  assert(!dssMap._openPopup, "hover must not open a popup (it would close the pinned info popup)");
  let box = cardBox(5);
  assert(box.top >= 48, "hover card cut off at the top: top=" + box.top);
  assert(box.bottom <= MAP_SIZE.y - 24, "hover card cut off at the bottom: bottom=" + box.bottom);
  assert(dssHoverTooltip._updates > 0, "the tooltip was shifted but never re-positioned");
  // ...and at the BOTTOM edge.
  hoverCell._on.mousemove({ latlng: { lat: 15.46, lng: 120.58 }, containerPoint: { x: 100, y: MAP_SIZE.y - 3 } });
  box = cardBox(MAP_SIZE.y - 3);
  assert(box.top >= 48 && box.bottom <= MAP_SIZE.y - 24, "hover card cut off near the bottom: " + JSON.stringify(box));
  // Mid-map: no shift at all.
  hoverCell._on.mousemove({ latlng: { lat: 15.47, lng: 120.58 }, containerPoint: { x: 100, y: 210 } });
  assert.strictEqual(dssHoverTooltip.options.offset[1], 0, "no vertical shift needed mid-map");
  // Never wider than half the map, so 'auto' (left/right of the cursor) always fits sideways.
  const maxW = Number(/max-width:(\\d+)px/.exec(dssHoverTooltip._content)[1]);
  assert(maxW + 14 + 12 <= MAP_SIZE.x / 2, "hover card could be wider than half the map: " + maxW);
  // The FIES demand block was deliberately taken OFF the hover card: it
  // made the card tall enough to cover much of the map. It stays on the
  // detail panel, where it is a tidy labelled row.
  assert(!/Est. Household Demand/.test(dssHoverTooltip._content), "hover card should not carry the FIES demand block");
  hoverCell._on.mouseout();
  assert(!dssMap.hasLayer(dssHoverTooltip), "mouseout did not close the tooltip");

  // ---- Click isolates; double-click restores (and is not undone by the slow detail request) ----
  dssPolygons[0].polygon._on.click();
  assert.strictEqual(dssIsolatedLocation, "Poblacion", "one click should isolate");
  assert(dssMap.hasLayer(dssPolygons[0].polygon), "isolated cell must stay on the map");
  assert(!dssMap.hasLayer(dssPolygons[1].polygon), "other cells must be hidden while isolated");
  assert.strictEqual(dssPolygons[0].polygon._style.weight, 3, "isolated cell should get the bold outline");

  dssPolygons[0].polygon._on.dblclick();
  assert.strictEqual(dssIsolatedLocation, null, "double-click should restore");
  assert(dssMap.hasLayer(dssPolygons[1].polygon), "all cells should be back");
  assert(calls.find((c) => c.name === "setView"), "restore should re-center the map");
  await flush();
  const afterRestore = lastSetView();
  assert.strictEqual(afterRestore.args[1], 13, "the in-flight detail request zoomed back in after 'show all'");
  assert(!dssMap._openPopup, "the in-flight detail request opened a popup after 'show all'");

  // ---- Panel-less page: a click opens a pinned info popup that fits ----
  dssPolygons[1].polygon._on.click();
  await flush();
  const popup = dssMap._openPopup;
  assert(popup, "click on a panel-less map should open the info popup");
  assert(/Burot/.test(popup._content), "info popup is for the wrong barangay");
  assert.deepStrictEqual(popup._ll, [coords.Burot.lat, coords.Burot.lng], "info popup should point at the barangay");
  assert.strictEqual(popup.options.autoPan, true, "info popup must autoPan into view");
  assert.strictEqual(popup.options.keepInView, true, "info popup must keepInView");
  const [padL, padT] = popup.options.autoPanPaddingTopLeft;
  const [padR, padB] = popup.options.autoPanPaddingBottomRight;
  assert(padT >= 40, "top padding must clear the 'Powered by Google' mark");
  assert(padR >= 40, "right padding must clear the zoom buttons");
  assert(popup.options.maxHeight + 52 + padT + padB <= MAP_SIZE.y, "info popup can be taller than the map");
  assert(popup.options.maxWidth + padL + padR <= MAP_SIZE.x, "info popup can be wider than the map");
  assert(/1\\. Food &amp; Beverage/.test(popup._content), "info popup should rank the top industries");
  assert(/3\\. Other Services/.test(popup._content), "info popup should list all three top industries");
  assert.strictEqual((popup._content.match(/\\(est\\.\\)/g) || []).length, 1, "only the estimated count gets '(est.)'");
  assert(/Open in full map/.test(popup._content) && /location=Burot/.test(popup._content), "popup should deep-link to the full map");
  const clickView = lastSetView();
  assert.deepStrictEqual(clickView.args[2], { animate: false }, "popup path must not animate (autoPan measures mid-zoom otherwise)");

  // Restoring closes it (it is keepInView and would drag the map back).
  dssPolygons[1].polygon._on.dblclick();
  assert(!dssMap._openPopup, "show-all must close the info popup first");

  // ---- Detail panel: top 3 industries, not fixed Food/Service/Retail ----
  elements.detailPanel = { innerHTML: "" };
  renderDetailPanel(detailFor("Poblacion"));
  const panelHtml = elements.detailPanel.innerHTML;
  assert(/1\\. Food &amp; Beverage/.test(panelHtml) && /2\\. Wholesale &amp; Retail Trade/.test(panelHtml), "panel should rank the top industries");
  assert(!/Food Industry|Service Industry|Retail Industry|undefined/.test(panelHtml), "panel still shows the old fixed rows");
  assert.strictEqual((panelHtml.match(/\\(est\\.\\)/g) || []).length, 1, "panel should mark only estimated counts");
  renderDetailPanel(Object.assign(detailFor("Poblacion"), { top_industries: [] }));
  assert(/No business counts on file yet/.test(elements.detailPanel.innerHTML), "empty state missing");
  delete elements.detailPanel;

  // Tier filter still works.
  dssVisibleTiers.Saturated = false;
  applyTierVisibility();
  assert(!dssMap.hasLayer(dssPolygons[1].polygon), "filtered-off tier should be hidden");
  dssVisibleTiers.Saturated = true;
  applyTierVisibility();

  // ---- DSS_FOCUS_LOCATION: outline + centre + popup, others still visible ----
  dssSelectedLocation = null; dssIsolatedLocation = null; closeInfoPopup();
  window.DSS_FOCUS_LOCATION = "  poblacion ";   // as typed in a plan: any case, stray spaces
  await loadLocations();
  await flush();
  const focusCell = dssPolygons.find((p) => p.location === "Poblacion").polygon;
  const otherCell = dssPolygons.find((p) => p.location === "Burot").polygon;
  assert.strictEqual(dssIsolatedLocation, null, "focus must not isolate");
  assert(dssMap.hasLayer(otherCell), "focus must keep the other barangays visible");
  assert.strictEqual(focusCell._style.weight, 3, "focused barangay should be outlined");
  assert.strictEqual(otherCell._style.weight, 1, "only the focused barangay is outlined");
  assert(calls.find((c) => c.name === "bringToFront" && c.args[0] === focusCell), "focused cell should be drawn on top");
  const focusView = lastSetView();
  assert.deepStrictEqual(focusView.args[0], [coords.Poblacion.lat, coords.Poblacion.lng], "map not centred on the focus barangay");
  assert(dssMap._openPopup && /Poblacion/.test(dssMap._openPopup._content), "focus should open the barangay's info popup");
  assert(/Top industries here/.test(dssMap._openPopup._content), "focus popup should carry the top industries");
  delete window.DSS_FOCUS_LOCATION;

  // The map is re-measured once layout settles -- a map measured while
  // its Bootstrap column is still sizing renders tiles for the wrong
  // width (grey gaps / misplaced tiles).
  await new Promise((r) => setTimeout(r, 5));
  assert(calls.find((c) => c.name === "invalidateSize"), "map was never re-measured after layout");

  console.log("ALL LEAFLET CONTRACT CHECKS PASSED (" + dssPolygons.length + " cells, hover tooltip fit, info popup fit, focus, isolate/restore, top-3 panel, tier filter, attribution, re-measure)");
})().catch((e) => { console.error("FAILED:", e.message); process.exit(1); });`;
eval(mapSrc + "\n" + testBody);
