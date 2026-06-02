import requests
from mcp.server.fastmcp import FastMCP

mcp  = FastMCP("CAD Agent")
BASE = "http://localhost:5199"


@mcp.tool()
def get_model_space() -> str:
    """Returns the current Civil 3D drawing as JSON (alignments, surfaces,
    corridors, pipe networks, lines, arcs, layers, etc.).
    Only call this when the user is asking about what is in their drawing."""
    return requests.get(f"{BASE}/modelspace").text


@mcp.tool()
def draw_line(start_x: float, start_y: float,
              end_x: float,   end_y: float,
              layer: str = "0") -> str:
    """Draws a straight line in the active Civil 3D drawing.
    Only call when the user explicitly asks to draw or create a line."""
    payload = {
        "intent":      "draw_line",
        "start_point": [start_x, start_y, 0],
        "end_point":   [end_x,   end_y,   0],
        "layer":       layer,
    }
    return requests.post(f"{BASE}/execute", json=payload).text


@mcp.tool()
def draw_shape(vertices: list[list[float]],
               layer:    str  = "0",
               closed:   bool = True) -> str:
    """Draws a polyline shape in the drawing.
    vertices is a list of [x, y] pairs, e.g. [[0,0],[10,0],[10,10]].
    Only call when the user asks to draw a shape or polygon."""
    payload = {
        "intent":   "draw_shape",
        "vertices": vertices,
        "layer":    layer,
        "closed":   closed,
    }
    return requests.post(f"{BASE}/execute", json=payload).text


@mcp.tool()
def create_layer(name: str, color: int = 7) -> str:
    """Creates a new layer in the drawing.
    color is an AutoCAD color index (1=red, 2=yellow, 3=green, 7=white).
    Only call when the user asks to create a layer."""
    payload = {"intent": "create_layer", "name": name, "color": color}
    return requests.post(f"{BASE}/execute", json=payload).text


@mcp.tool()
def edit_layer(name: str, color: int | None = None, visible: bool | None = None) -> str:
    """Edits an existing layer's color or visibility.
    Only call when the user asks to change a layer's properties."""
    payload = {"intent": "edit_layer", "name": name}
    if color   is not None: payload["color"]   = color
    if visible is not None: payload["visible"] = visible
    return requests.post(f"{BASE}/execute", json=payload).text


@mcp.tool()
def move_to_layer(handles: list[str], layer: str) -> str:
    """Moves objects (identified by their handle strings) to a layer.
    Only call when the user asks to move or reassign objects to a layer."""
    payload = {"intent": "add_to_layer", "layer": layer, "handles": handles}
    return requests.post(f"{BASE}/execute", json=payload).text


@mcp.tool()
def delete_objects(handles: list[str]) -> str:
    """Permanently deletes objects from the drawing by their handle strings.
    Handles are found in the get_model_space output.
    Only call when the user explicitly asks to delete or remove objects."""
    payload = {
        "intent":  "delete",
        "objects": [{"object_handle": h, "object_type": "object"} for h in handles],
    }
    return requests.post(f"{BASE}/execute", json=payload).text


if __name__ == "__main__":
    mcp.run()
