import * as THREE from "three";

const ROUTE_COLORS = { closest: "#f0c84b", fewest: "#4bc8f0" };
const ROUTE_NOTES = {
  closest: "Smallest total change in skills: many small steps, each between very similar jobs.",
  fewest: "Fewest job changes: bigger leaps, but fewer of them.",
};
const $ = (id) => document.getElementById(id);

const state = { from: null, to: null, paths: null, mode: "closest", graph: null,
  countries: [], selected: new Set(), active: null, version: "" };   // active: Uint8Array mask, null = every job
const isActive = (id) => !state.active || state.active[id] === 1;

// Query-string fragment for the current country selection ("" = no filtering).
function countryParam() {
  const { countries, selected } = state;
  return countries.length && selected.size && selected.size < countries.length
    ? `&countries=${encodeURIComponent([...selected].join(","))}` : "";
}
let scene, camera, renderer, points, pointColors, baseColors, pointSizes, positions, edgeLines;
let homeDistance = 120, routeGroup, labelEls = [], hovered = -1;

// The camera is not user-controlled: it orbits `axis` through `center` on its own, and eases
// toward `goal` whenever a route is shown or cleared.
const SPIN_SPEED = 0.12;   // radians per second
const view = { center: new THREE.Vector3(), axis: new THREE.Vector3(0, 1, 0), offset: new THREE.Vector3(0, 0, 120) };
const goal = { center: new THREE.Vector3(), axis: new THREE.Vector3(0, 1, 0), offset: new THREE.Vector3(0, 0, 120) };
let lastFrame = performance.now();

const familyColor = (family) => new THREE.Color().setHSL(((family * 0.618034) % 1), 0.62, 0.62);

async function getJson(url) {
  // Tell the server which data version this page loaded; ids from an older one would be wrong.
  if (state.version && url.startsWith("/api/matrix/")) url += `${url.includes("?") ? "&" : "?"}v=${state.version}`;
  const response = await fetch(url);
  const body = await response.json();
  if (response.status === 409 && body.stale) { location.reload(); throw new Error(body.error); }
  if (!response.ok) throw new Error(body.error || response.statusText);
  return body;
}

function showError(message) { $("matrix-error").textContent = message || ""; }

// ---------- 3D scene ----------
function initScene(graph) {
  const wrap = $("canvas-wrap");
  scene = new THREE.Scene();
  scene.background = new THREE.Color("#05060f");
  camera = new THREE.PerspectiveCamera(55, wrap.clientWidth / wrap.clientHeight, 0.1, 2000);
  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(wrap.clientWidth, wrap.clientHeight);
  wrap.appendChild(renderer.domElement);
  const n = graph.title.length;
  positions = new Float32Array(graph.pos);
  baseColors = new Float32Array(n * 3);
  pointColors = new Float32Array(n * 3);
  pointSizes = new Float32Array(n);
  const palette = graph.families.map((_, i) => familyColor(i));
  for (let i = 0; i < n; i++) {
    const color = palette[graph.family[i]];
    baseColors.set([color.r, color.g, color.b], i * 3);
    pointSizes[i] = (graph.trusted[i] ? 0.9 : 0.5) + graph.size[i] * 1.8;
  }
  pointColors.set(baseColors);

  // Frame the dense core: a few far-out stray points shouldn't push the camera away.
  const radii = Array.from({ length: n }, (_, i) =>
    Math.hypot(positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2])).sort((a, b) => a - b);
  homeDistance = Math.max(20, radii[Math.floor(n * 0.95)] * 2.3);
  goHome();
  view.center.copy(goal.center); view.axis.copy(goal.axis); view.offset.copy(goal.offset);

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(pointColors, 3));
  geometry.setAttribute("size", new THREE.BufferAttribute(pointSizes, 1));
  points = new THREE.Points(geometry, new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    uniforms: { scale: { value: wrap.clientHeight / 2 } },
    vertexShader: `attribute float size; attribute vec3 color; varying vec3 vColor; uniform float scale;
      void main() { vColor = color; vec4 mv = modelViewMatrix * vec4(position, 1.0);
        gl_PointSize = size * scale / -mv.z; gl_Position = projectionMatrix * mv; }`,
    fragmentShader: `varying vec3 vColor;
      void main() { float d = length(gl_PointCoord - 0.5); if (d > 0.5) discard;
        gl_FragColor = vec4(vColor, smoothstep(0.5, 0.1, d) * 0.85); }`,
  }));
  scene.add(points);

  const edgePositions = new Float32Array(graph.edges.length * 3);
  graph.edges.forEach((node, k) => edgePositions.set(positions.subarray(node * 3, node * 3 + 3), k * 3));
  const edgeGeometry = new THREE.BufferGeometry();
  edgeGeometry.setAttribute("position", new THREE.BufferAttribute(edgePositions, 3));
  edgeLines = new THREE.LineSegments(edgeGeometry, new THREE.LineBasicMaterial({
    color: "#8c90b8", transparent: true, opacity: 0.06, depthWrite: false }));
  scene.add(edgeLines);

  routeGroup = new THREE.Group();
  scene.add(routeGroup);
  window.addEventListener("resize", onResize);
  renderer.domElement.addEventListener("pointermove", onPointerMove);
  renderer.domElement.addEventListener("click", onClick);
  renderer.domElement.addEventListener("pointerleave", () => setHover(-1));
  animate();
}

function onResize() {
  const wrap = $("canvas-wrap");
  camera.aspect = wrap.clientWidth / wrap.clientHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(wrap.clientWidth, wrap.clientHeight);
  points.material.uniforms.scale.value = wrap.clientHeight / 2;
}

function goHome() {
  goal.center.set(0, 0, 0);
  goal.axis.set(0, 1, 0);
  goal.offset.set(0, homeDistance * 0.25, homeDistance * 0.97);
}

function animate(now = performance.now()) {
  requestAnimationFrame(animate);
  const dt = Math.min(0.1, (now - lastFrame) / 1000);
  lastFrame = now;
  const spin = SPIN_SPEED * dt;
  goal.offset.applyAxisAngle(goal.axis, spin);
  view.offset.applyAxisAngle(view.axis, spin);
  const ease = 1 - Math.pow(0.001, dt);   // reaches the goal in roughly a second
  view.center.lerp(goal.center, ease);
  view.axis.lerp(goal.axis, ease).normalize();
  view.offset.lerp(goal.offset, ease);
  camera.up.copy(view.axis);
  camera.position.copy(view.center).add(view.offset);
  camera.lookAt(view.center);
  renderer.render(scene, camera);
  placeLabels();
}

// Nearest point to the cursor in screen space (cheap enough for ~20k points on hover).
function pickNode(event) {
  const rect = renderer.domElement.getBoundingClientRect();
  const mx = event.clientX - rect.left, my = event.clientY - rect.top;
  scene.updateMatrixWorld();
  const v = new THREE.Vector3();
  let best = -1, bestDistance = 12 * 12;
  for (let i = 0; i < positions.length / 3; i++) {
    v.set(positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2]).applyMatrix4(scene.matrixWorld).project(camera);
    if (v.z > 1 || !isActive(i)) continue;
    const dx = (v.x * 0.5 + 0.5) * rect.width - mx, dy = (-v.y * 0.5 + 0.5) * rect.height - my;
    const distance = dx * dx + dy * dy - pointSizes[i] * 2;
    if (distance < bestDistance) { bestDistance = distance; best = i; }
  }
  return best;
}

let pickTimer = 0;
function onPointerMove(event) {
  const tooltip = $("tooltip");
  tooltip.style.left = `${event.offsetX + 14}px`;
  tooltip.style.top = `${event.offsetY + 14}px`;
  if (event.buttons) return;
  cancelAnimationFrame(pickTimer);
  pickTimer = requestAnimationFrame(() => setHover(pickNode(event)));
}

function setHover(node) {
  if (node === hovered) return;
  hovered = node;
  const tooltip = $("tooltip");
  tooltip.hidden = node < 0;
  if (node >= 0) tooltip.textContent = state.graph.title[node];
  renderer.domElement.style.cursor = node >= 0 ? "pointer" : "default";
}

function onClick() {
  if (hovered >= 0) selectNode(hovered);
}

// ---------- routes ----------
function colorPoints(highlight) {
  const lit = highlight ? new Set(highlight) : null;
  for (let i = 0; i < pointSizes.length; i++) {
    // Jobs outside the country selection are fully hidden (additive blending: black adds nothing).
    const f = !isActive(i) ? 0 : lit ? (lit.has(i) ? 1.4 : 0.07) : 1;
    for (let c = 0; c < 3; c++) pointColors[i * 3 + c] = Math.min(1, baseColors[i * 3 + c] * f);
  }
  points.geometry.attributes.color.needsUpdate = true;
  edgeLines.material.opacity = highlight ? 0.02 : 0.06;
}

// Only draw background edges whose two ends are both in the selection.
function updateEdges() {
  const edges = state.graph.edges;
  if (!state.active) { edgeLines.geometry.setIndex(null); return; }
  const keep = [];
  for (let k = 0; k < edges.length; k += 2) {
    if (state.active[edges[k]] && state.active[edges[k + 1]]) keep.push(k, k + 1);
  }
  edgeLines.geometry.setIndex(keep);
}

function drawRoute() {
  routeGroup.clear();
  labelEls.forEach((el) => el.remove());
  labelEls = [];
  const route = state.paths?.[state.mode];
  if (!route) { colorPoints(null); return; }
  colorPoints(route.nodes);
  const color = new THREE.Color(ROUTE_COLORS[state.mode]);
  const vec = (i) => new THREE.Vector3(positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2]);
  const pts = route.nodes.map(vec);
  for (let k = 1; k < pts.length; k++) {
    const tube = new THREE.Mesh(
      new THREE.TubeGeometry(new THREE.LineCurve3(pts[k - 1], pts[k]), 1, 0.3, 6, false),
      new THREE.MeshBasicMaterial({ color }));
    routeGroup.add(tube);
  }
  route.nodes.forEach((node, k) => {
    const end = k === 0 || k === route.nodes.length - 1;
    const ball = new THREE.Mesh(new THREE.SphereGeometry(end ? 1.1 : 0.7, 16, 12),
      new THREE.MeshBasicMaterial({ color: end ? "#ffffff" : color }));
    ball.position.copy(pts[k]);
    routeGroup.add(ball);
    const label = document.createElement("div");
    label.className = "path-label";
    label.textContent = end ? `${k + 1}. ${state.graph.title[node]}` : `${k + 1}`;
    label.dataset.index = k;
    $("canvas-wrap").parentElement.appendChild(label);
    labelEls.push(label);
  });
  flyTo(pts);
}

function placeLabels() {
  if (!labelEls.length) return;
  const route = state.paths?.[state.mode];
  const rect = renderer.domElement.getBoundingClientRect();
  const v = new THREE.Vector3();
  labelEls.forEach((el) => {
    const node = route.nodes[+el.dataset.index];
    v.set(positions[node * 3], positions[node * 3 + 1], positions[node * 3 + 2]).applyMatrix4(scene.matrixWorld).project(camera);
    const visible = v.z < 1;
    el.style.display = visible ? "block" : "none";
    el.style.left = `${(v.x * 0.5 + 0.5) * rect.width}px`;
    el.style.top = `${(-v.y * 0.5 + 0.5) * rect.height}px`;
  });
}

function flyTo(pts) {
  const box = new THREE.Box3().setFromPoints(pts);
  const radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 25);
  goal.center.copy(box.getCenter(new THREE.Vector3()));
  // Spin around the route's own line so every step stays spread across the screen.
  const along = pts[pts.length - 1].clone().sub(pts[0]);
  goal.axis.copy(along.lengthSq() > 1e-6 ? along.normalize() : new THREE.Vector3(0, 1, 0));
  const side = view.offset.clone();
  side.addScaledVector(goal.axis, -side.dot(goal.axis));
  if (side.lengthSq() < 1e-4) side.crossVectors(goal.axis, new THREE.Vector3(1, 0, 0));
  goal.offset.copy(side.normalize().multiplyScalar(radius * 2.4));
}

function renderResult() {
  const result = $("result");
  const route = state.paths?.[state.mode];
  result.hidden = !route;
  if (!route) return;
  const color = ROUTE_COLORS[state.mode];
  result.style.setProperty("--tab-color", color);
  document.querySelectorAll(".route-tabs button").forEach((b) =>
    b.setAttribute("aria-selected", b.dataset.mode === state.mode));
  document.querySelector(".route-tabs").style.setProperty("--tab-color", color);
  $("route-note").textContent = ROUTE_NOTES[state.mode];
  $("stat-distance").textContent = route.distance.toFixed(2);
  $("stat-hops").textContent = route.hops;
  $("stat-jump").textContent = route.largest_jump.toFixed(2);
  const list = $("steps");
  list.innerHTML = "";
  route.steps.forEach((step) => {
    const li = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = step.title;
    const sub = document.createElement("small");
    sub.textContent = (step.similarity !== undefined ? `${Math.round(step.similarity * 100)}% similar to previous · ` : "start · ")
      + step.family_name;
    li.append(title, sub);
    li.addEventListener("click", () => showNode(step.id));
    list.appendChild(li);
  });
}

// ---------- pickers ----------
function setupPicker(role) {
  const root = document.querySelector(`.picker[data-role="${role}"]`);
  const input = root.querySelector("input"), list = root.querySelector(".picker-list");
  let timer = 0, results = [], active = -1, seq = 0;

  const close = () => { list.hidden = true; active = -1; };
  const choose = (item) => { setNode(role, item); close(); };
  const paint = () => {
    list.innerHTML = "";
    if (!results.length) {
      const li = document.createElement("li");
      li.className = "none";
      li.textContent = "No matching jobs";
      list.appendChild(li);
    }
    results.forEach((item, i) => {
      const li = document.createElement("li");
      li.className = i === active ? "active" : "";
      const name = document.createElement("span");
      name.textContent = item.title;
      const meta = document.createElement("small");
      meta.textContent = `${item.family_name} · ${item.postings.toLocaleString()} posting${item.postings === 1 ? "" : "s"}`;
      li.append(name, meta);
      li.addEventListener("mousedown", (e) => { e.preventDefault(); choose(item); });
      list.appendChild(li);
    });
    list.hidden = false;
  };

  input.addEventListener("input", () => {
    root.classList.remove("chosen");
    state[role] = null;
    updateButton();
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) { close(); return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      try {
        const data = await getJson(`/api/matrix/search?q=${encodeURIComponent(q)}${countryParam()}`);
        if (mine !== seq) return;
        results = data.results; active = results.length ? 0 : -1;
        paint();
      } catch (error) { showError(error.message); }
    }, 140);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!results.length) return;
      active = (active + (e.key === "ArrowDown" ? 1 : -1) + results.length) % results.length;
      paint(); e.preventDefault();
    } else if (e.key === "Enter" && !list.hidden && results[active]) {
      choose(results[active]); e.preventDefault();
    } else if (e.key === "Escape") close();
  });
  input.addEventListener("blur", () => setTimeout(close, 100));
}

function setNode(role, item) {
  const root = document.querySelector(`.picker[data-role="${role}"]`);
  state[role] = item;
  const input = root.querySelector("input");
  input.value = item ? item.title : "";
  root.classList.toggle("chosen", !!item);
  updateButton();
  if (item) showNode(item.id);
}

function updateButton() { $("find-path").disabled = !(state.from && state.to); }

async function showNode(id) {
  try {
    const node = await getJson(`/api/matrix/node?id=${id}${countryParam()}`);
    const box = $("node-info");
    box.hidden = false;
    box.innerHTML = "";
    const h = document.createElement("h2"); h.textContent = node.title;
    const meta = document.createElement("small");
    meta.textContent = `${node.family_name} · ${node.postings.toLocaleString()} postings · ${node.companies} compan${node.companies === 1 ? "y" : "ies"}`;
    const where = document.createElement("small");
    if (node.countries) {
      const seen = node.countries.map((c) => `${c.name} (${c.postings.toLocaleString()})`).join(", ");
      where.textContent = seen && `Seen in: ${seen}`;
    }
    const snippet = document.createElement("p"); snippet.textContent = node.snippet.length >= 500 ? `${node.snippet}…` : node.snippet;
    const label = document.createElement("p"); label.textContent = "Most similar jobs";
    const chips = document.createElement("div"); chips.className = "chips";
    node.neighbors.forEach((nb) => {
      const chip = document.createElement("span");
      chip.textContent = `${nb.title} · ${Math.round(nb.similarity * 100)}%`;
      chip.addEventListener("click", () => showNode(nb.id));
      chips.appendChild(chip);
    });
    const buttons = document.createElement("div"); buttons.className = "pick-buttons";
    for (const [role, text] of [["from", "Use as start"], ["to", "Use as destination"]]) {
      const b = document.createElement("button"); b.type = "button"; b.textContent = text;
      b.addEventListener("click", () => setNode(role, node));
      buttons.appendChild(b);
    }
    box.append(h, meta, where, snippet, label, chips, buttons);
  } catch (error) { showError(error.message); }
}

function selectNode(id) {
  const title = state.graph.title[id];
  const item = { id, title };
  if (!state.from) setNode("from", item);
  else if (!state.to && state.from.id !== id) setNode("to", item);
  else showNode(id);
}

async function findPath() {
  showError("");
  try {
    state.paths = await getJson(`/api/matrix/path?from=${state.from.id}&to=${state.to.id}${countryParam()}`);
    state.mode = "closest";
    renderResult();
    drawRoute();
  } catch (error) {
    state.paths = null;
    renderResult();
    drawRoute();
    showError(error.message);
  }
}

function reset() {
  state.paths = null;
  setNode("from", null); setNode("to", null);
  $("node-info").hidden = true;
  renderResult(); drawRoute();
  goHome();
}

// ---------- country filter ----------
function summarize() {
  const { countries, selected } = state;
  const el = $("country-summary");
  if (!selected.size) el.textContent = "none selected";
  else if (selected.size === countries.length) el.textContent = "All";
  else if (selected.size <= 2) el.textContent = countries.filter((c) => selected.has(c.code)).map((c) => c.name).join(", ");
  else el.textContent = `${selected.size} of ${countries.length}`;
}

let filterTimer = 0;
function scheduleFilter() {
  summarize();
  clearTimeout(filterTimer);
  filterTimer = setTimeout(applyFilter, 250);
}

async function applyFilter() {
  if (!state.selected.size) {
    state.active = new Uint8Array(state.graph.title.length);
    updateEdges();
    colorPoints(null);
    $("stage-status").textContent = "Select at least one country";
    return;
  }
  try {
    const data = await getJson(`/api/matrix/filter?${countryParam().slice(1)}`);
    if (data.all) state.active = null;
    else { state.active = new Uint8Array(state.graph.title.length); data.active.forEach((i) => { state.active[i] = 1; }); }
    updateEdges();
    $("stage-status").textContent = `${data.count.toLocaleString()} of ${state.graph.title.length.toLocaleString()} jobs shown`;
    for (const role of ["from", "to"]) if (state[role] && !isActive(state[role].id)) setNode(role, null);
    if (state.paths) {
      if (state.from && state.to) await findPath();
      else { state.paths = null; renderResult(); drawRoute(); }
    } else colorPoints(null);
    showError("");
  } catch (error) { showError(error.message); }
}

async function setupCountries() {
  let data;
  try { data = await getJson("/api/matrix/countries"); } catch { return; }
  if (!data.countries.length) return;
  state.countries = data.countries;
  state.selected = new Set(data.countries.map((c) => c.code));
  const list = $("country-list");
  const rows = data.countries.map((c) => {
    const row = document.createElement("label");
    row.className = "country-row";
    row.dataset.name = c.name.toLowerCase();
    const box = document.createElement("input");
    box.type = "checkbox"; box.checked = true; box.value = c.code;
    const name = document.createElement("span"); name.textContent = c.name;
    const count = document.createElement("small"); count.textContent = c.jobs.toLocaleString();
    row.title = `${c.jobs.toLocaleString()} jobs · ${c.postings.toLocaleString()} postings`;
    row.append(box, name, count);
    list.appendChild(row);
    return row;
  });
  list.addEventListener("change", (e) => {
    (e.target.checked ? state.selected.add(e.target.value) : state.selected.delete(e.target.value));
    scheduleFilter();
  });
  const setAll = (on) => {
    rows.forEach((row) => { row.querySelector("input").checked = on; });
    state.selected = new Set(on ? data.countries.map((c) => c.code) : []);
    scheduleFilter();
  };
  $("country-all").addEventListener("click", () => setAll(true));
  $("country-none").addEventListener("click", () => setAll(false));
  $("country-search").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    rows.forEach((row) => { row.hidden = !!q && !row.dataset.name.includes(q); });
  });
  $("country-note").textContent = "Only postings with a location are included. Numbers are jobs per country.";
  $("country-filter").hidden = false;
  summarize();
}

async function main() {
  setupPicker("from");
  setupPicker("to");
  $("find-path").addEventListener("click", findPath);
  $("clear").addEventListener("click", reset);
  $("swap").addEventListener("click", () => {
    const [a, b] = [state.from, state.to];
    setNode("from", b); setNode("to", a);
    if (a && b) findPath();
  });
  document.querySelectorAll(".route-tabs button").forEach((b) =>
    b.addEventListener("click", () => { state.mode = b.dataset.mode; renderResult(); drawRoute(); }));
  try {
    state.graph = await getJson("/api/matrix/graph");
    state.version = state.graph.version || "";
    initScene(state.graph);
    await setupCountries();
    $("stage-status").textContent = `${state.graph.title.length.toLocaleString()} jobs · ${state.graph.families.length} career families`;
  } catch (error) {
    $("stage-status").textContent = "";
    showError(error.message);
  }
}

main();
