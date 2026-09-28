"""Playbooks: one folder per plan type under playbooks/, kept in the repo.

    playbooks/<type>/base.json      what a designer STARTS from for this plan type
                                    (people decide this; the exemplar export uses it
                                    to split a finished drawing into base and delta)
    playbooks/<type>/playbook.md    how the plan type is produced, written by Claude
                                    Desktop from an exemplar brief
    playbooks/<type>/checks.json    the machine-checkable half: acceptance checks a
                                    dashboard run evaluates after it draws

Nothing here reads a drawing. The base spec is data about a plan type, the
playbook is prose plus checks, and both are versioned with the code because
they are know-how, not a client's drawing.
"""

import json
import os
import re
import time

import checks as checks_mod

import cadagent_config
# save_playbook writes here, so a packaged install seeds a copy in the data dir
PLAYBOOKS_DIR = cadagent_config.folder("playbooks_dir", "playbooks",
                                       os.path.join(os.path.dirname(os.path.abspath(__file__)), "playbooks"), seed=True)

REQUIRED_SECTIONS = ["Scope", "Base", "Steps", "Rules of thumb", "Checks", "Vocabulary", "Open questions"]

_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,39}$")


class PlaybookError(RuntimeError):
    pass


def _type_dir(plan_type: str) -> str:
    plan_type = str(plan_type or "").strip().lower()
    if not _TYPE_RE.match(plan_type):
        raise PlaybookError(f"plan type {plan_type!r} must be a short lowercase slug (letters, digits, - _)")
    return os.path.join(PLAYBOOKS_DIR, plan_type)


def plan_types() -> list:
    try:
        return sorted(d for d in os.listdir(PLAYBOOKS_DIR)
                      if os.path.isdir(os.path.join(PLAYBOOKS_DIR, d)) and not d.startswith("_"))
    except OSError:
        return []


def _write_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ── Base spec ─────────────────────────────────────────────────────────────────


def default_base_spec(plan_type: str, description: str = "") -> dict:
    """A template to edit. keep.* is the base: whole snapshot categories, xrefs
    (fnmatch patterns on the xref name; "*" = every xref), tags and entity kinds.
    delta.tags are always the designer's work even when they arrive from an
    xref. nested_layers are extra xref layer suffixes worth pulling out of the
    xrefs for this plan type (the plugin's /nested route matches on suffix)."""
    return {
        "schema": 1,
        "name": plan_type,
        "description": description or (
            f"What a {plan_type} plan starts from. Edit keep/delta below: the base is the "
            "xrefs (PBase and the others) plus the alignments; everything else drawn in the "
            "drawing itself is the delta."),
        "keep": {
            "categories": ["alignments", "layouts", "surfaces", "parcels", "corridor_lines",
                           "sample_lines", "view_frames", "cogo_points"],
            "xrefs": ["*"],
            "tags": ["pad", "curb", "lot_line", "setback", "centreline", "right_of_way",
                     "boundary", "easement", "contour"],
            "kinds": ["alignment", "viewport", "surface", "parcel"],
        },
        "delta": {"tags": []},
        "nested_layers": [],
        "_notes": [
            "An xref that is unloaded has no dependent layers in the drawing, so its block "
            "reference is not recognised as an xref and falls to keep.kinds / the default.",
            "Tags are the words in layer_tags.json; run list_tags on an exemplar to see them.",
        ],
    }


def init_plan_type(plan_type: str, description: str = "") -> str:
    """Writes the base.json template for a new plan type; refuses to overwrite."""
    path = os.path.join(_type_dir(plan_type), "base.json")
    if os.path.exists(path):
        raise PlaybookError(f"{path} already exists")
    _write_atomic(path, json.dumps(default_base_spec(plan_type, description), indent=2) + "\n")
    # the sheet spec template beside it (sheets.py imports this module, hence the late import)
    import sheets
    if not os.path.exists(sheets.spec_path(plan_type)):
        _write_atomic(sheets.spec_path(plan_type), json.dumps(sheets.default_spec(plan_type), indent=2) + "\n")
    return path


def load_base_spec(plan_type: str) -> dict:
    path = os.path.join(_type_dir(plan_type), "base.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            spec = json.load(f)
    except FileNotFoundError:
        raise PlaybookError(f"no base spec for plan type {plan_type!r}: create it with "
                            f"`python playbooks.py init {plan_type}` and edit {path}")
    except ValueError as e:
        raise PlaybookError(f"{path} is not valid JSON: {e}")
    if not isinstance(spec, dict):
        raise PlaybookError(f"{path} must hold a JSON object")
    keep = spec.get("keep") if isinstance(spec.get("keep"), dict) else {}
    delta = spec.get("delta") if isinstance(spec.get("delta"), dict) else {}
    out = {
        "schema": spec.get("schema", 1),
        "name": str(spec.get("name") or plan_type),
        "description": str(spec.get("description") or ""),
        "keep": {
            "categories": [str(c) for c in keep.get("categories") or []],
            "xrefs": [str(x) for x in keep.get("xrefs") or []],
            "tags": [str(t).lower() for t in keep.get("tags") or []],
            "kinds": [str(k).lower() for k in keep.get("kinds") or []],
        },
        "delta": {"tags": [str(t).lower() for t in delta.get("tags") or []]},
        "nested_layers": [str(s).lower() for s in spec.get("nested_layers") or []],
    }
    return out


# ── Playbook and checks ───────────────────────────────────────────────────────


def _checks_path(plan_type: str) -> str:
    return os.path.join(_type_dir(plan_type), "checks.json")


def _playbook_path(plan_type: str) -> str:
    return os.path.join(_type_dir(plan_type), "playbook.md")


def missing_sections(markdown: str) -> list:
    heads = {m.group(1).strip().lower() for m in re.finditer(r"^##\s+(.+?)\s*$", markdown or "", re.M)}
    return [s for s in REQUIRED_SECTIONS if s.lower() not in heads]


def _coerce_checks(checks) -> list:
    if isinstance(checks, str):
        try:
            checks = json.loads(checks) if checks.strip() else []
        except ValueError as e:
            raise PlaybookError(f"checks is not valid JSON: {e}")
    if isinstance(checks, dict):
        checks = checks.get("checks", [])
    if not isinstance(checks, list):
        raise PlaybookError("checks must be a list of check objects (or {\"checks\": [...]})")
    return checks


def save_playbook(plan_type: str, markdown: str, checks) -> dict:
    """Writes playbook.md and checks.json after validating both. The markdown
    must carry every required section; every check must validate."""
    missing = missing_sections(markdown)
    if not str(markdown or "").strip():
        raise PlaybookError("markdown is empty")
    if missing:
        raise PlaybookError("markdown is missing required sections: " + ", ".join(missing)
                            + " (each as a '## <name>' heading)")
    check_list = _coerce_checks(checks)
    errors = checks_mod.validate(check_list)
    if errors:
        raise PlaybookError("checks did not validate: " + "; ".join(errors[:12]))
    warnings = checks_mod.warnings(check_list)

    previous = None
    try:
        with open(_checks_path(plan_type), "r", encoding="utf-8") as f:
            previous = json.load(f)
    except (OSError, ValueError):
        previous = None
    version = int((previous or {}).get("version") or 0) + 1
    import spatial
    record = {"schema": 1, "plan_type": plan_type, "version": version,
              "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "vocabulary_version": spatial.TAGS.version, "checks": check_list}
    _write_atomic(_playbook_path(plan_type), markdown.rstrip() + "\n")
    _write_atomic(_checks_path(plan_type), json.dumps(record, indent=2) + "\n")
    return {"plan_type": plan_type, "version": version, "checks": len(check_list),
            "warnings": warnings, "playbook": _playbook_path(plan_type), "checks_file": _checks_path(plan_type)}


def load_checks(plan_type: str) -> list:
    path = _checks_path(plan_type)
    try:
        with open(path, "r", encoding="utf-8") as f:
            record = json.load(f)
    except FileNotFoundError:
        raise PlaybookError(f"plan type {plan_type!r} has no checks.json yet (save_playbook writes it)")
    except ValueError as e:
        raise PlaybookError(f"{path} is not valid JSON: {e}")
    return list((record or {}).get("checks") or [])


def load_playbook(plan_type: str) -> dict:
    out = {"plan_type": plan_type, "markdown": None, "checks": None, "base": None, "version": None}
    try:
        with open(_playbook_path(plan_type), "r", encoding="utf-8") as f:
            out["markdown"] = f.read()
    except OSError:
        pass
    try:
        with open(_checks_path(plan_type), "r", encoding="utf-8") as f:
            record = json.load(f)
        out["checks"] = record.get("checks")
        out["version"] = record.get("version")
        out["saved_at"] = record.get("saved_at")
        out["vocabulary_version"] = record.get("vocabulary_version")
    except (OSError, ValueError):
        pass
    try:
        out["base"] = load_base_spec(plan_type)
    except PlaybookError as e:
        out["base_error"] = str(e)
    return out


def list_playbooks() -> list:
    out = []
    for t in plan_types():
        entry = {"plan_type": t, "has_base": os.path.exists(os.path.join(_type_dir(t), "base.json")),
                 "has_playbook": os.path.exists(_playbook_path(t)),
                 "has_sheets": os.path.exists(os.path.join(_type_dir(t), "sheets.json")),
                 "checks": 0, "version": None, "saved_at": None}
        try:
            with open(_checks_path(t), "r", encoding="utf-8") as f:
                record = json.load(f)
            entry["checks"] = len(record.get("checks") or [])
            entry["version"] = record.get("version")
            entry["saved_at"] = record.get("saved_at")
        except (OSError, ValueError):
            pass
        out.append(entry)
    return out


# ── What Desktop is asked to write ───────────────────────────────────────────


def playbook_output_contract() -> str:
    """The shape of the deliverable, appended to every brief so the author
    knows exactly what save_playbook accepts."""
    kinds = checks_mod.describe_kinds()
    example = json.dumps([
        {"id": "main_present", "kind": "count", "tag": "water", "min": 1,
         "why": "a water plan without a main is not a water plan"},
        {"id": "main_off_curb", "kind": "offset", "tag": "water", "alignment": "*", "min": 8, "max": 20,
         "min_fraction": 0.9, "why": "mains run in the parkway between curb and right of way"},
        {"id": "hydrant_on_main", "kind": "within", "tag": "appurtenance", "name": "hydrant", "of": "water",
         "distance": 2, "why": "a hydrant hangs off the main"},
        {"id": "hydrant_spacing", "kind": "spacing", "tag": "appurtenance", "name": "hydrant", "along": "*",
         "max": 500, "severity": "warn", "why": "fire code spacing"},
        {"id": "no_sanitary_crossing", "kind": "no_crossing", "tag": "water", "other": "sanitary",
         "why": "crossings need a separate detail; flag them"},
    ], indent=1)
    return "\n".join([
        "## What to write (output contract)",
        "Write ONE markdown document and ONE JSON list of checks, then call",
        "save_playbook(plan_type, markdown, checks_json). The markdown must have these",
        "'## ' sections, in this order:",
        "  ## Scope            what this plan type is and is not",
        "  ## Base             what the designer starts from (restate the base spec in words)",
        "  ## Steps            the ordered block sequence that produces the delta, with the",
        "                      parameters that mattered in the exemplars (use block names",
        "                      from the palette; say where judgement is needed). State the run",
        "                      policy: on_conflict pause for production unless a step says why",
        "                      warn is safe; and which checks are expected to fail mid-design",
        "                      (covered fails until cut_sheets has run)",
        "  ## Rules of thumb   distances, offsets, spacings, sides, in the index's words",
        "  ## Checks           the same checks as the JSON, one line each, with the reason",
        "  ## Vocabulary       the tags this plan type needs and any layers still unnamed",
        "  ## Open questions   what the exemplars did not settle",
        "  ## Sheets           (optional) how the exemplars were cut into sheets: scale, viewport",
        "                      rectangle, overlap, what stays visible, the furniture on every sheet;",
        "                      then propose a sheets.json and save it with save_sheet_spec",
        "Never quote absolute coordinates; say station, offset, side, region.",
        "",
        "checks_json: a JSON list. Common fields: id (unique slug), kind, tag, optional",
        "name (substring of the entity's name, e.g. \"hydrant\"), layer (substring),",
        "scope (\"delta\" = only what the run drew, default; \"all\" = the whole drawing),",
        "severity (\"error\" default | \"warn\"), why (free text). Kinds:",
        kinds,
        "Example:",
        example,
    ])


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python playbooks.py init <type> [description] | list | show <type>")
        return 0
    cmd = argv[0]
    try:
        if cmd == "init":
            print(init_plan_type(argv[1], " ".join(argv[2:])))
        elif cmd == "list":
            print(json.dumps(list_playbooks(), indent=2))
        elif cmd == "show":
            print(json.dumps(load_playbook(argv[1]), indent=2))
        else:
            print(f"unknown command {cmd!r}")
            return 2
    except (PlaybookError, IndexError) as e:
        print(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
