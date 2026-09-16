// Charts of the /stats page, drawn by Chart.js from the JSON block
// #stats-data. The colours are the daisyUI tokens of the current theme,
// resolved through a probe element, so a theme switch redraws every chart.
(function () {
  const data = JSON.parse(document.getElementById("stats-data").textContent);
  const S = data.strings;
  const charts = [];

  const probe = document.createElement("span");
  probe.hidden = true;
  document.body.appendChild(probe);
  // A daisyUI colour, faded to `alpha`: the canvas takes what the browser
  // computed, not the variable itself.
  function token(name, alpha = 1) {
    probe.style.color =
      alpha < 1
        ? `color-mix(in oklab, var(--color-${name}) ${alpha * 100}%, transparent)`
        : `var(--color-${name})`;
    return getComputedStyle(probe).color;
  }

  // Solid, the very colour of the wave badges elsewhere on the site.
  function waveDatasets(rows) {
    return data.waves.map((w) => ({
      label: w.label,
      data: rows[w.number],
      backgroundColor: token(w.color),
      hoverBackgroundColor: token(w.color, 0.75),
      borderRadius: 2,
      stack: "pois",
      // Drawn under the running-total curves: a lower order paints last.
      order: 1,
    }));
  }

  const percent = (v) => v + " %";
  // The tooltip lists every series of the hovered period: a sliver too thin
  // to point at is still read.
  const byIndex = { mode: "index", intersect: false };
  const grid = () => ({ color: token("base-content", 0.08) });

  const builders = {
    pace: () => ({
      type: "bar",
      data: {
        labels: data.pace.labels,
        datasets: [
          ...waveDatasets(data.pace.waves),
          // One running total per wave, in the wave's colour.
          ...data.waves.map((w) => ({
            type: "line",
            label: S.running_total,
            data: data.pace.cumulative[w.number],
            borderColor: token(w.color),
            backgroundColor: token(w.color),
            pointRadius: 3,
            pointHoverRadius: 6,
            tension: 0.2,
            yAxisID: "y2",
          })),
        ],
      },
      options: {
        interaction: byIndex,
        scales: {
          x: { stacked: true, grid: { display: false } },
          y: { stacked: true, beginAtZero: true, grid: grid() },
          y2: { position: "right", beginAtZero: true, grid: { display: false } },
        },
        plugins: {
          // The bars name the waves and the curves wear their colours: the
          // running totals need no entry of their own.
          legend: { labels: { filter: (item) => item.text !== S.running_total } },
          tooltip: {
            callbacks: {
              footer: (items) =>
                S.integrations + ": " + data.pace.imports[items[0].dataIndex],
            },
          },
        },
      },
    }),

    by_imports: () => ranking(data.by_imports),
    by_pois: () => ranking(data.by_pois),
    tags: () => ranking(data.tags),
    brands: () => ranking(data.brands),
    spiders: () => share(data.spiders, S.brands_integrated, S.brands_rejected),
    changesets: () => share(data.changesets, S.accepted, S.refused),
  };

  // Horizontal stacked bars: one row per label, the waves side by side.
  function ranking(rows) {
    return {
      type: "bar",
      data: { labels: rows.labels, datasets: waveDatasets(rows.waves) },
      options: {
        indexAxis: "y",
        interaction: byIndex,
        scales: {
          x: { stacked: true, beginAtZero: true, grid: grid() },
          y: { stacked: true, grid: { display: false } },
        },
      },
    };
  }

  // Columns normalised to 100 %: the reading is the success share, not the
  // volume. A period without data has no column; the tooltip gives the counts.
  function share(rows, okLabel, koLabel) {
    const total = rows.ok.map((ok, i) => ok + rows.ko[i]);
    const pct = (arr) => arr.map((v, i) => (total[i] ? Math.round((100 * v) / total[i]) : null));
    return {
      type: "bar",
      data: {
        labels: rows.labels,
        datasets: [
          { label: okLabel, data: pct(rows.ok), backgroundColor: token("success", 0.6), hoverBackgroundColor: token("success") },
          { label: koLabel, data: pct(rows.ko), backgroundColor: token("error", 0.6), hoverBackgroundColor: token("error"), borderRadius: 2 },
        ],
      },
      options: {
        interaction: byIndex,
        scales: {
          x: { stacked: true, grid: { display: false } },
          y: { stacked: true, min: 0, max: 100, ticks: { callback: percent }, grid: grid() },
        },
        plugins: {
          tooltip: {
            callbacks: {
              label: (item) => {
                const raw = (item.datasetIndex ? rows.ko : rows.ok)[item.dataIndex];
                return `${item.dataset.label}: ${raw} (${item.raw} %)`;
              },
            },
          },
        },
      },
    };
  }

  function draw() {
    charts.splice(0).forEach((c) => c.destroy());
    Chart.defaults.color = token("base-content", 0.7);
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.maintainAspectRatio = false;
    Chart.defaults.plugins.legend.labels.boxWidth = 12;
    // A bar and a line are told apart in the legend too.
    Chart.defaults.plugins.legend.labels.usePointStyle = true;
    document.querySelectorAll("canvas[data-chart]").forEach((canvas) => {
      charts.push(new Chart(canvas, builders[canvas.dataset.chart]()));
    });
  }

  draw();
  new MutationObserver(draw).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
})();
