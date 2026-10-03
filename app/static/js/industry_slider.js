// app/static/js/industry_slider.js
// ---------------------------------------------------------------------
// The industry slider on the Home page: twenty industry cards in one
// horizontal row, four at a time on a wide screen.
//
// PROGRESSIVE ENHANCEMENT. The row is a CSS scroll-snap track
// (.dss-industry-track in style.css), so without this script it already
// swipes on a phone and scrolls with a trackpad or Shift+wheel -- every
// card stays one gesture away. This script only adds what scripting
// buys:
//
//   * Previous / Next buttons. They are rendered `hidden` and shown here,
//     because a button that does nothing without the script should not
//     be on the page. Each moves by one screenful of cards and is
//     disabled at its end of the row.
//   * Left / Right (and Home / End) while the track itself has focus --
//     one card per press. The track is tabindex="0" so a keyboard user
//     can reach it at all; a scrolling region nobody can focus is a
//     region a keyboard cannot scroll.
//   * "Showing 1-4 of 20" under the heading, in an aria-live="polite"
//     region. It is updated once scrolling SETTLES, not on every frame,
//     so a screen reader hears where the row stopped rather than every
//     card that slid past on the way.
//
// Motion: with prefers-reduced-motion the buttons jump instead of
// gliding (the CSS turns smooth scrolling off for the same visitors).
//
// The pure arithmetic (which cards are showing, the label, where a
// button goes) is kept free of the DOM and exported under Node, the
// same arrangement as tour.js.
(function () {
  "use strict";

  // ---- 1. Pure helpers ------------------------------------------------

  // A card counts as SHOWING when at least `share` of its width is
  // inside the visible part of the track. On a phone the row shows
  // about 1.2 cards on purpose -- the sliver of the next one is the
  // hint that there is more -- and that sliver is not "showing" it.
  function visibleRange(view, items, share) {
    const need = share == null ? 0.6 : share;
    let first = -1;
    let last = -1;
    for (let i = 0; i < items.length; i += 1) {
      const width = items[i].right - items[i].left;
      if (width <= 0) continue;
      const shown = Math.min(items[i].right, view.right) - Math.max(items[i].left, view.left);
      if (shown / width >= need) {
        if (first < 0) first = i;
        last = i;
      }
    }
    return first < 0 ? null : { first: first, last: last };
  }

  function rangeLabel(range, total) {
    if (!range) return total + " industries";
    const from = range.first + 1;
    const to = range.last + 1;
    return from === to
      ? "Showing " + from + " of " + total
      : "Showing " + from + "–" + to + " of " + total;
  }

  // Where Previous / Next go: one screenful on. Next brings the first
  // card that is not showing yet to the start; Previous goes back by as
  // many cards as are showing now, so the two undo each other.
  function pageTarget(range, total, direction) {
    if (!range || total < 1) return 0;
    const perPage = Math.max(1, range.last - range.first + 1);
    if (direction > 0) return Math.min(total - 1, range.last + 1);
    return Math.max(0, range.first - perPage);
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { visibleRange: visibleRange, rangeLabel: rangeLabel, pageTarget: pageTarget };
    return;
  }

  // ---- 2. The slider ----------------------------------------------------

  function prefersReducedMotion() {
    return Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function init(root) {
    // Idempotent: a second run on the same slider (a script included
    // twice, a partial re-render) must not double every handler.
    if (root.dataset.sliderReady === "1") return;
    const track = root.querySelector("[data-industry-track]");
    if (!track) return;
    const items = Array.prototype.slice.call(track.querySelectorAll(".dss-industry-item"));
    if (!items.length) return;
    root.dataset.sliderReady = "1";

    const nav = root.querySelector("[data-industry-slider-nav]");
    const prev = root.querySelector("[data-industry-slider-prev]");
    const next = root.querySelector("[data-industry-slider-next]");
    const status = root.querySelector("[data-industry-slider-status]");

    // While a smooth scroll is still travelling, the cards on screen are
    // the ones it is passing, not the ones it is heading for. A second
    // press of Next in that moment pages on from the DESTINATION, so two
    // quick presses move two screenfuls instead of one and a half.
    let pending = null;
    let settleTimer = null;
    let frame = null;

    function startPadding() {
      return parseFloat(window.getComputedStyle(track).paddingLeft) || 0;
    }

    function currentRange() {
      const box = track.getBoundingClientRect();
      const pad = startPadding();
      const view = { left: box.left + pad, right: box.right - pad };
      const rects = items.map(function (item) {
        const r = item.getBoundingClientRect();
        return { left: r.left, right: r.right };
      });
      return visibleRange(view, rects);
    }

    function goTo(index) {
      const target = Math.max(0, Math.min(items.length - 1, index));
      const box = track.getBoundingClientRect();
      const max = track.scrollWidth - track.clientWidth;
      const wanted = track.scrollLeft + (items[target].getBoundingClientRect().left - box.left - startPadding());
      const left = Math.max(0, Math.min(max, wanted));
      if (Math.abs(left - track.scrollLeft) < 1) {
        // Nowhere to go (already there, or the row's end): no scroll
        // event will come to settle things, so settle them now.
        pending = null;
        refresh();
        return;
      }
      const reduce = prefersReducedMotion();
      // A jump lands at once; only a glide needs its destination kept.
      pending = reduce ? null : target;
      track.scrollTo({ left: left, behavior: reduce ? "auto" : "smooth" });
    }

    function baseRange() {
      const range = currentRange();
      if (pending == null || !range) return range;
      const perPage = range.last - range.first;
      return { first: pending, last: Math.min(items.length - 1, pending + perPage) };
    }

    function page(direction) {
      goTo(pageTarget(baseRange(), items.length, direction));
    }

    // A button that turns disabled while it has focus drops that focus
    // to <body> -- a keyboard user pressing Next to the end of the row
    // would be thrown back to the top of the page. Hand it to the other
    // button (or the track) first.
    function setDisabled(button, disabled, fallback) {
      if (!button || button.disabled === disabled) return;
      if (disabled && document.activeElement === button) {
        (fallback && !fallback.disabled ? fallback : track).focus();
      }
      button.disabled = disabled;
    }

    function refreshButtons() {
      frame = null;
      const max = track.scrollWidth - track.clientWidth;
      setDisabled(prev, track.scrollLeft <= 1, next);
      setDisabled(next, track.scrollLeft >= max - 1, prev);
    }

    function refreshStatus() {
      settleTimer = null;
      pending = null;
      if (!status) return;
      const text = rangeLabel(currentRange(), items.length);
      // Only a real change is written, so the live region does not
      // repeat itself when a scroll ends where it began.
      if (status.textContent.trim() !== text) status.textContent = text;
    }

    function refresh() {
      refreshButtons();
      refreshStatus();
    }

    function onScroll() {
      if (frame == null) frame = window.requestAnimationFrame(refreshButtons);
      if (settleTimer != null) window.clearTimeout(settleTimer);
      settleTimer = window.setTimeout(refreshStatus, 150);
    }

    if (prev) prev.addEventListener("click", function () { page(-1); });
    if (next) next.addEventListener("click", function () { page(1); });

    track.addEventListener("scroll", onScroll, { passive: true });
    track.addEventListener("keydown", function (event) {
      // Only on the track itself: a focused card link inside it keeps
      // the keys a link normally has.
      if (event.target !== track || event.altKey || event.ctrlKey || event.metaKey) return;
      const range = baseRange();
      const first = range ? range.first : 0;
      let target = null;
      if (event.key === "ArrowRight") target = first + 1;
      else if (event.key === "ArrowLeft") target = first - 1;
      else if (event.key === "Home") target = 0;
      else if (event.key === "End") target = items.length - 1;
      if (target == null) return;
      event.preventDefault();
      goTo(target);
    });

    if (typeof window.ResizeObserver === "function") {
      new window.ResizeObserver(function () { refresh(); }).observe(track);
    } else {
      window.addEventListener("resize", refresh);
    }

    if (nav) nav.hidden = false;
    refresh();
  }

  function initAll() {
    document.querySelectorAll("[data-industry-slider]").forEach(init);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAll);
  } else {
    initAll();
  }
})();
