// Verifies map.js against a STRICT stub of the Leaflet 1.9 API surface it
// uses. The stub throws on any method name or argument shape that real
// Leaflet would reject, so this catches Google->Leaflet porting mistakes
// (wrong method, wrong latlng shape, wrong polygon nesting depth) without
// needing the CDN, which this sandbox blocks.
const fs = require("fs");
const assert = require("assert");

const calls = [];
function rec(name, args) { calls.push({ name, args }); }

function assertLatLng(v, where) {
  const ok = Array.isArray(v) ? v.length === 2 && v.every((n) => typeof n === "number")
                              : v && typeof v.lat === "number" && typeof v.lng === "number";
  assert(ok, `${where}: not a Leaflet LatLng (got ${JSON.stringify(v)})`);
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
  bringToFront() { return this; }
}

class Popup {
  constructor(opts) {
    Object.keys(opts || {}).forEach((k) => assert(
      ["closeButton", "autoPan", "className", "offset", "maxWidth", "minWidth"].includes(k),
      `L.popup: unknown option '${k}'`));
    this._content = null;
  }
  setContent(html) { assert(typeof html === "string", "popup content must be a string"); this._content = html; return this; }
  setLatLng(ll) { assertLatLng(ll, "popup.setLatLng"); this._ll = ll; return this; }
  openOn(map) { assert(map && map._isMap, "openOn() needs the map"); map._openPopup = this; return this; }
}

const L = {
  map(el, opts) {
    assert(el, "L.map needs an element");
    Object.keys(opts || {}).forEach((k) => assert(
      ["center", "zoom", "scrollWheelZoom", "zoomControl", "attributionControl", "maxZoom", "minZoom"].includes(k),
      `L.map: unknown option '${k}' (Google options like mapTypeId/gestureHandling are not Leaflet)`));
    if (opts && opts.center) assertLatLng(opts.center, "L.map center");
    const map = {
      _isMap: true, _layers: new Set(), _controls: [], _openPopup: null,
      setView(ll, z) { assertLatLng(ll, "map.setView"); assert(typeof z === "number", "setView zoom"); rec("setView", [ll, z]); return this; },
      removeLayer(l) { this._layers.delete(l); return this; },
      hasLayer(l) { return this._layers.has(l); },
      closePopup() { this._openPopup = null; return this; },
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
  control(opts) {
    const c = { options: opts, addTo(map) { assert(map._isMap); map._controls.push(this); this._el = this.onAdd(map); return this; } };
    return c;
  },
  DomUtil: { create: (tag, cls) => ({ tagName: tag, className: cls, innerHTML: "", title: "" }) },
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
global.fetch = async (url) => ({
  json: async () => {
    if (url.includes("tarlac-city-boundary")) {
      return { type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "Polygon", coordinates: [ringA] } }] };
    }
    if (url.includes("barangay-choropleth")) return choropleth;
    if (url.includes("barangay-coords")) return { barangays: { Poblacion: { lat: 15.48, lng: 120.59 } }, pending: 0 };
    if (url.includes("locations-forecast")) return forecastRows;
    return [];
  },
});

// ---- load map.js + assertions in ONE scope ----
const mapSrc = fs.readFileSync(__dirname + "" + "/../app/static/js/map.js", "utf8");
const testBody = `(async () => {
  window.dssInitMap();
  assert(dssMap && dssMap._isMap, "dssInitMap did not create a Leaflet map");
  assert(calls.find((c) => c.name === "tileLayer"), "no OSM tile layer added");
  assert(dssMap._controls.length >= 1, "Google attribution control not added");
  const attrEl = dssMap._controls[0]._el;
  assert(/Powered by Google/.test(attrEl.innerHTML), "attribution control missing 'Powered by Google'");
  assert(dssMap._controls[0].options.position === "topleft", "Google attribution must sit at the TOP of the map");

  await loadAndDrawCityBoundary();
  assert(dssCityBoundaryPolygon, "city boundary polygon not drawn");
  assert(calls.find((c) => c.name === "bringToBack"), "city boundary should be sent to back");
  assert(dssCityBoundaryPolygon._style.interactive === false, "city boundary must not steal clicks");

  // Let the background loadLocations() kicked off by dssInitMap() settle
  // first, then draw explicitly -- same rows either way.
  await new Promise((r) => setTimeout(r, 0));
  const rows = forecastRows;
  dssLocationsCache = rows;
  await drawChoropleth(rows);
  assert.strictEqual(dssPolygons.length, 2, "expected one polygon per barangay");

  // The MultiPolygon barangay must be TWO separate polygons, not one with a hole.
  const burot = dssPolygons.find((p) => p.location === "Burot").polygon;
  assert.strictEqual(burot._latlngs.length, 2, "Burot's 2 pieces must be 2 separate polygons");
  assert.strictEqual(burot._latlngs[0].length, 1, "each piece should have exactly its own outer ring (no accidental hole)");

  // Hover popup
  dssPolygons[0].polygon._on.mousemove({ latlng: { lat: 15.47, lng: 120.58 } });
  assert(dssMap._openPopup, "hover did not open a popup");
  assert(/Poblacion/.test(dssMap._openPopup._content), "popup missing barangay name");
  // The FIES demand block was deliberately taken OFF the hover tooltip:
  // it made the popup tall enough to cover much of the map. It stays on
  // the detail panel, where it is a tidy labelled row.
  assert(!/Est. Household Demand/.test(dssMap._openPopup._content),
    "hover popup should no longer carry the FIES demand block");
  dssPolygons[0].polygon._on.mouseout();
  assert(!dssMap._openPopup, "mouseout did not close the popup");

  // Single click isolates; both cells still exist, only one is on the map.
  dssPolygons[0].polygon._on.click();
  assert.strictEqual(dssIsolatedLocation, "Poblacion", "one click should isolate");
  assert(dssMap.hasLayer(dssPolygons[0].polygon), "isolated cell must stay on the map");
  assert(!dssMap.hasLayer(dssPolygons[1].polygon), "other cells must be hidden while isolated");
  assert.strictEqual(dssPolygons[0].polygon._style.weight, 3, "isolated cell should get the bold outline");

  // Double-click the same one restores everything.
  dssPolygons[0].polygon._on.dblclick();
  assert.strictEqual(dssIsolatedLocation, null, "double-click should restore");
  assert(dssMap.hasLayer(dssPolygons[1].polygon), "all cells should be back");
  assert(calls.find((c) => c.name === "setView"), "restore should re-center the map");

  // Tier filter still works.
  dssVisibleTiers.Saturated = false;
  applyTierVisibility();
  assert(!dssMap.hasLayer(dssPolygons[1].polygon), "filtered-off tier should be hidden");

  // The map is re-measured once layout settles -- a map measured while
  // its Bootstrap column is still sizing renders tiles for the wrong
  // width (grey gaps / misplaced tiles).
  await new Promise((r) => setTimeout(r, 5));
  assert(calls.find((c) => c.name === "invalidateSize"), "map was never re-measured after layout");

  console.log("ALL LEAFLET CONTRACT CHECKS PASSED (" + dssPolygons.length + " cells, popup, isolate/restore, tier filter, attribution, re-measure)");
})().catch((e) => { console.error("FAILED:", e.message); process.exit(1); });`;
eval(mapSrc + "\n" + testBody);
