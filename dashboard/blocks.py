"""Function blocks the dashboards can chain into a sequence.

BLOCK_SPECS is the single source of truth for every non-prompt command a
dashboard can run. Three things are derived from it, so none of them can drift
apart:

  * the block palette and its parameter forms in the browser (served by
    GET /blocks, which is this module minus the builder functions),
  * validation and payload construction for a block sequence (compile_blocks),
  * the positional text syntax (parse_script), where a line's arguments map onto
    a spec's fields in order.

A field carries a "default" when the plugin has a sensible fallback, and
"optional": True with no default when leaving it blank must omit it from the
payload entirely -- edit_layer's color, for instance, should not be forced to a
value just because the user never touched it.
"""

import inspect
import json
import math
import re
import sys
import types
import typing
from pathlib import Path

# drafting_tools.py lives in the repo root; server.py adds that to sys.path only
# after importing this module, so do it here as well.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import drafting_tools

GRADING_LABEL_KINDS = ("pad_fp", "lot_row", "lot_setback", "swale_meet", "swale_high",
                       "lot_corner", "curb_transition", "drive_curb", "drive_slope",
                       "swale_slope")

# -- Field coercion -----------------------------------------------------------

_TRUE = {"true", "t", "yes", "y", "1", "on"}
_FALSE = {"false", "f", "no", "n", "0", "off"}

_MISSING = object()


def _blank(raw) -> bool:
    return raw is None or (isinstance(raw, str) and not raw.strip())


def _number(raw, label: str) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number, got '{raw}'")


def _points(raw, label: str) -> list:
    """Accepts [[x, y], ...] from a block, or 'x,y;x,y' from a typed line."""
    if isinstance(raw, str):
        chunks = [c for c in raw.replace("|", ";").split(";") if c.strip()]
        raw = [c.split(",") for c in chunks]
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list of x,y points")

    points = []
    for index, point in enumerate(raw, start=1):
        if isinstance(point, str):
            point = point.split(",")
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise ValueError(f"{label} point {index} needs both an x and a y")
        points.append([_number(point[0], f"{label} point {index} x"),
                       _number(point[1], f"{label} point {index} y")])
    return points


def _handles(raw, label: str) -> list:
    """Accepts a list of handles, or a comma/space separated string of them."""
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list of object handles")

    handles = []
    for handle in raw:
        handle = str(handle).strip()
        if not handle:
            continue
        try:
            int(handle, 16)  # the plugin looks handles up as hex
        except ValueError:
            raise ValueError(f"{label}: '{handle}' is not a hex object handle")
        handles.append(handle)
    return handles


def _names(raw, label: str) -> list:
    """Accepts a list of names from a picker, or a comma separated string."""
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{label} must be a list of names")
    return [str(n).strip() for n in raw if str(n).strip()]


def coerce_field(field: dict, raw):
    """Turns one raw form value into the typed value a builder expects.

    Returns _MISSING for an optional field left blank, which build_block drops
    from the payload rather than sending as null.
    """
    label = field["label"]

    if _blank(raw) and not isinstance(raw, (list, bool)):
        if "default" in field:
            return field["default"]
        if field.get("optional"):
            return _MISSING
        raise ValueError(f"{label} is required")

    kind = field["type"]

    if kind == "number":
        return _number(raw, label)

    if kind in ("int", "color"):
        value = int(round(_number(raw, label)))
        low, high = field.get("min"), field.get("max")
        if low is not None and value < low:
            raise ValueError(f"{label} must be at least {low}, got {value}")
        if high is not None and value > high:
            raise ValueError(f"{label} must be at most {high}, got {value}")
        return value

    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError(f"{label} must be true or false, got '{raw}'")

    if kind == "points":
        points = _points(raw, label)
        minimum = field.get("min_items", 1)
        if len(points) < minimum:
            raise ValueError(f"{label} needs at least {minimum} points, got {len(points)}")
        return points

    if kind == "multichoice":
        # Options come from the assigned drawing rather than the spec, so the
        # browser fills them in from "options_from" -- see /dashboards/{id}/
        # alignments. An empty selection is meaningful (it means "all"), so it
        # is passed through rather than treated as a missing value.
        names = _names(raw, label)
        if not names and field.get("min_items"):
            raise ValueError(f"{label} needs at least one selection")
        return names

    if kind == "handles":
        handles = _handles(raw, label)
        if not handles:
            raise ValueError(f"{label} needs at least one object handle")
        return handles

    if kind == "point":
        # One x,y pair: "10,20" typed, or [10, 20] from a form.
        points = _points(raw, label)
        if len(points) != 1:
            raise ValueError(f"{label} must be a single x,y point")
        return points[0]

    if kind == "strings":
        # Names that are not handles (layers, types, style names): comma separated.
        return _names(raw, label)

    if kind == "json":
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(str(raw))
        except ValueError as e:
            raise ValueError(f"{label} must be valid JSON ({e})")

    if kind == "choice":
        text = str(raw).strip()
        options = field.get("options", [])
        match = next((o for o in options if o.lower() == text.lower()), None)
        if match is None:
            raise ValueError(f"{label} must be one of {', '.join(options)}, got '{text}'")
        return match

    text = str(raw).strip()
    if not text and not field.get("optional"):
        raise ValueError(f"{label} is required")
    return text


# -- Builders (validated values -> plugin payload) -----------------------------


def _build_draw_line(v):
    return {
        "intent": "draw_line",
        "start_point": [v["x1"], v["y1"], 0],
        "end_point": [v["x2"], v["y2"], 0],
        "layer": v["layer"],
    }


def _build_draw_arc(v):
    return {
        "intent": "draw_arc",
        "center": [v["cx"], v["cy"], 0],
        "radius": v["radius"],
        # The plugin's Arc takes radians; degrees are what a drafter reads off a plan.
        "start_angle": math.radians(v["start_deg"]),
        "end_angle": math.radians(v["end_deg"]),
        "layer": v["layer"],
    }


def _build_draw_shape(v):
    return {
        "intent": "draw_shape",
        "vertices": v["vertices"],
        "closed": v["closed"],
        "layer": v["layer"],
    }


def _build_create_layer(v):
    return {
        "intent": "create_layer",
        "name": v["name"],
        "color": v["color"],
        "linetype": v["linetype"],
    }


def _build_edit_layer(v):
    payload = {"intent": "edit_layer", "name": v["name"]}
    if "color" in v:
        payload["color"] = v["color"]
    if "visible" in v:
        payload["visible"] = v["visible"]
    return payload


def _build_add_to_layer(v):
    return {"intent": "add_to_layer", "layer": v["layer"], "handles": v["handles"]}


def _build_remove_from_layer(v):
    return {"intent": "remove_from_layer", "handles": v["handles"]}


def _build_delete(v):
    return {
        "intent": "delete",
        "objects": [{"object_handle": h, "object_type": "object"} for h in v["handles"]],
    }


# -- The blocks ----------------------------------------------------------------

BLOCK_SPECS = [
    {
        "type": "draw_line",
        "label": "Draw Line",
        "group": "Geometry",
        "description": "One straight segment between two points.",
        "build": _build_draw_line,
        "fields": [
            {"name": "x1", "label": "X1", "type": "number", "default": 0},
            {"name": "y1", "label": "Y1", "type": "number", "default": 0},
            {"name": "x2", "label": "X2", "type": "number", "default": 0},
            {"name": "y2", "label": "Y2", "type": "number", "default": 0},
            {"name": "layer", "label": "Layer", "type": "text", "default": "0"},
        ],
    },
    {
        "type": "draw_arc",
        "label": "Draw Arc",
        "group": "Geometry",
        "description": "Arc by center, radius and a sweep in degrees.",
        "build": _build_draw_arc,
        "fields": [
            {"name": "cx", "label": "Center X", "type": "number", "default": 0},
            {"name": "cy", "label": "Center Y", "type": "number", "default": 0},
            {"name": "radius", "label": "Radius", "type": "number", "default": 1},
            {"name": "start_deg", "label": "Start deg", "type": "number", "default": 0},
            {"name": "end_deg", "label": "End deg", "type": "number", "default": 90},
            {"name": "layer", "label": "Layer", "type": "text", "default": "0"},
        ],
    },
    {
        "type": "draw_shape",
        "label": "Draw Shape",
        "group": "Geometry",
        "description": "Polyline through a list of vertices.",
        "build": _build_draw_shape,
        "fields": [
            {"name": "vertices", "label": "Vertices", "type": "points",
             "min_items": 2, "help": "At least two x,y points."},
            {"name": "closed", "label": "Closed", "type": "bool", "default": True},
            {"name": "layer", "label": "Layer", "type": "text", "default": "0"},
        ],
    },
    {
        "type": "create_layer",
        "label": "Create Layer",
        "group": "Layers",
        "description": "Adds a layer. Fails if the name is already taken.",
        "build": _build_create_layer,
        "fields": [
            {"name": "name", "label": "Name", "type": "text"},
            {"name": "color", "label": "Color (ACI)", "type": "color",
             "default": 7, "min": 0, "max": 256},
            {"name": "linetype", "label": "Linetype", "type": "choice",
             "default": "Continuous",
             "options": ["Continuous", "HIDDEN", "CENTER", "DASHED", "PHANTOM"],
             "help": "Ignored if that linetype isn't loaded in the drawing."},
        ],
    },
    {
        "type": "edit_layer",
        "label": "Edit Layer",
        "group": "Layers",
        "description": "Changes an existing layer. Blank fields are left alone.",
        "build": _build_edit_layer,
        "fields": [
            {"name": "name", "label": "Name", "type": "text"},
            {"name": "color", "label": "Color (ACI)", "type": "color",
             "optional": True, "min": 0, "max": 256},
            {"name": "visible", "label": "Visible", "type": "bool", "optional": True},
        ],
    },
    {
        "type": "add_to_layer",
        "label": "Move To Layer",
        "group": "Layers",
        "description": "Moves objects onto a layer, creating it if needed.",
        "build": _build_add_to_layer,
        "fields": [
            {"name": "layer", "label": "Layer", "type": "text"},
            {"name": "handles", "label": "Handles", "type": "handles",
             "help": "Hex object handles, comma separated."},
        ],
    },
    {
        "type": "remove_from_layer",
        "label": "Move To Layer 0",
        "group": "Layers",
        "description": "Sends objects back to layer 0.",
        "build": _build_remove_from_layer,
        "fields": [
            {"name": "handles", "label": "Handles", "type": "handles",
             "help": "Hex object handles, comma separated."},
        ],
    },
    {
        "type": "delete",
        "label": "Delete Objects",
        "group": "Edit",
        "description": "Erases objects by handle.",
        "build": _build_delete,
        "fields": [
            {"name": "handles", "label": "Handles", "type": "handles",
             "help": "Hex object handles, comma separated."},
        ],
    },
    {
        "type": "draw_driveways",
        "label": "Draw Driveways",
        "group": "Generated",
        "description": ("A driveway on every lot pad: perpendicular to the back "
                        "of road from the upstream pad corner, with tangent "
                        "fillets. Reads the drawing when it runs."),
        # No "build": unlike every other block this cannot become a payload up
        # front. It has to read the live drawing first, so the runner expands it
        # at run time -- see EXPANDING_BLOCKS and _expand_block in server.py.
        "expand": "driveways",
        "fields": [
            {"name": "alignment", "label": "Alignments", "type": "multichoice",
             "options_from": "alignments", "default": [],
             "help": "Pick every street to draw driveways along. Each pad is "
                     "worked against the one street it fronts, so a lot in "
                     "reach of two still gets a single driveway. Select none "
                     "to use every alignment in the drawing."},
            {"name": "layer", "label": "Layer", "type": "text", "default": "0"},
            {"name": "width", "label": "Driveway width", "type": "number", "default": 20},
            {"name": "radius", "label": "Fillet radius", "type": "number", "default": 5},
            {"name": "max_offset", "label": "Serve pads within", "type": "number",
             "default": 150,
             "help": "Feet from the alignment. Pads further off front a "
                     "different street and are left alone."},
        ],
    },
    {
        "type": "draw_rear_swales",
        "label": "Draw Rear Swales",
        "group": "Generated",
        "description": ("A rear yard swale behind every pad: the back of pad "
                        "offset into the rear yard, split 10ft from its upstream "
                        "end, with 45-degree legs run out to the lot lines."),
        "expand": "swales",   # reads the live drawing; see _expand in server.py
        "fields": [
            {"name": "alignment", "label": "Alignments", "type": "multichoice",
             "options_from": "alignments", "default": [],
             "help": "Pick every street to run swales behind. Each pad is "
                     "worked against the one street it fronts. Select none "
                     "to use every alignment in the drawing."},
            {"name": "layer", "label": "Layer", "type": "text", "default": "0"},
            {"name": "offset", "label": "Rear offset", "type": "number", "default": 5},
            {"name": "first_leg", "label": "First part length", "type": "number", "default": 10},
            {"name": "lot_line_layer", "label": "Lot line layer", "type": "text",
             "default": "c-lots-line",
             "help": "Layer the 45-degree legs stop against. Matched by suffix, so "
                     "an xrefed 'x-plat (ph1a)|c-lots-line' is found too."},
            {"name": "max_offset", "label": "Serve pads within", "type": "number",
             "default": 150,
             "help": "Feet from the alignment. Pads further off front a "
                     "different street and are left alone."},
        ],
    },
    {
        "type": "cut_sheets",
        "label": "Cut Sheets",
        "group": "Generated",
        "description": ("Paper-space sheets over the designer's work: tiles the work's "
                        "extent with viewports at the plan type's scale, clones the "
                        "template layout for each, fills the title block, places the "
                        "furniture (north arrow, scale bar, notes, warnings) and writes "
                        "the tables. One all-or-nothing sequence per sheet. Reads the "
                        "drawing and playbooks/<type>/sheets.json when it runs."),
        "expand": "sheets",   # reads the live drawing; see _expand in server.py
        "fields": [
            {"name": "plan_type", "label": "Plan type", "type": "text",
             "help": "A type under playbooks/ with a sheets.json (see list_playbooks)."},
            {"name": "scale", "label": "Scale (ft per inch)", "type": "number", "optional": True,
             "help": "40 for 1\" = 40'. Blank: the first scale in the spec that fits max_sheets."},
            {"name": "max_sheets", "label": "Max sheets", "type": "number", "optional": True},
            {"name": "twist_deg", "label": "Twist (deg)", "type": "number", "default": 0,
             "help": "Rotates every viewport; 0 = north up."},
            {"name": "title", "label": "Sheet title", "type": "text", "default": "",
             "help": "Fills {title} in the title block and furniture."},
        ],
    },
    {
        "type": "lay_pressure_run",
        "label": "Lay Pressure Run",
        "group": "Generated",
        "description": ("Real Civil 3D pressure network parts (water): pipes along the "
                        "alignment offset to one side between two stations, bend fittings "
                        "at the vertices, hydrants every so many feet. Uses the drawing's "
                        "pressure parts list; one all-or-nothing transaction. Reads the "
                        "drawing when it runs."),
        "expand": "pressure_run",   # reads the live drawing; see _expand in server.py
        "fields": [
            {"name": "alignment", "label": "Alignment", "type": "text", "default": "",
             "help": "Alignment name (blank when the drawing has one)."},
            {"name": "offset", "label": "Offset (ft)", "type": "number", "default": 10},
            {"name": "side", "label": "Side", "type": "choice", "default": "right", "options": ["right", "left"],
             "help": "Looking up-station."},
            {"name": "from_station", "label": "From station", "type": "number", "optional": True},
            {"name": "to_station", "label": "To station", "type": "number", "optional": True},
            {"name": "pipe_size", "label": "Pipe size", "type": "text", "default": "",
             "help": "A description substring or nominal diameter from list_pressure_parts (blank = the only pipe)."},
            {"name": "hydrant_every", "label": "Hydrant spacing (ft)", "type": "number", "default": 0,
             "help": "0 = no hydrants."},
            {"name": "hydrant_part", "label": "Hydrant part", "type": "text", "default": "Hydrant"},
            {"name": "network", "label": "Network", "type": "text", "default": "",
             "help": "Existing pressure network name, or a new one to create."},
            {"name": "parts_list", "label": "Parts list", "type": "text", "default": ""},
            {"name": "layer", "label": "Layer", "type": "text", "default": ""},
        ],
    },
    {
        "type": "draw_profile_views",
        "label": "Draw Profile Views",
        "group": "Generated",
        "description": ("Sanitary sewer profile views, one per 1000 ft of each alignment from "
                        "1+00 (split to 500 ft where 36/40 ft of elevation cannot hold it): "
                        "restations to 1+00, samples the EG surface, adds the network's pipes "
                        "and manholes, labels pipes, manholes and every 1+00 station, and "
                        "places the views east of the drawing. One all-or-nothing transaction. "
                        "Reads the drawing and playbooks/<type>/sheets.json when it runs."),
        "expand": "profile_views",   # reads the live drawing; see _expand in server.py
        "fields": [
            {"name": "plan_type", "label": "Plan type", "type": "text", "default": "sanitary",
             "help": "A type under playbooks/ whose sheets.json has a profile section (see get_sheet_spec)."},
            {"name": "alignment", "label": "Alignments", "type": "multichoice",
             "options_from": "alignments", "default": [],
             "help": "Select none to draw views for every alignment in the drawing."},
            {"name": "span", "label": "Elevation span (ft)", "type": "choice", "default": "40", "options": ["40", "36"]},
            {"name": "station_host", "label": "Station labels on", "type": "choice", "default": "flowline",
             "options": ["flowline", "eg"],
             "help": "flowline: a profile through the pipe inverts carries the 1+00 labels; eg: the surface profile does."},
            {"name": "surface", "label": "EG surface", "type": "text", "optional": True,
             "help": "Blank: the surface named like EG, or the only one."},
            {"name": "structure_label", "label": "MH label style", "type": "text", "optional": True,
             "help": "Structure profile label style for every manhole (blank = drawing default)."},
        ],
    },
    {
        "type": "create_sanitary_profile",
        "label": "Create Sanitary Profile",
        "group": "Generated",
        "description": ("The whole sanitary profile production in a set order: 1. draw JUST the "
                        "profile views (no labels, no blocks), alphabetical by alignment, an "
                        "alignment that fails is skipped and the rest drawn; 2. arrange them two "
                        "to a sheet, centred, and cut the sheets from the template with the "
                        "border and one full-sheet viewport; 3. LAST, the labels and the "
                        "dimension line: pipe and 1+00 station labels anchored to the bottom of "
                        "each view, the MH and MH -> Alignment labels where their styles put "
                        "them. Alignments whose views already exist are left as they are."),
        "expand": "sanitary_profile",   # see _expand_sanitary_profile in server.py
        "fields": [
            {"name": "plan_type", "label": "Plan type", "type": "text", "default": "sanitary",
             "help": "A type under playbooks/ whose sheets.json has a profile section."},
            {"name": "alignment", "label": "Alignments", "type": "multichoice",
             "options_from": "alignments", "default": [],
             "help": "Select none for every alignment in the drawing."},
            {"name": "span", "label": "Elevation span (ft)", "type": "choice", "default": "40", "options": ["40", "36"]},
            {"name": "station_host", "label": "Station labels on", "type": "choice", "default": "flowline",
             "options": ["flowline", "eg"],
             "help": "flowline: a profile through the pipe inverts carries the 1+00 labels; eg: the surface profile does."},
            {"name": "surface", "label": "EG surface", "type": "text", "optional": True,
             "help": "Blank: the surface named like EG, or the only one."},
            {"name": "structure_label", "label": "MH label style", "type": "text", "optional": True,
             "help": "Blank: the spec's (else the drawing's default structure profile label style)."},
            {"name": "title", "label": "Sheet title", "type": "text", "default": "",
             "help": "Fills {title} in the title block and furniture."},
        ],
    },
    {
        "type": "cut_profile_sheets",
        "label": "Cut Profile Sheets",
        "group": "Generated",
        "description": ("Sheets for the drawing's profile views: two stacked viewports per "
                        "sheet at the profile scale (1\" = 40'), the template layout cloned, "
                        "the title block filled ({alignment} {stations}), furniture placed. "
                        "One all-or-nothing sequence per sheet. Expanded at its turn in a run, "
                        "so it can follow Draw Profile Views in the same run."),
        "expand": "profile_sheets",   # deferred: see DEFERRED_EXPANDERS in server.py
        "fields": [
            {"name": "plan_type", "label": "Plan type", "type": "text", "default": "sanitary",
             "help": "A type under playbooks/ with a sheets.json (template, slots, scale)."},
            {"name": "alignment", "label": "Alignments", "type": "multichoice",
             "options_from": "alignments", "default": [],
             "help": "Select none for the views of every alignment."},
            {"name": "title", "label": "Sheet title", "type": "text", "default": "",
             "help": "Fills {title} in the title block and furniture."},
        ],
    },
]



def _build_label_grading(v):
    payload = {"intent": "label_grading"}
    for key in ("apply", "surface", "layer", "fp_style", "elevation_style", "tc_style",
                "slope_style", "marker_style", "existing_tolerance", "pad_reach",
                "drive_curb_side"):
        if key in v and not _blank(v[key]):
            payload[key] = v[key]
    if v.get("kinds"):
        payload["kinds"] = v["kinds"]
    return payload


BLOCK_SPECS.append({
    "type": "label_grading",
    "label": "Grading Labels",
    "group": "Generated",
    "description": ("Surface elevation and slope labels on the FG surface: pad FP, "
                    "lot corners, right-of-way and setback crossings, swale high "
                    "points and meeting points, curb arc/straight transitions, "
                    "driveway-at-curb, and driveway / swale slope labels. Reads pads, "
                    "lot lines, curb, driveways and swales from model space and xrefs."),
    "build": _build_label_grading,
    "fields": [
        {"name": "apply", "label": "Place labels", "type": "bool", "default": True,
         "help": "Off = report what would be labelled without writing anything."},
        {"name": "surface", "label": "Surface", "type": "text", "optional": True,
         "help": "FG surface name. Blank = the one surface whose name contains 'FG'."},
        {"name": "kinds", "label": "Label kinds", "type": "strings", "optional": True,
         "help": "Comma separated subset of: " + ", ".join(GRADING_LABEL_KINDS) +
                 ". Blank = all of them."},
        {"name": "layer", "label": "Layer", "type": "text", "default": "0"},
        {"name": "fp_style", "label": "FP style", "type": "text", "optional": True,
         "help": "Spot elevation style for pad labels. Blank = the drawing's default "
                 "surface spot elevation style. A name that does not exist falls back "
                 "to the default with a warning."},
        {"name": "elevation_style", "label": "Elevation style", "type": "text", "optional": True,
         "help": "Spot elevation style for lot, swale and driveway labels. Blank = default."},
        {"name": "tc_style", "label": "Top of curb style", "type": "text", "optional": True,
         "help": "Spot elevation style for curb transitions. Blank = default."},
        {"name": "slope_style", "label": "Slope style", "type": "text", "optional": True,
         "help": "Slope label style for driveway and swale slopes. Blank = default."},
        {"name": "marker_style", "label": "Marker style", "type": "text", "optional": True,
         "help": "Blank = the drawing's default marker."},
        {"name": "existing_tolerance", "label": "Skip existing within", "type": "number",
         "default": 1.0,
         "help": "Feet. A surface label of the same type this close to a target "
                 "means it is already labelled."},
        {"name": "pad_reach", "label": "Pad reach", "type": "number", "default": 60,
         "help": "Feet. Swale and lot-corner linework further than this from every "
                 "pad belongs to no lot and is ignored."},
        {"name": "drive_curb_side", "label": "Driveway curb label side", "type": "choice",
         "options": ["right", "left", "both"], "default": "right",
         "help": "Which flare of the driveway gets the TC label where its arc lands on the "
                 "back of curb, looking from the pad toward the road."},
    ],
})


def _build_space_labels(v):
    payload = {"intent": "space_labels"}
    for key in ("apply", "layer", "types", "mode", "include_other", "curb_layer", "curb_side",
                "centreline_layer", "include_dragged", "include_pinned", "clearance", "label_gap",
                "cell_size", "max_passes"):
        if key in v and not _blank(v[key]):
            payload[key] = v[key]
    for key in ("obstacle_layers", "ignore_layers", "pad_layers"):
        if v.get(key):
            payload[key] = v[key]
    if v.get("leader_lengths"):
        payload["leader_lengths"] = [float(x) for x in v["leader_lengths"]]
    return payload


BLOCK_SPECS.append({
    "type": "space_labels",
    "label": "Space Labels",
    "group": "Generated",
    "description": ("Finished-grade elevation labels are dragged clear of linework, each "
                    "other and each other's leaders (candidates in 8 directions scored on an "
                    "occupancy grid of the drawing's linework, most crowded placed first, then "
                    "repaired); top-of-curb labels are rotated to follow the back of curb and "
                    "flipped in place so the text sits on the outside of the curb, never "
                    "dragged. Pad and slope labels are left alone unless asked."),
    "build": _build_space_labels,
    "fields": [
        {"name": "apply", "label": "Move labels", "type": "bool", "default": True,
         "help": "Off = report where every label would go without writing anything."},
        {"name": "layer", "label": "Label layer", "type": "text", "optional": True,
         "help": "Only labels on a layer ending in this. Blank = any layer."},
        {"name": "types", "label": "Label types", "type": "choice",
         "options": ["surface", "elevation", "slope", "all"], "default": "surface"},
        {"name": "mode", "label": "Treatment", "type": "choice",
         "options": ["auto", "position", "align"], "default": "auto",
         "help": "auto = by label kind (curb labels align, finished-grade labels position); "
                 "position or align forces one treatment on every selected label."},
        {"name": "include_other", "label": "Also position pad and slope labels", "type": "bool",
         "default": False},
        {"name": "curb_layer", "label": "Back of curb layer", "type": "text", "default": "c-road-back",
         "help": "Suffix. Curb labels are rotated to this linework."},
        {"name": "curb_side", "label": "Curb text side", "type": "choice",
         "options": ["outside", "keep"], "default": "outside",
         "help": "outside = flip a curb label in place when its text lands on the road side "
                 "(away from the centreline, else toward the pad, else toward the right of way). "
                 "keep = rotate only."},
        {"name": "centreline_layer", "label": "Road centreline layer", "type": "text",
         "default": "c-rway-center",
         "help": "Suffix. The side of the curb away from this is the outside."},
        {"name": "pad_layers", "label": "Pad layers", "type": "strings", "optional": True,
         "help": "Suffixes. Blank = c-lots-pad, -30, -40, -50. Used to tell the outside of the "
                 "curb when no centreline is in reach."},
        {"name": "include_dragged", "label": "Move already-dragged labels", "type": "bool",
         "default": False,
         "help": "Off = labels somebody already dragged stay put and count as obstacles."},
        {"name": "include_pinned", "label": "Move pinned labels", "type": "bool", "default": False},
        {"name": "obstacle_layers", "label": "Obstacle layers", "type": "strings", "optional": True,
         "help": "Layer suffixes that count as linework. Blank = every visible layer."},
        {"name": "ignore_layers", "label": "Ignore layers", "type": "strings", "optional": True,
         "help": "Layer suffixes that never count as obstacles."},
        {"name": "clearance", "label": "Clearance", "type": "number", "default": 0.5,
         "help": "Feet of padding kept around linework."},
        {"name": "label_gap", "label": "Label gap", "type": "number", "optional": True,
         "help": "Feet a placed label keeps from other text and leaders. Blank = 0."},
        {"name": "cell_size", "label": "Grid cell", "type": "number", "optional": True,
         "help": "Feet. Blank = a quarter of the text height."},
        {"name": "leader_lengths", "label": "Leader lengths", "type": "strings", "optional": True,
         "help": "Feet, comma separated. Blank = 1, 2, 3 and 4.5 text heights."},
        {"name": "max_passes", "label": "Repair passes", "type": "int", "default": 10},
    ],
})


# -- Grading sheets from a key map ----------------------------------------------
#
# The sheet windows of a grading set are drawn in a separate key map drawing:
# a rectangle per sheet (linework, not always closed) with the sheet's number
# inside. The first block reads that drawing and keeps its cells for the
# dashboard; the second lets the user pick cells, labels each cell's window
# with the grading label commands, then cuts one sheet per cell.

BLOCK_SPECS.append({
    "type": "read_key_map",
    "label": "Grading Key Map",
    "group": "Generated",
    "description": ("Reads the grading key map drawing (another .dwg, in the same coordinates) "
                    "and keeps its sheet cells for this dashboard: every closed area of the "
                    "linework that holds a key text becomes a cell named by that key. Lines "
                    "need not be closed polylines: endpoints are snapped, crossings split, "
                    "open tails ignored. Writes nothing; the Cut Grading Sheets block lists "
                    "the cells it found. Civil 3D opens the key map if it is not open."),
    "expand": "key_map",   # reads the key map drawing; see _expand in server.py
    "fields": [
        {"name": "key_map_file", "label": "Key map drawing", "type": "text", "optional": True,
         "help": "Full path of the key map .dwg. Blank = key_map.file in the plan type's sheets.json."},
        {"name": "plan_type", "label": "Plan type", "type": "text", "default": "grading",
         "help": "A type under playbooks/ whose sheets.json has a key_map section."},
        {"name": "cell_layers", "label": "Cell linework layers", "type": "strings", "optional": True,
         "help": "Layer name suffixes, comma separated. Blank = the spec's, else every layer."},
        {"name": "key_layers", "label": "Key text layers", "type": "strings", "optional": True,
         "help": "Layer name suffixes of the sheet-number text. Blank = the spec's, else every layer."},
        {"name": "snap", "label": "Snap (ft)", "type": "number", "optional": True,
         "help": "Gaps between lines up to this are closed. Blank = the spec's (0.5)."},
    ],
})

BLOCK_SPECS.append({
    "type": "cut_grading_sheets",
    "label": "Cut Grading Sheets",
    "group": "Generated",
    "description": ("label_grading then space_labels once over the whole drawing (with the "
                    "plan type's grading.labels / grading.spacing parameters), then one sheet "
                    "per chosen key-map cell: the template layout cloned, the viewport cut on "
                    "the cell (scale from the cell's size, twist from its rotation), the title "
                    "block filled with the key, the furniture placed. Needs the Grading Key "
                    "Map block (or read_key_map) run first."),
    "expand": "grading_sheets",   # reads the stored key map and the drawing; see _expand in server.py
    "fields": [
        {"name": "plan_type", "label": "Plan type", "type": "text", "default": "grading"},
        {"name": "cells", "label": "Cells", "type": "multichoice",
         "options_from": "keymap_cells", "default": [],
         "help": "Which key-map cells become sheets in this drawing. None = all of them."},
        {"name": "scale", "label": "Scale (ft per inch)", "type": "number", "optional": True,
         "help": "Blank = derived from each cell's size over the viewport. 40 forces 1\" = 40'."},
        {"name": "title", "label": "Sheet title", "type": "text", "default": "",
         "help": "Fills {title} in the title block and furniture."},
        {"name": "labels", "label": "Label first", "type": "bool", "default": True,
         "help": "Off = cut the sheets only; no label_grading / space_labels pass."},
        {"name": "label_scope", "label": "Label pass", "type": "choice",
         "options": ["drawing", "cells"], "default": "drawing",
         "help": "drawing = one label_grading + one space_labels over the whole drawing (fast). "
                 "cells = the pair once per chosen cell, inside its window."},
    ],
})


# -- Generated blocks: one per general drafting tool ---------------------------
#
# drafting_tools.py holds ~95 MCP tools (draw, modify, annotate, organise, query).
# Their block specs are derived from the tool signatures here, and their payloads
# are produced by calling the very same tool functions against a recording stub,
# so a block always sends exactly what the agent's tool would send.

_HANDLE_FIELDS = {"handles", "targets", "tools", "cutting_handles", "boundary_handles",
                  "tangent_handles", "against"}

_GROUP_RULES = [
    ("Draw", ("draw_", "create_region", "region_boolean", "trace_boundary")),
    ("Annotate", ("dimension_", "edit_dimension", "edit_text", "create_hatch", "edit_hatch",
                  "create_table", "set_table_cell", "list_styles", "create_text_style",
                  "create_dim_style")),
    ("Modify", ("move_entities", "copy_entities", "rotate_entities", "scale_entities",
                "mirror_entities", "stretch_entities", "align_entities", "offset_entities",
                "array_entities", "fillet_", "chamfer_", "trim_entities", "extend_entities",
                "break_entity", "lengthen_entity", "join_entities", "explode_entities",
                "mark_along_entity", "edit_polyline", "edit_spline", "reverse_curves",
                "set_properties", "match_properties", "set_draw_order")),
    ("Layers & Settings", ("list_layers", "layer_state", "isolate_layers", "set_current_layer",
                           "purge_unused", "rename_named_object", "get_drawing_settings",
                           "set_drawing_settings")),
    ("Blocks & References", ("define_block", "insert_block", "list_block_definitions",
                             "set_block_attributes", "define_attribute", "sync_block_attributes",
                             "export_block", "attach_", "group_entities")),
    ("Layouts & File", ("manage_layouts", "create_viewport", "zoom_view", "save_drawing")),
    ("Query", ("get_properties", "select_entities", "entity_key_points", "closest_point_on",
               "intersections", "tangent_points", "measure_entities", "angle_between",
               "point_along", "bounding_box", "find_duplicates", "list_cad_commands")),
    ("Advanced", ("run_sequence",)),
]

# Annotation-derived draw tools are text, leaders and tolerances rather than geometry.
_ANNOTATE_DRAW = {"draw_text", "draw_mtext", "draw_leader", "draw_tolerance"}


def _group_for(name: str) -> str:
    if name in _ANNOTATE_DRAW:
        return "Annotate"
    for group, prefixes in _GROUP_RULES:
        if any(name == p or name.startswith(p) for p in prefixes):
            return group
    return "Other"


def _label_for(name: str) -> str:
    return name.replace("_", " ").title().replace("Mtext", "MText").replace("Xref", "Xref").replace("Pdf", "PDF")


def _description_for(fn) -> str:
    doc = inspect.getdoc(fn) or ""
    doc = " ".join(doc.split())
    # First sentence, trimmed; the full docstring goes into the help tooltip.
    match = re.match(r"(.+?\.)(\s|$)", doc)
    return (match.group(1) if match else doc)[:160]


def _unwrap_optional(annotation):
    """Returns (inner_type, optional) for `T | None`; the annotation otherwise."""
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        optional = len(args) < len(typing.get_args(annotation))
        return (args[0] if len(args) == 1 else tuple(args)), optional
    return annotation, False


def _field_type(name: str, annotation) -> str:
    inner, _ = _unwrap_optional(annotation)
    if isinstance(inner, tuple):
        # str | int | list[int] etc. -- free text is the only form that fits every arm.
        return "text"
    origin = typing.get_origin(inner)
    if inner is bool:
        return "bool"
    if inner is int:
        return "int"
    if inner is float:
        return "number"
    if inner is dict:
        return "json"
    if origin is list:
        (item,) = typing.get_args(inner) or (str,)
        if item is float:
            return "point"
        if typing.get_origin(item) is list:
            return "points"
        if item is dict:
            return "json"
        if item is int:
            return "strings"
        return "handles" if name in _HANDLE_FIELDS else "strings"
    return "text"


class _Recorder:
    """Stands in for FastMCP: collects the tool functions instead of serving them."""

    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


_captured = []


def _capture_post(payload, file=None):
    _captured.append(payload)
    return "{}"


_recorder = _Recorder()
drafting_tools.register(_recorder, lambda fn: fn, _capture_post, lambda dashboard: None)


def _make_builder(fn):
    def build(values):
        del _captured[:]
        fn(dashboard="block", **values)
        if not _captured:
            raise ValueError(f"{fn.__name__} produced no command")
        return dict(_captured[-1])
    return build


def _generated_specs() -> list:
    specs = []
    for name, fn in _recorder.tools.items():
        params = inspect.signature(fn).parameters
        if "dashboard" not in params:
            continue  # pure helpers (polar_point, ...) talk to no drawing
        fields = []
        for pname, param in params.items():
            if pname == "dashboard":
                continue
            ftype = _field_type(pname, param.annotation)
            field = {"name": pname, "label": pname.replace("_", " "), "type": ftype}
            if param.default is inspect.Parameter.empty:
                pass
            elif param.default is None:
                field["optional"] = True
            elif ftype == "bool":
                field["default"] = bool(param.default)
            elif ftype in ("point", "points", "json", "strings", "handles"):
                field["default"] = (json.dumps(param.default)
                                    if isinstance(param.default, (list, dict)) else param.default)
            else:
                field["default"] = param.default
            if ftype == "points":
                field["min_items"] = 1
            fields.append(field)
        specs.append({
            "type": name,
            "label": _label_for(name),
            "group": _group_for(name),
            "description": _description_for(fn),
            "help": inspect.getdoc(fn) or "",
            "build": _make_builder(fn),
            "fields": fields,
        })
    order = [g for g, _ in _GROUP_RULES] + ["Other"]
    specs.sort(key=lambda s: (order.index(s["group"]), s["type"]))
    return specs


BLOCK_SPECS.extend(_generated_specs())

SPECS_BY_TYPE = {spec["type"]: spec for spec in BLOCK_SPECS}

# Only these groups appear in the browser's "Add block" menu. The rest -- one
# line, one arc, one layer at a time -- are quicker to do by hand in CAD than
# through a form, so they stay out of sight but fully runnable: an LLM composing
# a run through list_blocks / run_on_dashboard, a typed script line, or a saved
# sequence that already holds one can still use every block in BLOCK_SPECS.
PALETTE_GROUPS = ("Generated",)


def in_palette(spec: dict) -> bool:
    return spec.get("group") in PALETTE_GROUPS

MAX_REPEAT = 500
MAX_BLOCKS = 200
MAX_COMMANDS = 2000


def usage(spec: dict) -> str:
    """The positional text form of a block, shown in error messages."""
    parts = []
    for field in spec["fields"]:
        optional = "default" in field or field.get("optional")
        parts.append(f"[{field['name']}]" if optional else field["name"])
    return f"{spec['type']} " + " ".join(parts)


def palette() -> list:
    """BLOCK_SPECS without the builders, for the browser to render forms from.

    Every block is listed so a sequence holding any of them can be rendered and
    run; "palette" says whether the browser offers it in the "Add block" menu.
    """
    return [
        {**{k: v for k, v in spec.items() if k != "build"},
         "usage": usage(spec), "palette": in_palette(spec)}
        for spec in BLOCK_SPECS
    ]


def build_block(block_type: str, values: dict) -> dict:
    """Validates one block's parameters and returns the plugin payload."""
    spec = SPECS_BY_TYPE.get(block_type)
    if spec is None:
        raise ValueError(f"Unknown block '{block_type}'. "
                         f"Available: {', '.join(SPECS_BY_TYPE)}.")

    resolved = {}
    for field in spec["fields"]:
        value = coerce_field(field, (values or {}).get(field["name"]))
        if value is not _MISSING:
            resolved[field["name"]] = value

    if "expand" in spec:
        # Validated, but not buildable yet: carry the parameters through so the
        # runner can turn this into real payloads once it has read the drawing.
        return {"intent": "__expand__", "expander": spec["expand"], "params": resolved}
    return spec["build"](resolved)


def _repeat_of(raw, where: str) -> int:
    try:
        repeat = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: repeat must be a whole number, got '{raw}'")
    if repeat < 1 or repeat > MAX_REPEAT:
        raise ValueError(f"{where}: repeat must be between 1 and {MAX_REPEAT}")
    return repeat


def compile_blocks(blocks: list) -> list:
    """Expands a block sequence into one plugin payload per execution.

    Each payload keeps a "_block" id so the dashboard can highlight the block a
    running step came from, even though a repeat fans one block out into many.
    """
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("Nothing to run - add at least one block to the sequence.")
    if len(blocks) > MAX_BLOCKS:
        raise ValueError(f"A sequence can hold at most {MAX_BLOCKS} blocks.")

    commands = []
    for position, block in enumerate(blocks, start=1):
        if not isinstance(block, dict):
            raise ValueError(f"block {position}: expected an object")
        block_type = block.get("type")
        where = f"block {position} ({block_type or 'untyped'})"

        try:
            payload = build_block(block_type, block.get("params"))
        except ValueError as e:
            raise ValueError(f"{where}: {e}")

        payload["_block"] = str(block.get("id") or position)
        repeat = _repeat_of(block.get("repeat", 1), where)
        commands.extend([payload] * repeat)

        if len(commands) > MAX_COMMANDS:
            raise ValueError(f"That expands past {MAX_COMMANDS} commands - too many to queue.")

    return commands


def parse_command(line: str) -> dict:
    """Parses one line of the positional text syntax into a plugin payload."""
    parts = line.split()
    if not parts:
        raise ValueError("empty command")

    block_type = parts[0].lower()
    spec = SPECS_BY_TYPE.get(block_type)
    if spec is None:
        raise ValueError(f"Unknown command '{parts[0]}'. "
                         f"Supported: {', '.join(SPECS_BY_TYPE)}.")

    args = parts[1:]
    if len(args) > len(spec["fields"]):
        raise ValueError(f"too many arguments - usage: {usage(spec)}")

    values = {f["name"]: (args[i] if i < len(args) else None)
              for i, f in enumerate(spec["fields"])}
    try:
        return build_block(block_type, values)
    except ValueError as e:
        raise ValueError(f"{e} - usage: {usage(spec)}")


def parse_script(script: str) -> list:
    """Parses the text syntax into commands. Lines may carry a repeat count
    ('5x draw_line ...'); blank lines and # comments are ignored."""
    commands = []
    for lineno, raw in enumerate(script.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        repeat = 1
        first, _, rest = line.partition(" ")
        if first.lower().endswith("x") and first[:-1].isdigit():
            repeat = _repeat_of(first[:-1], f"line {lineno}")
            line = rest.strip()
            if not line:
                raise ValueError(f"line {lineno}: '{raw.strip()}' has a repeat but no command")

        try:
            payload = parse_command(line)
        except ValueError as e:
            raise ValueError(f"line {lineno}: {e}")

        payload["_block"] = f"line{lineno}"
        commands.extend([payload] * repeat)

    if not commands:
        raise ValueError("Nothing to run - enter at least one command.")
    return commands
