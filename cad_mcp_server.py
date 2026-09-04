import requests
import json, openpyxl, os
import base64, functools, inspect, math, time
import socket, subprocess, sys, threading, webbrowser
from urllib.parse import quote
from collections import defaultdict
from mcp.server.fastmcp import FastMCP
import dashboard_events
import driveways
import swales
from dashboard_events import SESSION_ID, DashboardError, emit, resolve_file

mcp     = FastMCP("CAD Agent")
# 127.0.0.1, not "localhost": localhost resolves to IPv6 ::1 first on this machine
# and stalls ~2s per request before falling back to IPv4 (measured 2.06s vs 0.015s).
BASE    = "http://127.0.0.1:5199"
TIMEOUT = 30
_NO_CONNECTION = '{"error": "Civil 3D is not reachable — make sure AutoCAD is open with the CADAgent plugin loaded."}'

_SIZE_LIMIT = 950_000  # bytes — 50 KB buffer below the 1 MB MCP threshold

_ALL_CATEGORIES = [
    "lines", "arcs", "polylines", "alignments",
    "pipe_networks", "surfaces", "corridors",
    "blocks", "layers", "adjacency",
]

# Per-drawing state, keyed by the `file` argument ("" means the focused drawing).
# Claude Desktop reuses one server process across every chat, so this must never be
# a single shared value -- one drawing's data would leak into another's queries.
_MS_CACHE: dict = {}   # drawing key -> full raw data from one AutoCAD fetch
_MS_ACCUM: dict = {}   # drawing key -> merged result across all chunks


def _drawing_key(file: str | None) -> str:
    return os.path.normcase(os.path.abspath(file)) if file else ""


def _file_for(dashboard: str) -> str:
    """The drawing the user assigned to this dashboard. Raises DashboardError,
    which the @observed wrapper turns into an explanatory tool result."""
    return resolve_file(dashboard)


def _save_path(file: str | None) -> str:
    stem = os.path.splitext(os.path.basename(file))[0] if file else "focused"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)
    return os.path.join(os.path.expanduser("~"), "Downloads", f"model_space_{safe}.json")


def _get(path: str, file: str | None = None) -> str:
    try:
        return requests.get(f"{BASE}{path}",
                            params={"file": file} if file else None,
                            timeout=TIMEOUT).text
    except requests.exceptions.ConnectionError:
        return _NO_CONNECTION


def _post(payload: dict, file: str | None = None) -> str:
    try:
        if file:
            payload = {**payload, "file": file}
        return requests.post(f"{BASE}/execute", json=payload, timeout=TIMEOUT).text
    except requests.exceptions.ConnectionError:
        return _NO_CONNECTION


_PREVIEW_LEN = 300


def _preview(value, limit: int = _PREVIEW_LEN) -> str:
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def observed(func):
    """Reports every call of a wrapped MCP tool to the local dashboard
    (best-effort — see dashboard_events.emit). Must sit closer to the raw
    function than @mcp.tool() so FastMCP still registers the real callable
    and introspects the original signature via functools.wraps."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        bound = kwargs
        if args:
            try:
                bound = dict(inspect.signature(func).bind_partial(*args, **kwargs).arguments)
            except TypeError:
                bound = kwargs
        # Activity belongs to the dashboard the call names. One process serves every
        # chat, so the dashboard -- not the chat -- is what separates the work.
        board = bound.get("dashboard")

        emit("tool_call_started", tool=func.__name__, dashboard=board,
             args={k: _preview(v) for k, v in bound.items()})
        start = time.monotonic()
        try:
            result = func(*args, **kwargs)
            emit("tool_call_finished", tool=func.__name__, dashboard=board,
                 duration_ms=round((time.monotonic() - start) * 1000),
                 result=_preview(result))
            return result
        except DashboardError as e:
            # Not an internal failure: the user simply hasn't assigned a drawing yet.
            # Return it as the tool result so the agent can relay it, rather than
            # raising and having the drawing silently go unset.
            message = json.dumps({"error": str(e)})
            emit("tool_call_finished", tool=func.__name__, dashboard=board,
                 duration_ms=round((time.monotonic() - start) * 1000),
                 error=str(e))
            return message
        except Exception as e:
            emit("tool_call_finished", tool=func.__name__, dashboard=board,
                 duration_ms=round((time.monotonic() - start) * 1000),
                 error=str(e))
            raise
    return wrapper


@mcp.tool()
@observed
def list_dashboards() -> str:
    """Lists the dashboards the user has created and which drawing each is assigned to.

    Every other tool works through a dashboard. You do NOT choose drawings — the user
    assigns a drawing to each dashboard in the dashboard UI. Call this when you are
    unsure which dashboard to work in, then ask the user if it's still ambiguous."""
    boards = dashboard_events.list_dashboards()
    if not boards:
        return json.dumps({
            "dashboards": [],
            "note": ("No dashboards exist yet. Ask the user to open "
                     "http://localhost:8765 and create one, then assign a drawing to it."),
        })
    return json.dumps({"dashboards": boards})


@mcp.tool()
@observed
def list_drawings() -> str:
    """Lists the drawings currently open in AutoCAD, with their full paths.
    Informational only — use it to tell the user what's open. You cannot direct work
    at these paths yourself; work is routed by the dashboard the user assigned."""
    return _get("/documents")


@mcp.tool()
@observed
def get_model_space(dashboard: str, categories: list[str] | None = None) -> str:
    """Returns a Civil 3D drawing as JSON.
    YOU MUST call this before ANY other tool and before responding to ANY user message.
    Never skip this call — drawing state changes between messages.

    dashboard: REQUIRED id of the dashboard to work through (e.g. "1"). The user
    assigns a drawing to each dashboard; you never choose the drawing yourself.
    Use the same dashboard for every call in a conversation. Call list_dashboards()
    if you don't know which one, and ask the user if it's ambiguous.

    categories: optional list limiting which data categories are returned.
    Omit to request all. Valid values:
      "lines", "arcs", "polylines", "alignments", "pipe_networks",
      "surfaces", "corridors", "blocks", "layers", "adjacency"

    CHUNKING: if the response approaches 1 MB, partial data is returned with a
    "__remaining_categories__" field. You MUST call get_model_space again with
    those categories (and the same dashboard) and repeat until
    "__remaining_categories__" is absent. The server accumulates every chunk
    automatically; the full JSON is written to disk once all chunks are retrieved."""

    file = _file_for(dashboard)
    key = _drawing_key(file)

    # Initial call: fetch fresh data from AutoCAD and reset this drawing's accumulator.
    if categories is None:
        raw = _get("/modelspace", file)
        try:
            _MS_CACHE[key] = json.loads(raw)
        except Exception:
            return raw
        _MS_ACCUM[key] = {}
    else:
        if key not in _MS_CACHE:
            return '{"error": "Call get_model_space() without categories first to load drawing data."}'

    data     = _MS_CACHE[key]
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

    # Merge this chunk into this drawing's accumulator.
    accum = _MS_ACCUM.setdefault(key, {})
    for cat, val in result.items():
        accum[cat] = val

    if remaining:
        result["__remaining_categories__"] = remaining
        result["__size_bytes__"]           = len(json.dumps(result).encode("utf-8"))
        result["__note__"] = (
            f"Size limit reached ({result['__size_bytes__']:,} bytes). "
            f"Call get_model_space({remaining}) to retrieve the rest. "
            f"Repeat until __remaining_categories__ is absent."
        )
    else:
        # All chunks received — write this drawing's accumulated JSON to disk.
        try:
            path = _save_path(file)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(accum, f)
        except Exception:
            pass  # don't fail the tool call if the save fails

    return json.dumps(result)


@mcp.tool()
@observed
def query_model_space(category: str, dashboard: str) -> str:
    """Returns one category of data from the most recently fetched model space JSON.
    Use this to look up specific drawing data without re-fetching everything from AutoCAD.
    Prefer this over re-calling get_model_space when you only need one category.

    dashboard: REQUIRED — the same dashboard you passed to get_model_space. Each
    dashboard's drawing has its own cached data.

    Valid categories: lines, arcs, polylines, alignments, pipe_networks,
                      surfaces, corridors, blocks, layers, adjacency"""
    file = _file_for(dashboard)
    accum = _MS_ACCUM.get(_drawing_key(file), {})
    # In-memory accumulator takes priority (already fetched, no disk I/O)
    if category in accum:
        return json.dumps({category: accum[category]})
    # Fall back to saved file (data from a previous session)
    try:
        with open(_save_path(file), "r", encoding="utf-8") as f:
            data = json.load(f)
        if category in data:
            return json.dumps({category: data[category]})
        return json.dumps({"error": f"Category '{category}' not found.",
                           "available": list(data.keys())})
    except FileNotFoundError:
        return '{"error": "No saved model space data for that drawing. Call get_model_space() first."}'
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
@observed
def draw_line(start_x: float, start_y: float,
              end_x: float,   end_y: float,
              layer: str,
              dashboard: str) -> str:
    """Draws a straight line in the drawing assigned to a dashboard.
    Only call when the user explicitly asks to draw or create a line or when markdown files need them.
    If the user does not explicitly state a layer, use layer as '0'.

    dashboard: REQUIRED id of the dashboard to work through. The drawing behind it
    is set by the user; you cannot target a drawing directly. If unsure which
    dashboard, call list_dashboards() and ask the user rather than guessing."""
    return _post({
        "intent":      "draw_line",
        "start_point": [start_x, start_y, 0],
        "end_point":   [end_x,   end_y,   0],
        "layer":       layer,
    }, _file_for(dashboard))

@mcp.tool()
@observed
def draw_arc(center_x: float, center_y: float,
             radius: float,
             start_angle_deg: float, end_angle_deg: float,
             layer: str,
             dashboard: str) -> str:
    """Draws an arc in a Civil 3D drawing.
    center_x, center_y define the center of the circle the arc belongs to.
    radius is the radius of that circle.
    start_angle_deg and end_angle_deg are in degrees, measured counter-clockwise
    from the positive X axis. The arc always draws counter-clockwise from start to end.
    Only call when the user explicitly asks to draw or create an arc.
    If the user does not explicitly state a layer, use layer as '0'.

    dashboard: REQUIRED id of the dashboard to work through. The drawing behind it
    is set by the user; call list_dashboards() if you are unsure which one."""
    import math
    return _post({
        "intent":      "draw_arc",
        "center":      [center_x, center_y, 0],
        "radius":      radius,
        "start_angle": math.radians(start_angle_deg),
        "end_angle":   math.radians(end_angle_deg),
        "layer":       layer,
    }, _file_for(dashboard))


@mcp.tool()
@observed
def draw_shape(vertices:  list[list[float]],
               layer:     str,
               dashboard: str,
               closed:    bool = True) -> str:
    """Draws a polyline shape in the drawing assigned to a dashboard.
    vertices is a list of [x, y] pairs, e.g. [[0,0],[10,0],[10,10]].
    Only call when the user asks to draw a shape or polygon or when markdown files need them.
    If the user does not explicitly state a layer, use layer as '0'.

    dashboard: REQUIRED id of the dashboard to work through. The drawing behind it
    is set by the user; call list_dashboards() if you are unsure which one."""
    return _post({
        "intent":   "draw_shape",
        "vertices": vertices,
        "layer":    layer,
        "closed":   closed,
    }, _file_for(dashboard))


@mcp.tool()
@observed
def create_layer(name: str, dashboard: str, color: int = 7) -> str:
    """Creates a new layer in the drawing assigned to a dashboard.
    color is an AutoCAD color index (1=red, 2=yellow, 3=green, 7=white).
    Only call when the user asks to create a layer.

    dashboard: REQUIRED id of the dashboard to work through. The drawing behind it
    is set by the user; call list_dashboards() if you are unsure which one."""
    return _post({"intent": "create_layer", "name": name, "color": color},
                 _file_for(dashboard))


@mcp.tool()
@observed
def edit_layer(name: str, dashboard: str, color: int | None = None,
               visible: bool | None = None) -> str:
    """Edits an existing layer's color or visibility.
    Only call when the user asks to change a layer's properties.

    dashboard: REQUIRED id of the dashboard to work through. The drawing behind it
    is set by the user; call list_dashboards() if you are unsure which one."""
    payload = {"intent": "edit_layer", "name": name}
    if color   is not None: payload["color"]   = color
    if visible is not None: payload["visible"] = visible
    return _post(payload, _file_for(dashboard))


@mcp.tool()
@observed
def move_to_layer(handles: list[str], layer: str, dashboard: str) -> str:
    """Moves objects (identified by their handle strings) to a layer.
    Only call when the user asks to move or reassign objects to a layer.

    dashboard: REQUIRED id of the dashboard to work through. The drawing behind it
    is set by the user; call list_dashboards() if you are unsure which one."""
    return _post({"intent": "add_to_layer", "layer": layer, "handles": handles},
                 _file_for(dashboard))

@mcp.tool()
@observed
def quantify(objOfInterest: list[str], quantitiesOfInterest: dict,
             dashboard: str) -> str:
    """Writes a quantity takeoff Excel file for specific objects the user asked about.
    dashboard: REQUIRED — the same dashboard you passed to get_model_space.

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

    file = _file_for(dashboard)
    raw = _get("/modelspace", file)
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

    # Name the workbook after the drawing so quantifying two drawings doesn't
    # have the second run overwrite the first one's takeoff.
    stem = os.path.splitext(os.path.basename(file))[0] if file else "focused"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)
    path = os.path.join(os.path.expanduser("~"), "Downloads", f"quantities_{safe}.xlsx")
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

def _get_nested(file: str | None, layer_suffixes: list) -> dict:
    """Geometry from inside block references / xrefs, in world coordinates.

    Filtered by layer suffix because a site xref carries far more than any one
    caller wants. Returns empty on an older plugin that has no /nested route, so
    a stale DLL degrades to "no pads found" rather than an unexplained failure."""
    try:
        response = requests.get(f"{BASE}/nested",
                                params={"file": file, "layers": ",".join(layer_suffixes)},
                                timeout=TIMEOUT)
        data = response.json()
        return data if isinstance(data, dict) and "error" not in data else {}
    except (requests.exceptions.RequestException, ValueError):
        return {}


@mcp.tool()
@observed
def draw_driveways(dashboard: str, alignment: str = "", layer: str = "0",
                   width: float = 20.0, radius: float = 5.0,
                   max_offset: float = 150.0) -> str:
    """Draws a driveway from every lot pad out to the back of road.

    For each pad (a polyline on a lot pad layer -- "c-lots-pad" or one of the
    width variants "c-lots-pad-30/-40/-50", matched on the end of the name so
    xrefed layers count) this finds the
    FRONTAGE edge facing the back of road (layer "C-ROAD-BACK") — an edge
    crossing the pad's major axis, so the driveway runs down the length of the
    lot rather than off its side — takes the upstream end of that edge, and runs
    two lines perpendicular to the road `width` apart, each finished with a
    `radius` arc tangent to both the driveway line and the road.

    max_offset: only pads whose centroid is within this many feet of the
    alignment are served. One alignment is one street while the pad layer covers
    the whole subdivision, so pads beyond it front another street and are left
    alone; they are reported as "not_on_this_alignment", not as failures.

    alignment: the street or streets to work along, as one name or a comma
    separated list ("RD-1, RD-2"). Leave it blank to use every alignment in the
    drawing. Each pad is worked against the ONE street it fronts — decided by
    which alignment its frontage edge faces — so a lot in reach of two streets
    still gets a single driveway. The alignment also gives flow direction:
    "upstream" is the lower station along it, and the driveway is offset
    downstream from that corner. Call get_model_space for the available names.

    dashboard: REQUIRED id of the dashboard to work through — the user assigns
    its drawing; you never choose one. layer: layer to draw on ("0" by default).

    Reports how many pads were drawn and, for any that were not, why."""
    file = _file_for(dashboard)

    raw = _get("/modelspace", file)
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    if isinstance(data, dict) and "error" in data:
        return raw

    # Pads are normally XREFED in, so they are not in model space at all -- the
    # xref is one block reference there. Pull the nested geometry (already
    # transformed to world coordinates by the plugin) and merge it in, so the
    # planner sees pads and road whether they are drawn here or referenced.
    nested = _get_nested(file, [*driveways.PAD_LAYER_SUFFIXES, driveways.ROAD_LAYER])
    for category in ("polylines", "lines", "arcs"):
        if nested.get(category):
            data[category] = list(data.get(category) or []) + nested[category]

    try:
        payloads, summary = driveways.plan(data, alignment, layer, width, radius,
                                           max_offset)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    for payload in payloads:
        _post(payload, file)
    return json.dumps(summary)


@mcp.tool()
@observed
def draw_rear_swales(dashboard: str, alignment: str = "", layer: str = "0",
                     offset: float = 5.0, first_leg: float = 10.0,
                     lot_line_layer: str = "c-lots-line",
                     max_offset: float = 150.0) -> str:
    """Draws a rear yard swale behind every lot pad.

    Per pad: the rear edge (the pad edge furthest from back of road) is offset
    `offset` ft away from the road into the rear yard, split `first_leg` ft from
    its upstream end into two polylines, and a 45-degree leg runs roadward from
    each outer end until it meets a lot line.

    lot_line_layer: layer the legs stop against, matched by suffix so an xrefed
    "x-plat (ph1a)|c-lots-line" is found. Lot line naming varies between plats;
    if none is found the error lists the layers the drawing actually has.

    alignment: the street or streets to work along, as one name or a comma
    separated list ("RD-1, RD-2"); blank means every alignment in the drawing.
    Each pad is worked against the ONE street it fronts, exactly as
    draw_driveways does, and that alignment's heading gives flow direction, so
    "upstream" is the end the 10ft first part is measured from. max_offset:
    only pads within this many feet of an alignment are served.

    dashboard: REQUIRED id of the dashboard to work through — the user assigns
    its drawing. layer: layer to draw on.

    Reports how many pads were drawn and, for any that were not, why."""
    file = _file_for(dashboard)

    raw = _get("/modelspace", file)
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    if isinstance(data, dict) and "error" in data:
        return raw

    # Pads and lot lines are normally XREFED in, so model space holds only the
    # xref's block reference — merge the nested geometry the same way.
    nested = _get_nested(file, [*driveways.PAD_LAYER_SUFFIXES, driveways.ROAD_LAYER,
                                lot_line_layer])
    for category in ("polylines", "lines", "arcs"):
        if nested.get(category):
            data[category] = list(data.get(category) or []) + nested[category]

    try:
        payloads, summary = swales.plan(data, alignment, layer, offset, first_leg,
                                        swales.LEG_ANGLE, lot_line_layer, max_offset)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    for payload in payloads:
        _post(payload, file)
    return json.dumps(summary)


@mcp.tool()
@observed
def classify_image(path: str) -> str:
    """Classifies an image on disk and returns a short label + description.
    Only call when the user explicitly asks you to classify or identify an
    image and gives you a file path (e.g. an image uploaded via the dashboard,
    whose path is shown in its UI)."""
    try:
        import anthropic
    except ImportError:
        return '{"error": "The anthropic package is not installed. Run: pip install anthropic"}'

    try:
        with open(path, "rb") as f:
            image_bytes = f.read()
    except OSError as e:
        return json.dumps({"error": f"Could not read image at '{path}': {e}"})

    ext = os.path.splitext(path)[1].lower().lstrip(".")
    media_type = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext or 'jpeg'}"

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return '{"error": "ANTHROPIC_API_KEY is not set."}'

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": media_type,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }},
                {"type": "text", "text": (
                    "Classify the main subject of this image. Respond with ONLY a JSON object "
                    'of the form {"label": "<one or two words>", "description": "<one sentence>"} '
                    "and nothing else."
                )},
            ],
        }],
    )
    text = response.content[0].text.strip()
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        return json.dumps({"label": text[:60], "description": text})


# @mcp.tool()
# def delete_objects(handles: list[str]) -> str:
#     """Permanently deletes objects from the drawing by their handle strings.
#     Handles are found in the get_model_space output.
#     Only call when the user explicitly asks to delete or remove objects."""
#     return _post({
#         "intent":  "delete",
#         "objects": [{"object_handle": h, "object_type": "object"} for h in handles],
#     })

_DASHBOARD_PORT = 8765
_DASHBOARD_URL  = f"http://localhost:{_DASHBOARD_PORT}"

# Health is probed over HTTP rather than by a bare socket connect. A dashboard
# server left running from an older version of the code still holds the port and
# still accepts TCP, so a connect test reports it as fine -- while the browser
# gets routes that no longer exist and the UI comes up half-built (a stale server
# missing /blocks renders every dashboard with an empty block palette, silently).
# Point _HEALTH_PATH at a route the CURRENT server has and older ones don't.
_HEALTH_PATH = "/blocks"

# How long a server gets to answer before it counts as failed or stale. Covers
# uvicorn's own startup plus the sibling-process case described in _launch.
_STARTUP_WAIT = 15

# uploads/ is already gitignored and already holds the browser-open marker.
_LAUNCH_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "dashboard", "uploads", "launcher.log")


def _log(message: str) -> None:
    """The dashboard is launched detached with no console, so this file is the
    only place a launch failure can be seen. Best-effort, like everything here."""
    try:
        os.makedirs(os.path.dirname(_LAUNCH_LOG), exist_ok=True)
        with open(_LAUNCH_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{SESSION_ID}] {message}\n")
    except OSError:
        pass


def _dashboard_port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", _DASHBOARD_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _open_browser() -> bool:
    """Opens the dashboard, reporting whether it actually worked.

    webbrowser.open() signals failure by RETURNING False rather than raising, so
    a bare try/except around it treats a silent no-op as success. os.startfile is
    tried first on Windows because it raises a real OSError with a reason."""
    if os.name == "nt":
        try:
            os.startfile(_DASHBOARD_URL)
            return True
        except OSError as e:
            _log(f"os.startfile could not open {_DASHBOARD_URL}: {e}")

    try:
        if webbrowser.open(_DASHBOARD_URL):
            return True
        _log(f"webbrowser.open found no browser to open {_DASHBOARD_URL}")
    except Exception as e:
        _log(f"webbrowser.open failed for {_DASHBOARD_URL}: {e}")
    return False


def _dashboard_healthy() -> bool:
    """True only when the port is owned by a server serving the current routes."""
    try:
        response = requests.get(
            f"http://127.0.0.1:{_DASHBOARD_PORT}{_HEALTH_PATH}", timeout=1.0)
        return response.status_code == 200
    except requests.exceptions.RequestException:
        return False


def _wait_for_healthy(seconds: int = None) -> bool:
    """Polls until the dashboard serves the current routes, or the wait runs out.

    Generous on purpose: uvicorn binds the port well before it finishes starting,
    and this is the only thing separating "still coming up" from "stale"."""
    deadline = time.time() + (_STARTUP_WAIT if seconds is None else seconds)
    while time.time() < deadline:
        if _dashboard_healthy():
            return True
        time.sleep(0.25)
    return False


def _ensure_dashboard_running() -> None:
    """Best-effort: launch the observability dashboard in the background and
    open it in a browser tab, so the user never has to open a terminal.
    Runs on a daemon thread so it can never delay the MCP stdio handshake;
    if anything here fails, the CAD tools must still work normally."""
    def _launch():
        repo_dir = os.path.dirname(os.path.abspath(__file__))

        if not _dashboard_healthy():
            # Claude Desktop starts two of these processes about a second apart,
            # so the port is often already bound by a server the sibling process
            # started moments ago and that is still working through startup. A
            # bound port therefore does NOT mean the owner is stale -- it has to
            # be given time to answer before that conclusion is drawn, or the
            # second process writes off a perfectly good server and no tab opens.
            if _dashboard_port_open():
                if not _wait_for_healthy():
                    _log(f"Port {_DASHBOARD_PORT} is held by a process that never "
                         f"served {_HEALTH_PATH} -- most likely a dashboard server "
                         "left running from an older version of the code. Stop that "
                         "process and restart Claude Desktop.")
                    return
            else:
                try:
                    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                    os.makedirs(os.path.dirname(_LAUNCH_LOG), exist_ok=True)
                    with open(_LAUNCH_LOG, "a", encoding="utf-8") as log:
                        subprocess.Popen(
                            [sys.executable, "-m", "uvicorn", "dashboard.server:app",
                             "--port", str(_DASHBOARD_PORT), "--log-level", "warning"],
                            cwd=repo_dir,
                            # DEVNULL, not inherited: stdin here is the MCP stdio
                            # transport Claude Desktop talks over. Handing the
                            # dashboard a duplicate of it leaves that pipe held
                            # open by a process that never reads it.
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=log,  # a startup crash lands here, not in a void
                            creationflags=creationflags,
                        )
                except Exception as e:
                    _log(f"Could not start the dashboard server: {e}")
                    return  # dashboard is a nice-to-have; never break the CAD tools

                if not _wait_for_healthy():
                    _log(f"Dashboard server did not answer within {_STARTUP_WAIT}s; "
                         "not opening a tab.")
                    return  # never came up; don't open a dead tab

        # Open the UI so the user never has to type a URL: dashboards are created by
        # hand there, and nothing can be assigned a drawing until they do.
        #
        # Claude Desktop launches two cad_mcp_server.py processes about a second
        # apart and BOTH arrive here, so something has to pick one. The dashboard
        # server does it: /claim-browser runs on its single event loop with no
        # await between check and update, so exactly one process is granted the
        # open. This replaced a marker file, which could not do the job -- a
        # check-then-write test is not atomic, and both processes could pass it
        # before either wrote, opening two windows.
        try:
            granted = requests.post(
                f"{dashboard_events.DASHBOARD_BASE}/claim-browser", timeout=3
            ).json().get("open", False)
        except Exception as e:
            _log(f"Could not ask the dashboard which process should open a tab: {e}")
            return
        if not granted:
            return  # the sibling process is opening it

        if not _open_browser():
            # Give the claim back rather than letting the pair go silent: the
            # sibling arriving a second later can then try instead.
            try:
                requests.post(f"{dashboard_events.DASHBOARD_BASE}/release-browser",
                              timeout=3)
            except Exception:
                pass

    threading.Thread(target=_launch, daemon=True).start()


if __name__ == "__main__":
    _ensure_dashboard_running()
    mcp.run()