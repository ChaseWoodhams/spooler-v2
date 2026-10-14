"""
Generate the slicer settings schema used by Spooler's slicer workspace from OrcaSlicer's source.

OrcaSlicer (AGPL-3.0, like Spooler) defines every setting in src/libslic3r/PrintConfig.cpp — label,
tooltip, unit, range, choices, mode — and lays them out on pages and groups in src/slic3r/GUI/Tab.cpp.
This script reads both for a pinned release and writes one JSON file the browser renders from.

usage: python tools/orca_settings.py [--tag v2.4.2] [--out public/orca/settings.json] [--cache DIR]
"""

import argparse
import json
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

RAW = "https://raw.githubusercontent.com/OrcaSlicer/OrcaSlicer/{tag}/{path}"
SOURCES = {"config": "src/libslic3r/PrintConfig.cpp", "tabs": "src/slic3r/GUI/Tab.cpp"}
TAB_FUNCTIONS = {"process": "void TabPrint::build()", "filament": "void TabFilament::build()", "machine": "void TabPrinter::build_fff()"}
TYPES = {
    "coFloat": ("float", False), "coFloats": ("float", True), "coInt": ("int", False), "coInts": ("int", True),
    "coBool": ("bool", False), "coBools": ("bool", True), "coString": ("string", False), "coStrings": ("string", True),
    "coPercent": ("percent", False), "coPercents": ("percent", True), "coFloatOrPercent": ("float_or_percent", False),
    "coFloatsOrPercents": ("float_or_percent", True), "coEnum": ("enum", False), "coEnums": ("enum", True),
    "coPoint": ("point", False), "coPoints": ("point", True), "coPoint3": ("point", False), "coNone": ("none", False),
}

STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
DEFINITION = re.compile(r'\bdef\s*=\s*this->add(?:_nullable)?\(\s*"([^"]+)"\s*,\s*(co\w+)\s*\)')


def fetch(tag: str, path: str, cache: Path) -> str:
    local = cache / tag / path.replace("/", "__")
    if not local.is_file():
        local.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(RAW.format(tag=tag, path=path), headers={"User-Agent": "spooler-settings-generator"})
        local.write_bytes(urllib.request.urlopen(request, timeout=60).read())
    return local.read_text(encoding="utf-8", errors="replace")


def unescape(text: str) -> str:
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    return text.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")


def text_field(body: str, name: str) -> str | None:
    """A string assignment such as def->tooltip = L("a " "b"); — adjacent literals are joined."""
    match = re.search(r"def->" + name + r"\s*=\s*(?:L|_L|_u8L)?\s*\(?(.*?)\)?\s*;", body, re.S)
    if not match:
        return None
    parts = STRING.findall(match.group(1))
    return unescape("".join(parts)) if parts else None


def number_field(body: str, name: str, constants=None, references=None):
    match = re.search(r"def->" + name + r"\s*=\s*([^;]+);", body)
    if not match:
        return None
    expression = match.group(1).strip()
    literal = re.fullmatch(r"(-?\d+(?:\.\d*)?)(?:[fF])?", expression)
    if literal:
        return float(literal.group(1))
    sign, key = (-1, expression[1:]) if expression.startswith('-') else (1, expression)
    if key in (constants or {}):
        return sign * constants[key]
    copied = re.fullmatch(r"(\w+)->(min|max|max_literal)", expression)
    return (references or {}).get(copied.group(1), {}).get(copied.group(2)) if copied else None


def parse_options(source: str) -> dict:
    # CLI/SLA sections may reuse FFF names; only the FFF definition is authoritative.
    source = source[:source.find("void PrintConfigDef::init_sla") if "void PrintConfigDef::init_sla" in source else len(source)]
    matches = list(DEFINITION.finditer(source))
    options = {}
    references = {}
    constants = {name: float(value) for name, value in re.findall(r"\bconst(?:expr)?\s+(?:int|float|double)\s+(\w+)\s*=\s*(-?\d+(?:\.\d*)?)[fF]?\s*;", source)}
    for index, match in enumerate(matches):
        key, co_type = match.group(1), match.group(2)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        body = source[match.end():end]
        kind, vector = TYPES.get(co_type, ("unknown", False))
        mode = re.search(r"def->mode\s*=\s*com(\w+)", body)
        values = re.findall(r'def->enum_values\.(?:push_back|emplace_back)\(\s*"([^"]+)"', body)
        labels = [unescape(label) for label in
                  re.findall(r'def->enum_labels\.(?:push_back|emplace_back)\(\s*(?:L|_L)?\s*\(?\s*"((?:[^"\\]|\\.)*)"', body)]
        option = {
            "type": kind,
            "vector": vector,
            "label": text_field(body, "label"),
            "full_label": text_field(body, "full_label"),
            "category": text_field(body, "category"),
            "tooltip": text_field(body, "tooltip"),
            "sidetext": text_field(body, "sidetext"),
            "min": number_field(body, "min", constants, references),
            "max": number_field(body, "max", constants, references),
            "mode": (mode.group(1).lower() if mode else "simple"),
            "readonly": bool(re.search(r"def->readonly\s*=\s*true", body)),
            "nullable": "add_nullable" in match.group(0) or bool(re.search(r"def->nullable\s*=\s*true", body)),
            "ratio_over": text_field(body, "ratio_over"),
            "max_literal": number_field(body, "max_literal", constants, references),
        }
        copied = re.search(r"def->enum_values\s*=\s*(\w+)->enum_values", body)
        if copied and copied.group(1) in references:
            option["enum"] = references[copied.group(1)].get("enum", [])
        if values:
            option["enum"] = [[value, labels[i] if i < len(labels) else value] for i, value in enumerate(values)]
        # keep 0 limits (0 == False in Python), drop only missing values and false flags
        options[key] = {k: v for k, v in option.items() if k == "vector" or (v is not None and v is not False)}
        prefix = source[source.rfind("\n", 0, match.start()):match.start()]
        named = re.search(r"auto\s+(\w+)\s*=\s*$", prefix)
        if named:
            references[named.group(1)] = options[key]
        for ref in re.findall(r"(?:auto\s*\*?|ConfigOptionDef\s*\*?)\s*(\w+)\s*=\s*def\s*;", body):
            references[ref] = options[key]
    # These nullable Filament definitions are constructed from the printer's
    # extruder definitions, rather than literal add("key", type) calls.
    dynamic = re.search(r'filament_extruder_override_keys\s*=\s*\{(.*?)\};', source, re.S)
    if dynamic and 'this->add_nullable(opt_key, it_opt->second.type)' in source:
        keys = STRING.findall(re.sub(r'//[^\n]*', '', dynamic.group(1)))
        simple = {"filament_retraction_length", "filament_z_hop", "filament_long_retractions_when_cut", "filament_retraction_distances_when_cut"}
        for key in keys:
            parent = key.removeprefix("filament_")
            if parent not in options:
                raise ValueError(f"Missing definition for dynamic override {key}")
            options[key] = {k: v for k, v in options[parent].items()
                            if k in {"type", "vector", "label", "full_label", "tooltip", "sidetext", "min", "max", "enum"}}
            options[key].update(nullable=True, mode="simple" if key in simple else "advanced", inherits_from={"kind": "machine", "key": parent})
    for key in ("filament_ironing_flow", "filament_ironing_spacing", "filament_ironing_inset", "filament_ironing_speed"):
        options[key]["inherits_from"] = {"kind": "process", "key": key.removeprefix("filament_")}
    return options


def parse_tabs(source: str) -> dict:
    tabs = {}
    event = re.compile(
        r'add_options_page\(\s*L\("(?P<page>[^"]+)"'
        r'|new_optgroup\(\s*L\("(?P<group>[^"]+)"'
        r'|append_single_option_line\(\s*"(?P<single>[^"#]+)'
        r'|\bline\s*=\s*\{\s*L\("(?P<line>[^"]*)"'
        r'|line\.append_option\(\s*optgroup->get_option\(\s*"(?P<lineopt>[^"#]+)'
        r'|optgroup->append_line\(\s*line\s*\)(?P<append>)'
    )
    for tab, signature in TAB_FUNCTIONS.items():
        start = source.find(signature)
        if start < 0:
            raise SystemExit(f"{signature} not found in Tab.cpp")
        end = source.find("\nvoid Tab", start + len(signature))
        pages, page, group, line = [], None, None, None
        for m in event.finditer(source[start:end]):
            if m.group("page"):
                page = {"page": m.group("page"), "groups": []}
                pages.append(page)
            elif m.group("group") and page is not None:
                group = {"group": m.group("group"), "rows": []}
                page["groups"].append(group)
            elif m.group("single") and group is not None:
                group["rows"].append({"options": [m.group("single")]})
            elif m.group("line") is not None:
                line = {"label": m.group("line"), "options": []}
            elif m.group("lineopt") and line is not None:
                line["options"].append(m.group("lineopt"))
            elif m.group("append") is not None and line is not None and group is not None:
                if line["options"]:
                    group["rows"].append(line)
                line = None
        tabs[tab] = [{"page": p["page"], "groups": [g for g in p["groups"] if g["rows"]]} for p in pages]
        tabs[tab] = [p for p in tabs[tab] if p["groups"]]
    # TabFilament::build mounts this helper; extract its active loop declarations
    # (including ironing) so comments cannot accidentally become editable keys.
    helper = re.search(r'void TabFilament::add_filament_overrides_page\(\)(.*?)\nvoid Tab', source, re.S)
    if helper:
        body = re.sub(r'//[^\n]*', '', helper.group(1))
        groups = []
        for group, appender in (("Retraction", "retraction"), ("Ironing", "ironing")):
            loop = re.search(r'for\s*\(const std::string opt_key\s*:\s*\{([^{}]*)\}\)\s*append_' + appender + r'_option', body, re.S)
            if not loop:
                raise ValueError(f"Missing {group} override layout")
            groups.append({"group": group, "rows": [{"options": [key]} for key in STRING.findall(loop.group(1))]})
        tabs["filament"].append({"page": "Setting Overrides", "groups": groups})
    return tabs


def filament_dependencies(options):
    """Pinned TabFilament::update/update_filament_overrides_page conditions.

    Keep inactive fields inspectable: the client disables them with this reason
    instead of removing their stored values. Machine conditions use the locked
    machine, not a claim about attached hardware capability.
    """
    def requires(keys, key, test, value, reason, kind="filament"):
        for target in keys:
            options[target].setdefault("dependencies", []).append(
                {"kind": kind, "key": key, "test": test, "value": value, "reason": reason})
    requires(["overhang_fan_speed", "overhang_fan_threshold", "internal_bridge_fan_speed"],
             "enable_overhang_bridge_fan", "eq", "1", "Enable overhang and bridge cooling to use this value.")
    requires(["dont_slow_down_outer_wall"], "slow_down_for_layer_cooling", "eq", "1",
             "Enable slowing down for layer cooling to use this value.")
    requires(["additional_cooling_fan_speed"], "auxiliary_fan", "eq", "1",
             "The selected printer preset must enable an auxiliary fan.", "machine")
    requires(["activate_air_filtration", "during_print_exhaust_fan_speed", "complete_print_exhaust_fan_speed"],
             "support_air_filtration", "eq", "1", "The selected printer preset must support air filtration.", "machine")
    for stage, key in (("during_print", "during_print_exhaust_fan_speed"), ("on_completion", "complete_print_exhaust_fan_speed")):
        requires([key], "activate_air_filtration", "eq", "1", "Enable air filtration to use this fan speed.")
        requires([key], f"activate_air_filtration_{stage}", "eq", "1", "Enable air filtration for this stage to use this fan speed.")
    retract = [key for key, meta in options.items() if meta.get("inherits_from", {}).get("kind") == "machine" and key != "filament_retraction_length"]
    requires(retract, "filament_retraction_length", "gt", 0, "Retraction length must be greater than zero to use this override.")
    requires(["filament_long_retractions_when_cut", "filament_retraction_distances_when_cut"],
             "enable_long_retraction_when_cut", "eq", "2", "The selected printer preset must allow Filament retraction when cutting.", "machine")
    requires(["filament_retraction_distances_when_cut"], "filament_long_retractions_when_cut", "eq", "1",
             "Enable long retraction when cutting to use this distance.")
    for meta in options.values():
        if meta.get("vector"):
            meta.update(vector_shape="preserve-existing", edit_index=0)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", default="v2.4.2")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "public" / "orca" / "settings.json"))
    parser.add_argument("--cache", default=str(Path(tempfile.gettempdir()) / "orca-source"))
    args = parser.parse_args(argv)
    cache = Path(args.cache)
    options = parse_options(fetch(args.tag, SOURCES["config"], cache))
    filament_dependencies(options)
    tabs = parse_tabs(fetch(args.tag, SOURCES["tabs"], cache))
    used = {key for pages in tabs.values() for p in pages for g in p["groups"] for r in g["rows"] for key in r["options"]}
    missing = sorted(key for key in used if key not in options)
    schema = {
        "orca_version": args.tag.lstrip("v"),
        "source": f"https://github.com/OrcaSlicer/OrcaSlicer/tree/{args.tag} — PrintConfig.cpp and Tab.cpp (AGPL-3.0)",
        "tabs": tabs,
        "options": {key: options[key] for key in sorted(used) if key in options},
        "definitions": {key: options[key] for key in sorted(options)},
    }
    for key, meta in schema["options"].items():
        meta["scopes"] = [kind for kind, pages in tabs.items()
                          if any(key in row["options"] for page in pages for group in page["groups"] for row in group["rows"])]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(schema, indent=1, ensure_ascii=False), encoding="utf-8")
    counts = {tab: sum(len(r["options"]) for p in pages for g in p["groups"] for r in g["rows"]) for tab, pages in tabs.items()}
    print(f"{len(options)} options defined; tabs {counts}; written {out} ({out.stat().st_size // 1024} kB)")
    if missing:
        print(f"{len(missing)} options on the tabs have no definition: {', '.join(missing[:20])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
