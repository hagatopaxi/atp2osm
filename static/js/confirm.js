function extractWikidata(url) {
  const parts = url.split("/");
  const qCode = parts.find((part) => /^Q\d+$/.test(part));
  return qCode;
}

async function confirm_import() {
  const loading = document.getElementById("loading");
  loading.classList.remove("hidden");
  const button_validate = document.getElementById("submit_importation");
  const button_cancel = document.getElementById("cancel");
  button_validate.setAttribute("disabled", true);
  button_cancel.setAttribute("disabled", true);
  const wikidata = extractWikidata(window.location.href);
  const response = await fetch(`/brands/${wikidata}/upload${window.location.search}`, { method: "POST" });
  const warning = document.getElementById("warning");
  const warningIcon = warning.querySelector("i");
  const warningText = warning.querySelector("span");

  const data = await response.json();

  if (!response.ok) {
    loading.classList.add("hidden");
    button_validate.removeAttribute("disabled");
    button_cancel.removeAttribute("disabled");
    warningIcon.className = "iconoir-warning-circle";
    warningText.textContent = t("integration_failed", { errors: data.errors.join(", ") });
    warning.classList.remove("alert-warning");
    warning.classList.add("alert-error");
    return;
  }

  if (data.partial) {
    loading.classList.add("hidden");
    warningText.textContent = t("integration_partial", { errors: data.errors.join(", ") });
    setTimeout(() => { window.location.href = `/history/${data.id}`; }, 4000);
    return;
  }

  window.location.href = `/history/${data.id}`;
}
