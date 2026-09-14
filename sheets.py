"""Sheets: paper-space layouts cut from model space, dressed with furniture.

A plan type's sheet spec (playbooks/<type>/sheets.json) says what a sheet of
that type looks like: the template layout to clone (the title block),
where the viewport sits on the paper, the scales allowed, how much windows
overlap, which tags stay visible in the viewport, which title-block attributes
to fill, and the furniture -- north arrow, scale bar, notes, contractor
warnings -- each with a place on the sheet and a condition for being there.

The planner reads the spatial index, finds the extent of the designer's work
(the delta words of the base spec, or whatever words the spec names), tiles it
with viewport windows at the chosen scale, and turns every window into ONE
plugin `sequence`: clone the template layout, cut the viewport, fill the
title block, place the furniture, write the tables. The sequence is all or
nothing in the plugin, so a sheet either exists whole or not at all.

Nothing here talks to the plugin or the dashboard; cad_mcp_server.py and
dashboard/server.py call in with an index and a snapshot.
"""

import fnmatch
import json
import math
import os
import time
from collections import Counter

import checks as checks_mod
import playbooks
import spatial
from spatial import _signed_offset, _point_in_polygon

SCHEMA = 1
SHEETS_FILE = "sheets.json"
CORNERS = ("sw", "se", "nw", "ne", "center")
WHEN_WORDS = ("always", "first_sheet", "last_sheet")
TABLE_COLUMNS = {"no": "NO.", "handle": "HANDLE", "name": "NAME", "tag": "TYPE", "layer": "LAYER",
                 "station": "STATION", "offset": "OFFSET", "side": "SIDE", "x": "X", "y": "Y"}
MAX_SHEETS_HARD = 60


class SheetError(RuntimeError):
    pass


# ── The spec ─────────────────────────────────────────────────────────────────


def default_spec(plan_type: str) -> dict:
    """A template to edit. Paper units are inches, model units feet; a scale of
    40 means 1" = 40'. Fill template.file / source blocks with the paths."""
    code = "".join(ch for ch in plan_type.upper() if ch.isalnum())[:3] or "S"
    return {
        "schema": SCHEMA,
        "name": plan_type,
        "template": {"file": "", "layout": "", "title_block": ""},
        "viewport": {"center": [16.5, 12.5], "width": 30.0, "height": 21.0, "layer": "vport"},
        "scales": [40, 50, 60, 100],
        "overlap": 0.1,
        "margin": 50.0,
        "max_sheets": 12,
        "twist_deg": 0.0,
        "layout_name": "{type_code}-{n:02d}",
        "type_code": code,
        "subject": {"tags": [], "state": ""},
        "visible": {"tags": [], "layers": [], "xrefs": ["*"]},
        "hidden": {"tags": [], "layers": ["defpoints", "*-nplt"]},
        "title_block": {"attributes": {"SHEET": "{n}", "OF": "{count}", "SCALE": "{scale_text}", "TITLE": "{title}"}},
        "furniture": [
            {"block": "NORTH", "source_file": "", "anchor": "viewport", "corner": "ne", "at": [-1.5, -1.5],
             "rotation": "north", "scale": 1.0, "layer": "0", "when": "always"},
            {"block": "SCALEBAR-{scale}", "source_file": "", "anchor": "viewport", "corner": "sw", "at": [0.5, 0.5],
             "rotation": 0, "scale": 1.0, "layer": "0", "when": "always"},
        ],
        "tables": [],
        "_notes": [
            "TEMPLATE: template.file is the .dwg/.dwt with the title-block layout named template.layout;",
            "  leave both empty to cut plain layouts. subject.tags default to the base spec's delta tags.",
            "visible: what shows in every viewport (tags, layer patterns, xref patterns); every other",
            "  layer in the drawing is frozen in the viewport. hidden wins over visible.",
            "furniture.when: always | first_sheet | last_sheet | {\"tag_present\": word} |",
            "  {\"check\": {kind, tag, ...}, \"include_if\": \"fail\" | \"pass\"} (evaluated over that sheet).",
            "anchor sheet: `at` is a paper point; anchor viewport: `at` is an offset from `corner` of the viewport.",
            "Attribute values, mtext and block names may use {n} {count} {scale} {scale_text} {title}",
            "  {plan_type} {drawing} {date} {north_deg} {layout}.",
            "tables: {title, per: sheet|set, anchor, corner, at, columns: [no, name, station, offset, side, ...],",
            "  rows: {tag, state, along}} -- rows are the tag's entities on that sheet, sorted by station.",
        ],
    }


def spec_path(plan_type: str) -> str:
    return os.path.join(playbooks._type_dir(plan_type), SHEETS_FILE)


def _num(v, name, errors, lo=None, hi=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        errors.append(f"{name} must be a number")
        return None
    if lo is not None and f < lo or hi is not None and f > hi:
        errors.append(f"{name} must be between {lo} and {hi}")
    return f


def validate_spec(spec) -> list:
    """Errors that make a sheet spec unusable; [] when it is fine."""
    errors = []
    if not isinstance(spec, dict):
        return ["sheets spec must be a JSON object"]
    vp = spec.get("viewport") if isinstance(spec.get("viewport"), dict) else None
    if vp is None:
        errors.append("viewport {center, width, height} is required")
    else:
        c = vp.get("center")
        if not (isinstance(c, (list, tuple)) and len(c) == 2):
            errors.append("viewport.center must be [x, y] in paper units")
        else:
            _num(c[0], "viewport.center[0]", errors)
            _num(c[1], "viewport.center[1]", errors)
        w = _num(vp.get("width"), "viewport.width", errors, lo=0.1)
        h = _num(vp.get("height"), "viewport.height", errors, lo=0.1)
    scales = spec.get("scales")
    if not isinstance(scales, list) or not scales:
        errors.append("scales must be a non-empty list of feet per inch (40 = 1\" = 40')")
    else:
        for s in scales:
            _num(s, "scales entry", errors, lo=0.01)
    _num(spec.get("overlap", 0.1), "overlap", errors, lo=0.0, hi=0.9)
    _num(spec.get("margin", 0.0), "margin", errors, lo=0.0)
    _num(spec.get("max_sheets", 12), "max_sheets", errors, lo=1, hi=MAX_SHEETS_HARD)
    _num(spec.get("twist_deg", 0.0), "twist_deg", errors, lo=-360, hi=360)
    tpl = spec.get("template") or {}
    if not isinstance(tpl, dict):
        errors.append("template must be an object {file, layout, title_block}")
    elif bool(tpl.get("file")) != bool(tpl.get("layout")):
        errors.append("template.file and template.layout go together (both set, or both empty)")
    name = spec.get("layout_name", "{type_code}-{n:02d}")
    try:
        str(name).format(type_code="X", n=1, plan_type="t", scale=40)
    except (KeyError, ValueError, IndexError) as e:
        errors.append(f"layout_name pattern is not usable: {e}")
    if "{n" not in str(name):
        errors.append("layout_name must contain {n} so every sheet gets its own name")
    for i, f in enumerate(spec.get("furniture") or []):
        where = f"furniture[{i}]"
        if not isinstance(f, dict):
            errors.append(f"{where}: not an object")
            continue
        if not (f.get("block") or f.get("mtext")):
            errors.append(f"{where}: needs block (a block name) or mtext (text)")
        anchor = f.get("anchor", "sheet")
        if anchor not in ("sheet", "viewport"):
            errors.append(f"{where}: anchor must be sheet or viewport")
        if anchor == "viewport" and f.get("corner", "sw") not in CORNERS:
            errors.append(f"{where}: corner must be one of {CORNERS}")
        at = f.get("at", [0, 0])
        if not (isinstance(at, (list, tuple)) and len(at) == 2):
            errors.append(f"{where}: at must be [x, y]")
        errors.extend(f"{where}: {e}" for e in _when_errors(f.get("when", "always")))
        rot = f.get("rotation", 0)
        if rot != "north":
            _num(rot, f"{where}.rotation", errors)
    for i, t in enumerate(spec.get("tables") or []):
        where = f"tables[{i}]"
        if not isinstance(t, dict):
            errors.append(f"{where}: not an object")
            continue
        rows = t.get("rows") if isinstance(t.get("rows"), dict) else None
        if not rows or not rows.get("tag"):
            errors.append(f"{where}: rows.tag is required")
        for col in t.get("columns") or []:
            if str(col).lower() not in TABLE_COLUMNS:
                errors.append(f"{where}: unknown column {col!r}; known: {', '.join(TABLE_COLUMNS)}")
        if t.get("per", "sheet") not in ("sheet", "set"):
            errors.append(f"{where}: per must be sheet or set")
    return errors


def _when_errors(when) -> list:
    if when in WHEN_WORDS:
        return []
    if not isinstance(when, dict):
        return [f"when must be one of {WHEN_WORDS} or an object"]
    if "tag_present" in when:
        return [] if str(when["tag_present"]).strip() else ["tag_present needs a tag word"]
    if "check" in when:
        c = dict(when["check"] or {})
        c.setdefault("id", "when")
        errs = checks_mod.validate([c])
        if when.get("include_if", "fail") not in ("fail", "pass"):
            errs.append("include_if must be fail or pass")
        return errs
    return ["when must have tag_present or check"]


def _normalise(spec: dict, plan_type: str) -> dict:
    out = json.loads(json.dumps(default_spec(plan_type)))
    out.pop("_notes", None)
    for k, v in spec.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "title_block":
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    out["scales"] = [float(s) for s in out["scales"]]
    out["overlap"] = float(out.get("overlap", 0.1))
    out["margin"] = float(out.get("margin", 0.0))
    out["max_sheets"] = int(float(out.get("max_sheets", 12)))
    out["twist_deg"] = float(out.get("twist_deg", 0.0))
    out["viewport"]["width"] = float(out["viewport"]["width"])
    out["viewport"]["height"] = float(out["viewport"]["height"])
    out["viewport"]["center"] = [float(out["viewport"]["center"][0]), float(out["viewport"]["center"][1])]
    for f in out.get("furniture") or []:
        f.setdefault("anchor", "sheet")
        f.setdefault("corner", "sw")
        f.setdefault("at", [0, 0])
        f.setdefault("when", "always")
        f.setdefault("rotation", 0)
        f.setdefault("scale", 1.0)
        f.setdefault("layer", "0")
    for t in out.get("tables") or []:
        t.setdefault("per", "sheet")
        t.setdefault("anchor", "sheet")
        t.setdefault("corner", "sw")
        t.setdefault("at", [0, 0])
        t.setdefault("columns", ["no", "name", "station", "offset"])
    return out


def load_spec(plan_type: str) -> dict:
    path = spec_path(plan_type)
    try:
        with open(path, "r", encoding="utf-8") as f:
            spec = json.load(f)
    except FileNotFoundError:
        raise SheetError(f"no sheet spec for plan type {plan_type!r}: save one with save_sheet_spec "
                         f"(get_sheet_spec returns a template) or edit {path}")
    except ValueError as e:
        raise SheetError(f"{path} is not valid JSON: {e}")
    errors = validate_spec(spec)
    if errors:
        raise SheetError(f"{path} did not validate: " + "; ".join(errors[:6]))
    return _normalise(spec, plan_type)


def save_spec(plan_type: str, spec) -> dict:
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError as e:
            raise SheetError(f"sheets spec is not valid JSON: {e}")
    errors = validate_spec(spec)
    if errors:
        raise SheetError("sheets spec did not validate: " + "; ".join(errors[:8]))
    path = spec_path(plan_type)
    spec = dict(spec)
    spec["schema"] = SCHEMA
    spec["name"] = plan_type
    spec["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    playbooks._write_atomic(path, json.dumps(spec, indent=2) + "\n")
    return {"plan_type": plan_type, "path": path, "furniture": len(spec.get("furniture") or []),
            "tables": len(spec.get("tables") or []), "warnings": spec_warnings(spec)}


def spec_warnings(spec: dict) -> list:
    """Soft problems: files that are not on this machine, words the vocabulary
    does not know. Neither stops a plan; both stop a cut from working."""
    out = []
    tpl = spec.get("template") or {}
    if tpl.get("file") and not os.path.exists(str(tpl["file"])):
        out.append(f"template.file not found: {tpl['file']}")
    for f in spec.get("furniture") or []:
        sf = f.get("source_file")
        if f.get("block") and sf and not os.path.exists(str(sf)):
            out.append(f"furniture {f.get('block')}: source_file not found: {sf}")
        elif f.get("block") and not sf:
            out.append(f"furniture {f.get('block')}: no source_file; the block must already be defined in the drawing")
    known = set(spatial.TAGS.known_words())
    for w in list((spec.get("subject") or {}).get("tags") or []) + list((spec.get("visible") or {}).get("tags") or []):
        if str(w).lower() not in known:
            out.append(f"tag {w!r} is not a word the vocabulary produces")
    return out


# ── Scale arithmetic ─────────────────────────────────────────────────────────


def scale_text(scale_ft: float) -> str:
    """40 -> 1\" = 40'."""
    s = float(scale_ft)
    return f"1\" = {int(round(s))}'" if abs(s - round(s)) < 1e-9 else f"1\" = {s:g}'"


def paper_scale(scale_ft: float) -> float:
    """The viewport's CustomScale: paper units per model unit, 1/40 for 1\" = 40'
    when paper is inches and the model is feet."""
    return 1.0 / float(scale_ft)


def _rot(p, c, deg):
    a = math.radians(deg)
    dx, dy = p[0] - c[0], p[1] - c[1]
    return (c[0] + dx * math.cos(a) - dy * math.sin(a), c[1] + dx * math.sin(a) + dy * math.cos(a))


# ── Extent and windows ───────────────────────────────────────────────────────


def subject_words(spec: dict, plan_type: str) -> list:
    words = [str(w).lower() for w in (spec.get("subject") or {}).get("tags") or []]
    if not words:
        try:
            words = list(playbooks.load_base_spec(plan_type)["delta"]["tags"])
        except playbooks.PlaybookError:
            words = []
    return words


def _points_of(index, handles) -> list:
    pts = []
    for h in handles:
        for p in index.pieces_for(h):
            if p.kind == "pt":
                pts.append(p.geom)
            else:
                for a, b in p.chords():
                    pts.append(a)
                    pts.append(b)
        if not index.pieces_for(h) and h in index.rings:
            pts.extend(index.rings[h][0])
    return pts


def extent(index, words, state=None, margin=0.0):
    """(x0, y0, x1, y1, handles, source): the box around every entity the words
    name, grown by margin; the main cluster of the drawing when they name
    nothing. None when the drawing is empty."""
    handles = set()
    for w in words or []:
        handles |= index.handles_for(w, state or None)
    pts = _points_of(index, handles)
    source = "subject"
    if not pts:
        keys = [k for k in index.cells if k[0] == 0]
        cluster, _ = index._main_cluster(keys) if keys else ([], [])
        for k in cluster:
            x0, y0, x1, y1 = spatial.bounds(k)
            pts.extend([(x0, y0), (x1, y1)])
        handles = set()
        for k in cluster:
            handles |= index.cells[k].all_handles()
        source = "cluster"
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    m = float(margin or 0.0)
    return (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m, handles, source)


def _handles_in_ring(index, ring, words, state=None) -> set:
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    box = (min(xs), min(ys), max(xs), max(ys))
    out = set()
    for w in (words or [None]):
        for h in index.entities(w, bbox=box, state=state or None):
            out.add(h)
    if not words:
        out = set(index.entities(None, bbox=box))
    # a twisted ring is smaller than its box: keep only what really falls inside
    axis_aligned = abs(ring[0][1] - ring[1][1]) < 1e-9 and abs(ring[1][0] - ring[2][0]) < 1e-9
    if axis_aligned:
        return out
    keep = set()
    for h in out:
        if any(_point_in_polygon(p, ring) for p in _points_of(index, [h])):
            keep.add(h)
    return keep


def _grid(ext, w, h, overlap, twist):
    """Window centres and rings over an extent, in page order (rows from the
    top, columns west to east), in the twisted frame about the extent centre."""
    x0, y0, x1, y1 = ext[:4]
    c = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    corners = [_rot(p, c, -twist) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    lx0, lx1 = min(p[0] for p in corners), max(p[0] for p in corners)
    ly0, ly1 = min(p[1] for p in corners), max(p[1] for p in corners)
    W, H = lx1 - lx0, ly1 - ly0
    step_x, step_y = w * (1.0 - overlap), h * (1.0 - overlap)
    cols = 1 if W <= w + 1e-9 else 1 + int(math.ceil((W - w) / step_x - 1e-9))
    rows = 1 if H <= h + 1e-9 else 1 + int(math.ceil((H - h) / step_y - 1e-9))
    total_w, total_h = w + (cols - 1) * step_x, h + (rows - 1) * step_y
    first_x = c[0] - total_w / 2.0 + w / 2.0
    first_y = c[1] + total_h / 2.0 - h / 2.0
    out = []
    for r in range(rows):
        for k in range(cols):
            lc = (first_x + k * step_x, first_y - r * step_y)
            centre = _rot(lc, c, twist)
            ring = [_rot((lc[0] + dx, lc[1] + dy), c, twist)
                    for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))]
            out.append({"row": r, "col": k, "center": [round(centre[0], 3), round(centre[1], 3)],
                        "ring": [[round(p[0], 3), round(p[1], 3)] for p in ring]})
    return out, rows, cols


def plan_windows(index, spec: dict, plan_type: str = "", scale=None, max_sheets=None, twist_deg=None,
                 words=None) -> dict:
    """Where the sheets go. Picks the first scale in spec.scales that needs at
    most max_sheets sheets (or the given scale), tiles the subject's extent with
    overlapping windows the viewport shows at that scale, drops windows that
    hold no subject, and numbers the rest in page order."""
    words = list(words) if words is not None else subject_words(spec, plan_type)
    state = (spec.get("subject") or {}).get("state") or None
    ext = extent(index, words, state, spec.get("margin", 0.0))
    if ext is None:
        raise SheetError("the drawing has nothing to put on a sheet")
    twist = float(spec.get("twist_deg", 0.0) if twist_deg is None else twist_deg)
    limit = int(max_sheets or spec.get("max_sheets", 12))
    vp = spec["viewport"]
    warnings = []
    if ext[5] == "cluster":
        warnings.append("no entity matched the subject words " + (", ".join(words) or "(none)") +
                        "; the sheets cover the drawing's main cluster instead")
    candidates = [float(scale)] if scale else list(spec["scales"])
    subject_only = ext[5] == "subject"

    def tile(s):
        """Windows at one scale that hold some of the subject; each carries every
        entity in it (furniture conditions and tables look at all of them)."""
        w, h = vp["width"] * s, vp["height"] * s
        windows, rows, cols = _grid(ext, w, h, spec.get("overlap", 0.1), twist)
        kept = []
        for win in windows:
            subject = _handles_in_ring(index, win["ring"], words, state) if subject_only else \
                _handles_in_ring(index, win["ring"], None)
            if subject:
                kept.append({**win, "subject_handles": sorted(subject),
                             "handles": sorted(_handles_in_ring(index, win["ring"], None))})
        return kept, rows, cols

    tried, chosen = [], None
    for s in candidates:
        kept, rows, cols = tile(s)
        tried.append({"scale": s, "sheets": len(kept), "grid": [rows, cols]})
        if kept and (len(kept) <= limit or scale):
            chosen = (s, kept, rows, cols)
            break
    if chosen is None:
        # every scale needs too many sheets: take the one with the fewest and say so
        best = min(tried, key=lambda t: t["sheets"] or 10 ** 9)
        s = best["scale"]
        kept, rows, cols = tile(s)
        if not kept:
            raise SheetError("no window holds any of the subject; check subject.tags in the sheet spec")
        chosen = (s, kept, rows, cols)
        warnings.append(f"every scale needs more than {limit} sheets; using 1\" = {s:g}' with {len(kept)}")
    if len(chosen[1]) > MAX_SHEETS_HARD:
        raise SheetError(f"{len(chosen[1])} sheets at 1\" = {chosen[0]:g}' is more than {MAX_SHEETS_HARD}; "
                         "raise the scale or narrow subject.tags")
    s, kept, rows, cols = chosen
    sheets = []
    tag_of = index.entity_tag
    for n, win in enumerate(kept, start=1):
        counts = Counter(tag_of.get(h, "other") for h in win["handles"])
        sheets.append({"n": n, "layout": _layout_name(spec, plan_type, n, s), "center": win["center"],
                       "ring": win["ring"], "row": win["row"], "col": win["col"], "scale": s,
                       "handles": win["handles"], "subject_handles": win["subject_handles"],
                       "tags": dict(counts.most_common()), "north_deg": twist})
    return {"plan_type": plan_type, "scale": s, "scale_text": scale_text(s), "paper_scale": paper_scale(s),
            "twist_deg": twist, "count": len(sheets), "grid": [rows, cols], "sheets": sheets,
            "extent": [round(v, 2) for v in ext[:4]], "extent_source": ext[5], "subject": words,
            "tried": tried, "warnings": warnings}


def _layout_name(spec, plan_type, n, scale):
    return str(spec.get("layout_name") or "{type_code}-{n:02d}").format(
        type_code=spec.get("type_code") or "S", n=n, plan_type=plan_type, scale=int(round(scale)))


def describe_plan(index, plan: dict) -> list:
    """One line per sheet, in the index's words."""
    lines = [f"{plan['count']} sheet(s) at {plan['scale_text']}, grid {plan['grid'][0]} row(s) x "
             f"{plan['grid'][1]} column(s)" + (f", twisted {plan['twist_deg']:g} deg" if plan["twist_deg"] else "")]
    for sh in plan["sheets"]:
        what = ", ".join(f"{n} {t.replace('_', ' ')}" for t, n in list(sh["tags"].items())[:6])
        x0, y0 = min(p[0] for p in sh["ring"]), min(p[1] for p in sh["ring"])
        x1, y1 = max(p[0] for p in sh["ring"]), max(p[1] for p in sh["ring"])
        lines.append(f"  {sh['layout']}: row {sh['row'] + 1}, column {sh['col'] + 1}; "
                     f"x {x0:,.0f}-{x1:,.0f}, y {y0:,.0f}-{y1:,.0f}; holds {what or 'nothing named'}")
    lines.extend("  note: " + w for w in plan.get("warnings") or [])
    return lines


# ── What shows in the viewport ───────────────────────────────────────────────


def _layer_names(index, data) -> list:
    names = set()
    for l in (data or {}).get("layers") or []:
        n = l.get("name") if isinstance(l, dict) else l
        if n:
            names.add(str(n))
    names.update(k for k in index.layer_info if k)
    return sorted(names)


def _match_any(name, patterns) -> bool:
    n = name.lower()
    return any(fnmatch.fnmatchcase(n, str(p).lower()) for p in patterns or [])


def freeze_list(index, data, spec: dict) -> dict:
    """Layers to freeze in every viewport: every layer of the drawing that is
    not visible by tag, layer pattern or xref pattern; hidden always wins."""
    vis, hid = spec.get("visible") or {}, spec.get("hidden") or {}
    vis_tags = [str(t).lower() for t in vis.get("tags") or []]
    hid_tags = [str(t).lower() for t in hid.get("tags") or []]
    freeze, visible = [], []
    for layer in _layer_names(index, data):
        info = index.layer_info.get(layer) or {}
        tag = info.get("tag")
        xref = layer.split("|", 1)[0] if "|" in layer else ""
        shown = False
        if tag and vis_tags and spatial.TAGS.matches(tag, vis_tags):
            shown = True
        if _match_any(layer, vis.get("layers")):
            shown = True
        if xref and _match_any(xref, vis.get("xrefs")):
            shown = True
        if not xref and not vis_tags and not vis.get("layers"):
            shown = True          # nothing asked for: native layers stay on
        if (tag and hid_tags and spatial.TAGS.matches(tag, hid_tags)) or _match_any(layer, hid.get("layers")):
            shown = False
        (visible if shown else freeze).append(layer)
    return {"freeze": freeze, "visible": visible}


# ── Furniture and tables ─────────────────────────────────────────────────────


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _fill(text, values: dict):
    if not isinstance(text, str):
        return text
    try:
        return text.format_map(_Safe(values))
    except (ValueError, IndexError):
        return text


def _values(sheet, plan, spec, plan_type, drawing, title) -> dict:
    return {"n": sheet["n"], "count": plan["count"], "scale": int(round(plan["scale"])),
            "scale_text": plan["scale_text"], "title": title or "", "plan_type": plan_type,
            "drawing": os.path.splitext(os.path.basename(str(drawing or "")))[0],
            "date": time.strftime("%Y-%m-%d"), "north_deg": round(sheet.get("north_deg", 0.0), 2),
            "layout": sheet["layout"], "type_code": spec.get("type_code") or ""}


def _anchor_point(entry, spec) -> list:
    at = entry.get("at") or [0, 0]
    if entry.get("anchor", "sheet") != "viewport":
        return [float(at[0]), float(at[1])]
    vp = spec["viewport"]
    cx, cy = vp["center"]
    hw, hh = vp["width"] / 2.0, vp["height"] / 2.0
    corner = {"sw": (cx - hw, cy - hh), "se": (cx + hw, cy - hh), "nw": (cx - hw, cy + hh),
              "ne": (cx + hw, cy + hh), "center": (cx, cy)}[entry.get("corner", "sw")]
    return [round(corner[0] + float(at[0]), 4), round(corner[1] + float(at[1]), 4)]


def _wanted(entry, sheet, plan, index) -> bool:
    when = entry.get("when", "always")
    if when == "always":
        return True
    if when == "first_sheet":
        return sheet["n"] == 1
    if when == "last_sheet":
        return sheet["n"] == plan["count"]
    if isinstance(when, dict) and "tag_present" in when:
        return bool(index.handles_for(when["tag_present"]) & set(sheet["handles"]))
    if isinstance(when, dict) and "check" in when:
        c = dict(when["check"])
        c.setdefault("id", "when")
        c.setdefault("scope", "delta")
        results = checks_mod.evaluate(index, [c], set(sheet["handles"]))
        ok = bool(results and results[0]["ok"])
        crossings = (results[0]["detail"].get("crossings") if results else None)
        if crossings is not None:
            # a main that crosses gas three sheets away is not this sheet's warning
            ok = not any(_point_in_polygon(tuple(x["at"]), sheet["ring"]) for x in crossings)
        return (not ok) if when.get("include_if", "fail") == "fail" else ok
    return False


def furniture_for(sheet, plan, spec, index, plan_type="", drawing="", title="") -> list:
    """The furniture payloads for one sheet, positions in paper units."""
    values = _values(sheet, plan, spec, plan_type, drawing, title)
    out = []
    for entry in spec.get("furniture") or []:
        if not _wanted(entry, sheet, plan, index):
            continue
        at = _anchor_point(entry, spec)
        rot = entry.get("rotation", 0)
        rot = float(sheet.get("north_deg", 0.0)) if rot == "north" else float(rot or 0)
        if entry.get("block"):
            payload = {"intent": "insert_block", "layout": sheet["layout"], "name": _fill(entry["block"], values),
                       "position": at, "rotation_deg": rot, "scale": entry.get("scale", 1.0),
                       "layer": entry.get("layer", "0"),
                       "attributes": {k: _fill(v, values) for k, v in (entry.get("attributes") or {}).items()}}
            if entry.get("source_file"):
                payload["source_file"] = entry["source_file"]
                if entry.get("source_block"):
                    payload["source_block"] = _fill(entry["source_block"], values)
            out.append(payload)
        else:
            payload = {"intent": "draw_mtext", "layout": sheet["layout"], "text": _fill(entry["mtext"], values),
                       "position": at, "rotation_deg": rot, "layer": entry.get("layer", "0"),
                       "attachment": entry.get("attachment", "top_left")}
            for k in ("width", "height", "style"):
                if entry.get(k) is not None:
                    payload[k] = entry[k]
            out.append(payload)
    return out


def _nearest_alignment(index, p, name):
    name = str(name or "*")
    best = None
    for a in index.alignments:
        if name != "*" and a["name"].lower() != name.lower():
            continue
        d, sta, side = _signed_offset(a["points"], p)
        if best is None or d < best[1]:
            best = (a, d, sta, side)
    return best


def table_rows(table: dict, sheet, plan, index) -> list:
    """Rows for one table: the tag's entities on the sheet (or in the whole
    plan when per=set), one per handle, sorted by station along the alignment."""
    rows_spec = table.get("rows") or {}
    handles = index.handles_for(rows_spec.get("tag"), rows_spec.get("state") or None)
    if table.get("per", "sheet") == "sheet":
        handles &= set(sheet["handles"])
    else:
        allowed = set()
        for sh in plan["sheets"]:
            allowed |= set(sh["handles"])
        handles &= allowed
    rows = []
    for h in handles:
        pieces = index.pieces_for(h)
        pts = _points_of(index, [h])
        if not pts:
            continue
        rp = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        best = _nearest_alignment(index, rp, rows_spec.get("along", "*"))
        rows.append({"handle": h, "name": next((p.name for p in pieces if p.name), ""),
                     "tag": index.entity_tag.get(h, ""), "layer": next((p.layer for p in pieces if p.layer), ""),
                     "station": round(best[2], 1) if best else None, "offset": round(best[1], 1) if best else None,
                     "side": best[3] if best else "", "x": round(rp[0], 2), "y": round(rp[1], 2)})
    rows.sort(key=lambda r: (r["station"] if r["station"] is not None else 1e12, r["handle"]))
    for i, r in enumerate(rows, start=1):
        r["no"] = i
    return rows


def tables_for(sheet, plan, spec, index, plan_type="", drawing="", title="") -> list:
    values = _values(sheet, plan, spec, plan_type, drawing, title)
    out = []
    for table in spec.get("tables") or []:
        if table.get("per", "sheet") == "set" and sheet["n"] != 1:
            continue
        rows = table_rows(table, sheet, plan, index)
        if not rows and not table.get("always"):
            continue
        cols = [str(c).lower() for c in table.get("columns") or ["no", "name", "station", "offset"]]
        headers = table.get("headers") or [TABLE_COLUMNS[c] for c in cols]
        cells = [headers] + [[_cell_text(r.get(c)) for c in cols] for r in rows]
        title_text = _fill(table.get("title") or "", values)
        if title_text:
            cells = [[title_text] + [""] * (len(cols) - 1)] + cells
        payload = {"intent": "create_table", "layout": sheet["layout"], "position": _anchor_point(table, spec),
                   "cells": cells, "layer": table.get("layer", "0")}
        if title_text:
            payload["title"] = title_text
        for k in ("row_height", "column_width", "text_height", "style"):
            if table.get(k) is not None:
                payload[k] = table[k]
        out.append(payload)
    return out


def _cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.1f}"
    return str(v)


# ── The sequences ────────────────────────────────────────────────────────────


def sheet_payloads(plan: dict, spec: dict, index, data, plan_type="", drawing="", title="") -> list:
    """One `sequence` per sheet: layout, viewport, title block, furniture, tables.
    Each carries `sheet` (the plan entry) so the dashboard and the verifier can
    say which sheet a step belongs to."""
    frozen = freeze_list(index, data, spec)["freeze"]
    tpl = spec.get("template") or {}
    vp = spec["viewport"]
    out = []
    for sheet in plan["sheets"]:
        steps = []
        if tpl.get("file"):
            steps.append({"intent": "manage_layouts", "action": "clone_from_template", "name": sheet["layout"],
                          "source_file": tpl["file"], "source_layout": tpl["layout"]})
        else:
            steps.append({"intent": "manage_layouts", "action": "create", "name": sheet["layout"]})
        steps.append({"intent": "create_viewport", "layout": sheet["layout"], "center": list(vp["center"]),
                      "width": vp["width"], "height": vp["height"], "view_center": list(sheet["center"]),
                      "scale": paper_scale(plan["scale"]), "twist_deg": plan["twist_deg"],
                      "freeze_layers": frozen, "locked": True, "layer": vp.get("layer", "vport")})
        attrs = (spec.get("title_block") or {}).get("attributes") or {}
        if tpl.get("file") and attrs:
            values = _values(sheet, plan, spec, plan_type, drawing, title)
            steps.append({"intent": "set_block_attributes", "handle": "@0",
                          "attributes": {k: _fill(v, values) for k, v in attrs.items()}})
        steps.extend(furniture_for(sheet, plan, spec, index, plan_type, drawing, title))
        steps.extend(tables_for(sheet, plan, spec, index, plan_type, drawing, title))
        # "_sheet" travels with the payload for the dashboard and the verifier; the
        # runner strips underscored keys before posting, so the plugin never sees it.
        out.append({"intent": "sequence", "steps": steps,
                    "_sheet": {k: sheet[k] for k in ("n", "layout", "center", "ring", "scale", "tags")}})
    return out


def plan_and_payloads(index, data, spec, plan_type, scale=None, max_sheets=None, twist_deg=None,
                      drawing="", title="") -> tuple:
    plan = plan_windows(index, spec, plan_type, scale=scale, max_sheets=max_sheets, twist_deg=twist_deg)
    return plan, sheet_payloads(plan, spec, index, data, plan_type, drawing, title)


# ── Reading what is there ────────────────────────────────────────────────────


def list_layouts(data: dict, index=None) -> list:
    """The sheets a drawing already has, in words: viewports with their model
    windows, what each window holds, and the furniture on the paper."""
    out = []
    for lay in (data or {}).get("layouts") or []:
        vps = []
        for vp in lay.get("viewports") or []:
            ring = vp.get("model_ring") or _ring_of(vp)
            holds = {}
            if index is not None and ring:
                counts = Counter(index.entity_tag.get(h, "other") for h in _handles_in_ring(index, ring, None))
                counts.pop("viewport", None)          # other sheets' frames are not content
                holds = dict(counts.most_common(12))
            s = vp.get("scale")
            vps.append({"handle": vp.get("handle"), "on": vp.get("on", True), "scale": s,
                        "scale_text": scale_text(1.0 / s) if s else None, "twist_deg": vp.get("twist_deg", 0),
                        "model_center": vp.get("model_center"), "model_window": vp.get("model_window"),
                        "paper_center": vp.get("paper_center"), "paper_size": vp.get("paper_size"),
                        "frozen_layers": len(vp.get("frozen_layers") or []) if isinstance(vp.get("frozen_layers"), list)
                        else vp.get("frozen_layers"), "holds": holds})
        out.append({"name": lay.get("name"), "tab_order": lay.get("tab_order"), "paper_size": lay.get("paper_size"),
                    "viewports": vps,
                    "blocks": [{"name": b.get("name"), "position": b.get("position"),
                                "attributes": b.get("attributes") or {}} for b in lay.get("blocks") or []][:40],
                    "tables": [{"title": t.get("title"), "rows": t.get("rows")} for t in lay.get("tables") or []],
                    "texts": len(lay.get("texts") or [])})
    return out


def _ring_of(vp):
    win = vp.get("model_window")
    if not win:
        return None
    (x0, y0), (x1, y1) = win
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


# ── What finished drawings say about sheets ──────────────────────────────────


def sheet_stats(data: dict, delta_handles=None, index=None) -> dict:
    """Per-drawing facts about its sheets, for the exemplar bundle: scales,
    viewport rectangles, overlap between windows, margin around the work,
    frozen sets and the furniture on each layout. Coordinates are paper units
    or ratios; model coordinates are not written."""
    layouts = (data or {}).get("layouts") or []
    scales, rects, frozen, furniture, tables = Counter(), Counter(), Counter(), Counter(), Counter()
    windows = []
    per_layout = []
    for lay in layouts:
        vps = lay.get("viewports") or []
        names = []
        for vp in vps:
            if not vp.get("on", True):
                continue
            s = vp.get("scale")
            if s:
                scales[round(1.0 / s, 2)] += 1
            pc, ps = vp.get("paper_center"), vp.get("paper_size")
            if pc and ps:
                rects[(round(pc[0], 2), round(pc[1], 2), round(ps[0], 2), round(ps[1], 2))] += 1
            fl = vp.get("frozen_layers")
            if isinstance(fl, list):
                for l in fl:
                    frozen[str(l).lower()] += 1
            ring = vp.get("model_ring") or _ring_of(vp)
            if ring:
                windows.append(ring)
        for b in lay.get("blocks") or []:
            names.append(b.get("name"))
            furniture[str(b.get("name"))] += 1
        for t in lay.get("tables") or []:
            tables[str(t.get("title") or "?")] += 1
        per_layout.append({"name": lay.get("name"), "viewports": len(vps), "blocks": sorted(set(n for n in names if n)),
                           "tables": [t.get("title") for t in lay.get("tables") or []]})
    overlap = _overlap_ratio(windows)
    margin = None
    if index is not None and delta_handles and windows:
        pts = _points_of(index, delta_handles)
        if pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            ux0 = min(p[0] for r in windows for p in r)
            ux1 = max(p[0] for r in windows for p in r)
            uy0 = min(p[1] for r in windows for p in r)
            uy1 = max(p[1] for r in windows for p in r)
            margin = {"west": round(min(xs) - ux0, 1), "east": round(ux1 - max(xs), 1),
                      "south": round(min(ys) - uy0, 1), "north": round(uy1 - max(ys), 1)}
    return {"layouts": len(layouts), "viewports": sum(len(l.get("viewports") or []) for l in layouts),
            "scales": {scale_text(s): n for s, n in scales.most_common()},
            "viewport_rects": [{"center": [r[0], r[1]], "size": [r[2], r[3]], "count": n} for r, n in rects.most_common(3)],
            "overlap": overlap, "margin": margin,
            "frozen_common": [l for l, n in frozen.most_common(40) if n >= max(1, len(windows) // 2)],
            "furniture": dict(furniture.most_common(30)), "tables": dict(tables.most_common(10)),
            "per_layout": per_layout[:40]}


def _overlap_ratio(windows: list):
    """Median share of a window's width shared with its nearest neighbour."""
    if len(windows) < 2:
        return None
    boxes = [(min(p[0] for p in r), min(p[1] for p in r), max(p[0] for p in r), max(p[1] for p in r)) for r in windows]
    shares = []
    for i, a in enumerate(boxes):
        best = 0.0
        for j, b in enumerate(boxes):
            if i == j:
                continue
            ox = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
            oy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
            if ox > 0 and oy > 0:
                best = max(best, min(ox / (a[2] - a[0] or 1), oy / (a[3] - a[1] or 1)))
        shares.append(best)
    shares.sort()
    return round(shares[len(shares) // 2], 3)


def suggest_spec(plan_type: str, bundles_stats: list) -> dict:
    """A sheet spec pre-filled from what the exemplars did: the commonest
    viewport rectangle, the scales seen, the typical overlap, the furniture
    every sheet carried (with no source_file: the user fills the library path)."""
    spec = default_spec(plan_type)
    rects, scales, furniture, overlaps = Counter(), Counter(), Counter(), []
    for st in bundles_stats or []:
        for r in st.get("viewport_rects") or []:
            rects[(tuple(r["center"]), tuple(r["size"]))] += r["count"]
        for name, n in (st.get("scales") or {}).items():
            scales[name] += n
        for name, n in (st.get("furniture") or {}).items():
            furniture[name] += n
        if st.get("overlap") is not None:
            overlaps.append(st["overlap"])
    if rects:
        (c, s), _ = rects.most_common(1)[0]
        spec["viewport"].update({"center": list(c), "width": s[0], "height": s[1]})
    if scales:
        feet = []
        for name in scales:
            try:
                feet.append(float(name.split("=")[1].strip().rstrip("'")))
            except (IndexError, ValueError):
                pass
        if feet:
            spec["scales"] = sorted(set(feet))
    if overlaps:
        overlaps.sort()
        spec["overlap"] = overlaps[len(overlaps) // 2]
    # furniture on (nearly) every sheet across the exemplars, not just in every drawing
    n_layouts = sum(int(st.get("layouts") or 0) for st in bundles_stats or [])
    common = [name for name, n in furniture.most_common() if n_layouts and n >= 0.9 * n_layouts]
    if common:
        spec["furniture"] = [{"block": name, "source_file": "", "anchor": "sheet", "at": [0, 0], "rotation": 0,
                              "scale": 1.0, "layer": "0", "when": "always"} for name in common[:12]]
        spec["_notes"].append("furniture was seeded from the exemplars' block names; set source_file and positions")
    return spec
