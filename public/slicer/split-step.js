/* Spooler project workspace – the Model and Split steps: the uploaded model's analysis, units and orientation, the 3D
   cut editor with draggable cut planes, splitting, and the parts with their assembly order. Mounted by main.js, which
   owns the step bar; talks to /api/splits/<id> and polls it while a stage runs. */
import * as THREE from "three";
import { OrbitControls } from "three/addons/OrbitControls.js";
import { smoothGeometry } from "../mesh-shading.js";
import { parseMeshes } from "./viewport.js?v=5";

const STATE_LABEL = {
  analyzing: "Analysing", planning: "Planning cuts", ready: "Ready to split", checking: "Checking cuts",
  splitting: "Splitting", split: "Split", queued: "Split", failed: "Failed",
};
const RUNNING = new Set(["analyzing", "planning", "checking", "splitting"]);
const PART_COLORS = [0xff6b2b, 0x3b82f6, 0x22c55e, 0xeab308, 0xa855f7, 0x14b8a6,
                     0xf43f5e, 0x84cc16, 0x0ea5e9, 0xf97316, 0x8b5cf6, 0x10b981];
const CONNECTOR_PRESETS = [
  { id: "rod2",   label: "2 mm steel rod",       kind: "rod",  diameter: 2,    fallback: 1,    depth: 6.5, clearance: 0.15 },
  { id: "rod1",   label: "1 mm steel rod",       kind: "rod",  diameter: 1,    fallback: null, depth: 5,   clearance: 0.15 },
  { id: "pin175", label: "1.75 mm filament pin", kind: "pin",  diameter: 1.75, fallback: null, depth: 5,   clearance: 0.15 },
  { id: "none",   label: "No holes",             kind: "none" },
];
const MIN_SLAB_MM = 5;
const POLL_MS = 1500;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clone = value => JSON.parse(JSON.stringify(value));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const sizeText = size => `${size.map(v => Number(v.toFixed(2))).join(" × ")} mm`;
const cssColor = number => `#${PART_COLORS[(number - 1) % PART_COLORS.length].toString(16).padStart(6, "0")}`;

let host = null;        // { api, toast, onStep(step), onLayout(), onWhole(), onChanged(split) }
let current = null;     // the split's detail record
let view = null;        // "model" | "split" while the step is on screen
let sub = "cuts";       // what the Split step shows: "cuts" | "parts"
let viewer = null;
let cuts = [];          // working copy of the cuts being edited
let selectedCut = null;
let checkTimer = null;
let pollTimer = null;
let pendingSave = false; // cuts changed while a check was already running
let checkDirty = false;  // local cuts differ from the last finished check
let geometryKey = "";
let plan = null;         // how the parts go together (Sections, steps) or why they can't, for the current split
let planKey = "";
let guide = null;        // the Assembly guide job and the PDF on disk for the current split
let guideInfo = null;    // what the server can do: PDF library present, default paper
let guideTimer = null;
let paperChoice = "";
let droppedEdits = null; // edits Try to keep could not keep: [{index, edit, reason}], shown until dismissed
let planEditing = false; // the plan editor is open in the Assembly guide panel
let editStep = null;     // id of the step selected in the editor
let editReceivers = null; // {step id, receivers} the validator accepts for the selected step
let editMessage = "";    // why the last edit was refused, in plain words

function geometryFrom(mesh) {
  return smoothGeometry(mesh.positions, mesh.index);
}

function boxPlanes(box) {
  const { min, max } = box;
  return [
    new THREE.Plane(new THREE.Vector3(1, 0, 0), -min.x), new THREE.Plane(new THREE.Vector3(-1, 0, 0), max.x),
    new THREE.Plane(new THREE.Vector3(0, 1, 0), -min.y), new THREE.Plane(new THREE.Vector3(0, -1, 0), max.y),
    new THREE.Plane(new THREE.Vector3(0, 0, 1), -min.z), new THREE.Plane(new THREE.Vector3(0, 0, -1), max.z),
  ];
}

function projectBox(box, n) {
  let lo = Infinity, hi = -Infinity;
  for (const x of [box.min.x, box.max.x]) for (const y of [box.min.y, box.max.y]) for (const z of [box.min.z, box.max.z]) {
    const d = x * n.x + y * n.y + z * n.z;
    lo = Math.min(lo, d);
    hi = Math.max(hi, d);
  }
  return [lo, hi];
}

function modelBox() {
  const size = current?.analysis?.size || [100, 100, 100];
  return new THREE.Box3(new THREE.Vector3(0, 0, 0), new THREE.Vector3(...size));
}

function cutBox(cut) {
  const whole = modelBox().expandByScalar(2);
  return cut.scope ? new THREE.Box3(new THREE.Vector3(...cut.scope.slice(0, 3)), new THREE.Vector3(...cut.scope.slice(3))).intersect(whole)
                   : whole;
}

function clampOffset(cut, value) {
  const [lo, hi] = projectBox(cutBox(cut), new THREE.Vector3(...cut.normal).normalize());
  return Math.round(Math.min(hi - MIN_SLAB_MM, Math.max(lo + MIN_SLAB_MM, value)) * 10) / 10;
}

// ─── 3D viewer ────────────────────────────────────────────────────────────────
class SplitViewer {
  constructor(container, labelLayer) {
    this.container = container;
    this.labelLayer = labelLayer;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.localClippingEnabled = true;
    container.appendChild(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(40, 1, 1, 100000);
    this.camera.up.set(0, 0, 1);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x3a3d4a, 1.2));
    const key = new THREE.DirectionalLight(0xffffff, 1.8);
    key.position.set(0.6, -1.2, 1.6);
    const fill = new THREE.DirectionalLight(0xffffff, 0.6);
    fill.position.set(-1.4, 1.1, -0.4);
    this.scene.add(key, fill);
    this.model = new THREE.Group();
    this.cutGroup = new THREE.Group();
    this.boxGroup = new THREE.Group();
    this.scene.add(this.model, this.cutGroup, this.boxGroup);
    this.bounds = new THREE.Box3(new THREE.Vector3(), new THREE.Vector3(100, 100, 100));
    this.raycaster = new THREE.Raycaster();
    this.labels = [];
    this.editable = false;
    this.drag = null;
    this.onCutSelected = null;
    this.onCutDragging = null;
    this.onCutMoved = null;

    const canvas = this.renderer.domElement;
    canvas.addEventListener("pointerdown", e => this.pointerDown(e));
    canvas.addEventListener("pointermove", e => this.pointerMove(e));
    canvas.addEventListener("pointerup", e => this.pointerUp(e));
    canvas.addEventListener("pointercancel", e => this.pointerUp(e));
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.resize();
    const tick = () => {
      this.frameRequest = requestAnimationFrame(tick);
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
      this.placeLabels();
    };
    tick();
  }

  resize() {
    const w = this.container.clientWidth || 1, h = this.container.clientHeight || 1;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  dispose() {
    cancelAnimationFrame(this.frameRequest);
    this.resizeObserver.disconnect();
    this.controls.dispose();
    for (const group of [this.model, this.cutGroup, this.boxGroup]) this.clear(group);
    this.clearLabels();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }

  clear(group) {
    group.traverse(obj => {
      obj.geometry?.dispose();
      if (obj.material) (Array.isArray(obj.material) ? obj.material : [obj.material]).forEach(m => m.dispose());
    });
    group.clear();
  }

  clearLabels() {
    this.labels.forEach(label => label.el.remove());
    this.labels = [];
  }

  fit() {
    const center = this.bounds.getCenter(new THREE.Vector3());
    const radius = Math.max(this.bounds.getSize(new THREE.Vector3()).length() / 2, 10);
    this.camera.near = radius / 100;
    this.camera.far = radius * 100;
    this.camera.position.copy(center).add(new THREE.Vector3(1.1, -1.6, 0.9).normalize().multiplyScalar(radius * 2.8));
    this.camera.updateProjectionMatrix();
    this.controls.target.copy(center);
    this.controls.update();
  }

  showModel(meshes) {
    this.clear(this.model);
    this.clearLabels();
    for (const mesh of meshes) {
      this.model.add(new THREE.Mesh(geometryFrom(mesh),
        new THREE.MeshStandardMaterial({ color: 0xb4bac7, roughness: 0.7, metalness: 0.05 })));
    }
    this.bounds = new THREE.Box3().setFromObject(this.model);
  }

  showParts(meshes, parts) {
    this.clear(this.model);
    this.clear(this.cutGroup);
    this.clear(this.boxGroup);
    this.clearLabels();
    const center = new THREE.Box3();
    meshes.forEach((mesh, i) => {
      const part = parts[i];
      const object = new THREE.Mesh(geometryFrom(mesh),
        new THREE.MeshStandardMaterial({ color: PART_COLORS[i % PART_COLORS.length], roughness: 0.65, metalness: 0.05,
                                         emissive: 0x000000 }));
      const b = part.bbox;
      object.userData = { number: part.number, centre: new THREE.Vector3((b[0] + b[3]) / 2, (b[1] + b[4]) / 2, (b[2] + b[5]) / 2) };
      this.model.add(object);
      const el = document.createElement("div");
      el.className = "split-label";
      el.textContent = part.number;
      this.labelLayer.appendChild(el);
      this.labels.push({ el, object });
    });
    this.bounds = center.setFromObject(this.model);
    this.explodeCentre = this.bounds.getCenter(new THREE.Vector3());
  }

  setExplode(amount) {
    for (const object of this.model.children) {
      if (!object.userData.centre) continue;
      object.position.copy(object.userData.centre).sub(this.explodeCentre).multiplyScalar(amount * 0.7);
    }
  }

  highlight(numbers) {
    const wanted = [].concat(numbers ?? []);
    for (const object of this.model.children) {
      object.material.emissive?.setHex(wanted.includes(object.userData.number) ? 0x333333 : 0x000000);
    }
  }

  placeLabels() {
    if (!this.labels.length) return;
    const w = this.container.clientWidth, h = this.container.clientHeight;
    for (const { el, object } of this.labels) {
      const p = object.userData.centre.clone().add(object.position).project(this.camera);
      el.hidden = p.z > 1;
      el.style.transform = `translate(${(p.x + 1) / 2 * w}px, ${(1 - p.y) / 2 * h}px) translate(-50%, -50%)`;
    }
  }

  showCuts(list, selectedId) {
    this.clear(this.cutGroup);
    for (const cut of list) {
      const box = cutBox(cut);
      const n = new THREE.Vector3(...cut.normal).normalize();
      const centre = box.getCenter(new THREE.Vector3());
      const span = box.getSize(new THREE.Vector3()).length();
      const selected = cut.id === selectedId;
      const mesh = new THREE.Mesh(new THREE.PlaneGeometry(span, span), new THREE.MeshBasicMaterial({
        color: selected ? 0xff6b2b : 0x3b82f6, transparent: true, opacity: selected ? 0.5 : 0.25,
        side: THREE.DoubleSide, depthWrite: false, clippingPlanes: boxPlanes(box.clone().expandByScalar(0.5)),
      }));
      mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), n);
      const base = centre.clone().addScaledVector(n, -n.dot(centre));  // plane centre at offset 0
      mesh.position.copy(base).addScaledVector(n, cut.offset);
      mesh.renderOrder = selected ? 2 : 1;
      mesh.userData = { cut, box, n, base };
      this.cutGroup.add(mesh);
    }
  }

  showPieces(pieces) {
    this.clear(this.boxGroup);
    for (const piece of pieces || []) {
      if (piece.fits && !piece.tiny) continue;
      const b = piece.bbox;
      const box = new THREE.Box3(new THREE.Vector3(b[0], b[1], b[2]), new THREE.Vector3(b[3], b[4], b[5]));
      this.boxGroup.add(new THREE.Box3Helper(box, piece.fits ? 0xf59e0b : 0xef4444));
    }
  }

  setRay(event) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.raycaster.setFromCamera(new THREE.Vector2(((event.clientX - rect.left) / rect.width) * 2 - 1,
                                                   -((event.clientY - rect.top) / rect.height) * 2 + 1), this.camera);
  }

  pickCut(event) {
    this.setRay(event);
    const hits = this.raycaster.intersectObjects(this.cutGroup.children, false)
      .filter(hit => hit.object.userData.box.clone().expandByScalar(1).containsPoint(hit.point));
    return hits[0]?.object || null;
  }

  // parameter t of the point on the line origin + n·t closest to the mouse ray, or null when looking along n
  lineParam(event, origin, n) {
    this.setRay(event);
    const { ray } = this.raycaster;
    const w0 = ray.origin.clone().sub(origin);
    const b = ray.direction.dot(n);
    const denom = 1 - b * b;
    return denom < 1e-3 ? null : (n.dot(w0) - b * ray.direction.dot(w0)) / denom;
  }

  pointerDown(event) {
    if (event.button !== 0 || !this.editable) return;
    const mesh = this.pickCut(event);
    if (!mesh) return;
    const { cut, n } = mesh.userData;
    const origin = mesh.position.clone();
    this.controls.enabled = false;
    this.renderer.domElement.setPointerCapture(event.pointerId);
    this.drag = { mesh, cut, n, origin, start: cut.offset, t0: this.lineParam(event, origin, n), y0: event.clientY, moved: false };
    this.onCutSelected?.(cut.id);
  }

  pointerMove(event) {
    const canvas = this.renderer.domElement;
    if (!this.drag) {
      if (this.editable && event.buttons === 0) canvas.style.cursor = this.pickCut(event) ? "grab" : "";
      return;
    }
    const { mesh, cut, n, origin, start, t0, y0 } = this.drag;
    const t = this.lineParam(event, origin, n);
    const delta = t === null || t0 === null
      ? (y0 - event.clientY) * this.bounds.getSize(new THREE.Vector3()).length() / canvas.clientHeight
      : t - t0;
    cut.offset = clampOffset(cut, start + delta);
    mesh.position.copy(mesh.userData.base).addScaledVector(n, cut.offset);
    this.drag.moved = this.drag.moved || cut.offset !== start;
    canvas.style.cursor = "grabbing";
    this.onCutDragging?.(cut);
  }

  pointerUp(event) {
    if (!this.drag) return;
    const { cut, moved } = this.drag;
    this.drag = null;
    this.controls.enabled = true;
    this.renderer.domElement.style.cursor = "";
    if (this.renderer.domElement.hasPointerCapture(event.pointerId)) this.renderer.domElement.releasePointerCapture(event.pointerId);
    if (moved) this.onCutMoved?.(cut);
  }
}

// ─── Showing the step ─────────────────────────────────────────────────────────
function ensureViewer() {
  if (viewer) return;
  try {
    viewer = new SplitViewer($("split-viewer"), $("split-labels"));
    viewer.onCutSelected = id => selectCut(id);
    viewer.onCutDragging = cut => {
      const input = document.querySelector(`[data-cut-offset="${cut.id}"]`);
      if (input) input.value = cut.offset.toFixed(1);
    };
    viewer.onCutMoved = () => cutsEdited();
  } catch (err) {
    $("split-viewer").innerHTML = `<p class="split-viewer-error">The 3D preview isn't available in this browser (${esc(err.message)}).</p>`;
    viewer = null;
  }
}

function render() {
  if (!view) return;
  renderSide();
  renderProgress();
  syncViewer();
}

function renderProgress() {
  if (!view || !current) return;
  const box = $("split-progress");
  const running = RUNNING.has(current.state) && current.state !== "checking";
  box.hidden = !running;
  if (running) {
    const p = current.progress;
    box.querySelector(".split-progress-text").textContent = p?.message || `${STATE_LABEL[current.state]}…`;
    box.querySelector(".job-progress").classList.toggle("job-progress-busy", p?.percent == null);
    box.querySelector(".job-progress-bar").style.width = `${p?.percent ?? 0}%`;
  }
  if (current.state === "checking" && $("split-check")) $("split-check").innerHTML = '<span class="split-muted">Checking the cuts…</span>';
  const splitButton = document.querySelector('#split-side [data-action="split"]');
  if (splitButton) splitButton.disabled = !canSplit();
}

async function syncViewer() {
  if (!view || !viewer || !current) return;
  const partsView = view === "split" && sub === "parts" && current.has_parts_preview;
  $("split-viewer-tools").hidden = !partsView;
  const key = partsView ? `${current.id}:parts:${JSON.stringify(current.result?.summary)}`
                        : `${current.id}:model:${JSON.stringify(current.analysis?.size)}:${current.analysis?.triangles}`;
  if ((partsView || current.has_preview) && key !== geometryKey) {
    geometryKey = key;
    try {
      const response = await fetch(`/api/splits/${current.id}/${partsView ? "parts-preview" : "preview"}`);
      if (!response.ok) throw new Error(`Couldn't load the preview (HTTP ${response.status})`);
      const buffer = await response.arrayBuffer();
      if (!viewer || geometryKey !== key) return;
      if (partsView) {
        viewer.showParts(parseMeshes(buffer), current.result.parts);
        viewer.setExplode(Number($("split-explode").value) / 100);
      } else {
        viewer.showModel(parseMeshes(buffer));
      }
      viewer.fit();
    } catch (err) {
      geometryKey = "";
      host.toast(err.message, true);
      return;
    }
  }
  if (!partsView && viewer) {
    const editing = view === "split" && sub === "cuts";
    viewer.editable = editing && !["analyzing", "planning", "splitting"].includes(current.state);
    viewer.showCuts(editing ? cuts : [], selectedCut);
    viewer.showPieces(editing && !checkDirty ? current.check?.pieces : null);
  }
}

// ─── Side panel content ───────────────────────────────────────────────────────
function renderSide() {
  const side = $("split-side");
  if (!current) { side.innerHTML = '<p class="split-note">Loading the model…</p>'; return; }
  const parts = view !== "model" && sub === "parts" && current.result;
  if (parts) loadPlan();
  side.innerHTML = view === "model" ? sideModel() : parts ? sideParts() : sideCuts();
}

async function loadPlan() {
  const id = current.id, key = `${id}:${current.state}:${current.updated_at}:${JSON.stringify(current.result?.summary)}`;
  if (key === planKey) return;
  planKey = key;
  plan = null;
  if (!guide || guide.split_id !== id) guide = null;
  try {
    const loaded = await host.api("GET", `/api/splits/${id}/assembly-plan`);
    if (!current || current.id !== id || planKey !== key) return;
    plan = loaded;
  } catch (err) {
    if (!current || current.id !== id || planKey !== key) return;
    plan = { error: err.message };
  }
  if (view && sub === "parts") renderSide();
  loadGuide();   // whether the PDF on disk is out of date depends on the plan
}

async function loadGuide() {
  if (!current) return;
  const id = current.id;
  try {
    if (!guideInfo) guideInfo = (await host.api("GET", "/api/splits/status")).guide || { available: true, default_paper: "a4", papers: ["a4", "letter"] };
    const loaded = await host.api("GET", `/api/splits/${id}/guide`);
    if (!current || current.id !== id) return;
    guide = { ...loaded, split_id: id };
  } catch (err) {
    if (!current || current.id !== id) return;
    guide = { state: "failed", error: err.message, blocked: "", has_pdf: false, split_id: id };
  }
  clearTimeout(guideTimer);
  if (guide.state === "generating") guideTimer = setTimeout(loadGuide, 1500);
  if (view && sub === "parts") renderSide();
}

function guidePanel() {
  const info = guideInfo || { available: true, default_paper: "a4", papers: ["a4", "letter"] };
  const g = guide || { state: "none", blocked: "", has_pdf: false };
  const working = g.state === "generating";
  const paper = paperChoice || g.paper || info.default_paper;
  const why = !info.available ? info.hint : g.blocked;
  const disabled = working || !!why;
  const summary = plan?.ok ? `<p class="split-hint">${plural(plan.summary.parts, "part")}, ${plural(plan.summary.sections, "Section")}, ${plural(plan.summary.steps, "step")}, ${plural(plan.summary.rods, "rod")}. Plan code <strong>${esc(plan.code)}</strong>.</p>` : "";
  const warnings = plan?.ok ? (plan.warnings || []).map(w => `<p class="split-warn">${w.step ? `Step ${esc(w.step)}: ` : w.section ? `Section ${esc(w.section)}: ` : ""}${esc(w.message)}</p>`).join("") : "";
  const pdf = g.has_pdf
    ? `<p>${g.out_of_date ? '<span class="split-warn">Out of date: regenerate.</span> ' : ""}PDF made ${esc(new Date(g.generated_at * 1000).toLocaleString())}, plan code ${esc(g.plan_code)}, ${g.paper === "letter" ? "Letter" : "A4"}.
         <a class="btn btn-secondary btn-sm" href="/api/splits/${current.id}/guide.pdf" download>Download PDF</a></p>`
    : "";
  return `<section class="split-section" data-guide>
    <h4>Assembly guide</h4>
    ${summary}${warnings}
    <div class="split-actions-row">
      <label class="split-inline">Paper <select id="split-guide-paper"${working ? " disabled" : ""}>
        ${["a4", "letter"].map(v => `<option value="${v}"${paper === v ? " selected" : ""}>${v === "a4" ? "A4" : "Letter"}</option>`).join("")}</select></label>
      <button class="btn btn-primary btn-sm" data-action="guide"${disabled ? " disabled" : ""}${why ? ' aria-describedby="split-guide-why"' : ""}>${g.has_pdf ? "Generate again" : "Generate"}</button>
    </div>
    ${why ? `<small class="reason-line" id="split-guide-why">${esc(why)}</small>` : ""}
    ${working ? `<div class="split-progress" role="status"><progress max="100" value="${g.percent || 0}"></progress> ${esc(g.stage || "Working")}</div>` : ""}
    ${g.state === "failed" && g.error ? `<div class="job-error" role="alert">${esc(g.error)}</div>` : ""}
    ${pdf}
    ${editsBanner()}
    ${plan?.ok ? `<div class="split-actions-row"><button class="btn btn-secondary btn-sm" data-action="edit-toggle" aria-pressed="${planEditing}">${planEditing ? "Close the plan editor" : "Edit plan"}</button></div>` : ""}
    ${plan?.ok && planEditing ? planEditor() : ""}
    ${fold("split-assembly", "Steps", plan?.ok ? `${plural(plan.summary.sections, "Section")}, ${plural(plan.summary.steps, "step")}` : "", planHtml())}
  </section>`;
}

// ─── Plan editor ──────────────────────────────────────────────────────────────
const stepById = id => plan?.ok ? plan.sections.flatMap(sec => sec.steps).find(st => st.id === id) : null;

function editText(e) {
  if (e.op === "move") return `Move ${listText(e.parts)} to ${listText(e.to)}`;
  return `Reorder the steps of the Section of ${listText(e.section)}`;
}

// The outdated-edits banner (Discard / Try to keep) and, after Try to keep, the edits that were dropped.
function editsBanner() {
  const edits = plan?.ok && plan.edits;
  let html = "";
  if (edits && edits.state === "outdated") {
    html += `<div class="split-warn" role="alert" data-edits-outdated>
      <strong>Your edits are out of date.</strong> The automatic plan is used until you decide. ${esc(edits.reason)}
      <div class="split-actions-row">
        <button type="button" class="btn btn-secondary btn-sm" data-action="edits-discard">Discard</button>
        <button type="button" class="btn btn-primary btn-sm" data-action="edits-keep">Try to keep</button>
      </div></div>`;
  }
  if (droppedEdits) {
    html += `<div class="split-warn" role="status" data-edits-dropped>
      ${droppedEdits.length ? `<strong>${plural(droppedEdits.length, "edit")} could not be kept:</strong>
      <ul>${droppedEdits.map(d => `<li>Edit ${d.index + 1} (${esc(editText(d.edit))}): ${esc(d.reason)}</li>`).join("")}</ul>`
        : "All your edits still work and were kept."}
      <div class="split-actions-row"><button type="button" class="btn btn-secondary btn-sm" data-action="edits-dismiss">OK</button></div></div>`;
  }
  return html;
}

function planEditor() {
  const edits = plan.edits || { state: "none", count: 0 };
  const note = "";
  const picked = stepById(editStep);
  const options = editReceivers && editReceivers.step === editStep ? editReceivers.receivers : null;
  const outline = `<ol class="split-assembly split-edit-outline">${plan.sections.map(sec => `<li><strong>Section ${esc(sec.letter)}</strong>${sec.main ? " (main)" : ""}
      <ol>${sec.steps.map((st, i) => `<li><button type="button" class="btn btn-sm ${st.id === editStep ? "btn-primary" : "btn-secondary"}" data-action="edit-select" data-step="${esc(st.id)}" aria-pressed="${st.id === editStep}">
        ${esc(st.id)}: ${st.kind === "section" ? `Join Section ${esc(st.section)}` : `Add ${listText(st.parts)}`}</button></li>`).join("")}</ol></li>`).join("")}</ol>`;
  const controls = picked ? `<div class="split-actions-row">
      <label class="split-inline">Move to <select id="split-edit-move" ${options ? "" : "disabled"}>
        <option value="">${options ? (options.length ? "Choose where…" : "Nowhere else works") : "Looking…"}</option>
        ${(options || []).map(r => `<option value="${r.parts.join(",")}">${r.kind === "section" ? "Section of " : "Part "}${listText(r.parts)}</option>`).join("")}</select></label>
      <button type="button" class="btn btn-secondary btn-sm" data-action="edit-up" aria-label="Move ${esc(picked.id)} earlier">Up</button>
      <button type="button" class="btn btn-secondary btn-sm" data-action="edit-down" aria-label="Move ${esc(picked.id)} later">Down</button>
    </div>` : '<p class="split-hint">Pick a step to see where it can go. The 3D view shows its parts.</p>';
  return `<section class="split-section split-plan-editor">
    <h4>Edit plan${edits.count ? ` <small>${plural(edits.count, "edit")} saved</small>` : ""}</h4>
    ${note}${outline}${controls}
    ${editMessage ? `<p class="split-bad" role="alert">${esc(editMessage)}</p>` : ""}
    ${edits.count ? '<div class="split-actions-row"><button type="button" class="btn btn-secondary btn-sm" data-action="edit-reset">Reset to automatic</button></div>' : ""}
  </section>`;
}

async function selectEditStep(id) {
  editStep = id;
  editMessage = "";
  editReceivers = null;
  const step = stepById(id);
  viewer?.highlight(step ? step.parts : null);
  renderSide();
  if (!step) return;
  try {
    const found = await host.api("GET", `/api/splits/${current.id}/plan-edits?receivers_for=${step.parts.join(",")}`);
    if (editStep === id) { editReceivers = { step: id, receivers: found.receivers }; renderSide(); }
  } catch (err) {
    if (editStep === id) { editReceivers = { step: id, receivers: [] }; editMessage = err.message; renderSide(); }
  }
}

async function sendEdit(body) {
  editMessage = "";
  try {
    const done = await host.api("POST", `/api/splits/${current.id}/plan-edits`, body);
    plan = done.plan;
    if (body.op === "keep") droppedEdits = done.dropped || [];
    editStep = null;
    editReceivers = null;
    viewer?.highlight(null);
    renderSide();
    loadGuide();   // the PDF on disk may now be out of date
  } catch (err) {
    editMessage = err.message;   // the typed reason in plain words
    renderSide();
  }
}

async function startGuide() {
  guide = await host.api("POST", `/api/splits/${current.id}/guide`, { paper: $("split-guide-paper").value });
  paperChoice = "";
  renderSide();
  loadGuide();
}

const FAILURE_TITLE = { rod_direction: "Rods point in different directions", rod_side: "Rods meet from opposite sides", blocked: "A part is in the way",
  disconnected: "A part is not joined to the others", interlock: "Two parts lock each other", anomaly: "The split's data doesn't add up" };

function dirText(d) {
  const names = ["X", "Y", "Z"], i = d.findIndex(v => Math.abs(v) > 0.999);
  return i < 0 ? d.map(v => v.toFixed(2)).join(", ") : `${d[i] < 0 ? "-" : "+"}${names[i]}`;
}

function planBanner() {
  if (!plan?.failure) return "";
  const f = plan.failure;
  return `<section class="split-section split-failed" role="alert"><h4>${esc(plan.headline)}</h4>
    <p><strong>${esc(FAILURE_TITLE[f.kind] || f.kind)}.</strong> ${esc(f.message)}</p>
    ${f.fixes.length ? `<ol class="split-fixes">${f.fixes.map(x => `<li>${esc(x)}</li>`).join("")}</ol>` : ""}
    ${f.parts.length ? `<div class="split-actions-row"><button class="btn btn-secondary btn-sm" data-show-joint="${f.parts.join(",")}">Show joint</button></div>` : ""}
  </section>`;
}

function planHtml() {
  if (!plan) return '<p class="split-muted">Working out the order…</p>';
  if (plan.error) return `<p class="split-bad">${esc(plan.error)}</p>`;
  if (!plan.ok) return `<p class="split-bad">${esc(plan.headline)}. See the note above.</p>`;
  const where = parts => parts.length > 4 ? `the ${parts.length} parts built so far` : listText(parts);
  return `<ol class="split-assembly">${plan.sections.map(sec => `<li><strong>Section ${sec.letter}</strong>${sec.main ? " (main)" : ""}: ${listText(sec.parts)}
      ${sec.note ? `<span class="split-warn">${esc(sec.note)}</span>` : ""}
      <ol>${sec.steps.map(st => `<li>${st.id}: ${st.kind === "section" ? `Join Section ${st.section} (${listText(st.parts)})` : `Add ${listText(st.parts)}`}
        to ${where(st.receiver)} along ${dirText(st.direction)} — ${st.glue_only ? "glue only" : plural(st.rods, "rod")}.${(st.warnings || []).map(w => ` <span class="split-warn">${esc(w.message)}</span>`).join("")}</li>`).join("")}</ol></li>`).join("")}</ol>`;
}

// Second-level controls sit in folds that stay closed until asked for; which ones are open survives a re-render.
const openFolds = new Set();
const fold = (id, title, summary, body, open = false) => `<details class="split-section slicer-fold" data-fold="${id}"${open || openFolds.has(id) ? " open" : ""}>
  <summary><span>${title}</span>${summary ? `<small>${esc(summary)}</small>` : ""}</summary>${body}</details>`;

function failedBlock() {
  if (current.state !== "failed") return "";
  return `<div class="split-section split-failed"><div class="job-error" role="alert">${esc(current.error || "Something went wrong")}</div>
    <div class="split-actions-row"><button class="btn btn-secondary btn-sm" data-action="retry">Try again</button></div></div>`;
}

function numberField(id, label, value, min, stepValue = 1) {
  return `<label>${label}<input id="${id}" type="number" min="${min}" step="${stepValue}" value="${esc(value)}"></label>`;
}

function viewToggle() {
  if (!current.result) return "";
  return `<div class="split-seg split-view-toggle" role="group" aria-label="Split review view">
    <button aria-pressed="${sub === 'cuts'}" class="btn btn-sm ${sub === "cuts" ? "btn-primary" : "btn-secondary"}" type="button" data-action="sub" data-sub="cuts">Cuts</button>
    <button aria-pressed="${sub === 'parts'}" class="btn btn-sm ${sub === "parts" ? "btn-primary" : "btn-secondary"}" type="button" data-action="sub" data-sub="parts">Parts</button>
  </div>`;
}

function sideModel() {
  const a = current.analysis, s = current.settings;
  if (!a) return failedBlock() || `<section class="split-section"><h4>${esc(current.source_name)}</h4>
    <p class="split-note">Checking the model: reading it, repairing small holes and preparing a lighter copy for the preview.</p></section>`;
  const raw = Math.max(...a.raw_size);
  const hint = Math.max(...a.size) > 3000 ? 'Check the units: a dimension exceeds 3000 mm. If millimetres were treated as metres, enter scale 1 and analyse again; for oversized source coordinates, try scale 0.001.' : raw < 5 && s.scale === 1 ? "This model looks like it's in metres — try ×1000."
             : raw < 60 && s.scale === 1 ? "This is small — if it was modelled in centimetres use ×10, or ×25.4 for inches." : "";
  const blocked = RUNNING.has(current.state) || current.state === 'failed';
  const next = a.fits_whole
    ? `<button class="btn btn-secondary" data-action="go-split"${blocked ? ' disabled' : ''}>Split it anyway</button>
       <button class="btn btn-primary" data-action="whole"${blocked ? ' disabled' : ''}>Put it on a plate →</button>`
    : `<button class="btn btn-primary" data-action="go-split"${blocked ? ' disabled' : ''}>Plan the cuts →</button>`;
  return `${failedBlock()}
  <section class="split-section"><h4>${esc(current.source_name)}</h4>
    ${hint ? `<p class="split-hint split-bad">${esc(hint)}</p>` : ""}
    <dl class="split-facts">
      <dt>Size</dt><dd><strong>${sizeText(a.size)}</strong> · scale ×${s.scale}</dd>
            <dt>One piece?</dt><dd>${a.fits_whole ? "Fits the printer without splitting" : `No — about ${plural(a.estimated_parts, "part")} for this printer`}</dd>
    </dl>
  </section>
  ${fold("model-details", "Details", `${plural(a.source_triangles, "triangle")} · ${a.volume_cm3.toLocaleString()} cm³`, `<dl class="split-facts">
<dt>Source units</dt><dd>${({1:'Millimetres',10:'Centimetres',25.4:'Inches',1000:'Metres'})[s.scale] || 'Custom scale'} · up axis ${esc(s.up.toUpperCase())}</dd>
      <dt>Target bed</dt><dd>${s.printer.bed_x} × ${s.printer.bed_y} mm · height ${s.printer.height} mm</dd>
      <dt>Volume</dt><dd>${a.volume_cm3.toLocaleString()} cm³</dd>
      <dt>Triangles</dt><dd>${a.source_triangles.toLocaleString()}${a.triangles !== a.source_triangles ? ` → ${a.triangles.toLocaleString()} (simplified 0.01 mm)` : ""}</dd>
      <dt>Mesh</dt><dd>${a.repairs.length ? esc(a.repairs.join("; ")) : "Closed solid, no repairs needed"}</dd>
      <dt>Repair state</dt><dd>${current.state === 'failed' ? 'Not ready — resolve the error and retry' : RUNNING.has(current.state) ? 'Processing — wait for analysis and planning to finish' : 'Ready to continue after analysis'}</dd>
      <dt>Average wall</dt><dd>${a.mean_wall_mm} mm</dd>
    </dl>
    <p class="split-hint">Original source is retained. Unit and orientation changes reanalyse that source; they do not overwrite it. Keep your source file to export or import it again.</p>`)}
  ${fold("model-units", "Units and orientation", `${({1:"mm",10:"cm",25.4:"inch",1000:"m"})[s.scale] || "custom"} · ${s.up.toUpperCase()} up`, `
    <p id="split-input-error" class="slicer-error" role="alert"></p>
    <div class="split-row">
      ${numberField("split-scale", "Scale", s.scale, 0.0001, "any")}
      <label>Up axis<select id="split-up">${["z", "y", "x"].map(x => `<option value="${x}"${s.up === x ? " selected" : ""}>${x.toUpperCase()}</option>`).join("")}</select></label>
    </div>
    <div class="split-chips">${[[1, "mm"], [10, "cm"], [25.4, "inch"], [1000, "m"]]
      .map(([f, unit]) => `<button class="btn btn-secondary btn-sm" data-action="scale-preset" data-scale="${f}">${unit} ×${f}</button>`).join("")}</div>
    <div class="split-actions-row"><button class="btn btn-secondary btn-sm" data-action="apply-size">Apply and analyse again</button></div>
  `, !!hint)}
  <div class="split-actions">${next}</div>`;
}

function canSplit() {
  const pieces = current?.check?.pieces || [];
  return !!pieces.length && !pieces.some(p => !p.fits || p.tiny) && !checkDirty && !pendingSave && !RUNNING.has(current.state);
}

function checkText() {
  if (current.state === "checking" || checkDirty) return '<span class="split-muted">Checking the cuts…</span>';
  const pieces = current.check?.pieces || [];
  if (!pieces.length) return '<span class="split-muted">Plan the cuts to see the parts.</span>';
  const oversize = pieces.filter(p => !p.fits).length, tiny = pieces.filter(p => p.tiny).length;
  const problems = [oversize && `${oversize} too big for the printer`, tiny && `${tiny} too small to print`].filter(Boolean);
  return `${plural(pieces.length, "part")} · ${problems.length ? `<span class="split-bad">${problems.join(" · ")}</span>` : "all fit the printer"}`;
}

function axisName(normal) {
  const i = normal.findIndex(v => Math.abs(Math.abs(v) - 1) < 1e-6);
  return i >= 0 ? "XYZ"[i] : "angled";
}

function cutListHtml() {
  if (!cuts.length) return '<p class="queue-empty">No cuts.</p>';
  return cuts.map(cut => `<div class="split-cut${cut.id === selectedCut ? " selected" : ""}" data-action="select-cut" data-cut="${cut.id}">
    <button type="button" class="btn btn-secondary btn-sm" aria-label="Select Cut ${cut.id}" aria-pressed="${cut.id === selectedCut}" data-action="select-cut" data-cut="${cut.id}">Cut ${cut.id}</button><span class="split-cut-axis">${axisName(cut.normal)}</span>
    <input type="number" step="0.5" data-cut-offset="${cut.id}" value="${cut.offset.toFixed(1)}" aria-label="Cut ${cut.id} position (mm)">
    <button type="button" class="btn btn-danger btn-sm" data-action="delete-cut" data-cut="${cut.id}" aria-label="Remove Cut ${cut.id} and its child cuts" title="Remove this cut and the cuts made inside it">✕</button>
  </div>`).join("");
}

function connectorHint(c) {
  if (c.kind === "none") return "No holes — line the parts up by eye when gluing.";
  const hole = (c.diameter + 2 * c.clearance).toFixed(2).replace(/\.?0+$/, "");
  let text = `Holes ${hole} mm wide and ${c.depth} mm deep on each side — cut ${c.kind === "rod" ? "rods" : "pins"} to ${2 * c.depth - 1} mm.`;
  if (c.fallback_diameter) text += ` Walls too thin for that get ${c.fallback_diameter} mm holes.`;
  return text;
}

function sideCuts() {
  const s = current.settings, c = s.connectors, p = s.printer;
  const busy = RUNNING.has(current.state);
  const oversize = (current.check?.pieces || []).filter(x => !x.fits).length;
  const tiny = (current.check?.pieces || []).filter(x => x.tiny).length;
  const grid = s.planner.mode === "grid";
  const preset = CONNECTOR_PRESETS.find(x => x.kind === c.kind && (x.kind === "none" || (x.diameter === c.diameter && x.kind === c.kind)))?.id || "custom";
  return `${viewToggle()}${failedBlock()}
  ${fold("split-printer", "Printer", `${p.bed_x} × ${p.bed_y} × ${p.height} mm`, `
    <div class="split-row">${numberField("split-bed-x", "Bed width", p.bed_x, 50)}${numberField("split-bed-y", "Bed depth", p.bed_y, 50)}${numberField("split-height", "Height", p.height, 50)}</div>
    <p class="split-hint">240 mm keeps Centauri Carbon parts clear of the purge corner.</p>
  `)}
  <section class="split-section"><h4>Cuts</h4>
    <div class="split-seg">
      <label class="split-inline"><input type="radio" name="split-mode" value="smart"${s.planner.mode !== "grid" ? " checked" : ""}> Smart</label>
      <label class="split-inline"><input type="radio" name="split-mode" value="grid"${s.planner.mode === "grid" ? " checked" : ""}> Grid</label>
      <label class="split-inline" id="split-balanced-label"${grid ? "" : ' title="Even grid only applies to Grid cuts"'}><input type="checkbox" id="split-balanced"${s.planner.balanced ? " checked" : ""}${grid ? "" : " disabled"} aria-describedby="split-balanced-why"> Even grid</label>
    </div>
    <p class="split-hint">Smart cuts follow the shape and turn pieces to fit the bed. Grid cuts slice the whole model into bed-sized blocks.</p>
    <p class="split-hint" id="split-balanced-why"${grid ? " hidden" : ""}>Even grid only applies to Grid cuts.</p>
    <div class="split-actions-row">
      <button class="btn btn-secondary btn-sm" data-action="plan"${busy ? " disabled" : ""}>${cuts.length ? "Plan again" : "Plan cuts"}</button>
      <button class="btn btn-secondary btn-sm" data-action="autofix"${busy || !(oversize || tiny) || checkDirty ? " disabled" : ""}
        title="Keep these cuts, remove any that leave a part too small to print, and add more where parts are still too big">Auto-fix</button>
    </div>
    <div id="split-check" class="split-check" role="status">${checkText()}</div>
    <div id="split-cut-list" class="split-cut-list">${cutListHtml()}</div>
    <p class="split-hint">Select a numbered cut and type its position in mm, or drag its plane in the preview. The Remove button deletes that cut and any cuts inside it.</p>
  </section>
  ${fold("split-holes", "Alignment holes", c.kind === "none" ? "None" : `${c.kind === "rod" ? "Rods" : "Pins"} ${c.diameter} mm`, `
    <label>Type<select id="split-conn-preset">${CONNECTOR_PRESETS.map(x => `<option value="${x.id}"${x.id === preset ? " selected" : ""}>${x.label}</option>`).join("")}
      <option value="custom"${preset === "custom" ? " selected" : ""}>Custom</option></select></label>
    <div id="split-conn-fields" class="split-row"${c.kind === "none" ? " hidden" : ""}>
      ${numberField("split-conn-diameter", "Diameter", c.diameter, 0.8, 0.05)}
      ${numberField("split-conn-fallback", "Thin walls", c.fallback_diameter ?? "", 0.8, 0.05)}
      ${numberField("split-conn-clearance", "Clearance", c.clearance, 0, 0.05)}
      ${numberField("split-conn-depth", "Depth", c.depth, 2, 0.5)}
    </div>
    <p class="split-hint" id="split-conn-hint">${esc(connectorHint(c))}</p>
    <label class="split-inline"><input type="checkbox" id="split-labels"${s.labels?.enabled === false ? "" : " checked"}> Number the parts</label>
    <p class="split-hint">Cuts each part's number into a face that is hidden once glued, so you can tell the parts apart on the bench. Turn it off to keep every face clean.</p>
  `)}
  <div class="split-actions"><button class="btn btn-primary" data-action="split"${canSplit() ? "" : " disabled"}>${current.result ? "Split again" : "Split the model"}</button></div>`;
}

function listText(numbers) {
  const names = numbers.map(n => `part ${n}`);
  return names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names[0];
}

function sideParts() {
  const r = current.result;
  const s = r.summary;
  const rods = Object.entries(s.rods).map(([label, count]) => `${count} × ${esc(label)}, ${s.rod_lengths_mm[label]} mm long`).join("<br>") || "None";
  return `${viewToggle()}
  ${planBanner()}
  <section class="split-section"><h4>Summary</h4>
    <dl class="split-facts">
      <dt>Parts</dt><dd>${s.parts} from ${plural(s.cuts, "cut")}</dd>
      <dt>Rods</dt><dd>${rods}</dd>
      <dt>Hole check</dt><dd>${s.holes_not_shared_by_two ? `<span class="split-bad">${plural(s.holes_not_shared_by_two, "hole")} don't line up</span>` : "Every hole is shared by two parts"}${s.holes_removed ? ` · <span class="split-warn">${plural(s.holes_removed, "hole")} dropped</span>` : ""}</dd>
      <dt>Joints</dt><dd>${s.joints}${s.joints_without_rods ? ` · <span class="split-warn">${s.joints_without_rods} glue only</span>` : ""}${s.joints_unkeyed ? ` · <span class="split-warn">${s.joints_unkeyed} can swivel or fit two ways</span>` : ""}</dd>
      <dt>Supports</dt><dd>${s.parts_needing_supports ? `${plural(s.parts_needing_supports, "part")} need${s.parts_needing_supports === 1 ? "s" : ""} supports — they get plates of their own with supports on` : "None needed"}</dd>
    </dl>
  </section>
  <section class="split-section"><h4>Parts</h4>
    <div class="split-table-wrap"><table class="split-table">
      <thead><tr><th>#</th><th>Print size (mm)</th><th>Holes</th><th>Notes</th><th></th></tr></thead>
      <tbody>${r.parts.map(p => `<tr data-part-row="${p.number}">
        <td><button type="button" class="btn btn-secondary btn-sm" data-part-highlight="${p.number}" aria-label="Highlight Part ${p.number}">${p.number}</button><span class="split-swatch" style="background:${cssColor(p.number)}"></span></td>
        <td>${p.print_size.map(v => Math.round(v)).join(" × ")}</td>
        <td>${p.holes}</td>
        <td class="split-notes">${[p.needs_supports ? `<span class="split-warn" title="${p.overhang_pct}% of the surface overhangs more than 45°">supports</span>` : "",
               p.tall_on_small_base ? '<span class="split-warn" title="Tall part on a small footprint">brim</span>' : ""].filter(Boolean).join(" ")}</td>
        <td><a class="btn btn-secondary btn-sm" href="/api/splits/${current.id}/parts/${p.number}.stl" download>STL</a></td>
      </tr>`).join("")}</tbody>
    </table></div>
  </section>
  ${guidePanel()}
  <div class="split-actions"><button class="btn btn-primary" data-action="layout">Lay out on plates →</button></div>`;
}

// ─── Actions ──────────────────────────────────────────────────────────────────
function printerForm() {
  return { bed_x: Number($("split-bed-x").value), bed_y: Number($("split-bed-y").value), height: Number($("split-height").value) };
}

function connectorForm() {
  const preset = CONNECTOR_PRESETS.find(x => x.id === $("split-conn-preset").value);
  if (preset?.kind === "none") return { kind: "none" };
  const fallback = $("split-conn-fallback").value;
  return {
    kind: preset?.kind || "rod",
    diameter: Number($("split-conn-diameter").value),
    fallback_diameter: fallback === "" ? null : Number(fallback),
    clearance: Number($("split-conn-clearance").value),
    depth: Number($("split-conn-depth").value),
  };
}

function labelsForm() {
  return { enabled: $("split-labels").checked };
}

function applyConnectorPreset(id) {
  const preset = CONNECTOR_PRESETS.find(x => x.id === id);
  $("split-conn-fields").hidden = preset?.kind === "none";
  if (preset && preset.kind !== "none") {
    $("split-conn-diameter").value = preset.diameter;
    $("split-conn-fallback").value = preset.fallback ?? "";
    $("split-conn-clearance").value = preset.clearance;
    $("split-conn-depth").value = preset.depth;
  }
  updateConnectorHint();
}

function updateConnectorHint() {
  $("split-conn-hint").textContent = connectorHint(connectorForm());
}

function selectCut(id) {
  const focused = document.activeElement?.matches('[data-action="select-cut"]');
  selectedCut = id;
  if ($("split-cut-list")) $("split-cut-list").innerHTML = cutListHtml();
  if (focused) $('split-cut-list')?.querySelector(`button[data-action="select-cut"][data-cut="${id}"]`)?.focus();
  viewer?.showCuts(view === "split" && sub === "cuts" ? cuts : [], selectedCut);
}

function removeCut(id) {
  const doomed = new Set([id]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const cut of cuts) {
      if (cut.parent && doomed.has(cut.parent[0]) && !doomed.has(cut.id)) {
        doomed.add(cut.id);
        grew = true;
      }
    }
  }
  cuts = cuts.filter(cut => !doomed.has(cut.id));
  if (doomed.has(selectedCut)) selectedCut = null;
  cutsEdited();
}

function cutsEdited() {
  checkDirty = true;
  if ($("split-cut-list")) $("split-cut-list").innerHTML = cutListHtml();
  if ($("split-check")) $("split-check").innerHTML = checkText();
  viewer?.showCuts(cuts, selectedCut);
  viewer?.showPieces(null);
  renderProgress();
  clearTimeout(checkTimer);
  checkTimer = setTimeout(saveCuts, 700);
}

function started(record) {
  Object.assign(current, { state: record.state, stage: record.stage, progress: record.progress, error: record.error,
                           updated_at: record.updated_at, ...(record.settings ? { settings: record.settings } : {}) });
  renderProgress();
  schedulePoll();
  host.onChanged?.(current);
}

async function saveCuts() {
  if (!current) return;
  if (RUNNING.has(current.state)) { pendingSave = true; return; }
  try {
    started(await host.api("POST", `/api/splits/${current.id}/cuts`, { cuts }));
  } catch (err) {
    host.toast(err.message, true);
  }
}

async function planCuts(keepCuts) {
  const record = await host.api("POST", `/api/splits/${current.id}/plan`, {
    mode: document.querySelector('input[name="split-mode"]:checked')?.value || "smart",
    balanced: $("split-balanced").checked,
    printer: printerForm(),
    connectors: connectorForm(),
    keep_cuts: keepCuts,
  });
  started(record);
  render();
}

async function startSplit() {
  if (!canSplit()) return;
  const detail = await host.api("POST", `/api/splits/${current.id}/settings`, { printer: printerForm(), connectors: connectorForm(), labels: labelsForm() });
  current.settings = detail.settings;
  started(await host.api("POST", `/api/splits/${current.id}/split`, {}));
  render();
}

// ─── Following a running stage ────────────────────────────────────────────────
function schedulePoll() {
  clearTimeout(pollTimer);
  if (current && RUNNING.has(current.state)) pollTimer = setTimeout(refresh, POLL_MS);
}

async function refresh() {
  if (!current) return;
  const id = current.id, wasRunning = RUNNING.has(current.state);
  let detail;
  try {
    detail = await host.api("GET", `/api/splits/${id}`);
  } catch (_) {
    pollTimer = setTimeout(refresh, POLL_MS * 2);  // Spooler may be restarting
    return;
  }
  if (!current || current.id !== id) return;
  const finished = wasRunning && !RUNNING.has(detail.state);
  // a new stage or a first analysis/result changes what the panel shows
  const moved = detail.stage !== current.stage || !!detail.analysis !== !!current.analysis || !!detail.result !== !!current.result;
  current = detail;
  if (moved && !finished) {
    if (!checkDirty && !pendingSave) cuts = clone(detail.cuts?.cuts || []);
    render();
  }
  if (finished) {
    if (pendingSave) {
      pendingSave = false;
      saveCuts();
    } else {
      cuts = clone(detail.cuts?.cuts || []);
      checkDirty = false;
    }
    if (detail.state === "split") sub = "parts";
    else if (detail.state === "failed" && detail.stage !== "analyze") sub = "cuts";
    if (selectedCut !== null && !cuts.some(c => c.id === selectedCut)) selectedCut = null;
    render();
  } else {
    renderProgress();
  }
  host.onChanged?.(current);
  schedulePoll();
}

// ─── Wiring ───────────────────────────────────────────────────────────────────
function bindEvents() {
  const side = $("split-side");
  side.addEventListener("toggle", e => {   // toggle does not bubble, so listen on the way down
    const id = e.target.dataset?.fold;
    if (id) e.target.open ? openFolds.add(id) : openFolds.delete(id);
  }, true);
  side.addEventListener("click", async (e) => {
    const el = e.target.closest("[data-action]");
    if (!el || el.disabled || !current) return;
    try {
      switch (el.dataset.action) {
        case "go-split":     host.onStep("split"); break;
        case "whole":        await host.onWhole(); break;
        case "layout":       await host.onLayout(); break;
        case "sub":          sub = el.dataset.sub; render(); break;
        case "retry":        started(await host.api("POST", `/api/splits/${current.id}/retry`, {})); render(); break;
        case "scale-preset": $("split-scale").value = el.dataset.scale; break;
        case "apply-size": {
          if (!$('split-scale').checkValidity() || !Number.isFinite(Number($('split-scale').value)) || Number($('split-scale').value) <= 0) {
            $('split-input-error').textContent = 'Enter a positive scale of at least 0.0001, then analyse again.';
            $('split-scale').focus(); break;
          }
          current = await host.api("POST", `/api/splits/${current.id}/settings`, { scale: Number($("split-scale").value), up: $("split-up").value });
          cuts = clone(current.cuts?.cuts || []);
          geometryKey = "";
          sub = "cuts";
          render();
          schedulePoll();
          host.onChanged?.(current);
          break;
        }
        case "plan":         await planCuts(false); break;
        case "autofix":      await planCuts(true); break;
        case "select-cut":   if (!e.target.closest("input")) selectCut(Number(el.dataset.cut)); break;
        case "delete-cut":   removeCut(Number(el.dataset.cut)); break;
        case "split":        await startSplit(); break;
        case "guide":        await startGuide(); break;
        case "edit-toggle":  planEditing = !planEditing; editStep = null; editMessage = ""; viewer?.highlight(null); renderSide(); break;
        case "edit-select":  await selectEditStep(el.dataset.step); break;
        case "edit-up":
        case "edit-down":    await sendEdit({ op: "reorder", parts: stepById(editStep).parts, direction: el.dataset.action === "edit-up" ? "up" : "down" }); break;
        case "edit-reset":   await sendEdit({ op: "reset" }); break;
        case "edits-discard": droppedEdits = null; await sendEdit({ op: "discard" }); break;
        case "edits-keep":   droppedEdits = null; await sendEdit({ op: "keep" }); break;
        case "edits-dismiss": droppedEdits = null; renderSide(); break;
      }
    } catch (err) {
      if ($('split-input-error')) $('split-input-error').textContent = err.message;
      host.toast(err.message, true);
    }
  });
  side.addEventListener("change", (e) => {
    const t = e.target;
    if (t.dataset.cutOffset) {
      const cut = cuts.find(c => c.id === Number(t.dataset.cutOffset));
      const value = Number(t.value);
      if (cut && Number.isFinite(value)) {
        cut.offset = clampOffset(cut, value);
        cutsEdited();
      }
    } else if (t.id === "split-edit-move") {
      const step = stepById(editStep);
      if (step && t.value) sendEdit({ op: "move", parts: step.parts, to: t.value.split(",").map(Number) });
    } else if (t.name === "split-mode") {
      const grid = t.value === "grid";
      $("split-balanced").disabled = !grid;
      $("split-balanced-why").hidden = grid;
      $("split-balanced-label").title = grid ? "" : "Even grid only applies to Grid cuts";
    } else if (t.id === "split-guide-paper") {
      paperChoice = t.value;
    } else if (t.id === "split-conn-preset") {
      applyConnectorPreset(t.value);
    } else if (t.id?.startsWith("split-conn-")) {
      updateConnectorHint();
    }
  });
  side.addEventListener("mouseover", (e) => {
    const row = e.target.closest("[data-part-row]");
    viewer?.highlight(row ? Number(row.dataset.partRow) : null);
  });
  side.addEventListener('focusin', e => {
    const row = e.target.closest('[data-part-row]');
    viewer?.highlight(row ? Number(row.dataset.partRow) : null);
  });
  side.addEventListener('click', e => {
    const button = e.target.closest('[data-part-highlight]');
    if (button) viewer?.highlight(Number(button.dataset.partHighlight));
    const joint = e.target.closest('[data-show-joint]');
    if (joint) viewer?.highlight(joint.dataset.showJoint.split(",").map(Number));
  });
  $("split-explode").addEventListener("input", (e) => viewer?.setExplode(Number(e.target.value) / 100));
  $("btn-split-fit").addEventListener("click", () => viewer?.fit());
  document.addEventListener("keydown", (e) => {
    const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName);
    if (view !== "split" || sub !== "cuts" || typing || selectedCut === null || document.querySelector("dialog[open]")) return;
    if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      removeCut(selectedCut);
    }
  });
}

export const splitStep = {
  attach(options) {
    host = options;
    bindEvents();
  },

  get split() {
    return current;
  },

  get running() {
    return !!current && RUNNING.has(current.state);
  },

  async open(splitId) {
    clearTimeout(pollTimer);
    const detail = await host.api("GET", `/api/splits/${splitId}`);
    current = detail;
    droppedEdits = null;
    cuts = clone(detail.cuts?.cuts || []);
    selectedCut = null;
    pendingSave = checkDirty = false;
    geometryKey = "";
    sub = detail.result ? "parts" : "cuts";
    $("split-explode").value = 0;
    schedulePoll();
    render();
    return current;
  },

  close() {
    this.hide();
    clearTimeout(pollTimer);
    current = null;
  },

  show(step) {
    view = step;
    ensureViewer();
    render();
  },

  hide() {
    view = null;
    if (viewer) {
      viewer.dispose();
      viewer = null;
    }
    geometryKey = "";
  },
};
