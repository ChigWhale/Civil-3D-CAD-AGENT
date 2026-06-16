import requests
import json, openpyxl, os
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
def quantify() -> str:
    """Collects all objects from model space and writes a quantity takeoff to
    C:/CAD-Agent Test/quantities.xlsx.
    Rows: one per object. Columns: Type, Handle, Layer, Quantity, Unit.
    Call this when the user asks for a quantity takeoff or to fill out the Excel."""

    raw = _get("/modelspace")
    try:
        data = json.loads(raw)
    except Exception:
        return raw

    rows = []

    for ln in data.get("lines", []):
        rows.append(("Line", ln.get("handle",""), ln.get("layer",""), round(ln.get("length", 0), 4), "length"))

    for ar in data.get("arcs", []):
        rows.append(("Arc", ar.get("handle",""), ar.get("layer",""), round(ar.get("length", 0), 4), "length"))

    for pl in data.get("polylines", []):
        rows.append(("Polyline", pl.get("handle",""), pl.get("layer",""), round(pl.get("area", 0), 4), "area"))

    for al in data.get("alignments", []):
        rows.append(("Alignment", al.get("handle",""), al.get("name",""), round(al.get("length", 0), 4), "length"))

    for se in data.get("standard_entities", []):
        rows.append((se.get("type",""), "", "", se.get("count", 0), "count"))

    path = r"C:\Users\me\Desktop\MCP Test\quantities.xlsx"
    os.makedirs(os.path.dirname(path), exist_ok=True)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Quantities"
    ws.append(["Type", "Handle", "Layer / Name", "Quantity", "Unit"])
    for row in rows:
        ws.append(list(row))

    wb.save(path)
    return f"Excel written to {path} — {len(rows)} rows."

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