/* =====================================================================
   ADMIN: the shared "give a reason" dialog
   =====================================================================
   templates/admin/_reason_modal.html is one dialog reused by every
   suspend / activate / archive / restore button. The button that opened
   it carries the details in data- attributes; they are copied in here
   as the dialog opens, so the same markup serves every row.

   Tone is a fixed list rather than a free class name, so nothing in a
   data attribute can inject arbitrary classes onto the submit button. */
document.addEventListener("DOMContentLoaded", function () {
  const modal = document.getElementById("reasonModal");
  if (!modal) return;

  const form = modal.querySelector("[data-reason-form]");
  const title = modal.querySelector("[data-reason-title]");
  const subject = modal.querySelector("[data-reason-subject]");
  const note = modal.querySelector("[data-reason-note]");
  const submit = modal.querySelector("[data-reason-submit]");
  const field = modal.querySelector("textarea[name='reason']");
  const tones = { danger: "btn-danger", warning: "btn-warning", primary: "btn-primary", success: "btn-success" };

  modal.addEventListener("show.bs.modal", function (event) {
    const trigger = event.relatedTarget;
    if (!trigger) return;
    const data = trigger.dataset;

    form.action = data.action || "#";
    title.textContent = data.title || "Give a reason";
    subject.textContent = data.subject || "";
    note.textContent = data.note || "";
    note.hidden = !data.note;
    submit.textContent = data.verb || "Confirm";
    submit.className = "btn " + (tones[data.tone] || "btn-primary");
    field.value = "";
    // The suspension-length picker: shown (and submitted) only for a
    // Suspend button; disabled otherwise so it is never posted.
    const duration = modal.querySelector("[data-reason-duration]");
    if (duration) {
      const ask = data.askDuration === "1";
      duration.hidden = !ask;
      duration.querySelector("select").disabled = !ask;
    }
  });

  modal.addEventListener("shown.bs.modal", function () {
    field.focus();
  });

  // Stop a double click from sending the same archive twice.
  form.addEventListener("submit", function () {
    submit.disabled = true;
  });
  modal.addEventListener("hidden.bs.modal", function () {
    submit.disabled = false;
  });
});
