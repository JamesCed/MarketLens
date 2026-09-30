// app/static/js/map_timeline.js
// ---------------------------------------------------------------------
// The Saturation Map's month timeline: History | Current | Future.
//
// Drag the slider, or pick a month and year, and the map re-scores every
// barangay for that month -- the server does the work (GET
// /api/locations-forecast?as_of=YYYY-MM, see
// app/services/saturation_timeline_service.py); this file only chooses
// the month, hands it to map.js through window.DSS_MAP_AS_OF, and says
// in plain words where that month's figures came from.
//
// The bounds come from the page (data-start / data-current / data-end on
// #mapTimeline), so the server decides how far back and ahead is allowed.
(function () {
  "use strict";

  const root = document.getElementById("mapTimeline");
  if (!root) return;

  const range = document.getElementById("timelineRange");
  const monthSelect = document.getElementById("timelineMonth");
  const yearSelect = document.getElementById("timelineYear");
  const nowButton = document.getElementById("timelineNow");
  const label = document.getElementById("timelineLabel");
  const badge = document.getElementById("timelineBadge");
  const note = document.getElementById("timelineNote");

  const parse = (value) => {
    const [y, m] = String(value || "").split("-").map(Number);
    return y && m ? { y, m } : null;
  };
  const start = parse(root.dataset.start);
  const current = parse(root.dataset.current);
  const end = parse(root.dataset.end);
  if (!start || !current || !end) return;

  const toIndex = (ym) => (ym.y - start.y) * 12 + (ym.m - start.m);
  const fromIndex = (i) => {
    const total = start.y * 12 + (start.m - 1) + i;
    return { y: Math.floor(total / 12), m: (total % 12) + 1 };
  };
  const key = (ym) => `${ym.y}-${String(ym.m).padStart(2, "0")}`;
  const monthName = (ym, style) =>
    new Date(ym.y, ym.m - 1, 1).toLocaleString(undefined, { month: style || "long", year: "numeric" });

  const maxIndex = toIndex(end);
  const currentIndex = toIndex(current);

  // ---- controls -------------------------------------------------------
  range.min = "0";
  range.max = String(maxIndex);
  range.value = String(currentIndex);
  // Where "now" sits on the track, for the zone shading (CSS reads it).
  root.style.setProperty("--dss-tl-now", `${(currentIndex / maxIndex) * 100}%`);

  for (let m = 1; m <= 12; m += 1) {
    monthSelect.add(new Option(new Date(2000, m - 1, 1).toLocaleString(undefined, { month: "short" }), String(m)));
  }
  for (let y = start.y; y <= end.y; y += 1) yearSelect.add(new Option(String(y), String(y)));

  function periodOf(index) {
    if (index < currentIndex) return "history";
    if (index > currentIndex) return "future";
    return "current";
  }

  const BADGE = { history: "History", current: "Current", future: "Predicted" };

  function show(index) {
    const ym = fromIndex(index);
    const period = periodOf(index);
    range.value = String(index);
    monthSelect.value = String(ym.m);
    yearSelect.value = String(ym.y);
    label.textContent = period === "current" ? `${monthName(ym)} (this month)` : monthName(ym);
    badge.textContent = BADGE[period];
    badge.dataset.period = period;
    root.dataset.period = period;
    nowButton.disabled = period === "current";
    window.DSS_MAP_AS_OF = period === "current" ? "" : key(ym);
  }

  // ---- reloading ------------------------------------------------------
  // One request at a time; a change made while one is running is applied
  // as soon as it finishes, so the map always ends on the last choice.
  let loading = false;
  let queued = false;
  async function reload() {
    if (typeof window.dssReloadMap !== "function") return;
    if (loading) {
      queued = true;
      return;
    }
    loading = true;
    note.textContent = "Scoring the barangays for this month…";
    try {
      await window.dssReloadMap();
    } catch (err) {
      note.textContent = "Could not load that month. Try again, or press Now.";
    } finally {
      loading = false;
      if (queued) {
        queued = false;
        reload();
      }
    }
  }

  let timer = null;
  range.addEventListener("input", () => {
    show(Number(range.value));
    clearTimeout(timer);
    timer = setTimeout(reload, 350);
  });
  range.addEventListener("change", () => {
    clearTimeout(timer);
    reload();
  });

  function pickFromSelects() {
    const wanted = { y: Number(yearSelect.value), m: Number(monthSelect.value) };
    const index = Math.max(0, Math.min(maxIndex, toIndex(wanted)));
    show(index);
    reload();
  }
  monthSelect.addEventListener("change", pickFromSelects);
  yearSelect.addEventListener("change", pickFromSelects);
  nowButton.addEventListener("click", () => {
    show(currentIndex);
    reload();
  });

  // ---- what the map is showing ----------------------------------------
  document.addEventListener("dss:locations-loaded", (event) => {
    const rows = (event.detail && event.detail.rows) || [];
    if (!rows.length) {
      note.textContent = "";
      return;
    }
    const tally = {};
    rows.forEach((r) => { tally[r.basis] = (tally[r.basis] || 0) + 1; });
    const period = rows[0].period || "current";
    const ym = parse(rows[0].as_of) || current;
    const when = monthName(ym);

    if (period === "current") {
      note.textContent = "Market saturation as the AI scores it today.";
    } else if (period === "history") {
      const parts = [];
      if (tally.recorded) parts.push(`${tally.recorded} from business counts on file for that month`);
      if (tally["back-projected"]) {
        parts.push(`${tally["back-projected"]} back-projected along the PSA/DTI national MSME series`);
      }
      note.textContent =
        `Market saturation in ${when}: ${parts.join("; ")}. ` +
        "Population, rent and foot traffic are held at today's values.";
    } else {
      const confidences = rows.map((r) => Number(r.confidence_level)).filter((n) => !Number.isNaN(n));
      const avg = confidences.length
        ? Math.round(confidences.reduce((a, b) => a + b, 0) / confidences.length)
        : null;
      note.textContent =
        `Predicted market saturation for ${when}: the AI model re-scores each barangay with the number ` +
        "of businesses expected by then (from its own recorded trend, or the national MSME series)." +
        (avg !== null ? ` Average confidence ${avg}% -- lower the further ahead you look.` : "");
    }
  });

  // A link can open the map on a month: /saturation-map?as_of=2024-03.
  // Set before map.js's first load (it runs on DOMContentLoaded), so the
  // first draw is already the right month.
  const linked = parse(new URLSearchParams(window.location.search).get("as_of"));
  show(linked ? Math.max(0, Math.min(maxIndex, toIndex(linked))) : currentIndex);
})();
