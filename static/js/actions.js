// Event handlers declared in the markup without inline JavaScript, which the
// Content-Security-Policy forbids: `data-on-click="fn"` calls the global
// function `fn` on a click (likewise `data-on-submit`, `data-on-focusout`).
// `data-args` is a JSON array of its arguments, where "$el" stands for the
// element and "$event" for the event. Listening on the document covers the
// rows infinite-scroll inserts later.

function showDialog(id) {
  document.getElementById(id).show();
}

function closeDialog(id) {
  document.getElementById(id).close();
}

for (const type of ["click", "submit", "focusout"]) {
  const attribute = `data-on-${type}`;
  document.addEventListener(type, (event) => {
    const element = event.target.closest(`[${attribute}]`);
    if (!element) return;
    const handler = window[element.getAttribute(attribute)];
    if (typeof handler !== "function") return;
    const args = JSON.parse(element.dataset.args || "[]").map((arg) =>
      arg === "$el" ? element : arg === "$event" ? event : arg,
    );
    handler(...args);
  });
}
