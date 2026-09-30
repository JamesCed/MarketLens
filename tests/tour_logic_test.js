// tests/tour_logic_test.js
// -----------------------------------------------------------------
// The first-time walkthrough's pure logic (app/static/js/tour.js,
// section 1) and the shape of its step lists (tour_steps.js), without a
// browser. Run with:
//
//     node tests/tour_logic_test.js
//
// tests/test_onboarding.py runs this too when Node is installed.
//
// What is worth pinning down here is exactly what a browser test would
// struggle to make fail on purpose:
//   * the step index survives a page load, is per user, and a corrupt
//     or unavailable sessionStorage never throws;
//   * a step whose element is missing, hidden or matched by a selector
//     the browser cannot parse falls back to "no target" (a centred
//     card) instead of breaking the tour;
//   * the card is always placed fully on screen.
const assert = require("assert");
const path = require("path");

const tour = require(path.join(__dirname, "..", "app", "static", "js", "tour.js"));
const { steps: STEPS, pages: PAGES } = require(path.join(__dirname, "..", "app", "static", "js", "tour_steps.js"));

let passed = 0;
function test(name, fn) {
  try {
    fn();
    passed += 1;
  } catch (err) {
    console.error("FAIL: " + name);
    throw err;
  }
}

// ---------------------------------------------------------------
// Fakes
// ---------------------------------------------------------------
function fakeStorage() {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, String(v)),
    removeItem: (k) => data.delete(k),
    _data: data,
  };
}

function throwingStorage() {
  const boom = () => { throw new Error("SecurityError: storage disabled"); };
  return { getItem: boom, setItem: boom, removeItem: boom };
}

// A tiny DOM: each element lists the simple selectors it matches, knows
// its parent, and implements the three methods tour.js calls.
function el(sels, opts) {
  const o = opts || {};
  const node = {
    sels: sels,
    parentElement: o.parent || null,
    isConnected: o.connected !== false,
    rect: o.rect || { top: 100, left: 100, width: 200, height: 50 },
    rects: o.hidden ? 0 : 1,
    matchesOne(sel) { return this.sels.indexOf(sel.trim()) !== -1; },
    closest(selector) {
      const options = selector.split(",");
      for (let n = this; n; n = n.parentElement) {
        if (options.some((s) => n.matchesOne(s))) return n;
      }
      return null;
    },
    contains(other) {
      for (let n = other; n; n = n.parentElement) if (n === this) return true;
      return false;
    },
    getClientRects() { return new Array(this.rects); },
    getBoundingClientRect() {
      const r = this.rect;
      return { top: r.top, left: r.left, width: r.width, height: r.height,
        right: r.left + r.width, bottom: r.top + r.height };
    },
  };
  return node;
}

function fakeDoc(map) {
  return {
    querySelectorAll(selector) {
      if (selector.indexOf(":has(") !== -1) throw new SyntaxError("unsupported selector");
      return map[selector] || [];
    },
  };
}

const fakeWin = { innerWidth: 1280, innerHeight: 800, getComputedStyle: () => ({ visibility: "visible", display: "block" }) };
const visible = (e) => tour.isVisible(e, fakeWin);

// ---------------------------------------------------------------
// 1. Step index persistence
// ---------------------------------------------------------------
test("progress round-trips and is keyed by user", () => {
  const s = fakeStorage();
  assert.strictEqual(tour.readProgress(s, 7), null, "nothing saved yet");
  assert.strictEqual(tour.writeProgress(s, 7, 4), true);
  assert.strictEqual(tour.readProgress(s, 7), 4);
  assert.ok(s._data.has("dssTour:7"), "stored under dssTour:<userId>");
  assert.strictEqual(tour.readProgress(s, 8), null, "another user on the same browser starts clean");
  tour.clearProgress(s, 7);
  assert.strictEqual(tour.readProgress(s, 7), null, "cleared on finish/exit");
});

test("corrupt or nonsense progress is ignored, not trusted", () => {
  const s = fakeStorage();
  for (const bad of ["{not json", '{"index":-1}', '{"index":2.5}', '{"index":"x"}', "null", "[]"]) {
    s.setItem("dssTour:1", bad);
    assert.strictEqual(tour.readProgress(s, 1), null, "should ignore " + bad);
  }
});

test("unavailable storage never throws", () => {
  const s = throwingStorage();
  assert.strictEqual(tour.readProgress(s, 1), null);
  assert.strictEqual(tour.writeProgress(s, 1, 3), false);
  assert.doesNotThrow(() => tour.clearProgress(s, 1));
});

test("resuming picks the saved step, clamped, and knows whether it is on this page", () => {
  const steps = [{ page: "/home" }, { page: "/home" }, { page: "/community/" }, { page: "/settings" }];
  assert.deepStrictEqual(tour.resolveStart(null, steps, "/home", ""), { index: 0, onPage: true });
  assert.deepStrictEqual(tour.resolveStart(2, steps, "/community", ""), { index: 2, onPage: true },
    "trailing slash does not matter");
  assert.deepStrictEqual(tour.resolveStart(2, steps, "/home", ""), { index: 2, onPage: false },
    "saved step is elsewhere -> offer to go there, do not yank");
  assert.deepStrictEqual(tour.resolveStart(99, steps, "/settings", ""), { index: 3, onPage: true },
    "a shorter step list since last time clamps to the last step");
  assert.strictEqual(tour.pageMatches("/app/home", "/home", "/app"), true, "mounted under a prefix");
  assert.strictEqual(tour.pageMatches("/settings?section=plans#x", "/settings", ""), true, "query/hash ignored");
  assert.strictEqual(tour.pageMatches("/home", "/homework", ""), false);
});

test("steps for a role, and nothing for an unknown one", () => {
  assert.strictEqual(tour.stepsForRole(STEPS, "SME"), STEPS.SME);
  assert.deepStrictEqual(tour.stepsForRole(STEPS, "Guest"), []);
  assert.deepStrictEqual(tour.stepsForRole(undefined, "SME"), []);
});

// ---------------------------------------------------------------
// 2. Missing-target fallback
// ---------------------------------------------------------------
test("a missing target resolves to null (centred card), never throws", () => {
  const doc = fakeDoc({});
  assert.strictEqual(tour.findTarget(doc, { target: '[data-tour="nowhere"]' }, visible), null);
  assert.strictEqual(tour.findTarget(doc, {}, visible), null, "a step with no target at all");
});

test("an unparseable selector is skipped and the next candidate used", () => {
  const card = el([".dss-card"]);
  const doc = fakeDoc({ "#monthlyChart": [card] });
  const found = tour.findTarget(doc, { target: [".dss-card:has(#monthlyChart)", "#monthlyChart"] }, visible);
  assert.strictEqual(found, card);
});

test("hidden or off-canvas matches are passed over for a visible one", () => {
  const hidden = el(["#addBtn"], { hidden: true });
  const offCanvas = el(["#addBtn"], { rect: { top: 50, left: -230, width: 200, height: 40 } });
  const shown = el(["#addBtn"]);
  const doc = fakeDoc({ "#addBtn": [hidden, offCanvas, shown] });
  assert.strictEqual(tour.findTarget(doc, { target: "#addBtn" }, visible), shown);
  const onlyHidden = fakeDoc({ "#addBtn": [hidden, offCanvas] });
  assert.strictEqual(tour.findTarget(onlyHidden, { target: "#addBtn" }, visible), null);
});

test("`closest` widens the spotlight to the enclosing card", () => {
  const card = el([".dss-card"]);
  const canvas = el(["#monthlyChart"], { parent: card });
  const doc = fakeDoc({ "#monthlyChart": [canvas] });
  assert.strictEqual(tour.findTarget(doc, { target: "#monthlyChart", closest: ".dss-card" }, visible), card);
  const loose = fakeDoc({ "#monthlyChart": [el(["#monthlyChart"])] });
  assert.ok(tour.findTarget(loose, { target: "#monthlyChart", closest: ".dss-card" }, visible),
    "no such ancestor -> keep the element itself");
});

test("isVisible rejects detached, zero-size and hidden elements", () => {
  assert.strictEqual(visible(el(["#a"])), true);
  assert.strictEqual(visible(el(["#a"], { connected: false })), false);
  assert.strictEqual(visible(el(["#a"], { rect: { top: 0, left: 0, width: 0, height: 0 } })), false);
  const styled = { innerWidth: 1280, getComputedStyle: () => ({ visibility: "hidden", display: "block" }) };
  assert.strictEqual(tour.isVisible(el(["#a"]), styled), false);
  assert.strictEqual(visible(null), false);
});

// ---------------------------------------------------------------
// 3. "Try it" matching
// ---------------------------------------------------------------
test("a click on a barangay counts; a click on the empty map does not", () => {
  const map = el(["#dss-map"]);
  const pane = el([".leaflet-pane"], { parent: map });
  const barangay = el([".leaflet-interactive"], { parent: pane });
  const on = { selector: "#dss-map, #locationList", event: "click", match: ".leaflet-interactive, .dss-loc-item" };
  assert.strictEqual(tour.eventMatches(barangay, on), true);
  assert.strictEqual(tour.eventMatches(pane, on), false, "map background");
  const list = el(["#locationList"]);
  const item = el([".dss-loc-item"], { parent: list });
  assert.strictEqual(tour.eventMatches(item, on), true, "the barangay list counts too");
  assert.strictEqual(tour.eventMatches(el(["body"]), on), false, "elsewhere on the page");
});

test("a change inside a wrapper counts (the select sits inside the anchor)", () => {
  const wrapper = el(['[data-tour="industry-select"]']);
  const select = el(["select"], { parent: wrapper });
  assert.strictEqual(tour.eventMatches(select, { selector: '[data-tour="industry-select"]', event: "change" }), true);
  assert.strictEqual(tour.eventMatches({ nodeType: 3, parentElement: select },
    { selector: '[data-tour="industry-select"]', event: "change" }), true, "text-node targets");
});

// ---------------------------------------------------------------
// 4. Placement: the card is always fully on screen
// ---------------------------------------------------------------
const VIEW = { width: 1280, height: 800 };
const CARD = { width: 420, height: 320 };
function onScreen(p) {
  assert.ok(p.left >= 16 && p.left + CARD.width <= VIEW.width - 16 + 0.001, "horizontally on screen: " + JSON.stringify(p));
  assert.ok(p.top >= 16 && p.top + CARD.height <= VIEW.height - 16 + 0.001, "vertically on screen: " + JSON.stringify(p));
}

test("no target -> centred", () => {
  const p = tour.computePlacement(null, CARD, VIEW);
  assert.strictEqual(p.placement, "center");
  assert.strictEqual(p.left, (VIEW.width - CARD.width) / 2);
  onScreen(p);
});

test("beside the target when there is room, on the side that fits", () => {
  const sidebarLink = { top: 120, left: 10, width: 220, height: 40 };
  const right = tour.computePlacement(sidebarLink, CARD, VIEW);
  assert.strictEqual(right.placement, "right");
  onScreen(right);

  const detailPanel = { top: 100, left: 900, width: 360, height: 300 };
  const left = tour.computePlacement(detailPanel, CARD, VIEW);
  assert.strictEqual(left.placement, "left");
  onScreen(left);

  const searchBar = { top: 80, left: 200, width: 880, height: 60 };
  const below = tour.computePlacement(searchBar, CARD, VIEW);
  assert.strictEqual(below.placement, "bottom");
  onScreen(below);

  const pref = tour.computePlacement(sidebarLink, CARD, VIEW, "bottom");
  assert.strictEqual(pref.placement, "bottom", "the step's preferred side wins when it fits");
});

test("a target that fills the screen -> corner, still on screen", () => {
  const huge = { top: -40, left: 20, width: 1240, height: 1200 };
  const p = tour.computePlacement(huge, CARD, VIEW);
  assert.strictEqual(p.placement, "corner");
  onScreen(p);
});

test("a narrow phone screen docks the card at the bottom", () => {
  assert.strictEqual(tour.computePlacement({ top: 0, left: 0, width: 10, height: 10 }, CARD, { width: 390, height: 844 }).placement, "dock");
  assert.strictEqual(tour.computePlacement(null, CARD, { width: 639, height: 800 }).placement, "dock");
});

// ---------------------------------------------------------------
// 5. The step lists themselves
// ---------------------------------------------------------------
test("every role has a complete, well-formed tour", () => {
  assert.deepStrictEqual(Object.keys(STEPS).sort(), ["Admin", "LGU", "SME"]);
  for (const role of Object.keys(STEPS)) {
    const list = STEPS[role];
    assert.ok(list.length >= 8, role + " tour is too short to be a walkthrough");
    const ids = new Set();
    list.forEach((step, i) => {
      const where = role + " step " + (i + 1) + " (" + step.id + ")";
      assert.ok(step.id && !ids.has(step.id), where + ": missing or duplicate id");
      ids.add(step.id);
      assert.ok(typeof step.page === "string" && step.page.startsWith("/"), where + ": needs a page path");
      assert.ok(PAGES[step.page], where + ": page has no friendly name in PAGES");
      assert.ok(step.title && step.what, where + ": needs a title and a 'what this is for'");
      if (step.advanceOn) {
        assert.ok(step.advanceOn.selector && step.advanceOn.event, where + ": advanceOn needs selector + event");
        assert.ok(/^Try it:/.test(step.tryIt || ""), where + ": interactive step needs a 'Try it:' instruction");
        assert.ok(/^Nice!/.test(step.success || ""), where + ": interactive step needs a 'Nice!' confirmation");
      }
    });
    assert.strictEqual(list[0].target, undefined, role + ": the welcome step is a centred card");
    assert.strictEqual(list[list.length - 1].finish, true, role + ": the last step finishes the tour");
    assert.ok(/Take the tour/.test(list[list.length - 1].how), role + ": the end says how to replay");
    assert.ok(list.some((s) => s.advanceOn), role + ": a trial has at least one hands-on step");
  }
});

test("the SME tour covers the Home page anchors the Home page provides", () => {
  const anchors = ["plan-selector", "search", "location-picker", "industry-select", "clear-search",
    "industry-cards", "saved-plans", "add-plan", "mini-map", "forecast-panel", "recommendation-summary"];
  const targets = STEPS.SME.map((s) => [].concat(s.target || []).join(" "));
  anchors.forEach((a) => {
    assert.ok(targets.some((t) => t.indexOf('[data-tour="' + a + '"]') !== -1), "no SME step targets " + a);
  });
  const pages = new Set(STEPS.SME.map((s) => s.page));
  ["/home", "/saturation-map", "/trend-reports", "/recommendations", "/community/", "/settings"].forEach((p) => {
    assert.ok(pages.has(p), "SME tour never visits " + p);
  });
});

console.log("tour_logic_test.js: " + passed + " passed");
