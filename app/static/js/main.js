// app/static/js/main.js
// Shared behaviour used on every logged-in page: populates the
// notification bell dropdown from GET /api/notifications.

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
                      <span class="small">${n.message}</span>
                      <div class="text-muted" style="font-size:.7rem;">${n.created_at}</div>
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
