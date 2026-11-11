function extractWikidata(url) {
  const parts = url.split("/");
  return parts.find((part) => /^Q\d+$/.test(part));
}

function renderInvalidations() {
  const container = document.getElementById("invalidations_list");
  const data = JSON.parse(sessionStorage.getItem("invalidations") || "[]");

  if (data.length === 0) {
    const empty = document.createElement("p");
    empty.className = "text-base-content/60";
    empty.textContent = t("no_rejection_found");
    container.replaceChildren(empty);
    return;
  }

  // Built node by node: the title and the comment come from OSM, ATP and the
  // contributor, so none of them may reach the page as markup.
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  for (const item of data) {
    const card = element(
      "div",
      "card bg-base-100 border-error border shadow-md p-4 flex flex-row items-start gap-4",
    );
    card.appendChild(element("i", "iconoir-prohibition text-error text-2xl mt-1"));
    const body = document.createElement("div");
    body.appendChild(element("p", "font-semibold", item.title));
    for (const label of item.reason_labels || []) {
      body.appendChild(element("span", "badge badge-error badge-outline badge-sm mt-1 mr-1", label));
    }
    if (item.comment) {
      body.appendChild(element("p", "text-base-content/70 mt-1", item.comment));
    }
    if (item.atp_id) {
      const atp = `ATP ${item.spider_id ? item.spider_id + "/" : ""}${item.atp_id}`;
      body.appendChild(element("p", "text-xs text-base-content/50 font-mono mt-1", atp));
    }
    card.appendChild(body);
    container.appendChild(card);
  }
}

async function confirmRejection() {
  const wikidata = extractWikidata(window.location.href);
  const data = JSON.parse(sessionStorage.getItem("invalidations") || "[]");
  const confirmBtn = document.getElementById("confirm_btn");
  const loading = document.getElementById("loading");

  confirmBtn.setAttribute("disabled", true);
  loading.classList.remove("hidden");

  const comment = data.map((item) => ({
    osm_id: item.osm_id,
    osm_type: item.osm_type,
    atp_id: item.atp_id,
    spider_id: item.spider_id,
    reasons: item.reasons || [],
    comment: item.comment,
  }));

  try {
    const res = await fetch(`/brands/${wikidata}/report-error`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ comment: JSON.stringify(comment), brand_name: sessionStorage.getItem("brand_name") || "" }),
    });

    if (!res.ok) throw new Error(res.statusText);

    const data = await res.json();
    sessionStorage.removeItem("invalidations");
    sessionStorage.removeItem("brand_name");
    window.location.href = `/history/${data.id}`;
  } catch (err) {
    alert(t("send_failed", { error: err.message }));
    confirmBtn.removeAttribute("disabled");
    loading.classList.add("hidden");
  }
}

document.addEventListener("DOMContentLoaded", renderInvalidations);
