/* Spooler slicer – 3D viewport: the Centauri Carbon bed, objects with move/rotate gizmos, and the sliced
   toolpath preview layer by layer. Object placement matches projects.place() on the server: the model is
   centred on its bounding box, scaled, rotated about X then Y then Z, dropped onto the bed and moved so the
   centre of its footprint is at the object's position. */
import * as THREE from "three";
import { OrbitControls } from "three/addons/OrbitControls.js";
import { TransformControls } from "three/addons/TransformControls.js";
import { smoothGeometry } from "../mesh-shading.js";
import { loadPrinterModel } from "./printer.js";

export const FEATURE_COLORS = {
  "Outer wall": "#ff8a3d", "Inner wall": "#ffc66d", "Overhang wall": "#ff5d5d", "Sparse infill": "#6c8cff",
  "Internal solid infill": "#a66cff", "Solid infill": "#a66cff", "Top surface": "#ff5da2", "Bottom surface": "#4fd1c5",
  "Bridge": "#5fb3ff", "Internal Bridge": "#5fb3ff", "Gap infill": "#e5e5e5", "Skirt": "#8a8a8a", "Brim": "#3ccf6e",
  "Support": "#9aa0a6", "Support interface": "#c7ccd1", "Support transition": "#c7ccd1", "Prime tower": "#b58a5a",
  "Ironing": "#ffd84d",
};
export const featureColor = name => FEATURE_COLORS[name] || "#9fb3c8";
const TOOLPATH_SCALE = 100;
const TOOLPATH_OFFSET = 50;
const SEGMENT_BUDGET = 3_000_000;
const OBJECT_COLOR = 0xd9dde6;
const SELECTED_COLOR = 0xffa46b;
const BAD_COLOR = 0xef4444;
const WARN_COLOR = 0xfacc15;   // only warnings (too close): the layout can still be sliced
const SOFT_ISSUES = ["too close"];

export function parseMeshes(buffer) {
  if (String.fromCharCode(...new Uint8Array(buffer, 0, 4)) !== "SPV1") throw new Error("Unexpected preview format");
  const view = new DataView(buffer);
  const meshes = [];
  let offset = 8;
  for (let i = 0, count = view.getUint32(4, true); i < count; i++) {
    const vertices = view.getUint32(offset, true), triangles = view.getUint32(offset + 4, true);
    offset += 8;
    const positions = new Float32Array(buffer, offset, vertices * 3);
    offset += vertices * 12;
    const index = new Uint32Array(buffer, offset, triangles * 3);
    offset += triangles * 12;
    meshes.push({ positions, index });
  }
  return meshes;
}

/** The kinds of problem an object can have: the strings the server sends in plate_problems (problem_kinds.py). The viewport's own
 *  box checks only add marking while an object is being moved; the server's list is what decides. */
export const PROBLEM = {
  offTheBed: "off the bed", overThePurgeArea: "over the purge area", overlaps: "overlaps", tooClose: "too close", failedModel: "failed model",
};

/** Whether the outline of the object from above (convex hull of its vertices where it stands) enters the rectangle [x0, y0, x1, y1]. */
function footprintEntersRect(holder, [x0, y0, x1, y1]) {
  const pts = [];
  holder.updateWorldMatrix(true, true);
  holder.traverse(node => {
    const p = node.isMesh && node.geometry?.getAttribute("position");
    if (!p) return;
    const step = Math.max(1, Math.ceil(p.count / 20000)), v = new THREE.Vector3();
    for (let i = 0; i < p.count; i += step) { v.fromBufferAttribute(p, i).applyMatrix4(node.matrixWorld); pts.push([v.x, v.y]); }
  });
  if (pts.length < 3) return true;
  pts.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const half = list => { const h = []; for (const q of list) { while (h.length > 1 && cross(h[h.length - 2], h[h.length - 1], q) <= 0) h.pop(); h.push(q); } return h; };
  let poly = half(pts).slice(0, -1).concat(half([...pts].reverse()).slice(0, -1));   // counter-clockwise
  // clip the hull by the four sides of the rectangle (Sutherland-Hodgman); what is left lies inside it
  for (const d of [p => p[0] - x0, p => x1 - p[0], p => p[1] - y0, p => y1 - p[1]]) {
    const out = [];
    poly.forEach((a, i) => {
      const b = poly[(i + 1) % poly.length], da = d(a), db = d(b);
      if (da >= 0) out.push(a);
      if ((da >= 0) !== (db >= 0)) { const t = da / (da - db); out.push([a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])]); }
    });
    poly = out;
    if (poly.length < 3) return false;
  }
  let area = 0;
  poly.forEach((a, i) => { const b = poly[(i + 1) % poly.length]; area += a[0] * b[1] - b[0] * a[1]; });
  return Math.abs(area) / 2 > 0.01;
}

export class Viewport {
  constructor(container, callbacks) {
    this.container = container;
    this.callbacks = callbacks;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    container.prepend(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(38, 1, 1, 20000);
    this.camera.up.set(0, 0, 1);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x404452, 1.25));
    const key = new THREE.DirectionalLight(0xffffff, 1.7);
    key.position.set(0.5, -1.1, 1.8);
    const fill = new THREE.DirectionalLight(0xffffff, 0.5);
    fill.position.set(-1.2, 1, 0.4);
    this.scene.add(key, fill);

    this.bedGroup = new THREE.Group();
    this.objectGroup = new THREE.Group();
    this.toolpathGroup = new THREE.Group();
    // The platform carries the bed and whatever is on it; with the printer shown it moves in Z like the real bed.
    this.platform = new THREE.Group();
    this.platform.add(this.bedGroup, this.objectGroup, this.toolpathGroup);
    this.scene.add(this.platform);
    this.printer = null;           // { groups, root } once loaded
    this.printerWanted = false;
    this.head = null;              // [x, y, z] of the nozzle tip in printer coordinates
    this.geometries = new Map();   // model id → centred BufferGeometry
    this.holders = new Map();      // object id → { holder, mesh, object }
    this.serverIssues = new Map(); // object id → problems the server found (overlaps, failed model…): the one definition of a problem Object
    this.selected = null;
    this.group = new Set();        // the other selected objects, moved along with the selected one
    this.mode = "prepare";
    this.limits = { bed: [0, 0, 256, 256], exclude: [246, 0, 256, 20], height: 256 };

    this.transform = new TransformControls(this.camera, this.renderer.domElement);
    this.transform.setSpace("world");
    this.transform.setSize(0.8);
    this.scene.add(this.transform.getHelper ? this.transform.getHelper() : this.transform);
    this.transform.addEventListener("dragging-changed", e => {
      this.controls.enabled = !e.value;
      if (e.value) this._dragStart = this._startDrag();
      else { this._commitTransform(); this._dragStart = null; }
    });
    this.transform.addEventListener("objectChange", () => { this._dragGroup(); this._refreshWarnings(); });
    this.setTool("translate");

    const canvas = this.renderer.domElement;
    this.raycaster = new THREE.Raycaster();
    let down = null;
    canvas.addEventListener("pointerdown", e => { down = { x: e.clientX, y: e.clientY }; });
    canvas.addEventListener("pointerup", e => {
      if (!down || Math.hypot(e.clientX - down.x, e.clientY - down.y) > 4 || this.transform.dragging || this.mode !== "prepare") return;
      if (this.transform.axis) return;
      if (this.faceMode) {
        const face = this.pickFace(e);
        if (face) this.callbacks.onFace?.(face);
        return;
      }
      this.callbacks.onSelect?.(this._pick(e), { additive: e.shiftKey || e.ctrlKey || e.metaKey });
    });
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.resize();
    this.setBed(this.limits, false);
    this.fit();
    const tick = () => {
      this.frame = requestAnimationFrame(tick);
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    };
    tick();
  }

  resize() {
    const w = this.container.clientWidth || 1, h = this.container.clientHeight || 1;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  _clear(group) {
    group.traverse(obj => {
      if (obj.geometry && ![...this.geometries.values()].includes(obj.geometry)) obj.geometry.dispose();
      if (obj.material) (Array.isArray(obj.material) ? obj.material : [obj.material]).forEach(m => m.dispose());
    });
    group.clear();
  }

  // ── Bed ──────────────────────────────────────────────────────────────────────
  setBed(limits, smooth) {
    this.limits = limits;
    this._clear(this.bedGroup);
    const [x0, y0, x1, y1] = limits.bed;
    const w = x1 - x0, d = y1 - y0;
    const plate = new THREE.Mesh(new THREE.PlaneGeometry(w, d), new THREE.MeshStandardMaterial({
      color: smooth ? 0x3a3f4b : 0x2a2d35, roughness: smooth ? 0.35 : 0.95, metalness: 0.1 }));
    plate.position.set(x0 + w / 2, y0 + d / 2, -0.05);
    this.bedGroup.add(plate);
    const lines = [], major = [];
    for (let x = x0; x <= x1 + 0.01; x += 10) ((x - x0) % 50 === 0 ? major : lines).push(x, y0, 0, x, y1, 0);
    for (let y = y0; y <= y1 + 0.01; y += 10) ((y - y0) % 50 === 0 ? major : lines).push(x0, y, 0, x1, y, 0);
    const grid = (points, color, opacity) => {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(points, 3));
      return new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ color, transparent: true, opacity }));
    };
    this.bedGroup.add(grid(lines, 0x6b7280, 0.25), grid(major, 0x9ca3af, 0.45));
    if (limits.exclude) {
      const [ex0, ey0, ex1, ey1] = limits.exclude;
      const zone = new THREE.Mesh(new THREE.PlaneGeometry(ex1 - ex0, ey1 - ey0),
        new THREE.MeshBasicMaterial({ color: 0xef4444, transparent: true, opacity: 0.35, depthWrite: false }));
      zone.position.set((ex0 + ex1) / 2, (ey0 + ey1) / 2, 0.05);
      this.bedGroup.add(zone);
    }
    const axes = new THREE.AxesHelper(20);
    axes.position.set(x0, y0, 0.1);
    this.bedGroup.add(axes);
  }

  fit(box = null, direction = new THREE.Vector3(0, -1.35, 1.05), distance = 2.6) {
    const target = box && !box.isEmpty() ? box : new THREE.Box3(new THREE.Vector3(this.limits.bed[0], this.limits.bed[1], 0),
                                                               new THREE.Vector3(this.limits.bed[2], this.limits.bed[3], 60));
    const center = target.getCenter(new THREE.Vector3());
    const radius = Math.max(target.getSize(new THREE.Vector3()).length() / 2, 40);
    this.camera.position.copy(center).add(direction.clone().normalize().multiplyScalar(radius * distance));
    this.camera.near = radius / 50;
    this.camera.far = radius * 60;
    this.camera.updateProjectionMatrix();
    this.controls.target.copy(center);
    this.controls.update();
  }

  // ── Objects ──────────────────────────────────────────────────────────────────
  /** Forget a model's cached shape (model ids repeat between projects, and an id can be reused after a delete). */
  removeGeometry(modelId) {
    this.geometries.get(modelId)?.dispose();
    this.geometries.delete(modelId);
  }

  clearGeometries() {
    for (const id of [...this.geometries.keys()]) this.removeGeometry(id);
  }

  hasGeometry(modelId) {
    return this.geometries.has(modelId);
  }

  addModelGeometry(modelId, buffer) {
    const mesh = parseMeshes(buffer)[0];
    const geometry = smoothGeometry(mesh.positions, mesh.index);
    geometry.center();
    geometry.computeBoundingBox();
    this.geometries.set(modelId, geometry);
  }

  setObjects(objects, models, selectedId) {
    const keep = new Set();
    for (const object of objects) {
      keep.add(object.id);
      const model = models.find(m => m.id === object.model);
      let entry = this.holders.get(object.id);
      const geometry = this.geometries.get(object.model);
      if (!entry || entry.geometry !== geometry) {
        if (entry) this.objectGroup.remove(entry.holder);
        const holder = new THREE.Group();
        const material = new THREE.MeshStandardMaterial({ color: OBJECT_COLOR, roughness: 0.6, metalness: 0.05,
                                                          wireframe: !geometry, transparent: !geometry, opacity: geometry ? 1 : 0.5 });
        const mesh = new THREE.Mesh(geometry || new THREE.BoxGeometry(20, 20, 20), material);
        mesh.userData.objectId = object.id;
        holder.add(mesh);
        this.objectGroup.add(holder);
        entry = { holder, mesh, geometry };
        this.holders.set(object.id, entry);
      }
      entry.object = object;
      entry.failed = model?.state === "failed";
      this._place(entry);
    }
    for (const [id, entry] of this.holders) {
      if (!keep.has(id)) {
        if (this.transform.object === entry.holder) this.transform.detach();
        this.objectGroup.remove(entry.holder);
        entry.mesh.material.dispose();
        this.holders.delete(id);
      }
    }
    this.select(selectedId);
    this._refreshWarnings();
  }

  _place(entry) {
    const { holder, mesh, object } = entry;
    holder.position.set(0, 0, 0);
    holder.rotation.set(0, 0, 0);
    holder.scale.set(1, 1, 1);  // the scale tool stretches the holder while dragging; the object's own scale is on the mesh
    mesh.position.set(0, 0, 0);
    mesh.rotation.set(...object.rotation.map(THREE.MathUtils.degToRad), "ZYX");
    mesh.scale.set(...object.scale);
    holder.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(mesh, !!entry.geometry);
    mesh.position.set(-(box.min.x + box.max.x) / 2, -(box.min.y + box.max.y) / 2, -box.min.z);
    holder.position.set(object.position[0], object.position[1], 0);
    holder.updateMatrixWorld(true);
  }

  /** Where the selected object and the rest of the selection stand when a drag starts. */
  _startDrag() {
    const entry = this.holders.get(this.selected);
    if (!entry) return null;
    return { at: entry.holder.position.clone(), others: [...this.group].map(id => [id, this.holders.get(id)?.holder]).filter(([, h]) => h)
      .map(([id, holder]) => [id, holder, holder.position.clone()]) };
  }

  /** While the selected object is dragged with the Move tool, the rest of the selection follows it. */
  _dragGroup() {
    const entry = this.holders.get(this.selected);
    if (!this._dragStart || !entry || this.tool !== "translate") return;
    const delta = entry.holder.position.clone().sub(this._dragStart.at);
    for (const [, holder, start] of this._dragStart.others) holder.position.set(start.x + delta.x, start.y + delta.y, start.z);
  }

  /** Select one object and, with `group`, others to move along with it. */
  setGroup(ids) {
    this.group = new Set(ids);
    this._refreshWarnings();
  }

  /** The exact box of an object where it stands (by its vertices, not the turned bounding box). */
  footprint(objectId) {
    const entry = this.holders.get(objectId);
    return entry ? new THREE.Box3().setFromObject(entry.holder, !!entry.geometry) : null;
  }

  /** Snap dragging to a grid: millimetres for Move, degrees for Rotate; 0 turns snapping off. */
  setSnap(mm, degrees) {
    this.transform.setTranslationSnap(mm > 0 ? mm : null);
    this.transform.setRotationSnap(degrees > 0 ? THREE.MathUtils.degToRad(degrees) : null);
  }

  select(objectId) {
    this.selected = objectId ?? null;
    const entry = this.holders.get(this.selected);
    if (entry && this.mode === "prepare" && this.editable !== false) this.transform.attach(entry.holder);
    else this.transform.detach();
    this._refreshWarnings();
  }

  setTool(tool) {
    this.tool = tool;
    this.transform.setMode(tool);
    this.transform.showX = tool === "translate" || tool === "scale";
    this.transform.showY = tool === "translate" || tool === "scale";
    this.transform.showZ = tool === "rotate" || tool === "scale";
  }

  setEditable(editable) {
    this.editable = !!editable;
    this.container.dataset.editable = String(this.editable);
    if (this.editable && this.mode === "prepare") {
      const entry = this.holders.get(this.selected);
      if (entry) this.transform.attach(entry.holder);
    } else {
      this.transform.detach();
      if (this.transform.dragging) this._commitTransform();
    }
  }

  _pick(event) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.raycaster.setFromCamera(new THREE.Vector2(((event.clientX - rect.left) / rect.width) * 2 - 1,
                                                   -((event.clientY - rect.top) / rect.height) * 2 + 1), this.camera);
    const meshes = [...this.holders.values()].map(e => e.mesh);
    const hit = this.raycaster.intersectObjects(meshes, false)[0];
    return hit ? hit.object.userData.objectId : null;
  }

  _commitTransform() {
    if (this.editable === false) return;
    const entry = this.holders.get(this.selected);
    if (!entry) return;
    const { holder, object } = entry;
    if (this.tool === "scale") {
      // always uniform: the handle that moved furthest sets the factor for all three axes
      const s = holder.scale;
      const factor = [s.x, s.y, s.z].reduce((best, v) => (Math.abs(v - 1) > Math.abs(best - 1) ? v : best), 1);
      holder.scale.set(1, 1, 1);
      if (Math.abs(factor - 1) < 1e-4) return;
      const scale = object.scale.map(v => Math.round(Math.min(100, Math.max(0.01, v * factor)) * 10000) / 10000);
      this.callbacks.onTransform?.(object.id, { scale });
      return;
    }
    const turn = THREE.MathUtils.radToDeg(holder.rotation.z);
    const position = [Math.round(holder.position.x * 100) / 100, Math.round(holder.position.y * 100) / 100];
    const rotation = [object.rotation[0], object.rotation[1], Math.round(((object.rotation[2] + turn) % 360 + 360) % 360 * 100) / 100];
    this.callbacks.onTransform?.(object.id, { position, rotation });
  }

  /** The object and face under the pointer: { objectId, normal } with the normal in the model's own (unrotated) space. */
  pickFace(event) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.raycaster.setFromCamera(new THREE.Vector2(((event.clientX - rect.left) / rect.width) * 2 - 1,
                                                   -((event.clientY - rect.top) / rect.height) * 2 + 1), this.camera);
    const hit = this.raycaster.intersectObjects([...this.holders.values()].map(e => e.mesh), false)[0];
    if (!hit?.face) return null;
    // smooth shading averages normals, so measure the triangle itself
    const p = hit.object.geometry.getAttribute("position");
    const index = hit.object.geometry.getIndex();
    const at = i => new THREE.Vector3().fromBufferAttribute(p, index ? index.getX(hit.faceIndex * 3 + i) : hit.faceIndex * 3 + i);
    const [a, b, c] = [at(0), at(1), at(2)];
    const normal = b.sub(a).cross(c.sub(a)).normalize();
    return { objectId: hit.object.userData.objectId, normal: [normal.x, normal.y, normal.z] };
  }

  setFaceMode(on) {
    this.faceMode = !!on;
    this.renderer.domElement.style.cursor = on ? "crosshair" : "";
  }

  /** Look at the scene from a named side: top, bottom, front, back, left, right or iso. */
  setView(name, box = null) {
    const directions = { top: [0, -0.0001, 1], bottom: [0, -0.0001, -1], front: [0, -1, 0.0001], back: [0, 1, 0.0001],
                         left: [-1, 0, 0.0001], right: [1, 0, 0.0001], iso: [0, -1.35, 1.05] };
    if (directions[name]) this.fit(box || this.objectsBox(), new THREE.Vector3(...directions[name]));
  }

  /** Bounding boxes of the objects in bed coordinates, and which objects are off the bed or over the purge area. */
  problems() {
    const out = new Map();
    const [x0, y0, x1, y1] = this.limits.bed;
    for (const [id, entry] of this.holders) {
      // by the mesh's own vertices, as _place centres it: the box of the turned bounding box is bigger for any part turned off the axes, so Arrange's valid layouts showed red
      const box = new THREE.Box3().setFromObject(entry.holder, !!entry.geometry);
      const issues = [];
      if (entry.failed) issues.push(PROBLEM.failedModel);
      if (box.min.x < x0 - 0.01 || box.min.y < y0 - 0.01 || box.max.x > x1 + 0.01 || box.max.y > y1 + 0.01) issues.push(PROBLEM.offTheBed);
      if (box.max.z > this.limits.height + 0.01) issues.push("too tall");
      if (this.limits.exclude) {
        const [ex0, ey0, ex1, ey1] = this.limits.exclude;
        // the box only says where to look: a big part turned on the diagonal has its box corner in the purge area while its outline is clear, as the server judges it
        if (box.max.x > ex0 && box.min.x < ex1 && box.max.y > ey0 && box.min.y < ey1 && footprintEntersRect(entry.holder, this.limits.exclude)) issues.push(PROBLEM.overThePurgeArea);
      }
      for (const issue of this.serverIssues.get(id) || []) {
        if (!issues.includes(issue)) issues.push(issue);
      }
      out.set(id, { box, issues });
    }
    return out;
  }

  /** The server's problem list (object id → issues) marks objects red too, e.g. for overlaps the viewport doesn't measure. */
  setServerIssues(map) {
    this.serverIssues = map;
    this._refreshWarnings();
  }

  _refreshWarnings() {
    const problems = this.problems();
    for (const [id, entry] of this.holders) {
      const issues = problems.get(id).issues;
      const bad = issues.some(issue => !SOFT_ISSUES.includes(issue));
      const chosen = id === this.selected || this.group.has(id);
      entry.mesh.material.color.setHex(bad ? BAD_COLOR : issues.length ? WARN_COLOR : chosen ? SELECTED_COLOR : OBJECT_COLOR);
    }
    this.callbacks.onProblems?.(problems);
  }

  objectBox(objectId) {
    const entry = this.holders.get(objectId);
    return entry ? new THREE.Box3().setFromObject(entry.holder) : this.objectsBox();
  }

  objectsBox() {
    const box = new THREE.Box3();
    for (const entry of this.holders.values()) box.expandByObject(entry.holder);
    return box;
  }

  // ── Toolpath preview ─────────────────────────────────────────────────────────
  setMode(mode) {
    this.mode = mode;
    this.objectGroup.visible = mode === "prepare";
    this.toolpathGroup.visible = mode === "preview";
    if (mode === "prepare") this.select(this.selected);
    else { this.transform.detach(); this.selected = null; }
    this._placePrinter();
  }

  // ── Printer ──────────────────────────────────────────────────────────────────
  /** Show or hide the printer around the bed (only drawn in the layer preview). Loads the model on first use. */
  async showPrinter(on) {
    this.printerWanted = on;
    if (on && !this.printer) {
      this.printerLoading ||= loadPrinterModel().then(({ header, groups }) => {
        const root = new THREE.Group();
        root.add(groups.frame, groups.shell, groups.gantry, groups.toolhead);
        this.scene.add(root);
        this.platform.add(groups.bed);   // not bedGroup: setBed() clears that
        this.printer = { header, groups, root };
        this.setShell(this.shellMode || "glass");
      }).finally(() => { this.printerLoading = null; });
      await this.printerLoading;
    }
    this._placePrinter();
  }

  setShell(mode) {
    this.shellMode = mode;
    if (!this.printer) return;
    const shell = this.printer.groups.shell;
    shell.visible = mode !== "hidden";
    shell.traverse(obj => {
      if (!obj.material) return;
      obj.material.opacity = mode === "solid" ? 1 : 0.18;
      obj.material.transparent = mode !== "solid";
      obj.material.depthWrite = mode === "solid";
    });
  }

  get printerShown() {
    return !!this.printer && this.printerWanted && this.mode === "preview";
  }

  /** Nozzle tip at (x, y, z) in printer coordinates: frame fixed with the nozzle at height H, bed at H - z. */
  _placePrinter() {
    if (!this.printer) return;
    const shown = this.printerShown;
    const { groups, root } = this.printer;
    root.visible = shown;
    groups.bed.visible = shown;
    const top = this.limits.height;
    if (!shown) {
      this.platform.position.z = 0;
      return;
    }
    const [x0, y0, x1, y1] = this.limits.bed;
    const [x, y, z] = this.head || [(x0 + x1) / 2, y1, top];
    root.position.z = top;
    groups.toolhead.position.set(x, y, 0);
    groups.gantry.position.set(0, y, 0);
    this.platform.position.z = top - z;
  }

  /** Frame the whole printer, looking in through the door at bed height so the print isn't behind the top beam. */
  fitPrinter() {
    if (!this.printerShown) return this.fit();
    const box = new THREE.Box3().expandByObject(this.printer.root);
    const center = box.getCenter(new THREE.Vector3());
    this.fit(box, new THREE.Vector3(-0.25, -1.35, 0.35), 2.5);
    // Aim at the bed rather than the middle of the machine.
    const bed = this.platform.position.z;
    this.controls.target.set(center.x, center.y, Math.min(Math.max(bed + 20, box.min.z), box.max.z));
    this.controls.update();
  }

  showToolpath(analysis, buffer, hiddenFeatures = new Set()) {
    this._clear(this.toolpathGroup);
    this.layerEnds = [];
    this.toolpath = null;
    if (!analysis || !buffer) return;
    const view = new DataView(buffer);
    const features = analysis.features || [];
    let segments = 0;
    for (const layer of analysis.layers) {
      for (let pos = layer.offset, end = layer.offset + layer.length; pos < end;) {
        const count = view.getUint16(pos + 1, true);
        segments += Math.max(0, count - 1);
        pos += 3 + count * 4;
      }
    }
    const stride = Math.max(1, Math.ceil(segments / SEGMENT_BUDGET));
    const positions = new Float32Array(Math.ceil(segments / stride + analysis.layers.length) * 6);
    const colors = new Uint8Array(positions.length);
    const palette = features.map(name => new THREE.Color(featureColor(name)));
    let v = 0;
    for (const layer of analysis.layers) {
      const z = layer.z - (layer.height > 0 ? layer.height / 2 : 0.1);
      for (let pos = layer.offset, end = layer.offset + layer.length; pos < end;) {
        const feature = view.getUint8(pos);
        const count = view.getUint16(pos + 1, true);
        const base = pos + 3;
        pos = base + count * 4;
        if (hiddenFeatures.has(features[feature])) continue;
        const color = palette[feature] || new THREE.Color(0x9fb3c8);
        for (let i = 0; i + 1 < count; i += stride) {
          const j = Math.min(i + stride, count - 1);
          positions[v] = view.getUint16(base + i * 4, true) / TOOLPATH_SCALE - TOOLPATH_OFFSET;
          positions[v + 1] = view.getUint16(base + i * 4 + 2, true) / TOOLPATH_SCALE - TOOLPATH_OFFSET;
          positions[v + 2] = z;
          positions[v + 3] = view.getUint16(base + j * 4, true) / TOOLPATH_SCALE - TOOLPATH_OFFSET;
          positions[v + 4] = view.getUint16(base + j * 4 + 2, true) / TOOLPATH_SCALE - TOOLPATH_OFFSET;
          positions[v + 5] = z;
          for (let k = 0; k < 6; k += 3) {
            colors[v + k] = color.r * 255;
            colors[v + k + 1] = color.g * 255;
            colors[v + k + 2] = color.b * 255;
          }
          v += 6;
        }
      }
      this.layerEnds.push(v / 3);
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions.subarray(0, v), 3));
    geometry.setAttribute("color", new THREE.BufferAttribute(colors.subarray(0, v), 3, true));
    const lines = new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ vertexColors: true }));
    this.toolpathGroup.add(lines);
    this.toolpath = { geometry, stride, positions: positions.subarray(0, v), layerZ: analysis.layers.map(l => l.z) };
    return { segments, stride };
  }

  setLayer(layerIndex, fraction = 1) {
    if (!this.toolpath || !this.layerEnds.length) return;
    const index = Math.max(0, Math.min(layerIndex, this.layerEnds.length - 1));
    const start = index > 0 ? this.layerEnds[index - 1] : 0;
    const end = this.layerEnds[index];
    let count = start + Math.round((end - start) * fraction);
    count -= count % 2;
    this._draw(count, index);
  }

  /** Vertices drawn so far (always even: two per segment) and the layer they end in, for playback. */
  get drawn() {
    return this.toolpath ? { count: this.toolpath.count, layer: this.toolpath.layer } : null;
  }

  /** Draw the first `count` vertices of the toolpath and put the nozzle at the end of the last drawn move. */
  _draw(count, layer) {
    const { geometry, positions, layerZ } = this.toolpath;
    this.toolpath.count = count;
    this.toolpath.layer = layer;
    geometry.setDrawRange(0, count);
    const last = Math.max(0, count - 1) * 3;
    if (positions.length >= 3) this.head = [positions[last], positions[last + 1], layerZ[layer] ?? 0];
    this._placePrinter();
  }

  /** Where playback lands after moving the nozzle `mm` further along the toolpath from `count` drawn vertices.
      Distance includes the jumps between moves, so the head spends time travelling as it would. */
  advance(count, mm) {
    const { positions } = this.toolpath;
    const total = positions.length / 3;
    let at = count;
    while (at < total && mm > 0) {
      const from = at > 0 ? (at - 1) * 3 : at * 3, a = at * 3, b = a + 3;
      mm -= Math.hypot(positions[a] - positions[from], positions[a + 1] - positions[from + 1])
          + Math.hypot(positions[b] - positions[a], positions[b + 1] - positions[a + 1]);
      at += 2;
    }
    let layer = this.layerEnds.findIndex(end => end >= at);
    if (layer < 0) layer = this.layerEnds.length - 1;
    this._draw(at, layer);
    const start = layer > 0 ? this.layerEnds[layer - 1] : 0;
    const span = this.layerEnds[layer] - start;
    return { count: at, layer, fraction: span > 0 ? (at - start) / span : 1, done: at >= total };
  }

  toolpathBox(analysis) {
    const bounds = analysis?.bounds?.model;
    if (!bounds) return null;
    const lift = new THREE.Vector3(0, 0, this.platform.position.z);
    return new THREE.Box3(new THREE.Vector3(...bounds.min).add(lift), new THREE.Vector3(...bounds.max).add(lift));
  }

  dispose() {
    cancelAnimationFrame(this.frame);
    this.resizeObserver.disconnect();
    this.transform.dispose();
    this.controls.dispose();
    this.renderer.dispose();
  }
}
