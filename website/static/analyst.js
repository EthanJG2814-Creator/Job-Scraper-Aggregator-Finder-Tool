const COLORS = ["#bb4d26", "#b7d436", "#20231d", "#6f9fb0", "#d9a441", "#8a6fb0", "#7a8c5a", "#c27c8f"];

const vendorColors = {};
function colorFor(name) {
  if (!vendorColors[name]) vendorColors[name] = COLORS[Object.keys(vendorColors).length % COLORS.length];
  return vendorColors[name];
}

function esc(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

async function loadAts() {
  try {
    const response = await fetch("/api/ats-stats");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);

    document.getElementById("ats-total").textContent = `${data.total.toLocaleString()} active postings`;
    let start = 0;
    const slices = [];
    data.vendors.forEach((vendor) => {
      vendor.color = colorFor(vendor.name);
      vendor.percent = data.total ? (vendor.count / data.total) * 100 : 0;
      slices.push(`${vendor.color} ${start}% ${start + vendor.percent}%`);
      start += vendor.percent;
    });
    document.getElementById("ats-pie").style.background = slices.length
      ? `conic-gradient(${slices.join(", ")})`
      : "var(--line)";
    document.getElementById("ats-legend").innerHTML = data.vendors.map((vendor) => `
      <div class="legend-row">
        <span class="legend-swatch" style="background:${vendor.color}"></span>
        <span>${esc(vendor.name)}</span>
        <strong>${vendor.percent.toFixed(1)}%</strong>
        <small>${vendor.count.toLocaleString()} postings</small>
      </div>`).join("");
  } catch (error) {
    document.getElementById("ats-total").textContent = "";
    document.getElementById("ats-error").textContent = `Unable to load statistics: ${error.message}`;
  }
}

function niceMax(value) {
  const magnitude = 10 ** Math.floor(Math.log10(Math.max(value, 1)));
  return Math.ceil(value / magnitude) * magnitude || 1;
}

function shortDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const TOTAL_COLOR = "#d6336c";

function drawTimeline(data) {
  const totals = data.dates.map((_, i) => data.series.reduce((sum, s) => sum + s.counts[i], 0));
  const all = [...data.series, { name: "Total", counts: totals }];
  const colorOf = (name) => (name === "Total" ? TOTAL_COLOR : colorFor(name));
  const W = 900, H = 340, L = 60, R = 20, T = 14, B = 36;
  const n = data.dates.length;
  const max = niceMax(Math.max(0, ...totals));
  const x = (i) => L + (n > 1 ? (i / (n - 1)) * (W - L - R) : (W - L - R) / 2);
  const y = (v) => T + (1 - v / max) * (H - T - B);

  const grid = [0, 1, 2, 3, 4].map((k) => {
    const v = (max / 4) * k;
    return `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/>
      <text x="${L - 8}" y="${y(v) + 3}" text-anchor="end">${Math.round(v).toLocaleString()}</text>`;
  }).join("");
  const step = Math.max(1, Math.ceil(n / 10));
  const labels = data.dates.map((d, i) => (i % step === 0 || i === n - 1)
    ? `<text x="${x(i)}" y="${H - 12}" text-anchor="middle">${shortDate(d)}</text>` : "").join("");
  const lines = all.map((s) => {
    const color = colorOf(s.name);
    const isTotal = s.name === "Total";
    const points = s.counts.map((v, i) => `${x(i)},${y(v)}`).join(" ");
    const dots = n <= 60 ? s.counts.map((v, i) => `<circle cx="${x(i)}" cy="${y(v)}" r="${isTotal ? 3.5 : 3}" fill="${color}"><title>${esc(s.name)} · ${shortDate(data.dates[i])}: ${v.toLocaleString()} postings</title></circle>`).join("") : "";
    return `<polyline fill="none" stroke="${color}" stroke-width="${isTotal ? 2.5 : 2}"${isTotal ? ' stroke-dasharray="6 4"' : ""} points="${points}"/>${dots}`;
  }).join("");

  const chart = document.getElementById("timeline-chart");
  chart.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Line chart of postings per day by ATS vendor and in total">${grid}${labels}${lines}</svg>`;
  document.getElementById("timeline-legend").innerHTML = all
    .map((s) => `<span><i class="legend-swatch" style="background:${colorOf(s.name)}"></i>${esc(s.name)}</span>`).join("");
}

function drawTable(data) {
  const fmtDate = (iso) => {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
  };
  const num = (v) => v.toLocaleString();
  const dayTotals = data.dates.map((_, i) => data.series.reduce((sum, s) => sum + s.counts[i], 0));
  const vendorTotals = data.series.map((s) => s.counts.reduce((a, b) => a + b, 0));
  const head = `<thead><tr><th>Date</th>${data.series.map((s) => `<th>${esc(s.name)}</th>`).join("")}<th class="total">Total</th></tr></thead>`;
  const body = data.dates.map((d, i) => i).reverse().map((i) =>
    `<tr><td>${fmtDate(data.dates[i])}</td>${data.series.map((s) => `<td>${num(s.counts[i])}</td>`).join("")}<td class="total">${num(dayTotals[i])}</td></tr>`
  ).join("");
  const foot = `<tfoot><tr><td>Total</td>${vendorTotals.map((v) => `<td>${num(v)}</td>`).join("")}<td>${num(dayTotals.reduce((a, b) => a + b, 0))}</td></tr></tfoot>`;
  document.getElementById("timeline-table").innerHTML = `${head}<tbody>${body}</tbody>${foot}`;
}

function drawSummary(data) {
  const n = data.dates.length;
  const fmtDay = (iso) => shortDate(iso);
  const num = (v) => v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const stats = (counts) => {
    const total = counts.reduce((a, b) => a + b, 0);
    const mean = total / n;
    const sd = n > 1 ? Math.sqrt(counts.reduce((a, v) => a + (v - mean) ** 2, 0) / (n - 1)) : 0;
    const sorted = [...counts].sort((a, b) => a - b);
    const mid = Math.floor(n / 2);
    const median = n % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
    const max = Math.max(...counts), min = Math.min(...counts);
    return { total, mean, sd, cv: mean ? (sd / mean) * 100 : 0, median, max, min,
      maxDay: data.dates[counts.indexOf(max)], minDay: data.dates[counts.indexOf(min)] };
  };
  const rows = data.series.map((s) => ({ name: s.name, ...stats(s.counts) }));
  const all = { name: "All vendors", ...stats(data.dates.map((_, i) => data.series.reduce((sum, s) => sum + s.counts[i], 0))) };
  const grand = all.total;
  const line = (r, cls = "") => `<tr${cls}><td>${esc(r.name)}</td><td>${num(r.total)}</td><td>${grand ? ((r.total / grand) * 100).toFixed(2) : "0.00"}%</td>
    <td class="total">${num(r.mean)}</td><td>${num(r.median)}</td><td>${num(r.sd)}</td><td>${r.cv.toFixed(2)}%</td>
    <td>${num(r.max)} (${fmtDay(r.maxDay)})</td><td>${num(r.min)} (${fmtDay(r.minDay)})</td></tr>`;
  const head = `<thead><tr><th>Vendor</th><th>Total</th><th>Share</th><th class="total">Avg / day</th><th>Median</th><th>Std. dev.</th><th>CV</th><th>Peak day</th><th>Lowest day</th></tr></thead>`;
  document.getElementById("summary-table").innerHTML =
    `${head}<tbody>${rows.sort((a, b) => b.total - a.total).map((r) => line(r)).join("")}</tbody><tfoot>${line(all)}</tfoot>`;
  document.getElementById("summary-range").textContent = `${n} days · ${fmtDay(data.dates[0])} – ${fmtDay(data.dates[n - 1])}`;
}

async function loadTimeline() {
  try {
    const response = await fetch("/api/ats-timeline");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    if (!data.dates.length) throw new Error("No source data yet");
    document.getElementById("timeline-range").textContent =
      `${shortDate(data.dates[0])} – ${shortDate(data.dates[data.dates.length - 1])} · ${data.dates.length} days`;
    drawTimeline(data);
    drawTable(data);
    drawSummary(data);
  } catch (error) {
    document.getElementById("timeline-range").textContent = "";
    document.getElementById("timeline-error").textContent = `Unable to load timeline: ${error.message}`;
  }
}

loadAts().then(loadTimeline);
