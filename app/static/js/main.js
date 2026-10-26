// app/static/js/main.js
// Shared behaviour used on every logged-in page: populates the
// notification bell dropdown from GET /api/notifications.

// Notification text is DATA, never markup. A notification can quote
// something a person typed -- a business plan's name, an admin's reason
// for an action -- so interpolating n.message into innerHTML raw was
// a stored-XSS hole: a name like <img src=x onerror=...> would run
// script in the moderator's browser the moment they opened the bell.
// Everything interpolated below goes through escapeHtml first.
function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

document.addEventListener("DOMContentLoaded", function () {
  const menu = document.getElementById("notifDropdown");
  if (!menu) return;

  menu.closest(".dropdown").addEventListener("show.bs.dropdown", function () {
    fetch("/api/notifications")
      .then((r) => r.json())
      .then((items) => {
        if (!items.length) {
          menu.innerHTML = '<div class="px-3 py-3 text-muted small text-center">No notifications yet.</div>';
          return;
        }
        menu.innerHTML = items
          .map((n) => {
            const icon = n.type === "early_warning" ? "bi-exclamation-triangle text-danger" : "bi-info-circle text-primary";
            return `<div class="dropdown-item-text py-2 border-bottom ${n.is_read ? "" : "bg-light"}">
                      <i class="bi ${icon} me-1"></i>
                      <span class="small">${escapeHtml(n.message)}</span>
                      <div class="text-muted" style="font-size:.7rem;">${escapeHtml(n.created_at)}</div>
                    </div>`;
          })
          .join("");
      })
      .catch(() => {
        menu.innerHTML = '<div class="px-3 py-2 text-danger small">Could not load notifications.</div>';
      });
  });
});


// ---------------------------------------------------------------------
// Mobile / tablet navigation
// ---------------------------------------------------------------------
// Below 900px the sidebar is off-canvas (see style.css). This is what
// opens and closes it. The CSS for the slide has been there all along;
// nothing ever toggled the class, so on a phone or tablet the menu was
// unreachable and the only navigable thing was the user dropdown.
document.addEventListener("DOMContentLoaded", function () {
  const toggle = document.getElementById("sidebarToggle");
  const sidebar = document.getElementById("dssSidebar");
  if (!toggle || !sidebar) return;

  // Created here rather than in the template: it exists only to serve
  // this behaviour, so if the script does not run there is no stray
  // overlay sitting on the page.
  const backdrop = document.createElement("button");
  backdrop.type = "button";
  backdrop.className = "dss-nav-backdrop";
  backdrop.setAttribute("aria-label", "Close menu");
  backdrop.hidden = true;
  document.body.appendChild(backdrop);

  function setOpen(open) {
    sidebar.classList.toggle("open", open);
    backdrop.hidden = !open;
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    // Stops the page behind scrolling under the open menu, which on a
    // phone reads as the menu itself having come loose.
    document.body.style.overflow = open ? "hidden" : "";
  }

  toggle.addEventListener("click", () => setOpen(!sidebar.classList.contains("open")));
  backdrop.addEventListener("click", () => setOpen(false));

  // Picking a destination closes the menu. Without this the new page
  // loads behind a menu that is still covering it.
  sidebar.querySelectorAll("a.dss-nav-link").forEach((link) => {
    link.addEventListener("click", () => setOpen(false));
  });

  // Support opens a modal rather than navigating, so the menu has to
  // get out of the way or the dialog appears behind it.
  sidebar.querySelectorAll(".dss-nav-support").forEach((button) => {
    button.addEventListener("click", () => setOpen(false));
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && sidebar.classList.contains("open")) setOpen(false);
  });

  // Widened past the breakpoint with the menu open: the sidebar is
  // static again, so the backdrop and the scroll lock have to go or
  // they linger over a desktop layout.
  window.matchMedia("(min-width: 901px)").addEventListener("change", (event) => {
    if (event.matches) setOpen(false);
  });
});

// ---------------------------------------------------------------------
// Flash toasts
// ---------------------------------------------------------------------
// The toast markup is in shared/_flash.html; this dismisses it. Not
// Bootstrap's alert component, because these are fixed-position toasts
// with their own entry/exit animation and a countdown bar -- wiring
// Bootstrap's dismiss to that would fight it rather than help.
//
// Errors are NOT auto-dismissed. A confirmation you missed costs you
// nothing; a "that password was wrong" that vanished before you looked
// up costs you the reason your login failed.
document.addEventListener("DOMContentLoaded", function () {
  const wrap = document.getElementById("flashWrap");
  if (!wrap) return;

  const AUTO_DISMISS_MS = 5000;

  function dismiss(toast) {
    if (toast.dataset.leaving) return;
    toast.dataset.leaving = "1";
    toast.classList.add("is-leaving");
    // Remove after the exit animation rather than on a fixed timer, so
    // the two can never drift apart.
    toast.addEventListener("animationend", () => {
      toast.remove();
      if (!wrap.querySelector(".dss-flash")) wrap.remove();
    }, { once: true });
  }

  wrap.querySelectorAll(".dss-flash").forEach((toast) => {
    toast.querySelector(".dss-flash-close")?.addEventListener("click", () => dismiss(toast));

    const permanent = toast.classList.contains("dss-flash-error");
    const timer = toast.querySelector(".dss-flash-timer");
    if (permanent) {
      // No countdown bar on something that is not counting down --
      // showing one that never empties would be a lie about the UI.
      timer?.remove();
      return;
    }

    let handle = setTimeout(() => dismiss(toast), AUTO_DISMISS_MS);

    // Hovering pauses it. Reading a message should not be a race.
    toast.addEventListener("mouseenter", () => {
      clearTimeout(handle);
      if (timer) timer.style.animationPlayState = "paused";
    });
    toast.addEventListener("mouseleave", () => {
      handle = setTimeout(() => dismiss(toast), 1200);
      if (timer) timer.style.animationPlayState = "running";
    });
  });
});


/* =====================================================================
   SHOW / HIDE PASSWORD
   =====================================================================
   Any `<button class="ml-reveal" data-target="<input id>">` toggles that
   field between password and text, and swaps its eye icon.

   THIS LIVES HERE, ONCE, ON PURPOSE.

   It used to be copy-pasted into the bottom of login.html and
   register.html -- two identical listeners, each with a comment saying
   it was "delegated, so the same handler serves this page and the
   registration form". Delegation only helps within a page, so the third
   page to grow reveal buttons got the markup and no handler at all: on
   the password-reset form both eye buttons rendered, looked live, and
   did nothing when clicked.

   main.js is already loaded by base.html on every page, before
   {% block extra_scripts %}, so binding here means a reveal button
   works the moment somebody adds one -- which is the property the
   duplicated version was reaching for and could not have.

   Revealing costs nothing in security: the value is already in the DOM,
   and anyone who can read it can read it either way. What it buys is
   not retyping a long password on a phone keyboard. */
document.addEventListener("click", (event) => {
  const button = event.target.closest(".ml-reveal");
  if (!button) return;

  const field = document.getElementById(button.dataset.target);
  if (!field) return;

  const show = field.type === "password";
  field.type = show ? "text" : "password";

  const icon = button.querySelector("i");
  if (icon) icon.className = show ? "bi bi-eye-slash" : "bi bi-eye";
  button.setAttribute("aria-label", show ? "Hide password" : "Show password");
});


/* =====================================================================
   PAGE LOADER
   =====================================================================
   Shows the rotating circle (shared/_page_loader.html) while the next
   page is on its way, so a click never looks like it did nothing.

   - Same-site link clicks, normal form submits and script navigations
     (window.location.assign, form.submit(), the tour moving between
     pages) all show it. Script navigations are caught by beforeunload.
   - On a computer it appears only after LOADER_DELAY_MS, so a page that
     arrives quickly never flashes it. On a phone or tablet (a touch
     screen, or a window 900px wide or less) it appears at once: mobile
     browsers can stop drawing the old page as soon as the next one is
     requested, so a delayed circle often never got drawn at all, and
     iPhone Safari does not reliably fire beforeunload.
   - Downloads (CSV exports, dataset templates) do not unload the page,
     so they never show it; a link can also opt out with data-no-loader.
   - Coming back with the browser's Back button restores the page from
     cache with the loader still up, so pageshow hides it; a failsafe
     hides it if nothing has happened after FAILSAFE_MS. */
(function () {
  const loader = document.getElementById("dssPageLoader");
  if (!loader) return;

  const MOBILE = window.matchMedia("(max-width: 900px), (pointer: coarse)").matches;
  const LOADER_DELAY_MS = MOBILE ? 0 : 120;
  const FAILSAFE_MS = 25000;
  const DOWNLOAD_RE = /\.(csv|xlsx?|pdf|zip)(\?|#|$)|\/export|dataset-template/i;
  let showTimer = null;
  let failsafe = null;
  let suppressUntil = 0;

  function hide() {
    clearTimeout(showTimer);
    clearTimeout(failsafe);
    loader.hidden = true;
    document.documentElement.removeAttribute("aria-busy");
  }

  function reveal() {
    loader.hidden = false;
    document.documentElement.setAttribute("aria-busy", "true");
  }

  function show() {
    if (Date.now() < suppressUntil) return;
    clearTimeout(showTimer);
    if (LOADER_DELAY_MS > 0) {
      showTimer = setTimeout(reveal, LOADER_DELAY_MS);
    } else {
      reveal();
    }
    clearTimeout(failsafe);
    failsafe = setTimeout(hide, FAILSAFE_MS);
  }

  // A download keeps the page, so the beforeunload it may trigger must
  // not raise the loader.
  function suppress() { suppressUntil = Date.now() + 4000; }

  window.dssShowLoader = show;
  window.dssHideLoader = hide;

  document.addEventListener("click", (event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest("a[href]");
    if (!link) return;
    const raw = link.getAttribute("href") || "";
    if (!raw || raw.startsWith("#") || /^(javascript|mailto|tel):/i.test(raw)) return;
    if (link.target && link.target !== "_self") return;
    if (link.hasAttribute("download") || link.hasAttribute("data-no-loader")) { suppress(); return; }
    if (link.hasAttribute("data-bs-toggle")) return;
    let url;
    try { url = new URL(link.href, window.location.href); } catch (_e) { return; }
    if (url.origin !== window.location.origin) return;
    if (DOWNLOAD_RE.test(url.pathname + url.search)) { suppress(); return; }
    if (url.pathname === window.location.pathname && url.search === window.location.search && url.hash) return;
    if (event.defaultPrevented) return;
    // On a phone, raise it right now, inside the tap, before the browser
    // starts the request; take it down again if a later handler cancels
    // the navigation.
    if (MOBILE) {
      show();
      setTimeout(() => { if (event.defaultPrevented) hide(); }, 0);
      return;
    }
    // Let every other click handler run first; one that cancels the
    // navigation (a confirm, an in-page tab) cancels the loader too.
    setTimeout(() => { if (!event.defaultPrevented) show(); }, 0);
  });

  function formNavigates(form) {
    if (!(form instanceof HTMLFormElement)) return false;
    if (form.hasAttribute("data-no-loader") || (form.target && form.target !== "_self")) return false;
    if (DOWNLOAD_RE.test(form.getAttribute("action") || "")) { suppress(); return false; }
    return true;
  }

  document.addEventListener("submit", (event) => {
    if (MOBILE && !event.defaultPrevented && formNavigates(event.target)) {
      show();
      setTimeout(() => { if (event.defaultPrevented) hide(); }, 0);
      return;
    }
    setTimeout(() => {
      if (event.defaultPrevented) return;
      if (formNavigates(event.target)) show();
    }, 0);
  });

  window.addEventListener("beforeunload", show);
  window.addEventListener("pageshow", hide);
  // Leaving for another app or tab is not leaving the page: if a phone
  // comes back to it without a reload, the circle must not still be up.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !loader.hidden) setTimeout(hide, 600);
  });
})();


/* =====================================================================
   PASSWORD CHECKLIST
   =====================================================================
   Ticks off each part of the password rule (shared/_password_rules.html)
   as it is met. The server checks the same five rules regardless. */
(function () {
  const TESTS = {
    length: (p) => p.length >= 8,
    upper: (p) => /[A-Z]/.test(p),
    lower: (p) => /[a-z]/.test(p),
    number: (p) => /\d/.test(p),
    special: (p) => /[^A-Za-z0-9\s]/.test(p),
  };
  document.querySelectorAll("[data-pw-rules-for]").forEach((list) => {
    const field = document.getElementById(list.dataset.pwRulesFor);
    if (!field) return;
    const update = () => {
      const value = field.value || "";
      let allMet = true;
      list.querySelectorAll("[data-rule]").forEach((item) => {
        const met = (TESTS[item.dataset.rule] || (() => true))(value);
        allMet = allMet && met;
        item.classList.toggle("is-met", met);
        const icon = item.querySelector("i");
        if (icon) icon.className = met ? "bi bi-check-circle-fill" : "bi bi-circle";
      });
      // Lets the browser block the submit with a message, before the
      // round trip, when the rule is not met yet.
      field.setCustomValidity(value && !allMet ? "Your password does not meet every rule listed below it." : "");
    };
    field.addEventListener("input", update);
    update();
  });
})();


/* =====================================================================
   REQUIRED FIELDS GET A RED ASTERISK
   =====================================================================
   IT expert review: "always put a red * on required fields". Every
   label whose control is `required` gets .is-required (style.css draws
   the asterisk), and it follows the attribute when a script toggles it
   -- the sign-up wizard marks fields required step by step. */
(function () {
  function labelsFor(control) {
    // A radio group inside a fieldset is marked once, on its legend, not
    // after whichever option happens to carry the attribute.
    if (control.type === "radio") {
      const legend = control.closest("fieldset")?.querySelector("legend");
      if (legend) return [legend];
    }
    const found = control.labels ? [...control.labels] : [];
    return found.filter((label) => !label.classList.contains("btn"));
  }
  function sync(control) {
    labelsFor(control).forEach((label) => label.classList.toggle("is-required", control.required));
  }
  function syncAll(root) {
    root.querySelectorAll("input, select, textarea").forEach(sync);
  }
  syncAll(document);
  new MutationObserver((mutations) => {
    mutations.forEach((m) => {
      if (m.type === "attributes" && m.target.matches && m.target.matches("input, select, textarea")) sync(m.target);
      if (m.type === "childList") m.addedNodes.forEach((n) => { if (n.querySelectorAll) syncAll(n); });
    });
  }).observe(document.body, { attributes: true, attributeFilter: ["required"], childList: true, subtree: true });
})();
