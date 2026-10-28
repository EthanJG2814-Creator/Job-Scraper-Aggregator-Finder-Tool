const $ = (id) => document.getElementById(id);
let page = 1;
let pages = 1;
let jobs = [];

function esc(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

function highlight(value, terms) {
  const text = String(value ?? "");
  const matches = [...new Set(terms.map((term) => term.trim()).filter(Boolean))]
    .sort((a, b) => b.length - a.length);
  if (!matches.length) return esc(text);

  const pattern = new RegExp(`(${matches.map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  return text.split(pattern).map((part, index) =>
    index % 2
      ? `<mark class="search-highlight">${esc(part)}</mark>`
      : esc(part)
  ).join("");
}

function fmt(value) {
  if (!value) return "—";
  const date = new Date(value);
  return isNaN(date) ? value : date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function titleTerms() {
  return [$("title-input").value];
}

function descriptionTerms() {
  return $("description-input").value.split(",");
}

async function load() {
  const query = new URLSearchParams({
    title: $("title-input").value,
    description: $("description-input").value,
    sort: $("sort-filter").value,
    recent: $("recent-filter").value,
    page,
  });
  $("job-list").innerHTML = "";
  try {
    const response = await fetch(`/api/jobs?${query}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    jobs = data.jobs;
    pages = data.pages;
    $("result-count").textContent = `${data.total.toLocaleString()} postings`;
    $("page-input").value = data.page;
    $("page-input").max = data.pages;
    $("page-total").textContent = data.pages.toLocaleString();
    $("first-page").disabled = $("previous-page").disabled = page <= 1;
    $("next-page").disabled = $("last-page").disabled = page >= pages;
    $("job-list").innerHTML = jobs.length
      ? jobs.map((job, i) => `<button class="job-row" data-index="${i}">
          <span class="job-row-title">${highlight(job.title, titleTerms())}</span>
          <span class="job-row-company">${esc(job.company)}</span>
          <span class="job-row-meta">ID ${job.id} · ${esc(job.ats_vendor || "Unknown ATS")} · ${esc(job.employment_type || "Type n/a")}</span>
          <span class="job-row-meta">Published ${fmt(job.published_date)} · Scanned ${fmt(job.last_scanned)}</span>
        </button>`).join("")
      : '<p class="empty-state">No jobs found.</p>';
  } catch (error) {
    $("result-count").textContent = "";
    $("job-list").innerHTML = `<p class="error" style="padding:20px;color:#9b2c1d">Unable to load jobs: ${esc(error.message)}</p>`;
  }
}

function show(job) {
  const facts = [job.job_remote === true || job.job_remote === 1 || /^(true|yes|remote)$/i.test(job.job_remote) ? "Remote" : "", job.compensation_summary].filter(Boolean);
  $("job-detail").innerHTML = `
    <p class="detail-kicker">${esc(job.company)}</p>
    <h1>${highlight(job.title, titleTerms())}</h1>
    <p class="job-facts">Job ID: ${job.id} · ATS: ${esc(job.ats_vendor || "Unknown")} · Employment type: ${esc(job.employment_type || "n/a")}</p>
    ${facts.length ? `<p class="job-facts">${esc(facts.join(" · "))}</p>` : ""}
    <p class="job-facts">Published: ${fmt(job.published_date)} · Last scanned: ${fmt(job.last_scanned)}</p>
    ${job.job_url ? `<a class="apply-link" href="${esc(job.job_url)}" target="_blank" rel="noopener">Apply</a>` : ""}
    <section class="description-section"><div class="description">${highlight(job.description || "No description available.", descriptionTerms())}</div></section>`;
  $("job-detail").querySelector(".description").style.whiteSpace = "pre-wrap";
}

$("job-list").addEventListener("click", (event) => {
  const row = event.target.closest(".job-row");
  if (!row) return;
  document.querySelectorAll(".job-row.active").forEach((el) => el.classList.remove("active"));
  row.classList.add("active");
  show(jobs[row.dataset.index]);
});

function search() { page = 1; load(); }
$("search-button").addEventListener("click", search);
$("sort-filter").addEventListener("change", search);
$("recent-filter").addEventListener("change", search);
for (const id of ["title-input", "description-input"]) {
  $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
}
$("clear-button").addEventListener("click", () => {
  $("title-input").value = "";
  $("description-input").value = "";
  $("sort-filter").value = "mixed";
  $("recent-filter").value = "all";
  search();
});
$("previous-page").addEventListener("click", () => { if (page > 1) { page--; load(); } });
$("next-page").addEventListener("click", () => { if (page < pages) { page++; load(); } });
function goTo(n) {
  page = Math.min(Math.max(1, Number.isFinite(n) ? n : page), pages);
  load();
}
$("first-page").addEventListener("click", () => goTo(1));
$("last-page").addEventListener("click", () => goTo(pages));
$("page-input").addEventListener("change", (e) => goTo(parseInt(e.target.value, 10)));
load();
