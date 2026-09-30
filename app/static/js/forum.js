// app/static/js/forum.js
// Community forum pages. Everything here is an enhancement: every form
// still works with JavaScript off (Helpful posts back and reloads, the
// counters simply do not appear), because the server is what decides
// and validates. This file only makes it feel quicker.

(function () {
  "use strict";

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  // -------------------------------------------------------------------
  // Character counters
  // -------------------------------------------------------------------
  // A field with data-forum-count="<id>" writes "123 / 5000" into the
  // element with that id. Always shown rather than only near the limit:
  // someone who does not know there IS a limit should not find out by
  // hitting it. Turns amber in the last tenth.
  document.querySelectorAll("[data-forum-count]").forEach(function (field) {
    const out = document.getElementById(field.getAttribute("data-forum-count"));
    const max = parseInt(field.getAttribute("maxlength"), 10);
    if (!out || !max) return;
    function update() {
      const used = field.value.length;
      out.textContent = used + " / " + max;
      out.classList.toggle("is-near-limit", used > max * 0.9);
    }
    field.addEventListener("input", update);
    update();
  });

  // -------------------------------------------------------------------
  // Helpful
  // -------------------------------------------------------------------
  // Posted with fetch so the page does not jump back to the top. On any
  // failure the form is submitted normally instead, which reloads with
  // the server's answer -- the mark is never silently lost.
  document.querySelectorAll("form[data-forum-helpful]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.fetch) return;
      event.preventDefault();
      const button = form.querySelector("button");
      button.disabled = true;
      fetch(form.action, {
        method: "POST",
        headers: { "Accept": "application/json", "X-CSRFToken": csrfToken() },
        credentials: "same-origin",
      })
        .then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          return response.json();
        })
        .then(function (data) {
          const count = form.querySelector("[data-forum-helpful-count]");
          if (count) count.textContent = data.count;
          button.classList.toggle("is-marked", !!data.marked);
          button.setAttribute("aria-pressed", data.marked ? "true" : "false");
          const icon = button.querySelector(".bi");
          if (icon) {
            icon.classList.toggle("bi-hand-thumbs-up-fill", !!data.marked);
            icon.classList.toggle("bi-hand-thumbs-up", !data.marked);
          }
          button.disabled = false;
        })
        .catch(function () {
          button.disabled = false;
          form.submit();
        });
    });
  });

  // -------------------------------------------------------------------
  // Report dialog
  // -------------------------------------------------------------------
  const reportModal = document.getElementById("forumReportModal");
  if (reportModal) {
    const form = reportModal.querySelector("[data-forum-report-form]");
    const kind = reportModal.querySelector("[data-forum-report-kind]");
    const note = reportModal.querySelector("#reportNote");
    const hint = reportModal.querySelector("[data-forum-note-hint]");

    reportModal.addEventListener("show.bs.modal", function (event) {
      const trigger = event.relatedTarget;
      if (!trigger) return;
      form.reset();
      form.action = trigger.getAttribute("data-action");
      kind.textContent = trigger.getAttribute("data-kind") === "comment" ? "this comment" : "this post";
      note.required = false;
      hint.textContent = "(optional)";
    });

    // "Other" needs a word of explanation, or a moderator has nothing
    // to go on. The server enforces the same rule.
    form.addEventListener("change", function (event) {
      if (event.target.name !== "reason") return;
      const other = event.target.value === "other";
      note.required = other;
      note.minLength = other ? 5 : 0;
      hint.textContent = other ? "(please tell us briefly)" : "(optional)";
    });
  }

  // -------------------------------------------------------------------
  // Moderator reason dialog (Reject / Remove)
  // -------------------------------------------------------------------
  const reasonModal = document.getElementById("forumReasonModal");
  if (reasonModal) {
    const form = reasonModal.querySelector("[data-forum-reason-form]");
    reasonModal.addEventListener("show.bs.modal", function (event) {
      const trigger = event.relatedTarget;
      if (!trigger) return;
      form.reset();
      form.action = trigger.getAttribute("data-action");
      // textContent, never innerHTML: the subject is a member's title.
      reasonModal.querySelector("[data-forum-reason-title]").textContent = trigger.getAttribute("data-title") || "Give a reason";
      reasonModal.querySelector("[data-forum-reason-subject]").textContent = trigger.getAttribute("data-subject") || "";
      reasonModal.querySelector("[data-forum-reason-submit]").textContent = trigger.getAttribute("data-verb") || "Confirm";
    });
    reasonModal.addEventListener("shown.bs.modal", function () {
      reasonModal.querySelector("textarea").focus();
    });
  }

  // -------------------------------------------------------------------
  // Moderation tabs remember themselves in the URL, so a reload (and
  // the redirect after an action) lands on the same queue.
  // -------------------------------------------------------------------
  document.querySelectorAll('.forum-mod-tabs [data-bs-toggle="tab"]').forEach(function (tab) {
    tab.addEventListener("shown.bs.tab", function () {
      const key = tab.id.replace("tab-", "");
      const url = new URL(window.location.href);
      url.searchParams.set("tab", key);
      window.history.replaceState(null, "", url);
      // The reason dialog returns to the tab it was opened from.
      const next = document.querySelector('[data-forum-reason-form] input[name="next"]');
      if (next) next.value = url.pathname + url.search;
      document.querySelectorAll('.forum-mod-item input[name="next"]').forEach(function (input) {
        input.value = url.pathname + url.search;
      });
    });
  });
})();
