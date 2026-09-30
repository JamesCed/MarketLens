// app/static/js/tour.js
// -----------------------------------------------------------------
// The first-time walkthrough: the "Is this your first time here?"
// prompt, and the guided tour that follows a yes.
//
// It is a TRIAL, not a set of labels. Each step dims the page, lights up
// the real control it is talking about, and explains in plain words what
// that control is for. Some steps wait for the person to actually do the
// thing -- click a barangay, pick an industry, open the new-plan form --
// because doing it once is how someone who is not used to web apps
// learns it. Every one of those has a "Skip this step" way out, so a
// step that cannot be done (the map failed to load, say) never traps
// anyone.
//
// What each step SAYS lives in tour_steps.js. This file is only the
// machinery, and it knows nothing about any particular page.
//
// State, and where it lives:
//   * The SERVER keeps whether this account has been asked, is touring,
//     finished or skipped (User.onboarding_state), via the four POST
//     endpoints in app/controllers/onboarding_controller.py. That is
//     what makes the prompt appear once per account, not once per
//     browser.
//   * THIS TAB keeps which step it is on, in sessionStorage keyed by
//     user id. The tour spans several pages; each page load reads the
//     step back and carries on. Keyed by user so a different person
//     signing in on the same browser never inherits a half-finished tour.
//
// Built without libraries on purpose: it has to work on the same
// pages whether or not a CDN loads, and it is small enough that a
// dependency would be most of its weight.
//
// The first half of this file is pure functions with no reference to
// window or document, so tests/tour_logic_test.js can run them under
// Node. The browser half starts at "2. The tour".

(function () {
  "use strict";

  // =================================================================
  // 1. Pure helpers
  // =================================================================
  const STORAGE_PREFIX = "dssTour:";
  const DOCK_BELOW = 640; // px wide; narrower than this, the card docks at the bottom
  const EDGE = 16;        // px the card keeps clear of the screen edge
  const GAP = 16;         // px between the lit-up element and the card
  const SPOT_PAD = 8;     // px of breathing room around the lit-up element
  const SUCCESS_DELAY = 2600; // ms the "Nice! ..." line stays before moving on
  const TARGET_WAIT = 1500;   // ms to wait for an element a page draws late

  function storageKey(userId) {
    return STORAGE_PREFIX + String(userId);
  }

  // Every storage call is wrapped: sessionStorage throws in some private
  // modes, and a tour that cannot remember its place should still run
  // on the page in front of you rather than take the page down with it.
  function readProgress(storage, userId) {
    try {
      const raw = storage.getItem(storageKey(userId));
      if (raw == null) return null;
      const data = JSON.parse(raw);
      // A real number only: Number(null) is 0, which would quietly send
      // anyone with a mangled entry back to step 1 as if it were saved.
      const index = data && typeof data.index === "number" ? data.index : NaN;
      return Number.isInteger(index) && index >= 0 ? index : null;
    } catch (e) {
      return null;
    }
  }

  function writeProgress(storage, userId, index) {
    try {
      storage.setItem(storageKey(userId), JSON.stringify({ index: index, at: Date.now() }));
      return true;
    } catch (e) {
      return false;
    }
  }

  function clearProgress(storage, userId) {
    try {
      storage.removeItem(storageKey(userId));
    } catch (e) {
      /* nothing to clear */
    }
  }

  function clampIndex(index, total) {
    if (!total) return 0;
    const n = Number.isInteger(index) ? index : 0;
    return Math.min(Math.max(n, 0), total - 1);
  }

  // "/community/?x=1#top" and "/community" are the same page as far as
  // the tour is concerned: query strings change as you use a page (the
  // Settings tabs rewrite ?section=), and Flask treats the trailing
  // slash as the same route.
  function normalizePath(path) {
    let p = String(path || "/").split("#")[0].split("?")[0];
    if (!p.startsWith("/")) p = "/" + p;
    if (p.length > 1) p = p.replace(/\/+$/, "");
    return p || "/";
  }

  function pageMatches(currentPath, stepPage, root) {
    if (!stepPage) return true;
    return normalizePath(currentPath) === normalizePath((root || "") + stepPage);
  }

  function stepsForRole(allSteps, role) {
    const list = allSteps && role ? allSteps[role] : null;
    return Array.isArray(list) ? list : [];
  }

  // Where to start when a page loads mid-tour: the saved step if there
  // is one (clamped, in case the step list got shorter since), otherwise
  // the first. `onPage` says whether that step lives on this page -- if
  // not, the tour offers to take you there rather than yanking you away
  // from a page you chose to open.
  function resolveStart(savedIndex, steps, currentPath, root) {
    const index = clampIndex(savedIndex == null ? 0 : savedIndex, steps.length);
    const step = steps[index];
    return { index: index, onPage: !!step && pageMatches(currentPath, step.page, root) };
  }

  function candidateSelectors(step) {
    if (!step || !step.target) return [];
    return Array.isArray(step.target) ? step.target : [step.target];
  }

  // "Can the person actually see this?" -- not merely "is it in the DOM".
  // A hidden tab pane, a collapsed empty state or the off-canvas phone
  // menu are all in the DOM; lighting up an empty rectangle where they
  // would be is worse than showing the step as a centred card.
  function isVisible(el, win) {
    if (!el || el.isConnected === false) return false;
    if (!el.getClientRects || el.getClientRects().length === 0) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    // Pushed off the side of the page itself -- the phone menu when
    // closed sits at translateX(-100%). Nobody can scroll there, so it
    // does not count. Merely being right of the window does NOT rule an
    // element out: on a page wider than the screen you can scroll to it.
    const scrollX = (win && win.scrollX) || 0;
    const pageWidth = win && win.document && win.document.documentElement
      ? Math.max(win.document.documentElement.scrollWidth, win.innerWidth || 0)
      : Infinity;
    if (r.right + scrollX <= 0 || r.left + scrollX >= pageWidth) return false;
    if (win && win.getComputedStyle) {
      const style = win.getComputedStyle(el);
      if (style.visibility === "hidden" || style.display === "none") return false;
    }
    return true;
  }

  // The first visible match among the step's selectors, or null. Null is
  // not an error: it means "show this step as a centred card". A layout
  // change on some page makes the tour plainer there, never broken.
  function findTarget(doc, step, visible) {
    const check = visible || function (el) { return !!el; };
    const selectors = candidateSelectors(step);
    for (let s = 0; s < selectors.length; s += 1) {
      let matches;
      try {
        matches = doc.querySelectorAll(selectors[s]);
      } catch (e) {
        continue; // a selector this browser cannot parse -- try the next
      }
      const list = Array.prototype.slice.call(matches || []);
      for (let m = 0; m < list.length; m += 1) {
        let el = list[m];
        if (step.closest && el.closest) el = el.closest(step.closest) || el;
        if (check(el)) return el;
      }
    }
    return null;
  }

  // Did this event happen where an interactive step is waiting for it?
  function eventMatches(eventTarget, advanceOn) {
    if (!advanceOn || !advanceOn.selector) return false;
    let el = eventTarget;
    if (el && el.nodeType === 3) el = el.parentElement; // a text node
    if (!el || !el.closest) return false;
    let inside;
    try {
      inside = el.closest(advanceOn.selector);
    } catch (e) {
      return false;
    }
    if (!inside) return false;
    if (!advanceOn.match) return true;
    let hit;
    try {
      hit = el.closest(advanceOn.match);
    } catch (e) {
      return false;
    }
    return !!hit && (hit === inside || inside.contains(hit));
  }

  // Where the card goes, given where the lit-up element is.
  //   target   {top, left, width, height} in viewport pixels, or null
  //   card     {width, height}
  //   viewport {width, height}
  // Tries the preferred side, then right, left, below and above, and
  // takes the first where the whole card fits on screen. When nothing
  // fits -- the element fills most of the screen -- the card sits in the
  // bottom-right corner over it, which still leaves the element visible
  // behind the spotlight ring.
  function computePlacement(target, card, viewport, preferred) {
    const vw = viewport.width;
    const vh = viewport.height;
    const cw = card.width;
    const ch = card.height;
    if (vw < DOCK_BELOW) return { placement: "dock" };

    const clampTop = (top) => Math.max(EDGE, Math.min(top, vh - ch - EDGE));
    const clampLeft = (left) => Math.max(EDGE, Math.min(left, vw - cw - EDGE));

    if (!target) {
      return { placement: "center", top: clampTop((vh - ch) / 2), left: clampLeft((vw - cw) / 2) };
    }

    const sides = [preferred, "right", "left", "bottom", "top"].filter(
      (side, i, all) => side && all.indexOf(side) === i
    );
    for (let i = 0; i < sides.length; i += 1) {
      const side = sides[i];
      let top;
      let left;
      let fits;
      if (side === "right") {
        left = target.left + target.width + GAP;
        top = clampTop(target.top);
        fits = left + cw <= vw - EDGE;
      } else if (side === "left") {
        left = target.left - GAP - cw;
        top = clampTop(target.top);
        fits = left >= EDGE;
      } else if (side === "bottom") {
        top = target.top + target.height + GAP;
        left = clampLeft(target.left + target.width / 2 - cw / 2);
        fits = top + ch <= vh - EDGE;
      } else if (side === "top") {
        top = target.top - GAP - ch;
        left = clampLeft(target.left + target.width / 2 - cw / 2);
        fits = top >= EDGE;
      }
      if (fits) return { placement: side, top: top, left: left };
    }
    return { placement: "corner", top: clampTop(vh - ch - EDGE), left: clampLeft(vw - cw - EDGE) };
  }

  const pure = {
    storageKey, readProgress, writeProgress, clearProgress, clampIndex, normalizePath,
    pageMatches, stepsForRole, resolveStart, findTarget, isVisible, eventMatches,
    computePlacement, DOCK_BELOW,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = pure;
  }
  if (typeof window === "undefined" || typeof document === "undefined") return;

  // =================================================================
  // 2. The tour
  // =================================================================

  // An in-memory stand-in for when sessionStorage is unavailable. The
  // tour then cannot follow you across pages, but it still works on
  // the page you are on.
  function memoryStorage() {
    const data = {};
    return {
      getItem: (k) => (Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null),
      setItem: (k, v) => { data[k] = String(v); },
      removeItem: (k) => { delete data[k]; },
    };
  }

  function safeSessionStorage() {
    try {
      const s = window.sessionStorage;
      s.setItem("__dssTourProbe", "1");
      s.removeItem("__dssTourProbe");
      return s;
    } catch (e) {
      return memoryStorage();
    }
  }

  function prefersReducedMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  // Keys that mean something inside a form field (or a focused map,
  // where the arrows pan) are left alone.
  function isTypingTarget(el) {
    return !!(el && el.closest &&
      el.closest('input, select, textarea, [contenteditable=""], [contenteditable="true"], .leaflet-container'));
  }

  function post(url, body) {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      // Finish and Exit close the tour at once, and the very next click
      // may be a link. keepalive lets the request finish even though the
      // page that sent it is gone -- without it, a quick click away
      // after "Finish" left the account stuck at "touring".
      keepalive: true,
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-CSRFToken": meta ? meta.getAttribute("content") : "",
      },
      body: JSON.stringify(body || {}),
    })
      .then((r) => {
        // An expired sign-in comes back as the login page (HTML, 200
        // after the redirect), so "ok" alone is not enough -- the JSON
        // has to parse and say ok.
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then((data) => {
        if (!data || data.ok !== true) throw new Error("Unexpected response");
        return data;
      });
  }

  // Writes "Try it: ..." / "Nice! ..." with the lead-in in bold, as
  // text nodes -- never innerHTML, so nothing in a step can inject markup.
  function setLeadText(el, text, lead) {
    el.textContent = "";
    const value = String(text || "");
    if (lead && value.indexOf(lead) === 0) {
      const strong = document.createElement("strong");
      strong.textContent = lead;
      el.appendChild(strong);
      el.appendChild(document.createTextNode(value.slice(lead.length)));
    } else {
      el.appendChild(document.createTextNode(value));
    }
  }

  const CARD_HTML = `
    <div class="dss-tour-block" data-block="top"></div>
    <div class="dss-tour-block" data-block="right"></div>
    <div class="dss-tour-block" data-block="bottom"></div>
    <div class="dss-tour-block" data-block="left"></div>
    <div class="dss-tour-block" data-block="lid"></div>
    <div class="dss-tour-spot" aria-hidden="true"></div>
    <section class="dss-tour-card" role="dialog" aria-modal="false"
             aria-labelledby="dssTourTitle" aria-describedby="dssTourBody">
      <div class="dss-tour-progress" data-part="progress">
        <span class="dss-tour-count"></span>
        <span class="dss-tour-bar" aria-hidden="true"><span class="dss-tour-bar-fill"></span></span>
      </div>
      <h2 class="dss-tour-title" id="dssTourTitle" tabindex="-1"></h2>
      <div class="dss-tour-body" id="dssTourBody" aria-live="polite">
        <div class="dss-tour-section" data-part="what">
          <h3 class="dss-tour-label"></h3>
          <p class="dss-tour-text"></p>
          <ul class="dss-tour-list"></ul>
        </div>
        <div class="dss-tour-section" data-part="how">
          <h3 class="dss-tour-label"></h3>
          <p class="dss-tour-text"></p>
        </div>
        <p class="dss-tour-try" data-part="try">
          <i class="bi bi-hand-index-thumb" aria-hidden="true"></i>
          <span></span>
        </p>
        <p class="dss-tour-success" data-part="success">
          <i class="bi bi-check-circle-fill" aria-hidden="true"></i>
          <span></span>
        </p>
        <p class="dss-tour-note" data-part="note"></p>
      </div>
      <div class="dss-tour-confirm" data-part="confirm">
        <p class="dss-tour-confirm-text">Exit the tour? You can replay it anytime from <strong>Take the tour</strong> in the sidebar.</p>
        <div class="dss-tour-actions">
          <button type="button" class="dss-tour-btn dss-tour-btn-ghost" data-act="stay">Keep going</button>
          <button type="button" class="dss-tour-btn dss-tour-btn-primary" data-act="exit-yes">Yes, exit the tour</button>
        </div>
      </div>
      <div class="dss-tour-actions" data-part="nav">
        <button type="button" class="dss-tour-btn dss-tour-btn-link" data-act="exit">Exit tour</button>
        <span class="dss-tour-spacer"></span>
        <button type="button" class="dss-tour-btn dss-tour-btn-ghost" data-act="back">Back</button>
        <button type="button" class="dss-tour-btn dss-tour-btn-ghost" data-act="skip">Skip this step</button>
        <button type="button" class="dss-tour-btn dss-tour-btn-primary" data-act="next">Next</button>
      </div>
    </section>`;

  class Tour {
    constructor(config, steps, pages) {
      this.config = config;
      this.steps = steps;
      this.pages = pages || {};
      this.userId = config.userId;
      this.root = config.root || "";
      this.storage = safeSessionStorage();
      this.index = -1;
      this.mode = null; // "step" | "paused" | "navigating" | "message"
      this.target = null;
      this.actionDone = false;
      this.timers = [];
      this.frame = null;
      this.el = null;
      this.openedSidebar = false;
      this.returnFocus = null;
      this.onAdvanceEvent = this.onAdvanceEvent.bind(this);
      this.onKeydown = this.onKeydown.bind(this);
      this.schedule = this.schedule.bind(this);
    }

    // ---------------------------------------------------------------
    // Entry points
    // ---------------------------------------------------------------

    // From step 1 -- after "Yes" at the prompt, or "Take the tour".
    begin() {
      this.returnFocus = document.activeElement;
      this.go(0);
    }

    // A page loaded while the server says a tour is under way.
    resume() {
      if (!this.steps.length) return;
      const saved = readProgress(this.storage, this.userId);
      const start = resolveStart(saved, this.steps, window.location.pathname, this.root);
      if (start.onPage) this.show(start.index);
      else this.showPaused(start.index);
    }

    pageName(step) {
      return this.pages[step.page] || "next";
    }

    // ---------------------------------------------------------------
    // Moving between steps
    // ---------------------------------------------------------------
    go(index) {
      if (!this.steps.length) return;
      if (index >= this.steps.length) {
        this.finish();
        return;
      }
      const i = clampIndex(index, this.steps.length);
      const step = this.steps[i];
      if (!pageMatches(window.location.pathname, step.page, this.root)) {
        // The step lives on another page. Remember it, go there, and
        // that page's load picks the tour back up (resume()).
        this.leaveStep();
        writeProgress(this.storage, this.userId, i);
        this.showNavigating(step);
        window.location.assign(this.root + step.page);
        return;
      }
      this.show(i);
    }

    next() {
      const step = this.steps[this.index];
      if (step && step.finish) this.finish();
      else this.go(this.index + 1);
    }

    back() {
      if (this.index > 0) this.go(this.index - 1);
    }

    // ---------------------------------------------------------------
    // Showing one step
    // ---------------------------------------------------------------
    show(index) {
      this.leaveStep();
      this.index = index;
      this.mode = "step";
      this.actionDone = false;
      this.target = null;
      const step = this.steps[index];
      writeProgress(this.storage, this.userId, index);

      this.activate();
      this.el.root.classList.remove("is-paused", "is-message");
      this.el.root.classList.toggle("is-interactive", !!step.advanceOn);

      // Open the tab/pane the step is about before looking for it.
      if (step.prepare && step.prepare.click) {
        const opener = findTarget(document, { target: step.prepare.click });
        if (opener) opener.click();
      }

      this.revealSidebarFor(step);
      this.renderStep(step, index);
      if (step.advanceOn) {
        document.addEventListener(step.advanceOn.event, this.onAdvanceEvent, true);
      }

      // Most targets are there already. Some are drawn by the page a
      // moment later (a dialog fading in, a list built after a fetch),
      // so keep looking briefly -- the card waits in the centre
      // meanwhile rather than the tour stalling.
      const visible = (el) => isVisible(el, window);
      const found = findTarget(document, step, visible);
      if (found) {
        this.attachTarget(found);
      } else {
        this.position();
        const started = Date.now();
        const poll = () => {
          if (this.index !== index || this.mode !== "step") return;
          const late = findTarget(document, step, visible);
          if (late) this.attachTarget(late);
          else if (Date.now() - started < TARGET_WAIT) this.later(poll, 120);
        };
        this.later(poll, 120);
      }
      this.focusCard();
    }

    attachTarget(el) {
      this.target = el;
      this.releaseDialogFocus();
      this.scrollIntoView(el);
      this.position();
      // Smooth scrolling, a sidebar sliding in and a dialog fading in
      // all move the element after it was found. Re-measure a few
      // times as those settle; scroll/resize listeners cover the rest.
      [80, 250, 500, 900].forEach((ms) => this.later(this.schedule, ms));
      if (window.ResizeObserver) {
        this.resizeObserver = new ResizeObserver(this.schedule);
        this.resizeObserver.observe(el);
      }
    }

    // Leaving a step, for whatever reason: forget its target and its
    // "try it" listener, and close the dialog it opened if it said so.
    leaveStep() {
      this.clearTimers();
      if (this.resizeObserver) {
        this.resizeObserver.disconnect();
        this.resizeObserver = null;
      }
      const step = this.steps[this.index];
      if (step && step.advanceOn) {
        document.removeEventListener(step.advanceOn.event, this.onAdvanceEvent, true);
      }
      if (step && step.closeModals) this.closeDialogs();
      this.target = null;
    }

    renderStep(step, index) {
      const el = this.el;
      const total = this.steps.length;
      const labels = step.labels || {};

      el.count.textContent = "Step " + (index + 1) + " of " + total;
      el.barFill.style.width = Math.round(((index + 1) / total) * 100) + "%";
      el.progress.hidden = false;
      el.title.textContent = step.title || "";

      el.whatLabel.textContent = labels.what || "What this is for";
      el.whatLabel.hidden = false;
      el.whatText.textContent = step.what || "";
      el.what.hidden = !step.what && !(step.list && step.list.length);
      el.list.textContent = "";
      (step.list || []).forEach((item) => {
        const li = document.createElement("li");
        li.textContent = item;
        el.list.appendChild(li);
      });
      el.list.hidden = !(step.list && step.list.length);

      el.howLabel.textContent = labels.how || "How to use it";
      el.howLabel.hidden = false;
      el.howText.textContent = step.how || "";
      el.how.hidden = !step.how;

      el.tryIt.hidden = !step.tryIt;
      if (step.tryIt) setLeadText(el.tryText, step.tryIt, "Try it:");
      el.success.hidden = true;
      el.note.hidden = true;

      this.hideConfirm();
      el.nav.hidden = false;
      el.exit.hidden = false;
      el.back.hidden = index === 0;
      // A "try it" step shows Skip until it has been done, then Next.
      el.skip.hidden = !step.advanceOn;
      el.next.hidden = !!step.advanceOn;
      el.next.textContent = step.finish ? "Finish" : index === 0 ? "Let’s begin" : "Next";
      this.setBusy(false);
    }

    // The person did the thing a "try it" step asked for.
    onAdvanceEvent(event) {
      const step = this.steps[this.index];
      if (!step || this.mode !== "step" || this.actionDone) return;
      if (!eventMatches(event.target, step.advanceOn)) return;
      this.actionDone = true;
      // Saved before anything else: the action itself may load a new
      // page, and the tour should then carry on from the NEXT step.
      writeProgress(this.storage, this.userId, this.index + 1);

      const el = this.el;
      el.tryIt.hidden = true;
      setLeadText(el.successText, step.success || "Nice! You did it.", "Nice!");
      el.success.hidden = false;
      el.skip.hidden = true;
      el.next.hidden = false;
      el.root.classList.remove("is-interactive");
      const at = this.index;
      this.later(() => {
        if (this.index === at && this.mode === "step") this.next();
      }, SUCCESS_DELAY);
    }

    // ---------------------------------------------------------------
    // The other card modes
    // ---------------------------------------------------------------

    // Mid-tour, on a page the current step is not on (the person used
    // the menu, the Back button, or opened a new tab). Not an overlay:
    // a small card in the corner, so the page stays usable, offering to
    // pick up where they left off.
    showPaused(index) {
      this.leaveStep();
      this.index = index;
      this.mode = "paused";
      const step = this.steps[index];
      this.activate();
      this.el.root.classList.add("is-paused");
      this.el.root.classList.remove("is-interactive", "is-message");
      this.renderPlain(
        "Your tour is paused",
        "You were on step " + (index + 1) + " of " + this.steps.length + ": “" + step.title +
          "”, on the " + this.pageName(step) + " page.",
        "Press Continue to pick up where you left off, or Exit tour to explore on your own."
      );
      this.el.next.textContent = "Continue the tour";
      this.el.next.hidden = false;
      this.el.exit.hidden = false;
      this.position();
    }

    // Between pages. The browser is already loading the next one; this
    // just says so, so a slow connection never looks like a dead button.
    showNavigating(step) {
      this.mode = "navigating";
      this.activate();
      this.el.root.classList.remove("is-paused", "is-message", "is-interactive");
      this.renderPlain("One moment…", "Taking you to the " + this.pageName(step) + " page…", "");
      this.el.next.hidden = true;
      this.el.exit.hidden = true;
      this.setBusy(true);
      this.position();
    }

    showMessage(title, text) {
      this.leaveStep();
      this.mode = "message";
      this.activate();
      this.el.root.classList.add("is-paused", "is-message");
      this.renderPlain(title, text, "");
      this.el.next.textContent = "Close";
      this.el.next.hidden = false;
      this.el.exit.hidden = true;
      this.position();
      this.focusCard();
    }

    renderPlain(title, what, how) {
      const el = this.el;
      el.progress.hidden = true;
      el.title.textContent = title;
      el.whatLabel.hidden = true;
      el.whatText.textContent = what;
      el.what.hidden = false;
      el.list.hidden = true;
      el.howLabel.hidden = true;
      el.howText.textContent = how;
      el.how.hidden = !how;
      el.tryIt.hidden = true;
      el.success.hidden = true;
      el.note.hidden = true;
      el.back.hidden = true;
      el.skip.hidden = true;
      this.hideConfirm();
      el.nav.hidden = false;
      this.setBusy(false);
    }

    // ---------------------------------------------------------------
    // Leaving the tour
    // ---------------------------------------------------------------
    showConfirm() {
      if (!this.el) return;
      this.el.confirm.hidden = false;
      this.el.nav.hidden = true;
      this.el.card.querySelector('[data-act="stay"]').focus();
      this.schedule();
    }

    hideConfirm() {
      if (!this.el) return;
      this.el.confirm.hidden = true;
      this.el.nav.hidden = false;
    }

    exit() {
      const step = this.index + 1;
      const total = this.steps.length;
      this.setBusy(true);
      // Closed straight away whether or not the request succeeds: a
      // tour you cannot get out of is the worst possible outcome. If
      // the server missed it, the next page offers "Continue / Exit"
      // again rather than restarting anything.
      post(this.config.urls.skip, { step: step, total: total }).catch(() => {});
      this.config.state = "skipped";
      this.teardown();
    }

    finish() {
      this.setBusy(true);
      post(this.config.urls.complete, {}).catch(() => {});
      this.config.state = "completed";
      this.teardown();
    }

    teardown() {
      this.leaveStep(); // also closes a dialog the current step opened
      clearProgress(this.storage, this.userId);
      this.mode = null;
      this.index = -1;
      if (this.el) {
        this.el.root.hidden = true;
        this.el.root.classList.remove("is-paused", "is-message", "is-interactive");
      }
      document.documentElement.classList.remove("dss-tour-active");
      window.removeEventListener("resize", this.schedule);
      window.removeEventListener("scroll", this.schedule, true);
      document.removeEventListener("keydown", this.onKeydown);
      if (this.openedSidebar) this.toggleSidebar(false);
      const back = this.returnFocus;
      this.returnFocus = null;
      if (back && back.focus && back.isConnected) back.focus({ preventScroll: true });
    }

    // ---------------------------------------------------------------
    // DOM
    // ---------------------------------------------------------------
    build() {
      if (this.el) return;
      const root = document.createElement("div");
      root.className = "dss-tour";
      root.hidden = true;
      root.innerHTML = CARD_HTML;
      document.body.appendChild(root);

      const q = (sel) => root.querySelector(sel);
      const part = (name) => q('[data-part="' + name + '"]');
      this.el = {
        root: root,
        spot: q(".dss-tour-spot"),
        blocks: {
          top: q('[data-block="top"]'),
          right: q('[data-block="right"]'),
          bottom: q('[data-block="bottom"]'),
          left: q('[data-block="left"]'),
          lid: q('[data-block="lid"]'),
        },
        card: q(".dss-tour-card"),
        progress: part("progress"),
        count: q(".dss-tour-count"),
        barFill: q(".dss-tour-bar-fill"),
        title: q(".dss-tour-title"),
        what: part("what"),
        whatLabel: part("what").querySelector(".dss-tour-label"),
        whatText: part("what").querySelector(".dss-tour-text"),
        list: q(".dss-tour-list"),
        how: part("how"),
        howLabel: part("how").querySelector(".dss-tour-label"),
        howText: part("how").querySelector(".dss-tour-text"),
        tryIt: part("try"),
        tryText: part("try").querySelector("span"),
        success: part("success"),
        successText: part("success").querySelector("span"),
        note: part("note"),
        confirm: part("confirm"),
        nav: part("nav"),
        exit: q('[data-act="exit"]'),
        back: q('[data-act="back"]'),
        skip: q('[data-act="skip"]'),
        next: q('[data-act="next"]'),
      };

      root.addEventListener("click", (event) => {
        const button = event.target.closest("[data-act]");
        if (button) {
          this.act(button.getAttribute("data-act"));
          return;
        }
        // A click on the dimmed page. It is swallowed (the tour is
        // showing something else right now), but not silently: the
        // card gives a little nudge so it is obvious where to look.
        if (event.target.classList.contains("dss-tour-block")) this.nudge();
      });
    }

    act(action) {
      if (action === "next") {
        if (this.mode === "paused") this.go(this.index);
        else if (this.mode === "message") this.teardown();
        else this.next();
      } else if (action === "back") {
        this.back();
      } else if (action === "skip") {
        this.go(this.index + 1);
      } else if (action === "exit") {
        this.showConfirm();
      } else if (action === "stay") {
        this.hideConfirm();
        this.focusCard();
      } else if (action === "exit-yes") {
        this.exit();
      }
    }

    activate() {
      this.build();
      if (this.el.root.hidden) {
        this.el.root.hidden = false;
        window.addEventListener("resize", this.schedule);
        // Capture, so scrolling inside any container (the sidebar's own
        // scroll area, a table) re-positions the spotlight too.
        window.addEventListener("scroll", this.schedule, true);
        document.addEventListener("keydown", this.onKeydown);
      }
      document.documentElement.classList.toggle("dss-tour-active", this.mode === "step");
    }

    setBusy(busy) {
      if (!this.el) return;
      this.el.card.setAttribute("aria-busy", busy ? "true" : "false");
      this.el.card.querySelectorAll("button").forEach((b) => { b.disabled = busy; });
    }

    focusCard() {
      // Focus goes INTO the card on every step, so a keyboard or screen
      // reader user is never left somewhere behind the dimmed page.
      if (this.el) this.el.title.focus({ preventScroll: true });
    }

    nudge() {
      const card = this.el.card;
      card.classList.remove("is-nudged");
      // Reading offsetWidth restarts the animation if it is mid-play.
      void card.offsetWidth;
      card.classList.add("is-nudged");
      this.later(() => card.classList.remove("is-nudged"), 700);
    }

    // ---------------------------------------------------------------
    // Positioning
    // ---------------------------------------------------------------
    schedule() {
      if (this.frame) return;
      this.frame = window.requestAnimationFrame(() => {
        this.frame = null;
        this.position();
      });
    }

    position() {
      if (!this.el || this.el.root.hidden) return;
      const card = this.el.card;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const docked = vw < DOCK_BELOW;
      card.classList.toggle("is-docked", docked);

      if (this.mode === "paused" || this.mode === "message") {
        // A small card in the corner (see .is-paused in tour.css), no
        // spotlight and nothing dimmed -- the page stays usable.
        card.style.top = "";
        card.style.left = "";
        card.dataset.placement = "corner";
        return;
      }

      const step = this.mode === "step" ? this.steps[this.index] : {};
      let el = this.target;
      if (el && !isVisible(el, window)) el = null;
      if (!el) {
        // Lost it (a dialog was closed) or never had it: look again, in
        // case it has appeared since.
        el = findTarget(document, step, (e) => isVisible(e, window));
        if (el && el !== this.target) this.target = el;
      }

      let rect = null;
      if (el) {
        const r = el.getBoundingClientRect();
        rect = {
          top: r.top - SPOT_PAD,
          left: r.left - SPOT_PAD,
          width: r.width + SPOT_PAD * 2,
          height: r.height + SPOT_PAD * 2,
        };
      }
      this.drawSpot(rect, vw, vh, !!step.advanceOn && !this.actionDone);

      const place = computePlacement(
        rect,
        { width: card.offsetWidth, height: card.offsetHeight },
        { width: vw, height: vh },
        step.placement
      );
      card.dataset.placement = place.placement;
      if (place.placement === "dock") {
        card.style.top = "";
        card.style.left = "";
      } else {
        card.style.top = Math.round(place.top) + "px";
        card.style.left = Math.round(place.left) + "px";
      }
    }

    // The spotlight is one box whose enormous shadow dims everything
    // around it. Four invisible blocks around the hole catch clicks on
    // the dimmed page; a fifth "lid" covers the hole too, except on a
    // "try it" step, where the lit-up control is meant to be used.
    drawSpot(rect, vw, vh, interactive) {
      const spot = this.el.spot;
      const b = this.el.blocks;
      const place = (node, top, left, width, height) => {
        node.style.top = top + "px";
        node.style.left = left + "px";
        node.style.width = Math.max(0, width) + "px";
        node.style.height = Math.max(0, height) + "px";
      };

      if (!rect) {
        spot.classList.add("is-empty");
        place(spot, vh / 2, vw / 2, 0, 0);
        place(b.top, 0, 0, vw, vh);
        place(b.right, 0, 0, 0, 0);
        place(b.bottom, 0, 0, 0, 0);
        place(b.left, 0, 0, 0, 0);
        place(b.lid, 0, 0, 0, 0);
        return;
      }

      spot.classList.remove("is-empty");
      place(spot, rect.top, rect.left, rect.width, rect.height);

      // The hole, clipped to the screen so no block gets a negative size.
      const top = Math.max(0, Math.min(vh, rect.top));
      const bottom = Math.max(0, Math.min(vh, rect.top + rect.height));
      const left = Math.max(0, Math.min(vw, rect.left));
      const right = Math.max(0, Math.min(vw, rect.left + rect.width));
      place(b.top, 0, 0, vw, top);
      place(b.bottom, bottom, 0, vw, vh - bottom);
      place(b.left, top, 0, left, bottom - top);
      place(b.right, top, right, vw - right, bottom - top);
      if (interactive) place(b.lid, 0, 0, 0, 0);
      else place(b.lid, top, left, right - left, bottom - top);
    }

    scrollIntoView(el) {
      const behavior = prefersReducedMotion() ? "auto" : "smooth";
      const r = el.getBoundingClientRect();
      const vh = window.innerHeight;
      if (window.innerWidth < DOCK_BELOW) {
        // Docked card covers the bottom of the screen, so "in view"
        // means in the part above it.
        const cardHeight = this.el.card.offsetHeight || vh * 0.5;
        const visibleBottom = vh - cardHeight - 12;
        if (r.top < 64 || r.bottom > visibleBottom) {
          window.scrollBy({ top: r.top - 72, behavior: behavior });
        }
        return;
      }
      if (r.top < 0 || r.bottom > vh || r.left < 0 || r.right > window.innerWidth) {
        el.scrollIntoView({ block: r.height > vh * 0.8 ? "start" : "center", inline: "nearest", behavior: behavior });
      }
    }

    // ---------------------------------------------------------------
    // Page furniture the tour has to work with
    // ---------------------------------------------------------------

    // On a phone the menu is off-canvas. A step about the menu opens it
    // (with the page's own ☰ button, so main.js stays in charge of it);
    // the first step elsewhere closes it again.
    revealSidebarFor(step) {
      const sidebar = document.getElementById("dssSidebar");
      if (!sidebar) return;
      const raw = findTarget(document, step);
      const inSidebar = !!raw && sidebar.contains(raw);
      const offCanvas = !!(window.matchMedia && window.matchMedia("(max-width: 900px)").matches);
      if (inSidebar && offCanvas && !sidebar.classList.contains("open")) {
        this.toggleSidebar(true);
      } else if (!inSidebar && this.openedSidebar) {
        this.toggleSidebar(false);
      }
    }

    toggleSidebar(open) {
      const sidebar = document.getElementById("dssSidebar");
      const toggle = document.getElementById("sidebarToggle");
      if (!sidebar || !toggle) return;
      if (sidebar.classList.contains("open") !== open) toggle.click();
      this.openedSidebar = open;
    }

    closeDialogs() {
      if (!window.bootstrap || !window.bootstrap.Modal) return;
      document.querySelectorAll(".modal.show").forEach((modal) => {
        const instance = window.bootstrap.Modal.getInstance(modal);
        if (instance) instance.hide();
      });
    }

    // Bootstrap's dialogs keep keyboard focus inside themselves, and
    // would pull it straight back out of the tour card. While the tour
    // is explaining an open dialog, the card has to be reachable, so the
    // trap is released for that dialog. Bootstrap re-arms it the next
    // time the dialog opens. Private API, hence the guard: if it ever
    // changes, the only loss is keyboard focus on this one step.
    releaseDialogFocus() {
      if (!window.bootstrap || !window.bootstrap.Modal) return;
      document.querySelectorAll(".modal.show").forEach((modal) => {
        try {
          const instance = window.bootstrap.Modal.getInstance(modal);
          if (instance && instance._focustrap) instance._focustrap.deactivate();
        } catch (e) {
          /* see above */
        }
      });
    }

    // ---------------------------------------------------------------
    // Keyboard: Esc to exit, arrows to move
    // ---------------------------------------------------------------
    onKeydown(event) {
      if (this.mode !== "step" || !this.el) return;
      const confirming = !this.el.confirm.hidden;
      if (event.key === "Escape") {
        // Esc inside an open dialog closes the dialog -- that is what
        // the person means, and Bootstrap already does it.
        const dialog = document.querySelector(".modal.show");
        if (dialog && dialog.contains(event.target)) return;
        event.preventDefault();
        if (confirming) {
          this.hideConfirm();
          this.focusCard();
        } else {
          this.showConfirm();
        }
        return;
      }
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      if (confirming || isTypingTarget(event.target) || event.altKey || event.ctrlKey || event.metaKey) return;
      event.preventDefault();
      if (event.key === "ArrowLeft") {
        this.back();
        return;
      }
      const step = this.steps[this.index];
      if (step.advanceOn && !this.actionDone) this.go(this.index + 1); // same as "Skip this step"
      else this.next();
    }

    // ---------------------------------------------------------------
    // Timers
    // ---------------------------------------------------------------
    later(fn, ms) {
      this.timers.push(window.setTimeout(fn, ms));
    }

    clearTimers() {
      this.timers.forEach((t) => window.clearTimeout(t));
      this.timers = [];
    }
  }

  // =================================================================
  // 3. The "Is this your first time here?" prompt
  // =================================================================
  function setupPrompt(config, tour) {
    const prompt = document.getElementById("dssOnboardingPrompt");
    if (!prompt) return;
    const buttons = Array.prototype.slice.call(prompt.querySelectorAll("[data-onboarding-answer]"));
    const error = prompt.querySelector(".dss-tour-prompt-error");
    const opener = document.activeElement;

    function close() {
      prompt.hidden = true;
      document.documentElement.classList.remove("dss-tour-prompt-open");
      document.removeEventListener("keydown", onKey, true);
    }

    function busy(on) {
      buttons.forEach((b) => { b.disabled = on; });
      prompt.setAttribute("aria-busy", on ? "true" : "false");
    }

    function onKey(event) {
      if (event.key === "Escape") {
        // Not an answer -- "not now". Nothing is saved, so they are
        // simply asked again on the next page.
        event.preventDefault();
        close();
        if (opener && opener.focus) opener.focus({ preventScroll: true });
        return;
      }
      if (event.key === "Tab") {
        // It is a real modal question: keep focus on its two answers.
        const first = buttons[0];
        const last = buttons[buttons.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        } else if (!prompt.contains(document.activeElement)) {
          event.preventDefault();
          first.focus();
        }
      }
    }

    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        const yes = button.getAttribute("data-onboarding-answer") === "yes";
        busy(true);
        if (error) error.hidden = true;
        post(yes ? config.urls.start : config.urls.skip, {})
          .then((data) => {
            config.state = data.state;
            close();
            if (yes) tour.begin();
          })
          .catch(() => {
            // Stay open: closing would look like it worked, and the
            // same question would come back on the next page anyway.
            busy(false);
            if (error) {
              error.textContent = "Sorry, that didn’t go through. Please check your internet connection and try again.";
              error.hidden = false;
            }
          });
      });
    });

    prompt.hidden = false;
    document.documentElement.classList.add("dss-tour-prompt-open");
    document.addEventListener("keydown", onKey, true);
    if (buttons[0]) buttons[0].focus({ preventScroll: true });
  }

  // =================================================================
  // 4. Start-up
  // =================================================================
  function boot() {
    const config = window.DSS_ONBOARDING;
    if (!config || !config.urls) return; // signed out
    const steps = stepsForRole(window.DSS_TOUR_STEPS, config.role);
    const tour = new Tour(config, steps, window.DSS_TOUR_PAGES);
    window.dssTour = tour; // handy from the browser console while testing

    // "Take the tour" in the sidebar. Works on every page, including
    // an error page, because it starts by going to step 1's own page.
    document.querySelectorAll("[data-tour-replay]").forEach((button) => {
      button.addEventListener("click", () => {
        if (!steps.length) return;
        button.disabled = true;
        post(config.urls.restart, {})
          .then((data) => {
            config.state = data.state;
            tour.begin();
          })
          .catch(() => {
            tour.showMessage(
              "Sorry, the tour couldn’t start",
              "Please check your internet connection and try again."
            );
          })
          .finally(() => { button.disabled = false; });
      });
    });

    // Error pages: no prompt and no auto-resume (the server marks them
    // suppressed). The sidebar button above still works.
    if (config.suppressed || !steps.length) return;

    // The prompt and a resumed tour wait until the page's own start-up
    // code (map.js, the Settings tabs...) has run -- it also runs on
    // DOMContentLoaded -- so the tour measures the page as it will
    // actually look. The button above is bound straight away instead:
    // it is already in the DOM, and a quick click must not be lost.
    whenReady(() => {
      if (config.state == null || config.state === "") setupPrompt(config, tour);
      else if (config.state === "touring") tour.resume();
    });

    // Coming BACK to a page from the browser's back/forward cache runs
    // no scripts, so a card saying "Taking you to..." would still be on
    // screen. Re-read where the tour is and show that instead.
    window.addEventListener("pageshow", (event) => {
      if (event.persisted && config.state === "touring") tour.resume();
    });
  }

  function whenReady(fn) {
    const run = () => window.setTimeout(fn, 150);
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run);
    else run();
  }

  // tour.js is the last script in base.html, so the sidebar and the
  // onboarding config are already in the page when this runs.
  boot();
})();
