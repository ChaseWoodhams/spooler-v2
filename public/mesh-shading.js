/* Spooler – smooth shading for simplified preview meshes. Simplifying leaves long, uneven triangles, and with flat
   shading every one of them shows as a separate facet. Each corner's normal is averaged over the faces around it
   that bend less than the crease angle, so curved surfaces look smooth while real edges stay sharp.
   Returns non-indexed geometry (one vertex per corner) so corners on either side of a crease can differ. */
import * as THREE from "three";

export function smoothGeometry(positions, index, creaseDegrees = 30) {
  const triangles = index.length / 3, vertices = positions.length / 3;
  const weighted = new Float32Array(triangles * 3);   // face normal × twice the area: slivers count for little
  const unit = new Float32Array(triangles * 3);
  for (let f = 0; f < triangles; f++) {
    const a = index[3 * f] * 3, b = index[3 * f + 1] * 3, c = index[3 * f + 2] * 3;
    const ux = positions[b] - positions[a], uy = positions[b + 1] - positions[a + 1], uz = positions[b + 2] - positions[a + 2];
    const vx = positions[c] - positions[a], vy = positions[c + 1] - positions[a + 1], vz = positions[c + 2] - positions[a + 2];
    const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
    const length = Math.hypot(nx, ny, nz) || 1;
    weighted[3 * f] = nx; weighted[3 * f + 1] = ny; weighted[3 * f + 2] = nz;
    unit[3 * f] = nx / length; unit[3 * f + 1] = ny / length; unit[3 * f + 2] = nz / length;
  }

  // the faces around each vertex, as compressed rows: faces[start[v] .. start[v + 1]]
  const start = new Uint32Array(vertices + 1);
  for (let i = 0; i < index.length; i++) start[index[i] + 1]++;
  for (let v = 0; v < vertices; v++) start[v + 1] += start[v];
  const next = start.slice(0, vertices);
  const faces = new Uint32Array(index.length);
  for (let i = 0; i < index.length; i++) faces[next[index[i]]++] = (i / 3) | 0;

  const cos = Math.cos(creaseDegrees * Math.PI / 180);
  const outPositions = new Float32Array(index.length * 3), outNormals = new Float32Array(index.length * 3);
  for (let i = 0; i < index.length; i++) {
    const f = 3 * ((i / 3) | 0), v = index[i], o = 3 * i;
    const fx = unit[f], fy = unit[f + 1], fz = unit[f + 2];
    let sx = 0, sy = 0, sz = 0;
    for (let k = start[v]; k < start[v + 1]; k++) {
      const g = 3 * faces[k];
      if (fx * unit[g] + fy * unit[g + 1] + fz * unit[g + 2] >= cos) {
        sx += weighted[g]; sy += weighted[g + 1]; sz += weighted[g + 2];
      }
    }
    const length = Math.hypot(sx, sy, sz);
    if (length > 0) { outNormals[o] = sx / length; outNormals[o + 1] = sy / length; outNormals[o + 2] = sz / length; }
    else { outNormals[o] = fx; outNormals[o + 1] = fy; outNormals[o + 2] = fz; }
    outPositions[o] = positions[3 * v]; outPositions[o + 1] = positions[3 * v + 1]; outPositions[o + 2] = positions[3 * v + 2];
  }

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(outPositions, 3));
  geometry.setAttribute("normal", new THREE.BufferAttribute(outNormals, 3));
  return geometry;
}
