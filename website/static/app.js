const $ = (id) => document.getElementById(id);
let page = 1;
let pages = 1;
let jobs = [];
let latestRequest = 0;
let sessionId = localStorage.getItem("sessionId");
let currentJobId = null;
const locationPicker = new LocationPicker();

// Generate session ID if not exists
if (!sessionId) {
  sessionId = "session_" + Date.now() + "_" + Math.random().toString(36).substr(2, 9);
  localStorage.setItem("sessionId", sessionId);
}

function locationText(location) {
  const place = [location.city_name, location.state_abbv, location.country && countryName(location.country)]
    .filter(Boolean).join(", ");
  const workType = [location.is_remote ? "Remote" : "", location.is_hybrid ? "Hybrid" : ""].filter(Boolean).join(" / ");
  return [place, workType].filter(Boolean).join(" · ") || "Location unspecified";
}

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
  const request = ++latestRequest;
  const query = new URLSearchParams({
    title: $("title-input").value,
    description: $("description-input").value,
    sort: $("sort-filter").value,
    recent: $("recent-filter").value,
    vendor: $("vendor-filter").value,
    page,
  });
  if (locationPicker.selected.size) query.set("location", JSON.stringify(locationPicker.value()));
  $("job-list").innerHTML = "";
  $("job-detail").innerHTML = '<p class="empty-state">Select a job posting to review its description.</p>';
  try {
    const response = await fetch(`/api/jobs?${query}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    if (request !== latestRequest) return;
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
          ${job.locations.length ? `<span class="job-row-meta">${esc(job.locations.map(locationText).join(" | "))}</span>` : ""}
          <span class="job-row-meta">Published ${fmt(job.published_date)} · Scanned ${fmt(job.last_scanned)}</span>
        </button>`).join("")
      : '<p class="empty-state">No jobs found.</p>';
  } catch (error) {
    if (request !== latestRequest) return;
    $("result-count").textContent = "";
    $("job-list").innerHTML = `<p class="error" style="padding:20px;color:#9b2c1d">Unable to load jobs: ${esc(error.message)}</p>`;
  }
}

function show(job) {
  const facts = [job.job_remote === true || job.job_remote === 1 || /^(true|yes|remote)$/i.test(job.job_remote) ? "Remote" : "", job.compensation_summary].filter(Boolean);
  currentJobId = job.id;
  $("job-detail").innerHTML = `
    <div style="display: flex; justify-content: space-between; align-items: start; gap: 12px;">
      <div style="flex: 1;">
        <p class="detail-kicker">${esc(job.company)}</p>
        <h1>${highlight(job.title, titleTerms())}</h1>
      </div>
      <button id="bookmark-btn" class="bookmark-btn" data-job-id="${job.id}" style="padding: 8px 12px; background: #bb4d26; color: white; border: none; border-radius: 3px; cursor: pointer; font-size: 13px; white-space: nowrap; flex-shrink: 0;">☆ Save</button>
    </div>
    <p class="job-facts">Job ID: ${job.id} · ATS: ${esc(job.ats_vendor || "Unknown")} · Employment type: ${esc(job.employment_type || "n/a")}</p>
    ${facts.length ? `<p class="job-facts">${esc(facts.join(" · "))}</p>` : ""}
    ${job.locations.length ? `<section class="locations-section"><h2>Locations</h2>${job.locations.map((location) => `<span class="location-chip">${esc(locationText(location))}</span>`).join("")}</section>` : ""}
    <p class="job-facts">Published: ${fmt(job.published_date)} · Last scanned: ${fmt(job.last_scanned)}</p>
    ${job.job_url ? `<a class="apply-link" href="${esc(job.job_url)}" target="_blank" rel="noopener">Apply</a>` : ""}
    <section class="description-section"><div class="description">${highlight(job.description || "No description available.", descriptionTerms())}</div></section>`;
  $("job-detail").querySelector(".description").style.whiteSpace = "pre-wrap";
  
  // Add bookmark button functionality
  const bookmarkBtn = $("bookmark-btn");
  if (bookmarkBtn) {
    checkBookmarkStatus(job.id);
    bookmarkBtn.addEventListener("click", () => toggleBookmark(job.id, bookmarkBtn));
  }
}

async function checkBookmarkStatus(jobId) {
  try {
    const response = await fetch(`/api/bookmark-check?session=${encodeURIComponent(sessionId)}&job_id=${jobId}`);
    const data = await response.json();
    const btn = $("bookmark-btn");
    if (btn && data.bookmarked) {
      btn.textContent = "★ Saved";
      btn.style.background = "#d9a441";
    }
  } catch (error) {
    console.error("Error checking bookmark status:", error);
  }
}

async function toggleBookmark(jobId, btn) {
  try {
    const isCurrentlySaved = btn.textContent.includes("★");
    const endpoint = isCurrentlySaved ? "/api/bookmark-remove" : "/api/bookmark-add";
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, job_id: jobId })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    
    if (data.success || !data.error) {
      if (isCurrentlySaved) {
        btn.textContent = "☆ Save";
        btn.style.background = "#bb4d26";
      } else {
        btn.textContent = "★ Saved";
        btn.style.background = "#d9a441";
      }
    }
  } catch (error) {
    alert(`Error saving job: ${error.message}`);
  }
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
$("vendor-filter").addEventListener("change", search);
for (const id of ["title-input", "description-input"]) {
  $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
}
$("clear-button").addEventListener("click", () => {
  $("title-input").value = "";
  $("description-input").value = "";
  $("sort-filter").value = "mixed";
  $("recent-filter").value = "all";
  $("vendor-filter").value = "all";
  locationPicker.clear();
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
locationPicker.load();
