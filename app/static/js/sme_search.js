// app/static/js/sme_search.js
// -----------------------------------------------------------------
// Powers the search bar on the SME Home page (app/templates/sme/home.html).
// There is no barangays table in the real schema, so "search" here
// means: take whatever the user typed as a location (optionally with
// an industry, e.g. "Food & Beverage in San Vicente"), call the same
// AI forecasting endpoint the rest of the app uses
// (GET /api/forecast?industry_type=&location=), and show the result
// inline -- with a link through to the full Saturation Map for that
// exact industry/location.
//
// Globals expected on window (set inline in sme/home.html):
//   DSS_SME_LOCATIONS      -- array of the 76 reference barangay names
//   DSS_SME_BUSINESS_TYPES -- array of suggested industry types
//   DSS_FORECAST_URL       -- GET /api/forecast
//   DSS_SATURATION_MAP_URL -- link target for "View on Saturation Map"
//
// Also wires the Clear button (#smeSearchClear) and takes a location
// chosen in the "Pick on map" dialog (a dss:location-picked event from
// location_picker.js).

(function () {
  const form = document.getElementById("smeSearchForm");
  const input = document.getElementById("smeSearchInput");
  const industrySelect = document.getElementById("smeSearchIndustry");
  const suggestionsBox = document.getElementById("smeSearchSuggestions");
  const resultsBox = document.getElementById("smeSearchResults");
  if (!form || !input) return;

  const clearButton = document.getElementById("smeSearchClear");

  // Anything that goes into innerHTML below is escaped first: the
  // location is whatever was typed, echoed back by the API.
  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  const KNOWN_LOCATIONS = window.DSS_SME_LOCATIONS || [];
  const KNOWN_LOCATIONS_LOWER = KNOWN_LOCATIONS.map((l) => l.toLowerCase());
  const BUSINESS_TYPES = window.DSS_SME_BUSINESS_TYPES || [];

  const PILL_CLASS = {
    Low: "dss-pill-low",
    Moderate: "dss-pill-moderate",
    High: "dss-pill-high",
    Saturated: "dss-pill-saturated",
  };

  // Loose keyword -> industry_type mapping so a free-text query like
  // "cafe in Poblacion" resolves to a real suggested industry_type
  // instead of forcing the user to know the exact list of business
  // types up front. Anything unmatched just falls back to whatever
  // is selected in the dropdown.
  // Keys are the PSIC sections in app/ml/constants.py BUSINESS_TYPES.
  // More specific categories are listed first so, e.g., "clinic"
  // resolves to Human Health and Social Work rather than the catch-all
  // Other Service Activities bucket.
  const INDUSTRY_KEYWORDS = {
    "Human Health and Social Work Activities": ["clinic", "pharmacy", "drugstore", "hospital", "dental", "laboratory"],
    "Education": ["tutorial", "school", "training center", "review center", "driving school", "college"],
    "Financial and Insurance Activities": ["lending", "pawnshop", "insurance", "bank", "remittance"],
    "Accommodation and Food Service Activities": ["hotel", "inn", "resort", "restaurant", "catering", "lodging"],
    "Arts, Entertainment, and Recreation": ["cinema", "videoke", "arcade", "billiard", "gym", "fitness", "event venue"],
    "Professional, Scientific, and Technical Activities": ["law office", "accounting firm", "consulting", "engineering", "architect", "veterinary"],
    "Administrative and Support Service Activities": ["manpower", "security agency", "travel agency", "janitorial", "equipment rental"],
    "Information and Communication": ["computer shop", "it service", "software", "telecom", "publishing", "radio station"],
    "Transportation and Storage": ["trucking", "courier", "logistics", "terminal", "warehouse", "storage"],
    "Water Supply; Sewerage, Waste Management, and Remediation Activities": ["water district", "water supply", "waste", "septic"],
    "Electricity, Gas, Steam, and Air Conditioning Supply": ["electric cooperative", "power utility", "lpg distributor"],
    "Mining and Quarrying": ["quarry", "sand and gravel", "aggregates", "mining"],
    "Agriculture, Forestry, and Fishing": ["farm", "fishery", "poultry", "agri-processing", "livestock"],
    "Manufacturing": ["factory", "manufacturer", "production plant", "furniture", "garments", "printing press"],
    "Real Estate Activities": ["real estate", "property developer", "subdivision", "realty", "leasing"],
    "Construction": ["construction", "contractor", "builder", "plumbing contractor"],
    "Food and Beverage": ["food", "beverage", "cafe", "coffee", "bakery", "bakeshop", "diner"],
    "Wholesale and Retail Trade; Repair of Motor Vehicles and Motorcycles": [
      "retail", "store", "shop", "grocery", "supermarket", "boutique", "hardware",
      "wholesale", "distributor", "auto repair", "car shop", "motorcycle",
    ],
    "Activities of Households as Employers": ["household staffing", "domestic helper"],
    "Other Service Activities": ["service", "laundry", "salon", "barbershop", "spa", "repair shop", "funeral"],
  };

  function guessIndustryFromText(text) {
    const lower = text.toLowerCase();
    for (const industry of Object.keys(INDUSTRY_KEYWORDS)) {
      if (INDUSTRY_KEYWORDS[industry].some((kw) => lower.includes(kw))) {
        return BUSINESS_TYPES.includes(industry) ? industry : null;
      }
    }
    return null;
  }

  function parseQuery(raw) {
    const text = (raw || "").trim();
    if (!text) return null;
    const match = text.match(/^(.*)\bin\b(.+)$/i);
    if (match && match[2].trim()) {
      return {
        location: match[2].trim(),
        industry: guessIndustryFromText(match[1]),
      };
    }
    return { location: text, industry: guessIndustryFromText(text) };
  }

  function hideSuggestions() {
    if (!suggestionsBox) return;
    suggestionsBox.hidden = true;
    suggestionsBox.innerHTML = "";
  }

  function renderSuggestions(text) {
    if (!suggestionsBox) return;
    const q = text.trim().toLowerCase();
    if (!q) return hideSuggestions();

    const matches = KNOWN_LOCATIONS.filter((loc) => loc.toLowerCase().includes(q)).slice(0, 8);
    if (!matches.length) return hideSuggestions();

    suggestionsBox.hidden = false;
    suggestionsBox.innerHTML = matches
      .map((loc) => `<button type="button" class="list-group-item list-group-item-action dss-search-suggestion">${escapeHtml(loc)}</button>`)
      .join("");
    suggestionsBox.querySelectorAll(".dss-search-suggestion").forEach((btn) => {
      btn.addEventListener("click", () => {
        input.value = btn.textContent;
        hideSuggestions();
        runSearch();
      });
    });
  }

  function renderLoading() {
    if (!resultsBox) return;
    resultsBox.hidden = false;
    resultsBox.innerHTML = '<div class="text-muted small mt-2"><span class="spinner-border spinner-border-sm"></span> Searching market data...</div>';
  }

  function renderError(message) {
    if (!resultsBox) return;
    resultsBox.hidden = false;
    resultsBox.innerHTML = `<div class="alert alert-warning small mt-2 mb-0">${escapeHtml(message)}</div>`;
  }

  function renderResult(scores, matchedKnown) {
    if (!resultsBox) return;
    const pill = PILL_CLASS[scores.cluster_label] || "dss-pill-moderate";
    const dataNote = matchedKnown
      ? "Based on seed / on-file market data for this barangay."
      : "No seed data on file for this exact location -- showing an AI estimate based on dataset averages.";
    const mapUrl = `${window.DSS_SATURATION_MAP_URL}?location=${encodeURIComponent(scores.location)}&industry_type=${encodeURIComponent(scores.industry_type)}`;

    const e = escapeHtml;
    resultsBox.hidden = false;
    resultsBox.innerHTML = `
      <div class="dss-card mt-2 text-start">
        <div class="d-flex justify-content-between align-items-start flex-wrap gap-2">
          <div>
            <h6 class="mb-0"><i class="bi bi-geo-alt-fill text-primary"></i> ${e(scores.location)}</h6>
            <div class="text-muted small">${e(scores.industry_type)}</div>
          </div>
          <span class="dss-stat-pill ${pill}">${e(scores.cluster_label)} Saturation</span>
        </div>
        <div class="row small text-center mt-3 g-2">
          <div class="col-3"><div class="fs-5 fw-bold">${e(scores.viability_score)}/10</div><div class="text-muted">Viability</div></div>
          <div class="col-3"><div class="fs-5 fw-bold">${e(scores.saturation_index)}%</div><div class="text-muted">Saturation</div></div>
          <div class="col-3"><div class="fs-5 fw-bold">${e(scores.competitor_count)}</div><div class="text-muted">Competitors</div></div>
          <div class="col-3"><div class="fs-5 fw-bold">${e(scores.confidence_level)}%</div><div class="text-muted">Confidence</div></div>
        </div>
        <p class="text-muted small mt-2 mb-2">${dataNote}</p>
        <a class="btn btn-sm btn-outline-primary" href="${e(mapUrl)}">
          <i class="bi bi-map"></i> View on Saturation Map
        </a>
      </div>`;
  }

  async function runSearch() {
    const parsed = parseQuery(input.value);
    if (!parsed || !parsed.location) {
      renderError("Type a location to search (e.g., a barangay name).");
      return;
    }

    const industry = parsed.industry || (industrySelect ? industrySelect.value : BUSINESS_TYPES[0]);
    if (industrySelect && industry) industrySelect.value = industry;
    hideSuggestions();
    renderLoading();

    try {
      const url = `${window.DSS_FORECAST_URL}?industry_type=${encodeURIComponent(industry)}&location=${encodeURIComponent(parsed.location)}`;
      const resp = await fetch(url);
      if (!resp.ok) throw new Error("request failed");
      const scores = await resp.json();
      if (scores.error) throw new Error(scores.error);
      const matchedKnown = KNOWN_LOCATIONS_LOWER.includes(parsed.location.toLowerCase());
      renderResult(scores, matchedKnown);
    } catch (err) {
      renderError("Could not fetch a forecast for that search right now. Please try again.");
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    runSearch();
  });

  // Clear: empty the box, put the industry back to the chosen plan's,
  // and drop the result card. Nothing saved is touched.
  if (clearButton) {
    clearButton.addEventListener("click", () => {
      input.value = "";
      if (industrySelect) industrySelect.value = industrySelect.dataset.default || industrySelect.options[0].value;
      hideSuggestions();
      if (resultsBox) {
        resultsBox.hidden = true;
        resultsBox.innerHTML = "";
      }
      input.focus();
    });
  }

  // The "Pick on map" dialog (location_picker.js) hands its choice here.
  document.addEventListener("dss:location-picked", (e) => {
    if (!e.detail || e.detail.target !== "search") return;
    input.value = e.detail.name;
    runSearch();
  });
  input.addEventListener("input", () => renderSuggestions(input.value));
  input.addEventListener("focus", () => renderSuggestions(input.value));
  document.addEventListener("click", (e) => {
    if (suggestionsBox && !suggestionsBox.hidden && e.target !== input && !suggestionsBox.contains(e.target)) {
      hideSuggestions();
    }
  });
})();
