let currentInvalidItemId = null;
let invalidations = [];
let apiAnswered = false;
// A ticked box the reviewer has not applied describes a batch that does not
// exist: the page below it is the old one. Nothing goes to the next step
// until the filter is applied again.
let filterDirty = false;

function markSourceChecked(itemId) {
  document
    .querySelectorAll(`[data-validate-btn="${itemId}"]`)
    .forEach((btn) => {
      btn.removeAttribute("disabled");
    });
  const warning = document.querySelector(`[data-source-warning="${itemId}"]`);
  if (warning) warning.remove();

  // The first time a source is opened, ask whether it is an API: the button
  // is replaced by the question. Once answered, it is never asked again.
  if (apiAnswered) return;
  const sourceBtn = document.querySelector(`[data-source-btn="${itemId}"]`);
  if (sourceBtn) sourceBtn.classList.add("hidden");
  const question = document.querySelector(`[data-api-question="${itemId}"]`);
  if (question) question.classList.remove("hidden");
}

// Answer to "is this an API?": the message is frozen in the card and, when it
// is one, the remaining points are unlocked.
function answerIsApi(btn, isApi) {
  apiAnswered = true;
  const question = btn.closest("[data-api-question]");
  question.querySelector("[data-api-msg]").textContent = isApi
    ? t("source_is_api")
    : t("source_is_not_api");
  question.querySelector("[data-api-choices]").remove();
  if (isApi) markBrandIsApi();
}

// The source is an API: it is no longer required, so the remaining points are
// unlocked, the warnings are dropped and the remaining "open the source"
// buttons are replaced by a plain mention.
function markBrandIsApi() {
  document
    .querySelectorAll("[data-validate-btn][disabled]")
    .forEach((btn) => btn.removeAttribute("disabled"));
  document
    .querySelectorAll("[data-source-warning]")
    .forEach((warning) => warning.remove());
  document.querySelectorAll("[data-source-btn]:not(.hidden)").forEach((btn) => {
    const mention = document.createElement("div");
    mention.className = "alert alert-soft alert-info";
    mention.innerHTML = '<i class="iconoir-database"></i><span></span>';
    mention.querySelector("span").textContent = t("source_is_api");
    btn.replaceWith(mention);
  });
}

function extractWikidata(url) {
  const parts = url.split("/");
  return parts.find((part) => /^Q\d+$/.test(part));
}

function validateData(itemId) {
  const collapse = document.querySelector(`[data-item-id="${itemId}"]`);
  if (collapse) {
    collapse.classList.add("validated", "accepted");
    collapse.querySelector(".content").classList.add("hidden");
    checkAllValidated();
  }
}

function setReason(button, on) {
  button.classList.toggle("btn-active", on);
  button.classList.toggle("btn-outline", !on);
  button.classList.toggle("btn-soft", on);
  button.classList.toggle("btn-primary", on);
}

function toggleReason(button) {
  setReason(button, !button.classList.contains("btn-active"));
}

function invalidateData(itemId) {
  currentInvalidItemId = itemId;
  document.getElementById("invalidation_comment").value = "";
  document.querySelectorAll(".reason-btn").forEach((b) => setReason(b, false));
  document.getElementById("invalidation_modal").showModal();
}

document.addEventListener("DOMContentLoaded", () => {
  document
    .querySelectorAll(".reason-btn")
    .forEach((b) => b.addEventListener("click", () => toggleReason(b)));

  const filter = document.querySelector("[data-category-filter]");
  if (filter) {
    const boxes = Array.from(filter.querySelectorAll("input[name=keep]"));
    const applied = boxes.map((b) => b.checked).join();
    const apply = filter.querySelector("[data-apply-filter]");
    filter.addEventListener("change", () => {
      // Back on the applied ticks, the page below is the right one again.
      filterDirty = boxes.map((b) => b.checked).join() !== applied;
      apply?.toggleAttribute("disabled", !filterDirty);
      filter
        .querySelector("[data-filter-hint]")
        ?.classList.toggle("hidden", !filterDirty);
      document
        .querySelector("[data-review-items]")
        ?.classList.toggle("filter-stale", filterDirty);
      checkAllValidated();
    });
  }
});

function checkAllValidated() {
  const cards = document.querySelectorAll("[data-item-id]");
  const nextStepButton = document.querySelector("a.nextStep");

  const allValidated = Array.from(cards).every((card) =>
    card.classList.contains("validated"),
  );
  if (nextStepButton) {
    if (allValidated && !filterDirty) {
      nextStepButton.removeAttribute("disabled");
      if (invalidations.length > 0) {
        const wikidata = extractWikidata(window.location.href);
        nextStepButton.href = `/brands/${wikidata}/rejected`;
        nextStepButton.classList.remove("btn-primary");
        nextStepButton.classList.add("btn-error");
        nextStepButton.addEventListener("click", () => {
          const brandName =
            document.querySelector("[data-brand-name]")?.dataset.brandName;
          sessionStorage.setItem(
            "invalidations",
            JSON.stringify(invalidations),
          );
          sessionStorage.setItem("brand_name", brandName || "");
        });
      }
    } else {
      nextStepButton.setAttribute("disabled", true);
    }
  }
}

function publishComment() {
  const commentField = document.getElementById("invalidation_comment");
  const selected = Array.from(document.querySelectorAll(".reason-btn.btn-active"));
  const reasons = selected.map((b) => b.dataset.reason);
  const comment = commentField.value.trim();

  const collapse = document.querySelector(
    `[data-item-id="${currentInvalidItemId}"]`,
  );
  const title = collapse
    ? collapse.querySelector(".title").textContent.trim()
    : currentInvalidItemId;
  const nodeType = collapse ? collapse.dataset.nodeType : null;

  invalidations.push({
    osm_id: collapse ? parseInt(collapse.dataset.osmId) : null,
    osm_type: nodeType,
    atp_id: collapse ? collapse.dataset.atpId : null,
    spider_id: collapse ? collapse.dataset.spiderId : null,
    title,
    reasons,
    reason_labels: selected.map((b) => b.textContent.trim()),
    comment,
  });

  document.getElementById("invalidation_modal").close();

  if (collapse) {
    collapse.classList.add("validated", "rejected");
    collapse.querySelector(".content").classList.add("hidden");
    checkAllValidated();
  }

  currentInvalidItemId = null;
}
