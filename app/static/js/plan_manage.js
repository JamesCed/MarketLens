// app/static/js/plan_manage.js
// ---------------------------------------------------------------------
// Managing plans on the Home page: the Edit dialog on each plan chip
// (pencil icon) and the Move-to-Trash confirmation (bin icon).
//
// Both are PROGRESSIVE ENHANCEMENT over real forms. Every Edit dialog is
// a server-rendered <form> posting to /home/plans/<id>/update, and every
// bin icon sits in a <form> posting to /home/plans/<id>/trash, so with
// scripting off both still work -- the server flashes a message and
// redirects back to Home. This script only adds what scripting buys:
//
//   * Edit: the form is posted over fetch() with `Accept:
//     application/json`. A validation error ("Capital is required.")
//     comes back as JSON and is shown INSIDE the dialog, as text, with
//     everything the owner typed still in place -- the no-JS path has to
//     redirect, which loses it. On success the page reloads onto the
//     edited plan (/home?plan=<id>), because every panel on Home -- the
//     score, the map, the forecast, the quarterly chart -- depends on the
//     plan; repainting just the chip would leave the rest stale.
//
//   * Trash: the bin's form submit is intercepted and the shared
//     #planTrashConfirmModal asks first, naming the plan. Confirming
//     posts an ordinary form to the same URL, so the redirect and the
//     "moved to Trash" flash are exactly the no-JS ones.
//
// Plan names are typed by their owners, so they only ever reach the
// page through textContent, never innerHTML.
(function () {
  "use strict";

  function csrfToken(form) {
    const field = form && form.querySelector('input[name="csrf_token"]');
    if (field && field.value) return field.value;
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  // ---- Edit -----------------------------------------------------------
  function showError(form, message) {
    const box = form.querySelector("[data-plan-form-error]");
    if (!box) {
      window.alert(message);
      return;
    }
    box.textContent = message;
    box.hidden = false;
    // Bring the message into view: the dialog body scrolls, and the
    // error sits at its top, above the field the owner just changed.
    box.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function clearError(form) {
    const box = form.querySelector("[data-plan-form-error]");
    if (box) {
      box.textContent = "";
      box.hidden = true;
    }
  }

  function setBusy(form, busy) {
    const button = form.querySelector("[data-plan-save]");
    if (!button) return;
    button.disabled = busy;
    if (busy) {
      button.dataset.label = button.innerHTML;
      // Static markup only -- nothing user-supplied goes in here.
      button.innerHTML =
        '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span> Saving and re-forecasting…';
    } else if (button.dataset.label) {
      button.innerHTML = button.dataset.label;
    }
  }

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-plan-edit-form]");
    if (!form || typeof window.fetch !== "function") return;
    event.preventDefault();
    clearError(form);
    setBusy(form, true);

    const planId = form.dataset.planId;
    fetch(form.action, {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "X-CSRFToken": csrfToken(form) },
      body: new FormData(form),
    })
      .then((response) =>
        response
          .json()
          .catch(() => ({ success: false, error: "The server sent an unexpected reply." }))
          .then((data) => ({ ok: response.ok, data: data || {} })),
      )
      .then(({ ok, data }) => {
        if (ok && data.success) {
          window.location.assign(data.redirect || "/home?plan=" + encodeURIComponent(planId));
          return;
        }
        setBusy(form, false);
        showError(form, data.error || "Could not save this plan. Please check the fields and try again.");
      })
      .catch(() => {
        setBusy(form, false);
        showError(form, "Could not save this plan right now. Check your connection and try again.");
      });
  });

  // A dialog reopened after a failed save should not still show the
  // old error above corrected fields.
  document.addEventListener("hidden.bs.modal", (event) => {
    const form = event.target.querySelector && event.target.querySelector("form[data-plan-edit-form]");
    if (form) clearError(form);
  });

  // ---- Move to Trash --------------------------------------------------
  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-plan-trash-form]");
    if (!form) return;
    const modalEl = document.getElementById("planTrashConfirmModal");
    if (!modalEl || !window.bootstrap) return; // no dialog: the plain post goes ahead
    event.preventDefault();

    const confirmForm = modalEl.querySelector("form[data-plan-trash-confirm-form]");
    const nameEl = modalEl.querySelector("[data-plan-trash-name]");
    if (!confirmForm) return;
    confirmForm.action = form.action;
    if (nameEl) nameEl.textContent = form.dataset.planName || "this plan";
    window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
  });
})();
