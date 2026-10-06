/* Orientation helpers for the Prepare view: lay a chosen face on the bed, or find the orientation that prints with the
   least support. Rotations are Spooler's object rotations: degrees about X, then Y, then Z (three.js order "ZYX"). */

import * as THREE from "three";

const DOWN = new THREE.Vector3(0, 0, -1);
const deg = THREE.MathUtils.radToDeg;
const rad = THREE.MathUtils.degToRad;

export const matrixOf = rotation => new THREE.Matrix4().makeRotationFromEuler(new THREE.Euler(rad(rotation[0]), rad(rotation[1]), rad(rotation[2]), "ZYX"));

function rotationOf(matrix) {
  const e = new THREE.Euler().setFromRotationMatrix(matrix, "ZYX");
  return [e.x, e.y, e.z].map(v => Math.round(((deg(v) % 360) + 360) % 360 * 1000) / 1000);
}

/** The rotation that puts the face with `normal` (in the model's own space) flat on the bed, keeping the turn about Z. */
export function layOnFace(normal, rotation) {
  const turn = new THREE.Matrix4().makeRotationZ(rad(rotation[2]));
  const flip = new THREE.Matrix4().makeRotationFromQuaternion(new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(...normal).normalize(), DOWN));
  return rotationOf(turn.multiply(flip));
}

/** A direction in the world (x right, y back, z up) as it reads in the model's own space for an object turned by `rotation`. */
export function toModelSpace(direction, rotation) {
  const v = new THREE.Vector3(...direction).applyMatrix4(matrixOf(rotation).invert());
  return [v.x, v.y, v.z].map(n => Math.round(n * 1e6) / 1e6);
}

/** Support needed and contact with the bed for a geometry turned by `matrix`. Areas in mm², weighted by triangle area. */
export function measure(geometry, matrix, sampleEvery = 1) {
  const positions = geometry.getAttribute("position");
  const index = geometry.getIndex();
  const vertex = i => (index ? index.getX(i) : i);
  const count = index ? index.count : positions.count;
  const a = new THREE.Vector3(), b = new THREE.Vector3(), c = new THREE.Vector3(), n = new THREE.Vector3();
  let minZ = Infinity, maxZ = -Infinity;
  const triangles = [];
  for (let t = 0; t + 2 < count; t += 3 * sampleEvery) {
    a.fromBufferAttribute(positions, vertex(t)).applyMatrix4(matrix);
    b.fromBufferAttribute(positions, vertex(t + 1)).applyMatrix4(matrix);
    c.fromBufferAttribute(positions, vertex(t + 2)).applyMatrix4(matrix);
    n.copy(b).sub(a).cross(c.clone().sub(a));
    const area = n.length() / 2;
    if (area < 1e-9) continue;
    n.normalize();
    const low = Math.min(a.z, b.z, c.z);
    minZ = Math.min(minZ, low);
    maxZ = Math.max(maxZ, a.z, b.z, c.z);
    triangles.push([n.z, area, low, Math.max(a.z, b.z, c.z)]);
  }
  let overhang = 0, contact = 0;
  for (const [nz, area, low, high] of triangles) {
    if (nz < -0.7071) {                                  // faces down more than 45° from vertical
      if (high - minZ < 0.3) contact += area; else overhang += area;
    }
  }
  return { overhang: overhang * sampleEvery, contact: contact * sampleEvery, height: maxZ - minZ };
}

/** The candidate rotation with the least overhang (and the most on the bed), among the current one and every large
 *  flat-ish face laid down. Returns { rotation, overhang, contact, height, current: same for now }. */
export function bestOrientation(geometry, rotation) {
  const positions = geometry.getAttribute("position");
  const index = geometry.getIndex();
  const vertex = i => (index ? index.getX(i) : i);
  const count = index ? index.count : positions.count;
  const sampleEvery = Math.max(1, Math.floor(count / 3 / 40000));
  const a = new THREE.Vector3(), b = new THREE.Vector3(), c = new THREE.Vector3();
  const groups = new Map();
  for (let t = 0; t + 2 < count; t += 3 * sampleEvery) {
    a.fromBufferAttribute(positions, vertex(t));
    b.fromBufferAttribute(positions, vertex(t + 1));
    c.fromBufferAttribute(positions, vertex(t + 2));
    const n = b.clone().sub(a).cross(c.clone().sub(a));
    const area = n.length() / 2;
    if (area < 1e-9) continue;
    n.normalize();
    const key = `${Math.round(n.x * 12)},${Math.round(n.y * 12)},${Math.round(n.z * 12)}`;  // ~5° buckets
    const group = groups.get(key) || { sum: new THREE.Vector3(), area: 0 };
    group.sum.addScaledVector(n, area);
    group.area += area;
    groups.set(key, group);
  }
  const faces = [...groups.values()].sort((x, y) => y.area - x.area).slice(0, 36).map(g => g.sum.clone().normalize());
  const score = m => m.overhang - 0.5 * m.contact + 0.6 * m.height;
  const sample = Math.max(1, Math.floor(count / 3 / 20000));
  const nowMeasure = measure(geometry, matrixOf(rotation), sample);
  let best = { rotation: rotation.map(v => v), ...nowMeasure };
  for (const normal of faces) {
    const candidate = layOnFace([normal.x, normal.y, normal.z], rotation);
    const m = measure(geometry, matrixOf(candidate), sample);
    if (score(m) < score(best) - 1e-6) best = { rotation: candidate, ...m };
  }
  return { ...best, current: nowMeasure };
}
