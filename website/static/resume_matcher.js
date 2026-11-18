const $ = (id) => document.getElementById(id);
let profiles = [];
let selectedId = null;
let editingIndex = null;
let busy = false;

function status(message) {
  $("status").textContent = message;
}

function selectedProfile() {
  return profiles.find((profile) => profile.id === selectedId);
}

function resetExperienceForm() {
  editingIndex = null;
  $("experience-form").reset();
  $("end").disabled = false;
  $("form-title").textContent = "Add work experience";
  $("save-experience").textContent = "Add experience";
  $("cancel-edit").hidden = true;
}

function makeButton(label, className, action, index) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.dataset.action = action;
  if (index !== undefined) button.dataset.index = index;
  return button;
}

function render() {
  const list = $("profile-list");
  list.replaceChildren();
  for (const profile of profiles) {
    const button = makeButton(profile.name, "profile-button", "select");
    button.dataset.id = profile.id;
    if (profile.id === selectedId) button.setAttribute("aria-current", "true");
    list.append(button);
  }
  const profile = selectedProfile();
  $("select-message").hidden = Boolean(profile);
  $("profile-detail").hidden = !profile;
  if (!profile) return;
  $("profile-name").value = profile.name;
  $("experience-count").textContent = `${profile.experiences.length} entries`;
  const experienceList = $("experience-list");
  experienceList.replaceChildren();
  if (!profile.experiences.length) {
    const empty = document.createElement("p");
    empty.className = "resume-hint";
    empty.textContent = "No work experience added yet.";
    experienceList.append(empty);
  }
  profile.experiences.forEach((experience, index) => {
    const card = document.createElement("article");
    card.className = "experience-card";
    const title = document.createElement("h3");
    title.textContent = experience.title;
    card.append(title);
    if (experience.company) {
      const company = document.createElement("p");
      company.textContent = experience.company;
      card.append(company);
    }
    const dates = [experience.start || "Start unspecified", experience.current ? "Present" : experience.end || "End unspecified"];
    if (experience.start || experience.end || experience.current) {
      const meta = document.createElement("p");
      meta.className = "experience-meta";
      meta.textContent = dates.join(" — ");
      card.append(meta);
    }
    if (experience.description) {
      const description = document.createElement("p");
      description.className = "experience-description";
      description.textContent = experience.description;
      card.append(description);
    }
    const actions = document.createElement("div");
    actions.className = "form-actions";
    actions.append(
      makeButton("Edit", "refresh-button", "edit", index),
      makeButton("Delete", "resume-danger", "delete", index),
    );
    card.append(actions);
    experienceList.append(card);
  });
}

async function request(options) {
  const response = await fetch("/api/resume-profiles", options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

async function perform(operation) {
  if (busy) return null;
  busy = true;
  document.querySelectorAll(".resume-main button").forEach((button) => { button.disabled = true; });
  try {
    return await operation();
  } catch (error) {
    status(`Unable to save or load profiles: ${error.message}`);
    return null;
  } finally {
    busy = false;
    document.querySelectorAll(".resume-main button").forEach((button) => { button.disabled = false; });
  }
}

async function loadProfiles() {
  const result = await perform(() => request());
  if (!result) return;
  profiles = result.profiles;
  if (!selectedProfile()) selectedId = null;
  resetExperienceForm();
  render();
  status("Profiles loaded.");
}

async function change(action, profile, extra = {}) {
  const result = await perform(() => request({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, profile, ...extra }),
  }));
  if (!result) return false;
  profiles = result.profiles;
  selectedId = action === "delete" ? null : result.id;
  render();
  status(action === "delete" ? "Candidate deleted." : "Changes saved.");
  return true;
}

$("create-profile").addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = $("new-name").value.trim();
  if (!name) return status("Enter a candidate name.");
  if (await change("create", { name, experiences: [] })) {
    $("create-profile").reset();
    resetExperienceForm();
  }
});

$("profile-list").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action='select']");
  if (!button || busy || selectedId === button.dataset.id) return;
  selectedId = button.dataset.id;
  resetExperienceForm();
  render();
  status("");
});

$("profile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const profile = selectedProfile();
  const name = $("profile-name").value.trim();
  if (!name) return status("Enter a candidate name.");
  await change("update", { name, experiences: profile.experiences }, { id: profile.id, version: profile.version });
});

$("delete-profile").addEventListener("click", async () => {
  const profile = selectedProfile();
  if (!profile || !confirm(`Delete ${profile.name} and all their work experience?`)) return;
  if (await change("delete", null, { id: profile.id, version: profile.version })) resetExperienceForm();
});

$("current").addEventListener("change", () => {
  $("end").disabled = $("current").checked;
  if ($("current").checked) $("end").value = "";
});

$("experience-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const profile = selectedProfile();
  const title = $("job-title").value.trim();
  if (!title) return status("Enter a job title.");
  const experience = {
    title,
    company: $("company").value.trim(),
    start: $("start").value,
    end: $("end").value,
    current: $("current").checked,
    description: $("description").value.trim(),
  };
  if (experience.start && experience.end && experience.end < experience.start) {
    return status("End date must not be before start date.");
  }
  const experiences = profile.experiences.slice();
  if (editingIndex === null) experiences.push(experience);
  else experiences[editingIndex] = experience;
  if (await change("update", { name: profile.name, experiences }, { id: profile.id, version: profile.version })) {
    resetExperienceForm();
  }
});

$("experience-list").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  const profile = selectedProfile();
  if (!button || !profile || busy) return;
  const index = Number(button.dataset.index);
  const experience = profile.experiences[index];
  if (!experience) return;
  if (button.dataset.action === "edit") {
    editingIndex = index;
    $("job-title").value = experience.title;
    $("company").value = experience.company;
    $("start").value = experience.start;
    $("end").value = experience.end;
    $("current").checked = experience.current;
    $("end").disabled = experience.current;
    $("description").value = experience.description;
    $("form-title").textContent = "Edit work experience";
    $("save-experience").textContent = "Save experience";
    $("cancel-edit").hidden = false;
    $("job-title").focus();
  } else if (button.dataset.action === "delete" && confirm(`Delete ${experience.title}?`)) {
    const experiences = profile.experiences.filter((_, itemIndex) => itemIndex !== index);
    if (await change("update", { name: profile.name, experiences }, { id: profile.id, version: profile.version })) {
      resetExperienceForm();
    }
  }
});

$("cancel-edit").addEventListener("click", resetExperienceForm);
$("reload").addEventListener("click", loadProfiles);
loadProfiles();
