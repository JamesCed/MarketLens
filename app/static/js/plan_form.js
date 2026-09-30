// app/static/js/plan_form.js
// ---------------------------------------------------------------------
// Behaviour for the broader business-parameter fields in
// shared/_plan_fields.html, wherever they appear: the sign-up wizard,
// the Home page's Add New Plan dialog, and each Edit form in Settings.
//
//   * The sub-category list follows the chosen industry. Food and
//     Beverage offers Bakery / Coffee Shop / Milk Tea ...; Construction
//     offers General Contractor / Electrical ...; the list comes from
//     window.DSS_SUBCATEGORIES, which the page renders from
//     app/ml/subcategories.py -- the same table the server validates
//     against, so the two cannot disagree.
//   * The hint under the list shows the chosen sub-category's examples,
//     so an owner who does not know the word "sub-category" can still
//     see which one they are.
//   * The optional price list grows and shrinks a row at a time.
//
// Bound by delegation on the document, so a form that appears later
// (a modal, an Edit form revealed on click) works without re-binding.
(function () {
  "use strict";

  const DEFAULT_HINT =
    "This lets the system count your <strong>direct</strong> competitors &mdash; the " +
    "businesses selling what you sell &mdash; not the whole industry.";

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function catalogue() {
    return window.DSS_SUBCATEGORIES || {};
  }

  function refreshHint(container) {
    const select = container.querySelector("[data-plan-subcategory]");
    const hint = container.querySelector("[data-plan-subcategory-hint]");
    if (!select || !hint) return;
    const option = select.options[select.selectedIndex];
    const examples = option && option.dataset.examples;
    hint.innerHTML = examples
      ? `e.g. ${escapeHtml(examples)}. ${DEFAULT_HINT}`
      : DEFAULT_HINT;
  }

  function fillSubcategories(container, keepSelection) {
    const industrySelect = container.querySelector("[data-plan-industry]");
    const select = container.querySelector("[data-plan-subcategory]");
    if (!industrySelect || !select) return;

    const previous = keepSelection ? select.value : "";
    const options = catalogue()[industrySelect.value] || [];
    select.innerHTML = "";
    if (!options.length) {
      select.disabled = true;
      select.add(new Option("Choose an industry first", ""));
    } else {
      select.disabled = false;
      options.forEach((opt) => {
        const el = new Option(opt.label, opt.key);
        el.dataset.examples = opt.examples || "";
        if (opt.key === previous) el.selected = true;
        select.add(el);
      });
    }
    refreshHint(container);
  }

  function addItemRow(container) {
    const list = container.querySelector("[data-plan-items]");
    const template = list && list.querySelector("template[data-plan-item-template]");
    if (!template) return;
    const row = template.content.firstElementChild.cloneNode(true);
    list.insertBefore(row, template);
    const first = row.querySelector("input");
    if (first) first.focus();
  }

  document.addEventListener("change", (event) => {
    const container = event.target.closest("[data-plan-form]");
    if (!container) return;
    if (event.target.matches("[data-plan-industry]")) fillSubcategories(container, false);
    if (event.target.matches("[data-plan-subcategory]")) refreshHint(container);
  });

  document.addEventListener("click", (event) => {
    const container = event.target.closest("[data-plan-form]");
    if (!container) return;
    if (event.target.closest("[data-plan-add-item]")) {
      event.preventDefault();
      addItemRow(container);
    }
    const remove = event.target.closest("[data-plan-remove-item]");
    if (remove) {
      event.preventDefault();
      const row = remove.closest("[data-plan-item]");
      if (row) row.remove();
    }
  });

  function init() {
    document.querySelectorAll("[data-plan-form]").forEach((container) => {
      // The server already rendered the right options for a saved plan;
      // only fill in when it could not (no industry chosen yet on a
      // re-rendered form) and refresh the hint either way.
      const select = container.querySelector("[data-plan-subcategory]");
      if (select && select.disabled) fillSubcategories(container, true);
      refreshHint(container);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
