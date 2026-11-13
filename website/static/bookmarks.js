const $ = (id) => document.getElementById(id);
let page = 1;
let pages = 1;
let jobs = [];
let sessionId = localStorage.getItem("sessionId");

// Generate session ID if not exists
if (!sessionId) {
  sessionId = "session_" + Date.now() + "_" + Math.random().toString(36).substr(2, 9);
  localStorage.setItem("sessionId", sessionId);
}

function locationText(location) {
  const place = [location.city_name, location.state_abbv, location.country]
    .filter(Boolean).join(", ");
  const workType = [location.is_remote ? "Remote" : "", location.is_hybrid ? "Hybrid" : ""].filter(Boolean).join(" / ");
  return [place, workType].filter(Boolean).join(" · ") || "Location unspecified";
}

function esc(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

function fmt(value) {
  if (!value) return "—";
  const date = new Date(value);
  return isNaN(date) ? value : date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function fmtBookmarkDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (isNaN(date)) return value;
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

async function load() {
  $("job-list").innerHTML = '<p class="empty-state">Loading bookmarks…</p>';
  $("job-detail").innerHTML = '';
  
  try {
    const response = await fetch(`/api/bookmarks?session=${encodeURIComponent(sessionId)}&page=${page}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    
    jobs = data.jobs;
    pages = data.pages;
    const total = data.total;
    
    $("bookmark-count").textContent = `${total} bookmark${total !== 1 ? "s" : ""}`;
    $("result-count").textContent = `${total.toLocaleString()} bookmarks total`;
    $("page-input").value = data.page;
    $("page-input").max = data.pages;
    $("page-total").textContent = data.pages.toLocaleString();
    $("first-page").disabled = $("previous-page").disabled = page <= 1;
    $("next-page").disabled = $("last-page").disabled = page >= pages;
    
    if (total === 0) {
      $("job-list").innerHTML = '<p class="empty-state">No bookmarks yet. Save jobs from the Job Searcher page!</p>';
      $("pagination").style.display = "none";
      $("clear-all-btn").style.display = "none";
    } else {
      $("job-list").innerHTML = jobs.map((job, i) => `<button class="job-row" data-index="${i}" data-job-id="${job.id}">
        <span class="job-row-title">${esc(job.title)}</span>
        <span class="job-row-company">${esc(job.company)}</span>
        <span class="job-row-meta">ID ${job.id} · ${esc(job.ats_vendor || "Unknown ATS")} · ${esc(job.employment_type || "Type n/a")}</span>
        ${job.locations.length ? `<span class="job-row-meta">${esc(job.locations.map(locationText).join(" | "))}</span>` : ""}
        <span class="job-row-meta">Bookmarked ${fmtBookmarkDate(job.bookmarked_at)}</span>
      </button>`).join("");
      $("pagination").style.display = "block";
      $("clear-all-btn").style.display = "inline-block";
    }
    
    $("job-detail").innerHTML = '<p class="empty-state">Select a job posting to review its description.</p>';
  } catch (error) {
    $("bookmark-count").textContent = "Error loading bookmarks";
    $("job-list").innerHTML = `<p class="error" style="padding:20px;color:#9b2c1d">Unable to load bookmarks: ${esc(error.message)}</p>`;
  }
}

function show(job) {
  const facts = [job.job_remote === true || job.job_remote === 1 || /^(true|yes|remote)$/i.test(job.job_remote) ? "Remote" : "", job.compensation_summary].filter(Boolean);
  $("job-detail").innerHTML = `
    <div style="display: flex; justify-content: space-between; align-items: start;">
      <div>
        <p class="detail-kicker">${esc(job.company)}</p>
        <h1>${esc(job.title)}</h1>
      </div>
      <button class="remove-bookmark-btn" data-job-id="${job.id}" style="padding: 8px 12px; background: #9b2c1d; color: white; border: none; border-radius: 3px; cursor: pointer; font-size: 13px; white-space: nowrap;">✕ Remove</button>
    </div>
    <p class="job-facts">Job ID: ${job.id} · ATS: ${esc(job.ats_vendor || "Unknown")} · Employment type: ${esc(job.employment_type || "n/a")}</p>
    ${facts.length ? `<p class="job-facts">${esc(facts.join(" · "))}</p>` : ""}
    ${job.locations.length ? `<section class="locations-section"><h2>Locations</h2>${job.locations.map((location) => `<span class="location-chip">${esc(locationText(location))}</span>`).join("")}</section>` : ""}
    <p class="job-facts">Published: ${fmt(job.published_date)} · Last scanned: ${fmt(job.last_scanned)} · Bookmarked: ${fmtBookmarkDate(job.bookmarked_at)}</p>
    ${job.job_url ? `<a class="apply-link" href="${esc(job.job_url)}" target="_blank" rel="noopener">Apply</a>` : ""}
    <section class="description-section"><div class="description">${job.description || "No description available."}</div></section>`;
  $("job-detail").querySelector(".description").style.whiteSpace = "pre-wrap";
  
  // Attach remove button handler
  const removeBtn = $("job-detail").querySelector(".remove-bookmark-btn");
  if (removeBtn) {
    removeBtn.addEventListener("click", async (e) => {
      e.preventDefault();
      await removeBookmark(parseInt(removeBtn.dataset.jobId));
    });
  }
}

async function removeBookmark(jobId) {
  try {
    const response = await fetch(`/api/bookmark-remove`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, job_id: jobId })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    load();
  } catch (error) {
    alert(`Error removing bookmark: ${error.message}`);
  }
}

async function clearAllBookmarks() {
  if (!confirm("Are you sure you want to delete all bookmarks?")) return;
  try {
    const response = await fetch(`/api/bookmarks-clear`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    load();
  } catch (error) {
    alert(`Error clearing bookmarks: ${error.message}`);
  }
}

$("job-list").addEventListener("click", (event) => {
  const row = event.target.closest(".job-row");
  if (!row) return;
  document.querySelectorAll(".job-row.active").forEach((el) => el.classList.remove("active"));
  row.classList.add("active");
  show(jobs[row.dataset.index]);
});

$("first-page").addEventListener("click", () => { page = 1; load(); });
$("previous-page").addEventListener("click", () => { if (page > 1) { page--; load(); } });
$("next-page").addEventListener("click", () => { if (page < pages) { page++; load(); } });
$("last-page").addEventListener("click", () => { page = pages; load(); });
$("page-input").addEventListener("change", () => {
  const newPage = parseInt($("page-input").value);
  if (newPage >= 1 && newPage <= pages) {
    page = newPage;
    load();
  } else {
    $("page-input").value = page;
  }
});
$("clear-all-btn").addEventListener("click", clearAllBookmarks);

// Load bookmarks on page load
load();
