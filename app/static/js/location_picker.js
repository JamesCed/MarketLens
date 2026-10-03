// app/static/js/location_picker.js
// ---------------------------------------------------------------------
// Choose a barangay by pointing at it on a map, instead of scrolling a
// 76-name dropdown (revision MINOR 2). Two places use it on the Planning page:
//
//   * the search bar's "Pick on map" dialog (#locationPickerModal): click
//     a barangay, press "Use this barangay", and the search runs for it;
//   * the Add New Plan dialog's "Pick on map" button, which opens a small
//     map under the location box and fills the box on click.
//
// The cells are the same barangay regions the Saturation Map shades
// (GET window.DSS_BARANGAY_CHOROPLETH_URL), drawn neutral here -- this is
// a "where", not a score -- with the city's real outline for orientation.
// The GeoJSON is fetched once and shared by every picker on the page.
//
// Needs Leaflet (the Planning page already loads it for the mini map).
(function () {
  "use strict";

  const STYLE = { color: "#2563eb", weight: 1, opacity: 0.55, fillColor: "#2563eb", fillOpacity: 0.06 };
  const HOVER = { weight: 2, fillOpacity: 0.18 };
  const PICKED = { color: "#1e40af", weight: 3, opacity: 1, fillOpacity: 0.32 };

  let cellsPromise = null;
  let outlinePromise = null;

  function fetchJson(url) {
    if (!url) return Promise.resolve(null);
    return fetch(url, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);
  }

  function cells() {
    if (!cellsPromise) cellsPromise = fetchJson(window.DSS_BARANGAY_CHOROPLETH_URL);
    return cellsPromise;
  }

  function outline() {
    if (!outlinePromise) outlinePromise = fetchJson(window.DSS_TARLAC_CITY_BOUNDARY_URL);
    return outlinePromise;
  }

  // One picker per map container; built the first time it is shown,
  // because Leaflet measures its container at creation and a hidden
  // container measures as 0 x 0.
  function Picker(mapEl, statusEl, onPick) {
    this.mapEl = mapEl;
    this.statusEl = statusEl;
    this.onPick = onPick;
    this.map = null;
    this.layers = {};
    this.picked = null;
  }

  Picker.prototype.show = function (preselect) {
    if (typeof L === "undefined") {
      if (this.statusEl) this.statusEl.textContent = "The map could not load. Type the barangay name instead.";
      return;
    }
    if (this.map) {
      this.map.invalidateSize();
      if (preselect) this.select(preselect, false);
      return;
    }
    this.map = L.map(this.mapEl, { center: [15.4869, 120.59], zoom: 12, scrollWheelZoom: true });
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(this.map);
    setTimeout(() => this.map && this.map.invalidateSize(), 0);

    outline().then((geo) => {
      if (geo && this.map) {
        L.geoJSON(geo, { style: { color: "#0f172a", weight: 2.5, fill: false }, interactive: false }).addTo(this.map);
      }
    });

    if (this.statusEl) this.statusEl.textContent = "Loading the barangay map…";
    cells().then((geo) => {
      if (!this.map) return;
      if (!geo || !geo.features || !geo.features.length) {
        if (this.statusEl) this.statusEl.textContent = "The barangay map is not available right now. Type the name instead.";
        return;
      }
      const group = L.geoJSON(geo, {
        style: () => ({ ...STYLE }),
        onEachFeature: (feature, layer) => {
          const name = feature.properties && feature.properties.name;
          if (!name) return;
          this.layers[name.toLowerCase()] = layer;
          // Tooltip text is set with a DOM node, not markup: names come
          // from the coordinate set, and text cannot be misread as HTML.
          const label = document.createElement("span");
          label.textContent = name;
          layer.bindTooltip(label, { sticky: true, direction: "top", className: "dss-map-tooltip" });
          layer.on("mouseover", () => { if (this.picked !== layer) layer.setStyle(HOVER); });
          layer.on("mouseout", () => { if (this.picked !== layer) layer.setStyle(STYLE); });
          layer.on("click", () => this.select(name, true));
        },
      }).addTo(this.map);
      this.map.fitBounds(group.getBounds(), { padding: [8, 8] });
      if (this.statusEl) this.statusEl.textContent = "Click a barangay on the map to choose it.";
      if (preselect) this.select(preselect, false);
    });
  };

  Picker.prototype.select = function (name, fromClick) {
    const layer = this.layers[String(name || "").toLowerCase()];
    if (!layer) return;
    if (this.picked && this.picked !== layer) this.picked.setStyle(STYLE);
    this.picked = layer;
    layer.setStyle(PICKED);
    layer.bringToFront();
    const realName = layer.feature.properties.name;
    if (this.statusEl) {
      this.statusEl.textContent = "";
      const strong = document.createElement("strong");
      strong.textContent = realName;
      this.statusEl.append("Selected: ", strong);
    }
    if (fromClick && this.onPick) this.onPick(realName);
  };

  function knownName(value) {
    const list = window.DSS_SME_LOCATIONS || [];
    const lower = String(value || "").trim().toLowerCase();
    return list.find((n) => n.toLowerCase() === lower) || null;
  }

  function init() {
    // ---- search bar dialog ------------------------------------------
    const modalEl = document.getElementById("locationPickerModal");
    if (modalEl) {
      const useBtn = document.getElementById("locationPickerUse");
      let choice = null;
      const picker = new Picker(
        modalEl.querySelector("[data-location-picker-map]"),
        modalEl.querySelector("[data-location-picker-status]"),
        (name) => { choice = name; if (useBtn) useBtn.disabled = false; },
      );
      modalEl.addEventListener("shown.bs.modal", () => {
        const typed = knownName((document.getElementById("smeSearchInput") || {}).value);
        choice = typed;
        if (useBtn) useBtn.disabled = !typed;
        picker.show(typed);
      });
      if (useBtn) {
        useBtn.addEventListener("click", () => {
          if (!choice) return;
          const modal = window.bootstrap && window.bootstrap.Modal.getInstance(modalEl);
          if (modal) modal.hide();
          document.dispatchEvent(new CustomEvent("dss:location-picked", { detail: { target: "search", name: choice } }));
        });
      }
    }

    // ---- inline pickers (the Add New Plan dialog) --------------------
    document.querySelectorAll("[data-location-picker-toggle]").forEach((btn) => {
      const panel = document.getElementById(btn.getAttribute("aria-controls"));
      const group = btn.closest(".input-group");
      const input = group && group.querySelector("input");
      if (!panel || !input) return;
      const picker = new Picker(
        panel.querySelector("[data-location-picker-map]"),
        panel.querySelector("[data-location-picker-status]"),
        (name) => {
          input.value = name;
          input.dispatchEvent(new Event("change", { bubbles: true }));
        },
      );
      btn.addEventListener("click", () => {
        const open = panel.hidden;
        panel.hidden = !open;
        btn.setAttribute("aria-expanded", String(open));
        if (open) picker.show(knownName(input.value));
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
