const $ = (id) => document.getElementById(id);
const locationPicker = new LocationPicker();
let activeQuery = null;
let jobs = [];
let page = 1;
let pages = 1;
let latestRequest = 0;
let statusTimer = null;

async function getJSON(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

async function loadCandidates() {
  try {
    const data = await getJSON("/api/resume-profiles");
    const select = $("candidate");
    select.replaceChildren(new Option("Select a candidate", ""));
    for (const profile of data.profiles) select.add(new Option(profile.name, profile.id));
    if (!data.profiles.length) $("match-status").textContent = "Create a candidate in Profile Creation first.";
  } catch (error) {
    $("candidate").replaceChildren(new Option("Unable to load candidates", ""));
    $("match-status").textContent = `Unable to load candidates: ${error.message}`;
  }
}

async function refreshIndexStatus() {
  clearTimeout(statusTimer);
  try {
    const data = await getJSON("/api/resume-index-status");
    $("index-status").textContent = `Title index: ${data.status}. ${data.message}`;
    $("build-index").disabled = data.status === "building";
    if (data.status === "building") statusTimer = setTimeout(refreshIndexStatus, 3000);
  } catch (error) {
    $("index-status").textContent = `Unable to check title index: ${error.message}`;
  }
}

$("build-index").addEventListener("click", async () => {
  $("build-index").disabled = true;
  try {
    const data = await getJSON("/api/resume-index-build", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    $("index-status").textContent = data.message;
    await refreshIndexStatus();
  } catch (error) {
    $("index-status").textContent = `Unable to build title index: ${error.message}`;
    $("build-index").disabled = false;
  }
});

function locationText(location) {
  const place = [location.city_name, location.state_abbv, location.country && countryName(location.country)]
    .filter(Boolean).join(", ");
  const workType = [location.is_remote ? "Remote" : "", location.is_hybrid ? "Hybrid" : ""].filter(Boolean).join(" / ");
  return [place, workType].filter(Boolean).join(" · ") || "Location unspecified";
}

function scoreText(job) {
  return job.score === null ? "No job title to compare" : `Weighted score ${job.score.toFixed(3)}`;
}

function textElement(tag, text, className) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = text;
  return element;
}

function show(job) {
  const detail = $("job-detail");
  detail.replaceChildren(
    textElement("p", job.company || "Unknown company", "detail-kicker"),
    textElement("h1", job.title || "Untitled job"),
    textElement("p", scoreText(job), "job-facts"),
    textElement("p", `Job ID: ${job.id} · ATS: ${job.ats_vendor || "Unknown"} · Employment type: ${job.employment_type || "n/a"}`, "job-facts"),
    textElement("p", `Published: ${job.published_date || "Unknown"} · Last scanned: ${job.last_scanned || "Unknown"}`, "job-facts"),
  );
  if (job.score !== null) {
    detail.append(textElement(
      "p",
      `Title cosine: ${job.title_score.toFixed(3)} · Closest title: ${job.matched_title}`,
      "job-facts",
    ));
    detail.append(textElement(
      "p",
      `Average matrix similarity: ${job.cluster_score.toFixed(3)} · Closest experience: ${job.cluster_matched_title} · Closest distance: ${job.cluster_distance.toFixed(2)}${job.position_estimated ? " · Estimated job position" : ""}`,
      "job-facts",
    ));
  }
  if (job.locations.length) {
    const section = document.createElement("section");
    section.className = "locations-section";
    section.append(textElement("h2", "Locations"));
    for (const location of job.locations) section.append(textElement("span", locationText(location), "location-chip"));
    detail.append(section);
  }
  if (job.job_url) {
    try {
      const url = new URL(job.job_url);
      if (url.protocol === "https:" || url.protocol === "http:") {
        const link = textElement("a", "Apply", "apply-link");
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        detail.append(link);
      }
    } catch {
      detail.append(textElement("p", "This posting has an invalid application URL.", "error"));
    }
  }
  const section = document.createElement("section");
  section.className = "description-section";
  section.append(textElement("h2", "Description"), textElement("div", job.description || "No description available.", "description"));
  detail.append(section);
}

async function loadMatches() {
  if (!activeQuery) return;
  const requestId = ++latestRequest;
  const params = new URLSearchParams({ profile: activeQuery.profile, page });
  if (activeQuery.location.length) params.set("location", JSON.stringify(activeQuery.location));
  $("match-status").textContent = "Searching all eligible postings… The first search may take longer while matrix positions are prepared.";
  $("job-list").replaceChildren();
  $("job-detail").replaceChildren(textElement("p", "Select a matching job to review its details.", "empty-state"));
  try {
    const data = await getJSON(`/api/resume-matches?${params}`);
    if (requestId !== latestRequest) return;
    jobs = data.jobs;
    page = data.page;
    pages = data.pages;
    $("result-count").textContent = `${data.total.toLocaleString()} postings`;
    $("page-label").textContent = `Page ${page.toLocaleString()} of ${pages.toLocaleString()}`;
    $("first-page").disabled = $("previous-page").disabled = page <= 1;
    $("next-page").disabled = $("last-page").disabled = page >= pages;
    for (const [index, job] of jobs.entries()) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "job-row";
      button.dataset.index = index;
      button.append(
        textElement("span", job.title || "Untitled job", "job-row-title"),
        textElement("span", job.company || "Unknown company", "job-row-company"),
        textElement("span", `${scoreText(job)}${job.matched_title ? ` · Title: ${job.title_score.toFixed(3)} · Matrix: ${job.cluster_score.toFixed(3)}` : ""}`, "job-row-meta"),
      );
      if (job.locations.length) button.append(textElement("span", job.locations.map(locationText).join(" | "), "job-row-meta"));
      $("job-list").append(button);
    }
    if (!jobs.length) $("job-list").append(textElement("p", "No postings match this location.", "empty-state"));
    $("match-status").textContent = "";
  } catch (error) {
    if (requestId !== latestRequest) return;
    jobs = [];
    $("result-count").textContent = "Search failed";
    $("first-page").disabled = $("previous-page").disabled = $("next-page").disabled = $("last-page").disabled = true;
    $("match-status").textContent = `Unable to find matches: ${error.message}`;
  }
}

$("search-button").addEventListener("click", () => {
  const profile = $("candidate").value;
  if (!profile) {
    $("match-status").textContent = "Select a candidate first.";
    return;
  }
  activeQuery = { profile, location: locationPicker.value() };
  page = 1;
  loadMatches();
});
$("candidate").addEventListener("change", () => {
  ++latestRequest;
  activeQuery = null;
  jobs = [];
  $("job-list").replaceChildren();
  $("job-detail").replaceChildren(textElement("p", "Select a matching job to review its details.", "empty-state"));
  $("result-count").textContent = "Select Find matches to search.";
  $("first-page").disabled = $("previous-page").disabled = $("next-page").disabled = $("last-page").disabled = true;
});
$("job-list").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-index]");
  if (!button) return;
  document.querySelectorAll("#job-list .job-row").forEach((row) => row.classList.remove("active"));
  button.classList.add("active");
  show(jobs[Number(button.dataset.index)]);
});
$("first-page").addEventListener("click", () => { page = 1; loadMatches(); });
$("previous-page").addEventListener("click", () => { if (page > 1) { page--; loadMatches(); } });
$("next-page").addEventListener("click", () => { if (page < pages) { page++; loadMatches(); } });
$("last-page").addEventListener("click", () => { page = pages; loadMatches(); });
loadCandidates();
locationPicker.load();
refreshIndexStatus();
