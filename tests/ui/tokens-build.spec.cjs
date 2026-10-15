// The CSS variable blocks in public/style.css are generated from tokens/spooler.tokens.json.
// A hand edit of a generated block, or a token edit without `npm run tokens:build`, fails here.
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const assert = require('node:assert');

const root = path.join(__dirname, '..', '..');
const run = spawnSync(process.execPath, [path.join(root, 'scripts', 'build-tokens.cjs'), '--check'], { encoding: 'utf8' });
assert.strictEqual(run.status, 0, run.stderr || run.stdout);

// slicer.css must not redeclare a token: one source of truth.
const tokens = JSON.parse(fs.readFileSync(path.join(root, 'tokens', 'spooler.tokens.json'), 'utf8'));
const names = new Set();
(function walk(n) { for (const [k, v] of Object.entries(n)) { if (k.startsWith('$') || typeof v !== 'object') continue; if ('$value' in v) names.add(k); else walk(v); } })(tokens);
for (const file of fs.readdirSync(path.join(root, 'public')).filter(f => f.endsWith('.css') && f !== 'style.css')) {
  const css = fs.readFileSync(path.join(root, 'public', file), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  for (const m of css.matchAll(/(:root|body\.light-mode)\s*\{([^}]*)\}/g)) {
    for (const name of names) assert(!new RegExp(`--${name}\s*:`).test(m[2]), `${file} redeclares --${name}; change tokens/spooler.tokens.json instead`);
  }
}
console.log('PASS: style.css tokens are generated and not redeclared');
