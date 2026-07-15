import requests
import json, openpyxl, os
from collections import defaultdict
from mcp.server.fastmcp import FastMCP

mcp     = FastMCP("CAD Agent")
BASE    = "http://localhost:5199"
TIMEOUT = 30
_NO_CONNECTION = '{"error": "Civil 3D is not reachable — make sure AutoCAD is open with the CADAgent plugin loaded."}'

_SIZE_LIMIT = 950_000  # bytes — 50 KB buffer below the 1 MB MCP threshold

_ALL_CATEGORIES = [
    "lines", "arcs", "polylines", "alignments",
    "pipe_networks", "surfaces", "corridors",
    "blocks", "layers", "adjacency",
]

# Per-session state: populated on the initial (no-arg) call, reused for chunked follow-ups.
_MS_CACHE: dict | None = None   # full raw data from one AutoCAD fetch
_MS_ACCUM: dict        = {}     # merged result across all chunks this session
_MS_SAVE_PATH = os.path.join(os.path.expanduser("~"), "Downloads", "model_space_full.json")


def _get(path: str) -> str:
    try:
        return requests.get(f"{BASE}{path}", timeout=TIMEOUT).text
    except requests.exceptions.ConnectionError:
        return _NO_CONNECTION


def _post(payload: dict) -> str:
    try:
        return requests.post(f"{BASE}/execute", json=payload, timeout=TIMEOUT).text
    except requests.exceptions.ConnectionError:
        return _NO_CONNECTION


@mcp.tool()
def get_model_space(categories: list[str] | None = None) -> str:
    """Returns the current Civil 3D drawing as JSON.
    YOU MUST call this before ANY other tool and before responding to ANY user message.
    Never skip this call — drawing state changes between messages.

    categories: optional list limiting which data categories are returned.
    Omit to request all. Valid values:
      "lines", "arcs", "polylines", "alignments", "pipe_networks",
      "surfaces", "corridors", "blocks", "layers", "adjacency"

    CHUNKING: if the response approaches 1 MB, partial data is returned with a
    "__remaining_categories__" field. You MUST call get_model_space again with
    those categories and repeat until "__remaining_categories__" is absent.
    The server accumulates every chunk automatically; the full JSON is written
    to disk once all chunks have been retrieved."""

    global _MS_CACHE, _MS_ACCUM

    # Initial call: fetch fresh data from AutoCAD and reset accumulator.
    if categories is None:
        raw = _get("/modelspace")
        try:
            _MS_CACHE = json.loads(raw)
        except Exception:
            return raw
        _MS_ACCUM = {}
    else:
        if _MS_CACHE is None:
            return '{"error": "Call get_model_space() without arguments first to load drawing data."}'

    data     = _MS_CACHE
    pool     = _ALL_CATEGORIES if categories is None else categories
    to_fetch = [c for c in pool if c in data]

    result    = {}
    remaining = []

    for i, cat in enumerate(to_fetch):
        candidate = {**result, cat: data[cat]}
        if len(json.dumps(candidate).encode("utf-8")) > _SIZE_LIMIT:
            remaining = to_fetch[i:]   # this cat and everything after didn't fit
            break
        result[cat] = data[cat]

    # Merge this chunk into the session accumulator.
    for cat, val in result.items():
        _MS_ACCUM[cat] = val

    if remaining:
        result["__remaining_categories__"] = remaining
        result["__size_bytes__"]           = len(json.dumps(result).encode("utf-8"))
        result["__note__"] = (
            f"Size limit reached ({result['__size_bytes__']:,} bytes). "
            f"Call get_model_space({remaining}) to retrieve the rest. "
            f"Repeat until __remaining_categories__ is absent."
        )
    else:
        # All chunks received — write the complete accumulated JSON to disk.
        try:
            os.makedirs(os.path.dirname(_MS_SAVE_PATH), exist_ok=True)
            with open(_MS_SAVE_PATH, "w", encoding="utf-8") as f:
                json.dump(_MS_ACCUM, f)
        except Exception:
            pass  # don't fail the tool call if the save fails

    return json.dumps(result)


@mcp.tool()
def query_model_space(category: str) -> str:
    """Returns one category of data from the most recently fetched model space JSON.
    Use this to look up specific drawing data without re-fetching everything from AutoCAD.
    Prefer this over re-calling get_model_space when you only need one category.
    Valid values: lines, arcs, polylines, alignments, pipe_networks,
                  surfaces, corridors, blocks, layers, adjacency"""
    # In-memory accumulator takes priority (current session, no disk I/O)
    if category in _MS_ACCUM:
        return json.dumps({category: _MS_ACCUM[category]})
    # Fall back to saved file (data from a previous session)
    try:
        with open(_MS_SAVE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if category in data:
            return json.dumps({category: data[category]})
        return json.dumps({"error": f"Category '{category}' not found.",
                           "available": list(data.keys())})
    except FileNotFoundError:
        return '{"error": "No saved model space data. Call get_model_space() first."}'
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def draw_line(start_x: float, start_y: float,
              end_x: float,   end_y: float,
              layer: str) -> str:
    """Draws a straight line in the active Civil 3D drawing.
    Only call when the user explicitly asks to draw or create a line or when markdown files need them.
    If the user does not explicitly state a layer, use layer as '0' """
    return _post({
        "intent":      "draw_line",
        "start_point": [start_x, start_y, 0],
        "end_point":   [end_x,   end_y,   0],
        "layer":       layer,
    })

@mcp.tool()
def draw_arc(center_x: float, center_y: float,
             radius: float,
             start_angle_deg: float, end_angle_deg: float,
             layer: str) -> str:
    """Draws an arc in the active Civil 3D drawing.
    center_x, center_y define the center of the circle the arc belongs to.
    radius is the radius of that circle.
    start_angle_deg and end_angle_deg are in degrees, measured counter-clockwise
    from the positive X axis. The arc always draws counter-clockwise from start to end.
    Only call when the user explicitly asks to draw or create an arc.
    If the user does not explicitly state a layer, use layer as '0'"""
    import math
    return _post({
        "intent":      "draw_arc",
        "center":      [center_x, center_y, 0],
        "radius":      radius,
        "start_angle": math.radians(start_angle_deg),
        "end_angle":   math.radians(end_angle_deg),
        "layer":       layer,
    })


@mcp.tool()
def draw_shape(vertices: list[list[float]],
               layer:    str,
               closed:   bool = True) -> str:
    """Draws a polyline shape in the drawing.
    vertices is a list of [x, y] pairs, e.g. [[0,0],[10,0],[10,10]].
    Only call when the user asks to draw a shape or polygon or when markdown files need them.
    If the user does not explicitly state a layer, use layer as '0' """
    return _post({
        "intent":   "draw_shape",
        "vertices": vertices,
        "layer":    layer,
        "closed":   closed,
    })


@mcp.tool()
def create_layer(name: str, color: int = 7) -> str:
    """Creates a new layer in the drawing.
    color is an AutoCAD color index (1=red, 2=yellow, 3=green, 7=white).
    Only call when the user asks to create a layer."""
    return _post({"intent": "create_layer", "name": name, "color": color})


@mcp.tool()
def edit_layer(name: str, color: int | None = None, visible: bool | None = None) -> str:
    """Edits an existing layer's color or visibility.
    Only call when the user asks to change a layer's properties."""
    payload = {"intent": "edit_layer", "name": name}
    if color   is not None: payload["color"]   = color
    if visible is not None: payload["visible"] = visible
    return _post(payload)


@mcp.tool()
def move_to_layer(handles: list[str], layer: str) -> str:
    """Moves objects (identified by their handle strings) to a layer.
    Only call when the user asks to move or reassign objects to a layer."""
    return _post({"intent": "add_to_layer", "layer": layer, "handles": handles})

@mcp.tool()
def quantify(objOfInterest: list[str], quantitiesOfInterest: dict) -> str:
    """Writes a quantity takeoff Excel file for specific objects the user asked about.

    BEFORE calling this tool you MUST complete both steps below — do not call
    the tool until it is done:

    STEP 1 - Build quantitiesOfInterest:
    This is a dict where each key is an object category and each value is a list
    of field names to extract from that object's JSON. Only include categories
    AND fields the user explicitly asked about — do NOT default to all fields.
    Use ONLY the field names the user mentioned, chosen from these available options:

      "lines":      ["handle", "layer", "length", "start_point", "end_point"]
      "arcs":       ["handle", "layer", "length", "radius", "center"]
      "polylines":  ["handle", "layer", "length", "area", "closed"]
      "alignments": ["handle", "name", "length", "start_station", "end_station"]
      "pipes":      ["handle", "name", "description", "network", "length", "slope", "inner_diameter", "start_structure", "end_structure", "start_point", "end_point"]
      "structures": ["handle", "name", "description", "network", "rim_elevation", "sump_elevation", "inner_diameter"]

    Example — user asks ONLY for line lengths and layers (do NOT add handle or other fields):
      quantitiesOfInterest = {"lines": ["layer", "length"]}


    STEP 2 - Identify handles:
      Call get_model_space if you have not already. Then find every object in the
      JSON that matches what the user described and collect their "handle" values
      into objOfInterest. Match by any property the user mentions:
        - Object type (line, arc, polyline, pipe, structure)
        - Layer name (layer == "STORM")
        - Diameter (inner_diameter == 12) ***This can be extended to ask for circular or non-circular pipes***
        - Closed/open (closed == true)
        - Network name
      If no objects match, tell the user instead of calling this tool with an
      empty list.
      Pipe/structure handles are nested: pipe_networks[i].pipes[j].handle
                                         pipe_networks[i].structures[j].handle"""

    raw = _get("/modelspace")
    try:
        data = json.loads(raw)
    except Exception:
        return raw

    wanted = set(objOfInterest)
    rows = []

    for ln in data.get("lines", []):
        if ln.get("handle") in wanted:
            toAppend = ["Line"]
            for item in quantitiesOfInterest.get("lines",[]):
                val = ln.get(item)
                if isinstance(val, bool):
                    toAppend.append(f"{str(item)}: {val}")
                elif isinstance(val, (int, float)):
                    toAppend.append(f"{str(item)}: {round(val, 4)}")
                else:
                    toAppend.append(f"{str(item)}: {val or ''}")
            rows.append(toAppend)

    for ar in data.get("arcs", []):
        if ar.get("handle") in wanted:
            toAppend = ["Arc"]
            for item in quantitiesOfInterest.get("arcs", []):
                val = ar.get(item)
                if isinstance(val, bool):
                    toAppend.append(f"{str(item)}: {val}")
                elif isinstance(val, (int, float)):
                    toAppend.append(f"{str(item)}: {round(val, 4)}")
                else:
                    toAppend.append(f"{str(item)}: {val or ''}")
            rows.append(toAppend)

    for pl in data.get("polylines", []):
        if pl.get("handle") in wanted:
            toAppend = ["Polyline"]
            for item in quantitiesOfInterest.get("polylines", []):
                val = pl.get(item)
                if isinstance(val, bool):
                    toAppend.append(f"{str(item)}: {val}")
                elif isinstance(val, (int, float)):
                    toAppend.append(f"{str(item)}: {round(val, 4)}")
                else:
                    toAppend.append(f"{str(item)}: {val or ''}")
            rows.append(toAppend)

    for al in data.get("alignments", []):
        if al.get("handle") in wanted:
            toAppend = ["Alignment"]
            for item in quantitiesOfInterest.get("alignments", []):
                val = al.get(item)
                if isinstance(val, bool):
                    toAppend.append(f"{str(item)}: {val}")
                elif isinstance(val, (int, float)):
                    toAppend.append(f"{str(item)}: {round(val, 4)}")
                else:
                    toAppend.append(f"{str(item)}: {val or ''}")
            rows.append(toAppend)

    for net in data.get("pipe_networks", []):
        net_name = net.get("name", "")
        for pipe in net.get("pipes", []):
            if pipe.get("handle") in wanted:
                toAppend = ["Pipe"]
                for item in quantitiesOfInterest.get("pipes", []):
                    val = net_name if item == "network" else pipe.get(item)
                    if isinstance(val, bool):
                        toAppend.append(f"{str(item)}: {val}")
                    elif isinstance(val, (int, float)):
                        toAppend.append(f"{str(item)}: {round(val, 4)}")
                    else:
                        toAppend.append(f"{str(item)}: {val or ''}")
                rows.append(toAppend)
        for struct in net.get("structures", []):
            if struct.get("handle") in wanted:
                toAppend = ["Structure"]
                for item in quantitiesOfInterest.get("structures", []):
                    val = net_name if item == "network" else struct.get(item)
                    if isinstance(val, bool):
                        toAppend.append(f"{str(item)}: {val}")
                    elif isinstance(val, (int, float)):
                        toAppend.append(f"{str(item)}: {round(val, 4)}")
                    else:
                        toAppend.append(f"{str(item)}: {val or ''}")
                rows.append(toAppend)

    path = os.path.join(os.path.expanduser("~"), "Downloads", "quantities.xlsx")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    wb = openpyxl.Workbook()

    # ── Main sheet: one row per object ───────────────────────────────────────
    ws = wb.active
    ws.title = "Quantities"
    for row in rows:
        ws.append(list(row))

    # ── Pipe Summary: total length grouped by pipe name ──────────────────────
    pipe_totals = defaultdict(float)
    for net in data.get("pipe_networks", []):
        for pipe in net.get("pipes", []):
            if pipe.get("handle") in wanted:
                pipe_totals[pipe.get("description", "Unknown")] += pipe.get("length", 0)

    # ── Structure Summary: count grouped by structure name ───────────────────
    struct_totals = defaultdict(int)
    for net in data.get("pipe_networks", []):
        for struct in net.get("structures", []):
            if struct.get("handle") in wanted:
                key = struct.get("description") or struct.get("name") or "Unknown"
                struct_totals[key] += 1

    if pipe_totals or struct_totals:
        ws2 = wb.create_sheet("Pipe Summary")
        ws2.append(["Name", "Total Length", "Count"])
        for name, total in sorted(pipe_totals.items()):
            ws2.append([name, round(total, 4), ""])
        for description, count in sorted(struct_totals.items()):
            ws2.append([description, "", count])

    wb.save(path)
    return (f"Excel written to {path} — {len(rows)} rows.\n"
            f"DEBUG objOfInterest: {list(wanted)}\n"
            f"DEBUG quantitiesOfInterest: {quantitiesOfInterest}")

# @mcp.tool()
# def delete_objects(handles: list[str]) -> str:
#     """Permanently deletes objects from the drawing by their handle strings.
#     Handles are found in the get_model_space output.
#     Only call when the user explicitly asks to delete or remove objects."""
#     return _post({
#         "intent":  "delete",
#         "objects": [{"object_handle": h, "object_type": "object"} for h in handles],
#     })

if __name__ == "__main__":
    mcp.run()