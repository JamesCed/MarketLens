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
