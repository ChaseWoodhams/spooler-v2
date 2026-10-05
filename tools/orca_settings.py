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


def number_field(body: str, name: str):
    match = re.search(r"def->" + name + r"\s*=\s*(-?\d+(?:\.\d*)?)", body)
    return float(match.group(1)) if match else None


def parse_options(source: str) -> dict:
    matches = list(DEFINITION.finditer(source))
    options = {}
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
            "min": number_field(body, "min"),
            "max": number_field(body, "max"),
            "mode": (mode.group(1).lower() if mode else "simple"),
            "readonly": bool(re.search(r"def->readonly\s*=\s*true", body)),
        }
        if values:
            option["enum"] = [[value, labels[i] if i < len(labels) else value] for i, value in enumerate(values)]
        # keep 0 limits (0 == False in Python), drop only missing values and false flags
        options[key] = {k: v for k, v in option.items() if k == "vector" or (v is not None and v is not False)}
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
    return tabs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", default="v2.4.2")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "public" / "orca" / "settings.json"))
    parser.add_argument("--cache", default=str(Path(tempfile.gettempdir()) / "orca-source"))
    args = parser.parse_args(argv)
    cache = Path(args.cache)
    options = parse_options(fetch(args.tag, SOURCES["config"], cache))
    tabs = parse_tabs(fetch(args.tag, SOURCES["tabs"], cache))
    used = {key for pages in tabs.values() for p in pages for g in p["groups"] for r in g["rows"] for key in r["options"]}
    missing = sorted(key for key in used if key not in options)
    schema = {
        "orca_version": args.tag.lstrip("v"),
        "source": f"https://github.com/OrcaSlicer/OrcaSlicer/tree/{args.tag} — PrintConfig.cpp and Tab.cpp (AGPL-3.0)",
        "tabs": tabs,
        "options": {key: options[key] for key in sorted(used) if key in options},
    }
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
