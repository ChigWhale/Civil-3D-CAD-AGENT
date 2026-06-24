import requests
import json, openpyxl, os
from collections import defaultdict
from mcp.server.fastmcp import FastMCP

mcp     = FastMCP("CAD Agent")
BASE    = "http://localhost:5199"
TIMEOUT = 30
_NO_CONNECTION = '{"error": "Civil 3D is not reachable — make sure AutoCAD is open with the CADAgent plugin loaded."}'


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
def get_model_space() -> str:
    """Returns the current Civil 3D drawing as JSON (alignments, surfaces,
    corridors, pipe networks, lines, arcs, layers, etc.).
    Call this EVERY time a user enters a prompt."""
    return _get("/modelspace")


@mcp.tool()
def draw_line(start_x: float, start_y: float,
              end_x: float,   end_y: float,
              layer: str) -> str:
    """Draws a straight line in the active Civil 3D drawing.
    Only call when the user explicitly asks to draw or create a line.
    If the user does not explicitly state a layer, use layer as '0' """
    return _post({
        "intent":      "draw_line",
        "start_point": [start_x, start_y, 0],
        "end_point":   [end_x,   end_y,   0],
        "layer":       layer,
    })


@mcp.tool()
def draw_shape(vertices: list[list[float]],
               layer:    str,
               closed:   bool = True) -> str:
    """Draws a polyline shape in the drawing.
    vertices is a list of [x, y] pairs, e.g. [[0,0],[10,0],[10,10]].
    Only call when the user asks to draw a shape or polygon.
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

    path = r"C:\Users\me\Downloads\quantities.xlsx"
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
                struct_totals[struct.get("description", "Unknown")] += 1

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