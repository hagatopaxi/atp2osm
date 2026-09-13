// Submits a filter bar as soon as a non-text control changes (select, date) —
// the search field keeps its explicit button. Drives the filter chips too:
// a chip is rendered for every filter the page offers, hidden and disabled
// until "Filters" adds it; the cross hides it back and submits.
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form[data-autosubmit]").forEach((form) => {
    form
      .querySelectorAll("input[type=date], select")
      .forEach((c) => c.addEventListener("change", () => form.submit()));

    // Period presets (stats page): they fill the two date bounds instead of
    // being a filter of their own, so a period is always a pair of dates.
    const iso = (d) => new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    const bounds = (days) => {
      const today = new Date();
      return days
        ? [iso(new Date(today.getTime() - (days - 1) * 86400000)), iso(today)]
        : ["", ""];
    };
    const from = form.querySelector("input[name=from]");
    const to = form.querySelector("input[name=to]");
    form.querySelectorAll("button[data-range]").forEach((button) => {
      const [start, end] = bounds(Number(button.dataset.range));
      button.classList.toggle("btn-active", from.value === start && to.value === end);
      button.addEventListener("click", () => {
        from.value = start;
        to.value = end;
        form.submit();
      });
    });

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
