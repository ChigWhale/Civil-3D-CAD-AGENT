"""The block palette as prompt text, and the reference material beside it.

The planner may only use block types the dashboard has, with the field
names the dashboard validates, so the prompt lists them from
dashboard.blocks.BLOCK_SPECS itself (one line per block, options included)
rather than from any hand-written copy. Whole-drawing generators
(draw_driveways, cut_sheets ...) and layout / file blocks are left out: a
single reviewer comment is never one of those.
"""
import hashlib
import json
import os
import re
import sys

from .config import ROOT

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PLANNER_GROUPS = ("Geometry", "Layers", "Edit", "Draw", "Annotate", "Modify", "Query", "Blocks & References")
EXAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples")


def _specs():
    from dashboard import blocks
    return blocks.BLOCK_SPECS


def compact_specs(groups=None) -> list:
    groups = tuple(groups) if groups else PLANNER_GROUPS
    out = []
    for spec in _specs():
        if spec.get("group") not in groups:
            continue
        fields = []
        for f in spec.get("fields") or []:
            c = {"name": f["name"], "type": f.get("type", "text")}
            if "default" in f:
                c["default"] = f["default"]
            elif f.get("optional"):
                c["optional"] = True
            for k in ("options", "min_items"):
                if f.get(k) not in (None, "", []):
                    c[k] = f[k]
            fields.append(c)
        out.append({"type": spec["type"], "group": spec.get("group"),
                    "description": str(spec.get("description") or "").strip(), "fields": fields})
    return out


def _field_text(f: dict) -> str:
    s = f"{f['name']}:{f['type']}"
    if "default" in f:
        s += f"={json.dumps(f['default'])}"
    elif f.get("optional"):
        s += "?"
    if f.get("options"):
        s += "[" + "|".join(str(o) for o in f["options"]) + "]"
    return s


def palette_text(groups=None, max_desc: int = 110) -> str:
    lines = []
    for s in compact_specs(groups):
        desc = re.sub(r"\s+", " ", s["description"])[:max_desc]
        lines.append(f"- {s['type']} ({s['group']}): {desc} | fields: " + ", ".join(_field_text(f) for f in s["fields"]))
    return "\n".join(lines)


def palette_version() -> str:
    return hashlib.sha1(json.dumps(compact_specs(None), sort_keys=True, default=str).encode()).hexdigest()[:12]


def field_types() -> dict:
    """type -> {field name: field type}, for the handle guard in planner.validate."""
    return {s["type"]: {f["name"]: f["type"] for f in s["fields"]} for s in compact_specs(tuple(
        {sp.get("group") for sp in _specs()}))}


def load_examples(directory: str | None = None) -> list:
    directory = directory or EXAMPLES_DIR
    out = []
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except OSError:
        return out
    for n in names:
        try:
            with open(os.path.join(directory, n), "r", encoding="utf-8") as f:
                ex = json.load(f)
            if isinstance(ex, dict) and "comment" in ex and "answer" in ex:
                ex["_file"] = n
                out.append(ex)
        except (OSError, ValueError):
            continue
    return out


def playbook_steps(plan_type: str | None) -> str:
    if not plan_type:
        return ""
    try:
        import playbooks
        md = playbooks.load_playbook(plan_type).get("markdown") or ""
    except Exception:
        return ""
    m = re.search(r"^## Steps\s*$(.*?)(?=^## |\Z)", md, re.M | re.S)
    return m.group(1).strip() if m else ""


def recipe_blocks(plan_type: str | None) -> list:
    if not plan_type:
        return []
    try:
        from learn import recipe
        return recipe.blocks(plan_type)
    except Exception:
        return []
