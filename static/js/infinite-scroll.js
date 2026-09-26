// Infinite scroll, two flavours depending on where the rows come from.
//
// Fetch mode (#scroll-sentinel with data-total-pages): the server paginates,
// the next ?page=N is fetched and its rows appended. Plain ?page=N pages keep
// working without JS.
//
// Reveal mode ([data-infinite-scroll] on a table): the server already sent
// every row, we just hide all but the first CHUNK and uncover them on scroll.
const CHUNK = 50;

function watch(sentinel, loadMore) {
    const observer = new IntersectionObserver(async ([entry]) => {
        if (!entry.isIntersecting || sentinel.dataset.loading) return;
        sentinel.dataset.loading = "1";
        const done = await loadMore();
        delete sentinel.dataset.loading;
        if (done) {
            observer.disconnect();
            sentinel.remove();
        }
    }, { rootMargin: "400px" });
    observer.observe(sentinel);
}

const fetched = document.getElementById("scroll-sentinel");
if (fetched) {
    const tbody = document.querySelector("table tbody");
    let page = Number(fetched.dataset.page);
    const totalPages = Number(fetched.dataset.totalPages);
    watch(fetched, async () => {
        const url = new URL(location.href);
        url.searchParams.set("page", ++page);
        const html = await (await fetch(url)).text();
        const next = new DOMParser().parseFromString(html, "text/html");
        tbody.append(...next.querySelectorAll("table tbody tr"));
        return page >= totalPages;
    });
}

for (const table of document.querySelectorAll("table[data-infinite-scroll]")) {
    const rows = [...table.querySelectorAll("tbody tr")];
    if (rows.length <= CHUNK) continue;
    let shown = CHUNK;
    rows.slice(shown).forEach((row) => (row.hidden = true));

    const sentinel = document.createElement("div");
    sentinel.className = "mx-auto shrink-0 mt-4 loading loading-dots loading-md";
    // After the horizontal scroller, not inside it: there, a table scrolled
    // to its right edge would carry the sentinel out of view.
    table.parentElement.after(sentinel);
    watch(sentinel, () => {
        rows.slice(shown, (shown += CHUNK)).forEach((row) => (row.hidden = false));
        return shown >= rows.length;
    });
}
