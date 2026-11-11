const $ = (id) => document.getElementById(id);
let page = 1;
let pages = 1;
let jobs = [];
let latestRequest = 0;
let locationOptions = [];
const selectedLocations = new Map();
const countryNames = typeof Intl.DisplayNames === "function"
  ? new Intl.DisplayNames(["en"], { type: "region" }) : null;

function countryName(code) {
  try { return countryNames?.of(code) || code; } catch { return code; }
}

function checkbox(label, checked, onChange) {
  const wrapper = document.createElement("label");
  wrapper.className = "location-choice";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = checked;
  input.addEventListener("change", () => onChange(input.checked));
  const text = document.createElement("span");
  text.textContent = label;
  wrapper.append(input, text);
  return wrapper;
}

function cityChoices(container, cities, chosen) {
  if (!cities.length) return;
  const list = document.createElement("div");
  list.className = "location-children city-choices";
  const heading = document.createElement("h3");
  heading.className = "location-section-heading";
  heading.textContent = "Select city";
  list.append(heading);
  const hint = document.createElement("p");
  hint.className = "location-help";
  hint.textContent = "Select cities, or leave all unchecked to include all.";
  list.append(hint);
  for (const city of cities) {
    list.append(checkbox(city, chosen.has(city), (checked) => {
      if (checked) chosen.add(city); else chosen.delete(city);
    }));
  }
  container.append(list);
}

function renderLocations() {
  const container = $("location-options");
  const menu = container.closest(".location-menu");
  const scrollTop = menu.scrollTop;
  container.replaceChildren();
  $("location-summary").textContent = selectedLocations.size
    ? `${selectedLocations.size} ${selectedLocations.size === 1 ? "country" : "countries"} selected`
    : "All locations";
  if (!locationOptions.length) {
    container.textContent = "No parsed country locations are available yet.";
    return;
  }
  for (const country of locationOptions) {
    const section = document.createElement("div");
    const selection = selectedLocations.get(country.code);
    section.append(checkbox(`${countryName(country.code)} (${country.code})`, !!selection, (checked) => {
      if (checked) selectedLocations.set(country.code, { states: new Map(), cities: new Set() });
      else selectedLocations.delete(country.code);
      renderLocations();
    }));
    if (selection) {
      const stateEntries = Object.entries(country.states);
      if (stateEntries.length) {
        const states = document.createElement("details");
        states.className = "location-children";
        states.open = true;
        const summary = document.createElement("summary");
        summary.textContent = "Select state / province (optional)";
        states.append(summary);
        const hint = document.createElement("p");
        hint.className = "location-help";
        hint.textContent = "Leave all unchecked to include the whole country.";
        states.append(hint);
        for (const [state, cities] of stateEntries) {
          const group = document.createElement("div");
          const selectedCities = selection.states.get(state);
          const stateName = country.state_names[state];
          const stateLabel = stateName && stateName !== state ? `${stateName} (${state})` : state;
          group.append(checkbox(stateLabel, !!selectedCities, (checked) => {
            if (checked) selection.states.set(state, new Set());
            else selection.states.delete(state);
            renderLocations();
          }));
          if (selectedCities) cityChoices(group, cities, selectedCities);
          states.append(group);
        }
        section.append(states);
      } else {
        cityChoices(section, country.cities, selection.cities);
      }
    }
    container.append(section);
  }
  menu.scrollTop = scrollTop;
}

async function loadLocationOptions() {
  try {
    const response = await fetch("/api/location-options");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    if (!data.available) {
      $("location-options").textContent = "Location data is not available in this database.";
      return;
    }
    locationOptions = data.countries.sort((a, b) => countryName(a.code).localeCompare(countryName(b.code)));
    renderLocations();
  } catch (error) {
    $("location-options").textContent = `Unable to load locations: ${error.message}`;
  }
}

function locationFilter() {
  return [...selectedLocations].map(([country, selection]) => ({
    country,
    states: Object.fromEntries([...selection.states].map(([state, cities]) => [state, [...cities]])),
    cities: [...selection.cities],
  }));
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
  if (selectedLocations.size) query.set("location", JSON.stringify(locationFilter()));
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
  $("job-detail").innerHTML = `
    <p class="detail-kicker">${esc(job.company)}</p>
    <h1>${highlight(job.title, titleTerms())}</h1>
    <p class="job-facts">Job ID: ${job.id} · ATS: ${esc(job.ats_vendor || "Unknown")} · Employment type: ${esc(job.employment_type || "n/a")}</p>
    ${facts.length ? `<p class="job-facts">${esc(facts.join(" · "))}</p>` : ""}
    ${job.locations.length ? `<section class="locations-section"><h2>Locations</h2>${job.locations.map((location) => `<span class="location-chip">${esc(locationText(location))}</span>`).join("")}</section>` : ""}
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
  selectedLocations.clear();
  renderLocations();
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
loadLocationOptions();
