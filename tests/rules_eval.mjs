// Evaluates public/slicer/rules.js (the module the browser runs) over states read from stdin: [{key: value}, ...].
// Prints [{hidden: [...], disabled: [...]}, ...] with sorted keys. Used by tests/test_rules_parity.py.
import { pathToFileURL } from "node:url";
import path from "node:path";
import fs from "node:fs";

const rulesUrl = pathToFileURL(path.resolve(process.argv[2] || "public/slicer/rules.js")).href;
const { evaluate } = await import(rulesUrl);
const states = JSON.parse(fs.readFileSync(0, "utf8"));
const out = states.map(state => {
  const { hidden, disabled } = evaluate(key => state[key]);
  return { hidden: [...hidden].sort(), disabled: [...disabled].sort() };
});
process.stdout.write(JSON.stringify(out));
