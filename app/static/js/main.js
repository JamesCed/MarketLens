// app/static/js/main.js
// Shared behaviour used on every logged-in page: populates the
// notification bell dropdown from GET /api/notifications.

// Notification text is DATA, never markup. A notification can quote
// something a person typed -- a forum post title, a moderator's reason
// for rejecting it -- so interpolating n.message into innerHTML raw was
// a stored-XSS hole: a post titled <img src=x onerror=...> would run
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
