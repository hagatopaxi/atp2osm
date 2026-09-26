// Submits a filter bar as soon as a non-text control changes (select, date) —
// the search field keeps its explicit button. Drives the filter chips too:
// a chip is rendered for every filter the page offers, hidden and disabled
// until "Filters" adds it; the cross hides it back and submits.
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form[data-autosubmit]").forEach((form) => {
    form
      .querySelectorAll("input[type=date], select")
      .forEach((c) => c.addEventListener("change", () => form.submit()));

    // Filter chips (the list pages): see _filters.html.
    const chips = form.querySelector("[data-filter-chips]");
    const control = (chip) => chip.querySelector("select, input");
    // "Filters" toggles the row of chips — or, when none is in use and there
    // is nothing to toggle, opens the list of the filters to add.
    const menu = form.querySelector("[data-filters-menu]");
    form.querySelector("[data-filters-toggle]")?.addEventListener("click", () => {
      if (chips.querySelector("[data-chip]:not([hidden])")) chips.hidden = !chips.hidden;
      else menu.open = true;
    });
    form.querySelectorAll("[data-add]").forEach((item) => {
      item.addEventListener("click", () => {
        const chip = form.querySelector(`[data-chip="${item.dataset.add}"]`);
        item.hidden = true;
        chip.hidden = chips.hidden = false;
        control(chip).disabled = false;
        menu.open = false;
        control(chip).focus();
      });
    });
    // The chip row scrolls horizontally, so it clips whatever overflows it:
    // a badge menu is taken out of the flow and pinned under its button.
    chips?.querySelectorAll("details").forEach((details) => {
      details.addEventListener("toggle", () => {
        const menu = details.querySelector("ul");
        const box = details.querySelector("summary").getBoundingClientRect();
        menu.style.position = "fixed";
        menu.style.left = `${box.left}px`;
        menu.style.top = `${box.bottom + 4}px`;
      });
    });
    form.querySelectorAll("[data-pick]").forEach((item) => {
      item.addEventListener("click", () => {
        control(item.closest("[data-chip]")).value = item.dataset.pick;
        form.submit();
      });
    });
    form.querySelectorAll("[data-remove]").forEach((button) => {
      button.addEventListener("click", () => {
        control(button.closest("[data-chip]")).disabled = true;
        form.submit();
      });
    });

    // autofocus puts the caret at the start: move it back to the end.
    const search = form.querySelector("input[autofocus]");
    if (search) {
      const end = search.value.length;
      search.setSelectionRange(end, end);
    }
  });
});

// Sortable headers (_sort_th.html): shift+click adds the column to the sort
// instead of sorting on it alone — and does not open a new window.
document.addEventListener("click", (event) => {
  const link = event.shiftKey && event.target.closest("a[data-shift-href]");
  if (!link) return;
  event.preventDefault();
  location.href = link.dataset.shiftHref;
});
