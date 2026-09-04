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

import math

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
]

SPECS_BY_TYPE = {spec["type"]: spec for spec in BLOCK_SPECS}

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
    """BLOCK_SPECS without the builders, for the browser to render forms from."""
    return [
        {**{k: v for k, v in spec.items() if k != "build"}, "usage": usage(spec)}
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
