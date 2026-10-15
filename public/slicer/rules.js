/* Which process settings OrcaSlicer greys out ("field") or hides ("line") depending on other settings.
   The rules themselves are data in rules-data.js, shared with the server (setting_rules.py), so the two cannot drift apart.
   Each rule is [what, keys, condition]; `v` reads a setting's current value as a string.
   Expression operators: val num on off eq ne gt lt and or not in list startsWith endsWith true false.
   Keep the semantics identical to setting_rules.py: tests/test_rules_parity.py runs both over the same states. */
import DATA from "./rules-data.js";

const num = value => parseFloat(value) || 0;

function compile(expr) {
  if (!Array.isArray(expr)) return () => expr;                                  // string or number literal
  const [op, ...args] = expr;
  const sub = args.map(a => (op === "list" ? a : compile(a)));
  switch (op) {
    case "val": return v => v[args[0]];
    case "num": return v => num(v[args[0]]);
    case "on": return v => v[args[0]] === "1" || v[args[0]] === "true";
    case "off": return v => v[args[0]] === "none" || v[args[0]] === "disabled_fuzzy" || v[args[0]] === undefined;
    case "eq": return v => sub[0](v) === sub[1](v);
    case "ne": return v => sub[0](v) !== sub[1](v);
    case "gt": return v => sub[0](v) > sub[1](v);
    case "lt": return v => sub[0](v) < sub[1](v);
    case "and": return v => sub.every(f => !!f(v));
    case "or": return v => sub.some(f => !!f(v));
    case "not": return v => !sub[0](v);
    case "list": return () => args[0];
    case "in": return v => sub[1](v).includes(sub[0](v));
    case "startsWith": return v => typeof sub[0](v) === "string" && sub[0](v).startsWith(sub[1](v));
    case "endsWith": return v => typeof sub[0](v) === "string" && sub[0](v).endsWith(sub[1](v));
    case "true": return () => true;
    case "false": return () => false;
    default: throw new Error(`unknown rule operator ${op}`);
  }
}

export const RULES = DATA.map(rule => [rule.what, rule.keys, compile(rule.when)]);

/* Evaluate the rules for `values` (a function key → string | undefined).
   Returns { hidden: Set of keys, disabled: Set of keys }. A key hidden or disabled by any rule stays so:
   OrcaSlicer applies its rules in order and later ones win, so the last rule for a key and kind decides. */
export function evaluate(values) {
  const v = new Proxy({}, { get: (_, key) => (typeof key === "string" ? values(key) : undefined) });
  const state = { line: new Map(), field: new Map() };
  for (const [what, keys, when] of RULES) {
    let result;
    try { result = !!when(v); } catch (_) { continue; }
    for (const key of keys) state[what].set(key, result);
  }
  const collect = map => new Set([...map].filter(([, shown]) => !shown).map(([key]) => key));
  return { hidden: collect(state.line), disabled: collect(state.field) };
}
