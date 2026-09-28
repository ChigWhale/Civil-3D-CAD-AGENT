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
import re
import time
from collections import Counter

import checks as checks_mod
import playbooks
import profiles
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
        "profile": {
            "scale": 40.0,
            "span": 40.0,
            "station_host": "eg",
            "surface": "",
            "sheets": {k: dict(v) for k, v in profiles.PROFILE_DEFAULTS["sheets"].items()},
            "styles": dict(profiles.PROFILE_DEFAULTS["styles"]),
            "placement": dict(profiles.PROFILE_DEFAULTS["placement"]),
            "name_format": profiles.PROFILE_DEFAULTS["name_format"],
            "drag_lift": profiles.PROFILE_DEFAULTS["drag_lift"],
        },
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
            "profile (sanitary profile sheets): scale = horizontal ft per inch (40: 1000 ft = 25 in); span = 36 or 40 ft",
            "  of elevation per view; sheets[span] = the template layout to clone for that span, its paper size (in),",
            "  slots = where each view's datum corner sits on the paper (in from the lower-left; first slot = first view),",
            "  band_set / profile_view = style overrides for that span. Each sheet is ONE viewport the size of the sheet",
            "  border; the views are placed in model space to match the slots. station_host eg|flowline = which profile",
            "  carries the 1+00 station labels; styles = Civil 3D style names, matched loosely on dashes/arrows (a miss",
            "  falls back to the drawing default with a warning). Title block / furniture may use {alignment} {stations} {views}.",
            "key_map (grading sheets): {file: the key map .dwg, cell_layers / key_layers: layer suffixes (blank = all),",
            "  snap: gaps up to this many feet are closed, key_pattern: regex a key text must match}. Cells are the closed",
            "  faces of the linework; the sheet's scale is the cell width over viewport.width; layout_name may use {key}.",
            "grading: {labels: label_grading parameters, spacing: space_labels parameters} run per chosen cell before the cut.",
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
        str(name).format(type_code="X", n=1, plan_type="t", scale=40, key="K")
    except (KeyError, ValueError, IndexError) as e:
        errors.append(f"layout_name pattern is not usable: {e}")
    if "{n" not in str(name) and "{key" not in str(name):
        errors.append("layout_name must contain {n} (or {key} for key-map sheets) so every sheet gets its own name")
    for i, f in enumerate(spec.get("furniture") or []):
        where = f"furniture[{i}]"
        if not isinstance(f, dict):
            errors.append(f"{where}: not an object")
            continue
        if not (f.get("block") or f.get("mtext") or f.get("xref")):
            errors.append(f"{where}: needs block (a block name), mtext (text) or xref (a name, with source_file)")
        if f.get("xref") and not f.get("source_file"):
            errors.append(f"{where}: an xref needs source_file (the .dwg to reference)")
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
    prof = spec.get("profile")
    if prof is not None:
        if not isinstance(prof, dict):
            errors.append("profile must be an object {scale, span, slots, station_host, styles, placement}")
        else:
            _num(prof.get("scale", 40), "profile.scale", errors, lo=0.01)
            try:
                span = float(prof.get("span", 40))
            except (TypeError, ValueError):
                span = None
            if span is None or span not in profiles.SPANS:
                errors.append("profile.span must be 36 or 40 (feet of elevation per profile view)")
            if str(prof.get("station_host", "flowline")).lower() not in ("flowline", "eg"):
                errors.append("profile.station_host must be flowline or eg")
            table = prof.get("sheets")
            if table is not None:
                if not isinstance(table, dict):
                    errors.append("profile.sheets must be an object keyed by span: {\"36\": {layout, paper, slots}, \"40\": {...}}")
                else:
                    for key, entry in table.items():
                        where = f"profile.sheets[{key!r}]"
                        if str(key) not in ("36", "40"):
                            errors.append(f"{where}: the span must be 36 or 40")
                        if not isinstance(entry, dict):
                            errors.append(f"{where}: not an object")
                            continue
                        paper = entry.get("paper")
                        if paper is not None and not (isinstance(paper, (list, tuple)) and len(paper) == 2):
                            errors.append(f"{where}.paper must be [width, height] in inches")
                        elif paper is not None:
                            _num(paper[0], f"{where}.paper[0]", errors, lo=1)
                            _num(paper[1], f"{where}.paper[1]", errors, lo=1)
                        slots = entry.get("slots")
                        if slots is not None:
                            if not isinstance(slots, list) or not 1 <= len(slots) <= 2:
                                errors.append(f"{where}.slots must hold one or two [x, y] paper points (a view's datum corner)")
                            else:
                                for i, sl in enumerate(slots):
                                    if not (isinstance(sl, (list, tuple)) and len(sl) == 2):
                                        errors.append(f"{where}.slots[{i}] must be [x, y] in paper inches")
            if prof.get("styles") is not None and not isinstance(prof.get("styles"), dict):
                errors.append("profile.styles must be an object of style names")
    km = spec.get("key_map")
    if km is not None:
        if not isinstance(km, dict):
            errors.append("key_map must be an object {file, cell_layers, key_layers, snap, key_pattern}")
        else:
            if km.get("file") is not None and not isinstance(km.get("file"), str):
                errors.append("key_map.file must be a path string")
            for k in ("cell_layers", "key_layers"):
                v = km.get(k)
                if v is not None and not isinstance(v, (list, str)):
                    errors.append(f"key_map.{k} must be a list of layer suffixes (or a comma-separated string)")
            if km.get("snap") is not None:
                _num(km.get("snap"), "key_map.snap", errors, lo=0.001)
            pat = km.get("key_pattern")
            if pat:
                try:
                    re.compile(str(pat))
                except re.error as e:
                    errors.append(f"key_map.key_pattern is not a valid regular expression: {e}")
    gr = spec.get("grading")
    if gr is not None:
        if not isinstance(gr, dict):
            errors.append("grading must be an object {labels: {...}, spacing: {...}}")
        else:
            labels = gr.get("labels")
            if labels is not None and not isinstance(labels, dict):
                errors.append("grading.labels must be an object of label_grading parameters")
            elif labels and str(labels.get("drive_curb_side", "right")) not in ("right", "left", "both"):
                errors.append("grading.labels.drive_curb_side must be right, left or both")
            spacing = gr.get("spacing")
            if spacing is not None and not isinstance(spacing, dict):
                errors.append("grading.spacing must be an object of space_labels parameters")
            elif spacing:
                if str(spacing.get("types", "surface")) not in ("surface", "elevation", "slope", "all"):
                    errors.append("grading.spacing.types must be surface, elevation, slope or all")
                if str(spacing.get("mode", "auto")) not in ("auto", "position", "align"):
                    errors.append("grading.spacing.mode must be auto, position or align")
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
    if tpl.get("file") and not tpl.get("title_block") and (spec.get("title_block") or {}).get("attributes"):
        out.append("template.title_block is blank, so the title_block.attributes (SHEET, OF, ...) are not filled; "
                   "name the template's attributed block to fill them")
    for f in spec.get("furniture") or []:
        sf = f.get("source_file")
        if f.get("block") and sf and not os.path.exists(str(sf)):
            out.append(f"furniture {f.get('block')}: source_file not found: {sf}")
        elif f.get("block") and not sf:
            out.append(f"furniture {f.get('block')}: no source_file; the block must already be defined in the drawing")
        elif f.get("xref") and sf and not os.path.exists(str(sf)):
            out.append(f"furniture xref {f.get('xref')}: source_file not found: {sf}")
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


def plan_from_cells(index, spec: dict, cells: list, plan_type: str = "", scale=None, keys=None) -> dict:
    """A plan (the same shape plan_windows returns) from key-map cells: one
    sheet per chosen cell, its window the cell's ring, its scale derived from
    the cell's size over the viewport's paper size unless `scale` says
    otherwise, its twist the cell's. `keys` = the cells wanted (None = all)."""
    cells = list(cells or [])
    if not cells:
        raise SheetError("the key map has no cells")
    by_key = {}
    for c in cells:
        k = str(c.get("key"))
        if k in by_key:
            raise SheetError(f"the key map has two cells keyed {k!r}; give each cell its own key")
        by_key[k] = c
    if keys:
        wanted = [str(k).strip() for k in keys if str(k).strip()]
        missing = [k for k in wanted if k not in by_key]
        if missing:
            raise SheetError(f"no cell keyed {', '.join(missing)}; the key map has " +
                             ", ".join(sorted(by_key, key=profiles.natural_key)))
        chosen = [by_key[k] for k in wanted]
    else:
        chosen = sorted(cells, key=lambda c: profiles.natural_key(c.get("key")))
    vp = spec["viewport"]
    words = subject_words(spec, plan_type)
    state = (spec.get("subject") or {}).get("state") or None
    allowed = [float(s) for s in spec.get("scales") or []]
    warnings, sheets = [], []
    tag_of = index.entity_tag
    for n, cell in enumerate(chosen, start=1):
        key = str(cell.get("key"))
        width, height = float(cell.get("width") or 0.0), float(cell.get("height") or 0.0)
        if width <= 0 or height <= 0:
            raise SheetError(f"cell {key} has no size")
        derived, cross = width / vp["width"], height / vp["height"]
        if abs(derived - cross) > 0.02 * derived:
            warnings.append(f"cell {key} is {width:,.0f} x {height:,.0f} ft, not the viewport's "
                            f"{vp['width']:g} x {vp['height']:g} aspect; scale taken from the width")
        if abs(derived - round(derived)) <= 0.005 * derived:
            derived = float(round(derived))
        s = float(scale) if scale else derived
        if allowed and not any(abs(s - a) <= 0.005 * a for a in allowed):
            warnings.append(f"cell {key}: 1\" = {s:g}' is not one of the spec's scales "
                            f"({', '.join(f'{a:g}' for a in allowed)})")
        ring = [[float(p[0]), float(p[1])] for p in cell["ring"]]
        handles = sorted(_handles_in_ring(index, ring, None))
        subject = sorted(_handles_in_ring(index, ring, words, state)) if words else list(handles)
        if not handles:
            warnings.append(f"cell {key} holds nothing in this drawing")
        twist = float(cell.get("twist_deg") or 0.0)
        counts = Counter(tag_of.get(h, "other") for h in handles)
        sheets.append({"n": n, "key": key, "layout": _layout_name(spec, plan_type, n, s, key=key),
                       "center": [float(cell["center"][0]), float(cell["center"][1])], "ring": ring,
                       "row": int(cell.get("row") or 0), "col": int(cell.get("col") or 0),
                       "scale": s, "scale_text": scale_text(s), "twist_deg": twist, "north_deg": twist,
                       "window": [[float(v) for v in cell["bbox"][0]], [float(v) for v in cell["bbox"][1]]],
                       "width": width, "height": height, "handles": handles, "subject_handles": subject,
                       "tags": dict(counts.most_common()), "values": {"key": key}})
    scales = sorted({sh["scale"] for sh in sheets})
    twists = sorted({sh["twist_deg"] for sh in sheets})
    if len(scales) > 1:
        warnings.append("the cells are not all the same size: scales " + ", ".join(f"{v:g}" for v in scales))
    xs = [p[0] for sh in sheets for p in sh["ring"]]
    ys = [p[1] for sh in sheets for p in sh["ring"]]
    rows = 1 + max(sh["row"] for sh in sheets)
    cols = 1 + max(sh["col"] for sh in sheets)
    return {"plan_type": plan_type, "scale": scales[0], "scale_text": scale_text(scales[0]),
            "paper_scale": paper_scale(scales[0]), "twist_deg": twists[0] if len(twists) == 1 else 0.0,
            "count": len(sheets), "grid": [rows, cols], "sheets": sheets,
            "extent": [round(v, 2) for v in (min(xs), min(ys), max(xs), max(ys))], "extent_source": "key_map",
            "subject": words, "tried": [], "warnings": warnings}


def _layout_name(spec, plan_type, n, scale, key=""):
    return str(spec.get("layout_name") or "{type_code}-{n:02d}").format(
        type_code=spec.get("type_code") or "S", n=n, plan_type=plan_type, scale=int(round(scale)), key=key)


def describe_plan(index, plan: dict) -> list:
    """One line per sheet, in the index's words."""
    lines = [f"{plan['count']} sheet(s) at {plan['scale_text']}, grid {plan['grid'][0]} row(s) x "
             f"{plan['grid'][1]} column(s)" + (f", twisted {plan['twist_deg']:g} deg" if plan["twist_deg"] else "")]
    for sh in plan["sheets"]:
        what = ", ".join(f"{n} {t.replace('_', ' ')}" for t, n in list(sh["tags"].items())[:6])
        x0, y0 = min(p[0] for p in sh["ring"]), min(p[1] for p in sh["ring"])
        x1, y1 = max(p[0] for p in sh["ring"]), max(p[1] for p in sh["ring"])
        head = f"  {sh['layout']}: "
        if sh.get("key") is not None:
            head += f"key {sh['key']}, {scale_text(sh.get('scale') or plan['scale'])}"
            if sh.get("twist_deg"):
                head += f", twist {sh['twist_deg']:g} deg"
            head += "; "
        else:
            head += f"row {sh['row'] + 1}, column {sh['col'] + 1}; "
        lines.append(head + f"x {x0:,.0f}-{x1:,.0f}, y {y0:,.0f}-{y1:,.0f}; holds {what or 'nothing named'}")
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
    s = float(sheet.get("scale") or plan["scale"])
    return {"n": sheet["n"], "count": plan["count"], "scale": int(round(s)),
            "scale_text": scale_text(s), "title": title or "", "plan_type": plan_type,
            "drawing": os.path.splitext(os.path.basename(str(drawing or "")))[0],
            "date": time.strftime("%Y-%m-%d"), "north_deg": round(sheet.get("north_deg", 0.0), 2),
            "layout": sheet["layout"], "type_code": spec.get("type_code") or "",
            **(sheet.get("values") or {})}


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
        if entry.get("xref"):
            # the border / title block arriving as an xref on the sheet
            out.append({"intent": "attach_xref", "layout": sheet["layout"], "name": _fill(entry["xref"], values),
                        "path": entry.get("source_file"), "position": at, "rotation_deg": rot,
                        "scale": entry.get("scale", 1.0), "layer": entry.get("layer", "0"),
                        "overlay": bool(entry.get("overlay", False))})
        elif entry.get("block"):
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
        # a key-map plan carries the scale and twist per sheet; a tiled plan has one for all
        steps.append({"intent": "create_viewport", "layout": sheet["layout"], "center": list(vp["center"]),
                      "width": vp["width"], "height": vp["height"], "view_center": list(sheet["center"]),
                      "scale": paper_scale(sheet.get("scale") or plan["scale"]),
                      "twist_deg": float(sheet.get("twist_deg", plan["twist_deg"])),
                      "freeze_layers": frozen, "locked": True, "layer": vp.get("layer", "vport")})
        attrs = (spec.get("title_block") or {}).get("attributes") or {}
        # only when the template layout carries an attributed title block (named in
        # template.title_block): the border may arrive as an xref instead, and
        # then "@0" (the first object the clone created) points at nothing and the
        # whole sheet would be rolled back
        if tpl.get("file") and tpl.get("title_block") and attrs:
            values = _values(sheet, plan, spec, plan_type, drawing, title)
            steps.append({"intent": "set_block_attributes", "handle": "@0",
                          "attributes": {k: _fill(v, values) for k, v in attrs.items()}})
        steps.extend(furniture_for(sheet, plan, spec, index, plan_type, drawing, title))
        steps.extend(tables_for(sheet, plan, spec, index, plan_type, drawing, title))
        # "_sheet" travels with the payload for the dashboard and the verifier; the
        # runner strips underscored keys before posting, so the plugin never sees it.
        marker = {k: sheet[k] for k in ("n", "layout", "center", "ring", "scale", "tags")}
        if sheet.get("key") is not None:
            marker["key"] = sheet["key"]
        out.append({"intent": "sequence", "steps": steps, "_sheet": marker})
    return out


def plan_and_payloads(index, data, spec, plan_type, scale=None, max_sheets=None, twist_deg=None,
                      drawing="", title="") -> tuple:
    plan = plan_windows(index, spec, plan_type, scale=scale, max_sheets=max_sheets, twist_deg=twist_deg)
    return plan, sheet_payloads(plan, spec, index, data, plan_type, drawing, title)


# ── Profile sheets: one viewport per profile view ────────────────────────────


def profile_views_in(data: dict, alignments=None, names=None) -> list:
    """The drawing's profile views with extents, in alignment order (as the
    drawing lists its alignments) then by start station."""
    if isinstance(alignments, str):
        alignments = [a.strip() for a in alignments.split(",") if a.strip()]
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",") if n.strip()]
    want_a = {str(a).lower() for a in alignments or []}
    want_n = {str(n).lower() for n in names or []}
    order = {str(a.get("name") or "").lower(): i for i, a in enumerate((data or {}).get("alignments") or [])}
    out = []
    for pv in (data or {}).get("profile_views") or []:
        ring = pv.get("extents")
        if not (isinstance(ring, list) and len(ring) >= 2):
            continue
        if want_a and str(pv.get("alignment") or "").lower() not in want_a:
            continue
        if want_n and str(pv.get("name") or "").lower() not in want_n:
            continue
        out.append(pv)
    # alphabetical by alignment (SS-A before SS-B), then by station
    out.sort(key=lambda pv: (profiles.natural_key(pv.get("alignment")), float(pv.get("station_start") or 0.0),
                             profiles.natural_key(pv.get("name"))))
    return out


def _on_a_sheet(pv: dict, data: dict) -> str:
    """The layout whose viewport already shows this view (its insertion inside
    the viewport's model window), else ''."""
    at, _ = _view_origin(pv)
    for lay in (data or {}).get("layouts") or []:
        for vp in lay.get("viewports") or []:
            win = vp.get("model_window")
            try:
                (x0, y0), (x1, y1) = win
            except (TypeError, ValueError):
                continue
            if x0 - 1e-6 <= at[0] <= x1 + 1e-6 and y0 - 1e-6 <= at[1] <= y1 + 1e-6:
                return str(lay.get("name") or "?")
    return ""


def _next_sheet_number(spec: dict, plan_type: str, scale: float, data: dict) -> int:
    """One past the highest sheet number the drawing already has for this
    layout_name pattern, so a second cut continues SS-03, SS-04 ..."""
    names = {str(lay.get("name") or "").lower() for lay in (data or {}).get("layouts") or []}
    n = 1
    while _layout_name(spec, plan_type, n, scale).lower() in names and n < MAX_SHEETS_HARD * 4:
        n += 1
    return n


def _ring_box(ring) -> tuple:
    xs = [float(p[0]) for p in ring]
    ys = [float(p[1]) for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _stations_text(views: list) -> str:
    by = {}
    for pv in views:
        by.setdefault(str(pv.get("alignment") or ""), []).append(pv)
    parts = []
    for name, group in by.items():
        s0 = min(float(pv.get("station_start") or 0.0) for pv in group)
        s1 = max(float(pv.get("station_end") or 0.0) for pv in group)
        span = f"STA {profiles.station_text(s0)} TO {profiles.station_text(s1)}"
        parts.append(span if len(by) == 1 else f"{name} {span}")
    return " / ".join(parts)


def _view_span(pv: dict) -> float:
    try:
        return float(pv.get("elevation_max")) - float(pv.get("elevation_min"))
    except (TypeError, ValueError):
        return 0.0


def _view_length(pv: dict) -> float:
    try:
        return float(pv.get("station_end")) - float(pv.get("station_start"))
    except (TypeError, ValueError):
        return 0.0


def _view_origin(pv: dict):
    """The view's insertion (datum corner): Civil 3D's Location, else the lower-left
    of its extents (bands make that a little low; a warning says so)."""
    loc = pv.get("location")
    if isinstance(loc, (list, tuple)) and len(loc) >= 2:
        return [float(loc[0]), float(loc[1])], False
    x0, y0, _x1, _y1 = _ring_box(pv["extents"])
    return [x0, y0], True


def arrange_profile_views(data: dict, spec: dict, plan_type: str = "", alignments=None, views=None) -> tuple:
    """(request, groups): the drawing's profile views assigned to sheets -- two
    per sheet in alphabetical order of alignment then station, a sheet never
    mixing spans -- and laid out in sheet windows (paper x scale) in a grid
    where the views already sit, each view's datum corner at its slot. The
    request is the plugin's `arrange_profile_views` (only the views that must
    move, with the windows as `proposed` for the gate); `groups` feed
    profile_sheet_payloads so the sheets are built from the planned windows.
    Views already shown by a sheet's viewport are left alone."""
    ps = profiles.profile_spec(spec)
    scale = float(ps["scale"])
    pvs = [pv for pv in profile_views_in(data, alignments, views) if not _on_a_sheet(pv, data)]
    if not pvs:
        raise SheetError("no profile views to arrange (none in the drawing, or all already on sheets)")
    groups = []
    for pv in pvs:
        span = int(round(_view_span(pv)))
        if span not in (36, 40):
            raise SheetError(f"{pv.get('name')} spans {_view_span(pv):g} ft; sheets are defined for 36 and 40 ft views")
        lay = profiles.sheet_layout(ps, span)
        if groups and groups[-1]["span"] == span and len(groups[-1]["views"]) < len(lay["slots"]):
            groups[-1]["views"].append(pv)
        else:
            groups.append({"span": span, "layout": lay, "views": [pv]})
    # the grid goes where the views are now: from the left-most and highest sheet window the
    # views imply (each view's datum corner less its slot offset), so views that already
    # stand in their windows are not moved again -- arranging twice changes nothing
    implied = []
    for g in groups:
        win_h = g["layout"]["paper"][1] * scale
        for i, pv in enumerate(g["views"]):
            dx, dy = profiles.slot_offset(g["layout"], i, _view_length(pv), scale)
            at, _ = _view_origin(pv)
            implied.append((at[0] - dx, at[1] - dy + win_h))
    base_x, base_y = min(o[0] for o in implied), max(o[1] for o in implied)
    per_row = max(1, int(ps["placement"].get("sheets_per_row", 4) or 4))
    col_gap, row_gap = float(ps["placement"]["col_gap"]), float(ps["placement"]["row_gap"])
    moves, boxes = [], []
    col_x, row_y = base_x, base_y
    row_h = 0.0
    k = 0
    for g in groups:
        paper_w, paper_h = g["layout"]["paper"]
        win_w, win_h = paper_w * scale, paper_h * scale
        if k % per_row == 0 and k > 0:
            col_x = base_x
            row_y -= row_h + row_gap
            row_h = 0.0
        origin = [round(col_x, 4), round(row_y - win_h, 4)]
        g["origin"] = origin
        boxes.append({"min": list(origin), "max": [round(origin[0] + win_w, 4), round(origin[1] + win_h, 4)]})
        for i, pv in enumerate(g["views"]):
            dx, dy = profiles.slot_offset(g["layout"], i, _view_length(pv), scale)
            want = [round(origin[0] + dx, 4), round(origin[1] + dy, 4)]
            at, _ = _view_origin(pv)
            if abs(at[0] - want[0]) > 0.01 or abs(at[1] - want[1]) > 0.01:
                moves.append({"name": pv.get("name"), "handle": pv.get("handle"), "location": want, "from": [round(at[0], 4), round(at[1], 4)]})
                try:
                    x0, y0, x1, y1 = _ring_box(pv["extents"])
                    boxes.append({"min": [x0, y0], "max": [x1, y1]})       # where it was: those cells change too
                except (KeyError, TypeError, ValueError):
                    pass
        col_x += win_w + col_gap
        row_h = max(row_h, win_h)
        k += 1
    request = {"intent": "arrange_profile_views", "views": moves, "proposed": {"boxes": boxes, "segments": []}}
    return request, groups


def profile_sheet_payloads(data: dict, index, spec: dict, plan_type: str = "", drawing: str = "", title: str = "",
                           alignments=None, views=None, groups=None) -> tuple:
    """(plan, payloads): the drawing's profile views on sheets, two to a sheet
    (the span's slots), in alphabetical order of alignment then station. Two
    views share a sheet when they physically share a sheet window -- the
    planner put the second at the lower slot of the first's window -- so a run
    that drew one alignment at a time still comes out right (one view per
    sheet where the window has only one). The span picks the template layout
    (36 ft -> 34x22, 40 ft -> 36x24). Each sheet is ONE viewport the size of the
    sheet border, looking at the window (lower-left = insertion - slot x
    scale). Views already shown by a layout's viewport are skipped, and sheet
    numbers continue after the layouts the drawing has. One `sequence` per
    sheet: template layout, viewport, title block, furniture."""
    ps = profiles.profile_spec(spec)
    pvs = profile_views_in(data, alignments, views)
    if not pvs:
        raise SheetError("the drawing has no profile views to put on sheets (draw_profile_views first)")
    scale = float(ps["scale"])
    pscale = paper_scale(scale)
    # whatever the spec says is visible, a profile sheet must show the profile views: their
    # grid, bands and text sit on the profile family's layers (C-PROF-*)
    vis = dict(spec.get("visible") or {})
    vis["tags"] = list(vis.get("tags") or []) + ["profile"]
    vis["layers"] = list(vis.get("layers") or []) + ["c-prof*", "*prof*"]
    frozen = freeze_list(index, data, {**spec, "visible": vis})["freeze"]
    tpl = spec.get("template") or {}
    vp_layer = (spec.get("viewport") or {}).get("layer", "vport")
    plan = {"plan_type": plan_type, "scale": scale, "scale_text": scale_text(scale), "paper_scale": pscale,
            "twist_deg": 0.0, "count": 0, "warnings": [], "sheets": [], "skipped": []}
    todo = []
    for pv in pvs:
        on = _on_a_sheet(pv, data)
        if on:
            plan["skipped"].append(f"{pv.get('name')} is already on {on}")
        else:
            todo.append(pv)
    if not todo:
        raise SheetError("every profile view is already on a sheet: " + "; ".join(plan["skipped"][:6]))

    if groups is not None:
        # arranged first: the windows are the planner's, not read back from the views
        groups = [dict(g) for g in groups]
    else:
        groups = []
    # else: group by the window the views physically share
    for pv in (todo if not groups else []):
        span = int(round(_view_span(pv)))
        if span not in (36, 40):
            raise SheetError(f"{pv.get('name')} spans {_view_span(pv):g} ft; sheets are defined for 36 and 40 ft views")
        lay = profiles.sheet_layout(ps, span)
        at, approx = _view_origin(pv)
        if approx:
            plan["warnings"].append(f"{pv.get('name')}: no Location reported; its window was taken from its extents")
        joined = False
        if groups:
            g = groups[-1]
            k = len(g["views"])
            if g["span"] == span and k < len(lay["slots"]):
                dx, dy = profiles.slot_offset(lay, k, _view_length(pv), scale)
                want = [g["origin"][0] + dx, g["origin"][1] + dy]
                if abs(at[0] - want[0]) <= 1.0 and abs(at[1] - want[1]) <= 1.0:
                    g["views"].append(pv)
                    joined = True
        if not joined:
            dx0, dy0 = profiles.slot_offset(lay, 0, _view_length(pv), scale)
            groups.append({"span": span, "layout": lay, "origin": [at[0] - dx0, at[1] - dy0], "views": [pv]})
    for g in groups:
        if len(g["views"]) < len(g["layout"]["slots"]):
            plan["warnings"].append(f"{g['views'][0].get('name')} is alone on its sheet")

    n0 = _next_sheet_number(spec, plan_type, scale, data)
    plan["count"] = len(groups)
    plan["first_number"] = n0
    payloads = []
    for i, g in enumerate(groups):
        n = n0 + i
        lay = g["layout"]
        paper_w, paper_h = lay["paper"]
        layout_name = _layout_name(spec, plan_type, n, scale)
        source_layout = lay["layout"] or tpl.get("layout") or ""
        steps = []
        if tpl.get("file") and source_layout:
            steps.append({"intent": "manage_layouts", "action": "clone_from_template", "name": layout_name,
                          "source_file": tpl["file"], "source_layout": source_layout})
        else:
            if tpl.get("file") and not source_layout:
                plan["warnings"].append(f"{layout_name}: no template layout for the {g['span']} ft span; a plain layout was made")
            steps.append({"intent": "manage_layouts", "action": "create", "name": layout_name})
        border = lay.get("border") or {}
        if border.get("source_file"):
            # the span's border (x-border 36x24 / 34x22) on the sheet before the viewport, so
            # the viewport is fitted to it
            steps.append({"intent": "attach_xref", "layout": layout_name,
                          "name": border.get("xref") or os.path.splitext(os.path.basename(border["source_file"]))[0],
                          "path": border["source_file"], "position": [float(border["at"][0]), float(border["at"][1])],
                          "rotation_deg": 0.0, "scale": float(border.get("scale") or 1.0), "layer": border.get("layer") or "0",
                          "overlay": False})
            if not os.path.exists(str(border["source_file"])):
                plan["warnings"].append(f"{layout_name}: border file not found: {border['source_file']}")
        win_x0, win_y0 = g["origin"]
        centre = [round(win_x0 + paper_w * scale / 2.0, 4), round(win_y0 + paper_h * scale / 2.0, 4)]
        # the viewport is the paper, always: centred on it, the paper's size, looking at
        # the sheet window (paper x scale), so a view's slot on the paper is where it prints
        steps.append({"intent": "create_viewport", "layout": layout_name, "fit": "paper",
                      "center": [round(paper_w / 2.0, 4), round(paper_h / 2.0, 4)], "width": paper_w, "height": paper_h,
                      "view_center": centre, "scale": pscale, "twist_deg": 0.0, "freeze_layers": frozen,
                      "locked": True, "layer": vp_layer})
        ring = spatial.viewport_ring(centre, pscale, paper_w, paper_h, 0.0)
        names = [str(pv.get("name") or "") for pv in g["views"]]
        align_names = []
        for pv in g["views"]:
            a = str(pv.get("alignment") or "")
            if a and a not in align_names:
                align_names.append(a)
        handles = [pv.get("handle") for pv in g["views"] if pv.get("handle")]
        sheet = {"n": n, "layout": layout_name, "center": centre, "ring": ring, "scale": scale, "row": i, "col": 0,
                 "tags": {"profile_view": len(g["views"])}, "handles": handles, "subject_handles": handles, "north_deg": 0.0,
                 "views": names, "source_layout": source_layout, "paper": [paper_w, paper_h], "span": g["span"],
                 "values": {"alignment": " / ".join(align_names), "stations": _stations_text(g["views"]), "views": ", ".join(names)}}
        attrs = (spec.get("title_block") or {}).get("attributes") or {}
        # "@0" is the title block the clone created; a template layout that carries none
        # (page setup only, the border arriving as an xref) has nothing to fill
        if tpl.get("file") and tpl.get("title_block") and attrs:
            values = _values(sheet, plan, spec, plan_type, drawing, title)
            steps.append({"intent": "set_block_attributes", "handle": "@0",
                          "attributes": {k: _fill(v, values) for k, v in attrs.items()}})
        steps.extend(furniture_for(sheet, plan, spec, index, plan_type, drawing, title))
        plan["sheets"].append(sheet)
        payloads.append({"intent": "sequence", "steps": steps,
                         "_sheet": {**{k: sheet[k] for k in ("n", "layout", "center", "ring", "scale", "tags")}, "views": names}})
    return plan, payloads


# ── Reading what is there ────────────────────────────────────────────────────


def list_layouts(data: dict, index=None, verify: bool = False) -> list:
    """The sheets a drawing already has, in words: viewports with their model
    windows, what each window holds, and the furniture on the paper. With
    verify, each viewport also carries the index's sentence at the window's
    centre and corners and the raw view numbers, for checking the window
    transform against what the layout tab shows."""
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
            entry = {"handle": vp.get("handle"), "on": vp.get("on", True), "scale": s,
                     "scale_text": scale_text(1.0 / s) if s else None, "twist_deg": vp.get("twist_deg", 0),
                     "model_center": vp.get("model_center"), "model_window": vp.get("model_window"),
                     "paper_center": vp.get("paper_center"), "paper_size": vp.get("paper_size"),
                     "frozen_layers": len(vp.get("frozen_layers") or []) if isinstance(vp.get("frozen_layers"), list)
                     else vp.get("frozen_layers"), "holds": holds}
            if verify and index is not None and ring:
                entry["verify"] = window_words(index, vp, ring)
            vps.append(entry)
        out.append({"name": lay.get("name"), "tab_order": lay.get("tab_order"), "paper_size": lay.get("paper_size"),
                    "viewports": vps,
                    "blocks": [{"name": b.get("name"), "position": b.get("position"),
                                "attributes": b.get("attributes") or {}} for b in lay.get("blocks") or []][:40],
                    "tables": [{"title": t.get("title"), "rows": t.get("rows")} for t in lay.get("tables") or []],
                    "texts": len(lay.get("texts") or [])})
    return out


def window_words(index, vp: dict, ring) -> dict:
    """What the index says sits under a window's centre and its SW / NE corners,
    plus the raw view numbers the plugin reported. A person compares the
    sentences with the layout tab; a mismatch on a twisted sheet points at the
    DCS transform, a mismatch everywhere at the view centre."""
    c = vp.get("model_center") or [sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)]
    sw = min(ring, key=lambda p: (p[1], p[0]))
    ne = max(ring, key=lambda p: (p[1], p[0]))

    def words(p):
        key = spatial.key_at(float(p[0]), float(p[1]))
        return index.describe(key) if key in index.cells else "an empty cell"

    return {"at_center": words(c), "at_sw": words(sw), "at_ne": words(ne),
            "raw": {k: vp.get(k) for k in ("view_center_dcs", "view_target", "view_direction", "view_height",
                                            "twist_deg", "scale") if vp.get(k) is not None}}


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
