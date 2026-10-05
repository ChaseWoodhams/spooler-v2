/* Spooler slicer – the Centauri Carbon printer drawn around the bed. tools/build_printer_model.py writes the
   model as gzip of: "SPP1", a uint32 header length, a JSON header, then per chunk uint16 positions quantised
   to the chunk's bounds (padded to 4 bytes) and uint32 triangle indices. Chunks belong to groups that move
   separately: frame and shell are fixed, the gantry follows Y, the toolhead X and Y, and the bed Z. */
import * as THREE from "three";

export const PRINTER_MODEL_URL = "/printer/centauri-carbon.spp.gz";

async function gunzip(response) {
  if (!response.ok) throw new Error(`Printer model: HTTP ${response.status}`);
  // A server that set Content-Encoding has already inflated it; otherwise the gzip magic is still there.
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes[0] !== 0x1f || bytes[1] !== 0x8b) return bytes.buffer;
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Response(stream).arrayBuffer();
}

/** Loads the printer model. Returns { header, groups: { frame, shell, gantry, toolhead, bed } } of THREE.Groups. */
export async function loadPrinterModel(url = PRINTER_MODEL_URL) {
  const buffer = await gunzip(await fetch(url, { credentials: "same-origin" }));
  if (new TextDecoder().decode(new Uint8Array(buffer, 0, 4)) !== "SPP1") throw new Error("Unexpected printer model format");
  const headerLength = new DataView(buffer).getUint32(4, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 8, headerLength)));
  const groups = Object.fromEntries(["frame", "shell", "gantry", "toolhead", "bed"].map(name => {
    const group = new THREE.Group();
    group.name = name;
    return [name, group];
  }));
  let offset = 8 + headerLength;
  for (const chunk of header.chunks) {
    const quantised = new Uint16Array(buffer, offset, chunk.vertices * 3);
    offset += Math.ceil(chunk.vertices * 6 / 4) * 4;
    const index = new Uint32Array(buffer, offset, chunk.triangles * 3);
    offset += chunk.triangles * 12;
    const positions = new Float32Array(chunk.vertices * 3);
    for (let i = 0; i < positions.length; i++) {
      const axis = i % 3;
      positions[i] = chunk.min[axis] + quantised[i] / 65535 * (chunk.max[axis] - chunk.min[axis]);
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.setIndex(new THREE.BufferAttribute(index.slice(), 1));
    geometry.computeBoundingSphere();
    const shell = chunk.group === "shell";
    // CAD tessellation has no useful normals once merged and decimated; flat shading reads fine for machinery.
    const material = new THREE.MeshStandardMaterial({
      color: chunk.color, flatShading: true, roughness: 0.65, metalness: 0.15,
      transparent: shell, opacity: shell ? 0.18 : 1, depthWrite: !shell, side: shell ? THREE.DoubleSide : THREE.FrontSide,
    });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.renderOrder = shell ? 2 : 0;
    groups[chunk.group]?.add(mesh);
  }
  return { header, groups };
}
