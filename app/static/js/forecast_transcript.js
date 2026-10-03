// app/static/js/forecast_transcript.js
// ---------------------------------------------------------------------
// Upgrades a stored forecast's transcript, after the page has rendered.
//
// A forecast made while no Gemini key was configured -- or before the
// transcript existed -- carries only the rule-based MODEL SUMMARY. When
// Gemini can now be asked, shared/_plan_insights.html marks that
// forecast's transcript block with
//
//     data-forecast-transcript
//     data-transcript-url="/api/forecasts/<id>/transcript"
//     data-forecast-id="<id>"
//
// plus an empty live region for the "Gemini is transcribing the
// forecast..." status, and
// includes this script. The script posts for each marked forecast and,
// when Gemini's transcript comes back, swaps it in -- text and badge --
// without a reload. The page never waits on the LLM: the summary is
// readable from the first paint, and stays if anything goes wrong.
//
//   * ONE REQUEST PER FORECAST, ONE AT A TIME. The Recommendations page
//     can list several plans; firing every request at once would spend
//     the free tier's per-minute allowance in a burst, so they go in
//     sequence. Two blocks for the same forecast share one request.
//   * The transcript is inserted with textContent, paragraph by
//     paragraph -- it is model-written text and is never parsed as
//     HTML. Only the badge, which the server renders from its own
//     template (escaped by Jinja), is inserted as markup.
//   * A failure leaves the model summary in place with a quiet note.
//     The server stamps the failure on the forecast and the page stops
//     asking for 15 minutes; "unavailable" and "no payload" replies
//     just hide the status, since there is nothing to wait for.
//
// The macro includes this file once per marked block, so it guards
// itself: whichever copy runs first does the work for the whole page.
(function () {
  "use strict";

  if (window.__dssForecastTranscript) return;
  window.__dssForecastTranscript = true;

  const WORKING = "Gemini is transcribing the forecast…";
  const FAILED =
    "Gemini could not transcribe this forecast just now, so the model summary is shown. " +
    "It will be tried again later.";

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  // The status element is a live region the server renders EMPTY but
  // present, so screen readers already track it. The first message is
  // written on the next tick, after the spinner and text holder exist,
  // so it arrives as a change inside an exposed region and is announced.
  function setStatus(hook, message, busy) {
    const status = hook.querySelector("[data-transcript-status]");
    if (!status) return;
    let line = status.querySelector("[data-transcript-status-line]");
    let first = false;
    if (!line) {
      line = document.createElement("span");
      line.className = "d-inline-block mt-1";
      line.setAttribute("data-transcript-status-line", "");
      const spinner = document.createElement("span");
      spinner.className = "spinner-border spinner-border-sm me-1";
      spinner.setAttribute("aria-hidden", "true");
      const text = document.createElement("span");
      text.setAttribute("data-transcript-status-text", "");
      line.appendChild(spinner);
      line.appendChild(text);
      status.appendChild(line);
      first = true;
    }
    line.querySelector(".spinner-border").hidden = !busy;
    const text = line.querySelector("[data-transcript-status-text]");
    if (first) {
      // Only if nothing newer (a fast reply) has been written meanwhile.
      window.setTimeout(() => { if (!text.textContent) text.textContent = message; }, 50);
    } else {
      text.textContent = message;
    }
  }

  function hideStatus(hook) {
    const status = hook.querySelector("[data-transcript-status]");
    if (status) while (status.firstChild) status.removeChild(status.firstChild);
  }

  function showTranscript(hook, data) {
    const box = hook.querySelector("[data-transcript-text]");
    if (box) {
      const paragraphs = String(data.text || "")
        .split(/\n\s*\n/)
        .map((paragraph) => paragraph.trim())
        .filter(Boolean);
      while (box.firstChild) box.removeChild(box.firstChild);
      paragraphs.forEach((paragraph, index) => {
        const element = document.createElement("p");
        element.className = "small " + (index === paragraphs.length - 1 ? "mb-0" : "mb-2");
        element.textContent = paragraph;
        box.appendChild(element);
      });
    }
    const badge = hook.querySelector("[data-transcript-badge]");
    if (badge && data.badge_html) {
      const template = document.createElement("template");
      template.innerHTML = String(data.badge_html).trim();
      const fresh = template.content.firstElementChild;
      if (fresh) badge.replaceWith(fresh);
    }
    hook.removeAttribute("data-forecast-transcript");
  }

  function successMessage(data) {
    return String(data.generated_by || "").indexOf("llm:gemini") === 0
      ? "Gemini transcribed this forecast."
      : "An AI transcribed this forecast (Gemini was not available).";
  }

  async function requestTranscript(url) {
    try {
      const response = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "X-CSRFToken": csrfToken(),
          "X-Requested-With": "XMLHttpRequest",
        },
      });
      // A login redirect or a server error is not JSON; both count as a
      // failed attempt, never as a transcript.
      const data = await response.json().catch(() => null);
      return response.ok && data ? data : null;
    } catch (error) {
      return null;
    }
  }

  async function run() {
    if (typeof window.fetch !== "function") return;
    const groups = new Map();
    document.querySelectorAll("[data-forecast-transcript][data-transcript-url]").forEach((hook) => {
      const key = hook.getAttribute("data-forecast-id") || hook.getAttribute("data-transcript-url");
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(hook);
    });
    groups.forEach((hooks) => hooks.forEach((hook) => {
      const status = hook.querySelector("[data-transcript-status]");
      setStatus(hook, (status && status.getAttribute("data-working-text")) || WORKING, true);
    }));

    for (const hooks of groups.values()) {
      const data = await requestTranscript(hooks[0].getAttribute("data-transcript-url"));
      hooks.forEach((hook) => {
        if (data && data.ok) {
          showTranscript(hook, data);
          setStatus(hook, successMessage(data), false);
        } else if (data && (data.reason === "unavailable" || data.reason === "no_payload")) {
          hideStatus(hook);
        } else {
          setStatus(hook, FAILED, false);
        }
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
