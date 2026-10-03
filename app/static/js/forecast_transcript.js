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
//   * Not only the transcript: while the RECOMMENDATION (headline,
//     reasons, risks, competitor insight, innovation read) is still the
//     template, the same request has Gemini write it, and the parts of
//     the page marked data-ai-region / data-ai-part are repainted with
//     the server's fresh rendering (repaintRegions below).
//   * A failure leaves the model summary in place with a quiet note.
//     The server stamps the failure and names the wait ("retry_after",
//     two minutes); the page asks again when it is over, up to
//     MAX_ROUNDS times while it stays open. "unavailable" and "no
//     payload" replies just hide the status: nothing to wait for.
//
// The macro includes this file once per marked block, so it guards
// itself: whichever copy runs first does the work for the whole page.
(function () {
  "use strict";

  if (window.__dssForecastTranscript) return;
  window.__dssForecastTranscript = true;

  const WORKING = "Gemini is transcribing the forecast…";
  const RETRYING =
    "Gemini is busy right now, so the model's own summary is shown for the moment. " +
    "This page will ask Gemini again in a minute or two.";
  const FAILED =
    "Gemini could not be reached just now, so the model's own summary is shown. " +
    "It will be tried again the next time this page is opened.";

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

  function successMessage(data, wants) {
    const gemini = String(data.generated_by || "").indexOf("llm:gemini") === 0;
    if (wants === "recommendation") {
      return gemini ? "Gemini wrote this plan's recommendation." : "An AI wrote this plan's recommendation.";
    }
    return gemini
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

  function regionsFor(id) {
    return Array.prototype.filter.call(
      document.querySelectorAll("[data-ai-region][data-ai-part]"),
      (region) => region.getAttribute("data-ai-region") === String(id)
    );
  }

  // REPAINTING THE RECOMMENDATION. Gemini's headline, reasons, risks,
  // competitor insight and innovation read appear in several places, on
  // three pages, each laid out by the server's own templates. Rather
  // than re-building that markup here, the page is fetched again (the
  // same URL, same session) and each part marked
  //     data-ai-region="<forecast id>" data-ai-part="<name>"
  // is replaced by its fresh, server-rendered (Jinja-escaped) copy.
  // DOMParser runs no scripts, and the parts carry none.
  async function repaintRegions(id) {
    const regions = regionsFor(id);
    if (!regions.length) return;
    try {
      const response = await fetch(window.location.href, {
        credentials: "same-origin",
        headers: { Accept: "text/html" },
      });
      if (!response.ok) return;
      const fresh = new DOMParser().parseFromString(await response.text(), "text/html");
      const byPart = new Map();
      fresh.querySelectorAll("[data-ai-region][data-ai-part]").forEach((region) => {
        if (region.getAttribute("data-ai-region") === String(id)) {
          byPart.set(region.getAttribute("data-ai-part"), region);
        }
      });
      regions.forEach((region) => {
        const replacement = byPart.get(region.getAttribute("data-ai-part"));
        if (replacement) region.replaceWith(document.importNode(replacement, true));
      });
    } catch (error) {
      // The page keeps what it shows; a reload shows Gemini's text.
    }
  }

  // ASKING AGAIN. A failed upgrade is stamped on the server, which asks
  // the page to wait "retry_after" seconds (a busy minute at Gemini). An
  // owner who keeps the page open then sees Gemini's text arrive without
  // reloading: the page asks again when the wait is over, up to
  // MAX_ROUNDS times in all.
  const MAX_ROUNDS = 3;

  function retryable(data) {
    return !data || data.reason === "failed" || data.reason === "retry_later" ||
      (data.ok && Array.isArray(data.pending) && data.pending.length > 0);
  }

  async function process(groups, round) {
    const again = new Map();
    let wait = 0;
    for (const [id, hooks] of groups) {
      const data = await requestTranscript(hooks[0].getAttribute("data-transcript-url"));
      hooks.forEach((hook) => {
        const wants = hook.getAttribute("data-ai-wants");
        const stillPending = !!(data && wants && Array.isArray(data.pending) && data.pending.indexOf(wants) !== -1);
        if (data && data.ok && !stillPending) {
          showTranscript(hook, data);
          setStatus(hook, successMessage(data, wants), false);
        } else if (data && data.ok) {
          setStatus(hook, round < MAX_ROUNDS ? RETRYING : FAILED, false);
        } else if (data && (data.reason === "unavailable" || data.reason === "no_payload")) {
          hideStatus(hook);
        } else {
          setStatus(hook, round < MAX_ROUNDS ? RETRYING : FAILED, false);
        }
      });
      if (data && data.refreshed && regionsFor(id).length) {
        // The forecast was re-run (it predated the plan model): a new
        // row, so the page itself is out of date.
        window.location.reload();
        return;
      }
      if (data && Array.isArray(data.changed) && data.changed.length) await repaintRegions(id);
      if (retryable(data) && round < MAX_ROUNDS) {
        again.set(id, hooks);
        wait = Math.max(wait, Number((data && data.retry_after) || 60));
      }
    }
    if (again.size) {
      window.setTimeout(() => {
        again.forEach((hooks) => hooks.forEach((hook) => {
          if (hook.isConnected) setStatus(hook, workingText(hook), true);
        }));
        process(again, round + 1);
      }, Math.min(wait, 600) * 1000);
    }
  }

  function workingText(hook) {
    const status = hook.querySelector("[data-transcript-status]");
    return (status && status.getAttribute("data-working-text")) || WORKING;
  }

  async function run() {
    if (typeof window.fetch !== "function") return;
    const groups = new Map();
    document.querySelectorAll("[data-forecast-transcript][data-transcript-url]").forEach((hook) => {
      const key = hook.getAttribute("data-forecast-id") || hook.getAttribute("data-transcript-url");
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(hook);
    });
    for (const hooks of groups.values()) hooks.forEach((hook) => setStatus(hook, workingText(hook), true));
    // One request per forecast, one at a time (see process above: each
    // is `await requestTranscript(...)` in turn).
    await process(groups, 1);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
