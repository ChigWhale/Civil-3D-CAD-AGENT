import requests
import json, openpyxl, os
import base64, functools, hashlib, inspect, math, time
import socket, subprocess, sys, threading, webbrowser
from urllib.parse import quote
from collections import defaultdict
from mcp.server.fastmcp import FastMCP
import checks as checks_mod
import dashboard_events
import driveways
import exemplars
import playbooks
import pressure_run
import profiles
import sheets
import spatial
import spatial_cache
import swales
from dashboard_events import SESSION_ID, DashboardError, emit, resolve_file

mcp     = FastMCP("CAD Agent")
# 127.0.0.1, not "localhost": localhost resolves to IPv6 ::1 first on this machine
# and stalls ~2s per request before falling back to IPv4 (measured 2.06s vs 0.015s).
BASE    = "http://127.0.0.1:5199"
TIMEOUT = 30
HANDLER_TIMEOUT = 900   # compute/draw stages walk whole drawings; minutes, not seconds
_NO_CONNECTION = '{"error": "Civil 3D is not reachable — make sure AutoCAD is open with the CADAgent plugin loaded."}'

_SIZE_LIMIT = 950_000  # bytes — 50 KB buffer below the 1 MB MCP threshold

_ALL_CATEGORIES = [
    "lines", "arcs", "polylines", "alignments",
    "pipe_networks", "pressure_networks", "surfaces", "corridors", "corridor_lines",
    "feature_lines", "parcels", "cogo_points", "point_groups", "sample_lines",
    "catchments", "gradings", "intersections", "survey_figures", "view_frames",
    "profile_views", "section_views", "layouts",
    "blocks", "labels", "layers", "adjacency",
    # Entity types the general drafting tools (drafting_tools.py) create and edit.
    "circles", "ellipses", "splines", "points", "texts", "dimensions", "hatches",
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


def _post_raw(payload: dict, file: str | None = None) -> str:
    try:
        if file:
            payload = {**payload, "file": file}
        return requests.post(f"{BASE}/execute", json=payload, timeout=TIMEOUT).text
    except requests.exceptions.ConnectionError:
        return _NO_CONNECTION


# ── The gate around every write ──────────────────────────────────────────────
#
# Every payload that changes the drawing goes through here. Before it is posted,
# the proposed geometry is checked against the cached spatial index (spatial.py):
# a line through a pad, a text inside a lot pad, a box over the curb. Conflicts
# come back as a structured result with a token instead of being drawn through;
# the agent reads them, calls acknowledge_conflicts(token) if it means it, and
# repeats the call. After the write the index is rebuilt and compared with the
# one from before, so every write answers with what actually changed -- in the
# same cells and words it was planned in -- and flags any change outside the
# area the request should have touched.
#
# CAD_SPATIAL_GATE=0 / CAD_SPATIAL_VERIFY=0 switch either half off.

_GATE_ON = os.environ.get("CAD_SPATIAL_GATE", "1") != "0"
_VERIFY_ON = os.environ.get("CAD_SPATIAL_VERIFY", "1") != "0"

_HANDLE_KEYS = ("handles", "handle", "target", "tools", "objects", "source", "sources",
                "entities", "boundary", "path", "curve", "reference")


def _payload_handles(payloads: list) -> list:
    """Handles a request names, so cells they occupy count as planned."""
    out = []

    def walk(value):
        if isinstance(value, str):
            s = value.strip()
            if s and not s.startswith("@") and len(s) <= 16:
                out.append(s)
        elif isinstance(value, dict):
            for k, v in value.items():
                if k in _HANDLE_KEYS:
                    walk(v)
                elif k == "steps" and isinstance(v, list):
                    for step in v:
                        walk(step)
        elif isinstance(value, list):
            for v in value:
                if isinstance(v, dict) and "object_handle" in v:
                    walk(v["object_handle"])
                else:
                    walk(v)

    for p in payloads:
        if isinstance(p, dict):
            walk({k: v for k, v in p.items() if k != "intent"})
    return out


def _is_read_only(payload: dict) -> bool:
    intent = str(payload.get("intent") or "")
    if intent == "sequence":
        return all(_is_read_only(s) for s in payload.get("steps") or [] if isinstance(s, dict))
    return intent in spatial.READ_ONLY_INTENTS


def _blocked(conflicts: list, token: str, file: str | None) -> str:
    by_kind = {}
    for c in conflicts:
        by_kind[c["kind"]] = by_kind.get(c["kind"], 0) + 1
    return json.dumps({
        "ok": False,
        "blocked": True,
        "message": ("Not drawn: the proposed geometry conflicts with what is already in the "
                    "drawing. Read `conflicts`, decide, and if you still mean it call "
                    "acknowledge_conflicts(dashboard, token) then repeat this exact call. "
                    "Or change the request so it no longer conflicts."),
        "summary": by_kind,
        "conflicts": conflicts[:60],
        "conflict_count": len(conflicts),
        "token": token,
    })


def _index_before(file: str | None):
    """The pre-write index, or (None, reason) when it cannot be had."""
    try:
        return spatial_cache.get(file).index, None
    except spatial_cache.PluginError as e:
        return None, f"spatial check skipped: {e}"
    except Exception as e:                       # the gate must never take the write down
        return None, f"spatial check skipped: {e}"


def _verify_after(before, file: str | None, payloads: list, handles=None) -> dict:
    spatial_cache.invalidate(file)
    if before is None or not _VERIFY_ON:
        return {"verified": False, "note": "post-write check not run"}
    try:
        after = spatial_cache.get(file).index
    except Exception as e:
        return {"verified": False, "note": f"post-write check skipped: {e}"}
    try:
        return before.verify(after, payloads, handles or _payload_handles(payloads))
    except Exception as e:
        return {"verified": False, "note": f"post-write check failed: {e}"}


def _merge_result(text: str, extra: dict) -> str:
    """The plugin's own reply with `spatial` attached; legacy plain-text replies
    are wrapped so nothing is lost."""
    if text == _NO_CONNECTION:
        return text
    try:
        body = json.loads(text)
    except ValueError:
        body = {"result": text}
    if not isinstance(body, dict):
        body = {"result": body}
    body.update(extra)
    return json.dumps(body)


def _post_many(payloads: list, file: str | None = None, stop_on_error: bool = False) -> tuple:
    """Gate, post, verify -- for one payload or a whole planned batch (one index
    build before, one after, however many payloads). Returns (results, spatial)
    where results are the plugin's replies in order and spatial is the
    verification report, or the blocked result under "blocked"."""
    if not payloads:
        return [], {"verified": False, "note": "nothing to post"}
    if all(_is_read_only(p) for p in payloads):
        return [_post_raw(p, file) for p in payloads], None

    before, why = _index_before(file) if (_GATE_ON or _VERIFY_ON) else (None, "spatial check off")
    if before is not None and _GATE_ON:
        try:
            conflicts = before.conflicts(payloads)
        except Exception as e:
            conflicts, why = [], f"spatial check skipped: {e}"
        if conflicts:
            token = spatial_cache.conflict_token(conflicts)
            if not spatial_cache.acknowledged(file, token):
                return [], {"blocked": _blocked(conflicts, token, file)}
            spatial_cache.consume(file, token)

    results = []
    for p in payloads:
        text = _post_raw(p, file)
        results.append(text)
        if stop_on_error:
            try:
                body = json.loads(text)
                if isinstance(body, dict) and (body.get("error") or body.get("ok") is False):
                    break
            except ValueError:
                pass
    report = _verify_after(before, file, payloads)
    if why and isinstance(report, dict):
        report.setdefault("note", why)
    return results, report


def _post(payload: dict, file: str | None = None) -> str:
    results, report = _post_many([payload], file)
    if report and "blocked" in report:
        return report["blocked"]
    text = results[0] if results else _NO_CONNECTION
    return _merge_result(text, {"spatial": report}) if report is not None else text


# Compute-stage reports remembered per (drawing, handler, request) so the draw
# stage can be gated on the conflicts the compute stage found without running the
# (often minutes-long) compute twice.
_COMPUTES: dict = {}


def _handler_key(handler: str, request: dict, file: str | None) -> tuple:
    body = json.dumps(request, sort_keys=True, default=str)
    return (spatial_cache.drawing_key(file), handler, hashlib.sha1(body.encode()).hexdigest()[:16])


def _post_handler_raw(handler: str, stage: str, request: dict, file: str | None = None) -> str:
    try:
        payload = {"handler": handler, "stage": stage, "request": request}
        if file:
            payload["file"] = file
        return requests.post(f"{BASE}/handler", json=payload, timeout=HANDLER_TIMEOUT).text
    except requests.exceptions.ConnectionError:
        return _NO_CONNECTION


def _handler_conflicts(before, report: dict, request: dict) -> list:
    proposed = report.get("proposed") if isinstance(report, dict) else None
    if before is None or not proposed or not _GATE_ON:
        return []
    try:
        return before.conflicts([{"proposed": proposed, "layer": request.get("layer", "")}])
    except Exception:
        return []


def _post_handler(handler: str, stage: str, request: dict, file: str | None = None) -> str:
    """Dispatch one ICadObjectHandler stage (context | compute | draw) through the
    plugin /handler endpoint. The C# side computes and returns a structured JSON
    report -- conflicts included -- and never mutates the drawing unless stage is
    "draw" AND the report is clean.

    Around that, the spatial gate: a compute report that carries `proposed`
    geometry is checked against the index and answers with spatial_conflicts and
    a token; the draw stage is refused while those conflicts stand unacknowledged,
    and after it runs the index is rebuilt and the change reported under
    `spatial`."""
    if stage == "context":
        return _post_handler_raw(handler, stage, request, file)

    key = _handler_key(handler, request, file)
    before, why = _index_before(file) if (_GATE_ON or _VERIFY_ON) else (None, "spatial check off")

    if stage == "compute":
        text = _post_handler_raw(handler, stage, request, file)
        try:
            report = json.loads(text)
        except ValueError:
            return text
        if not isinstance(report, dict):
            return text
        conflicts = _handler_conflicts(before, report, request)
        token = spatial_cache.conflict_token(conflicts) if conflicts else None
        _COMPUTES[key] = (token, conflicts, time.time())
        if conflicts:
            report["spatial_conflicts"] = conflicts[:60]
            report["spatial_conflict_count"] = len(conflicts)
            report["conflict_token"] = token
            report["spatial_note"] = ("The proposed labels/geometry conflict with the drawing (see "
                                      "spatial_conflicts). apply=True will be refused until you call "
                                      "acknowledge_conflicts(dashboard, conflict_token) or change the request.")
        elif why:
            report["spatial_note"] = why
        return json.dumps(report)

    # draw / apply
    remembered = _COMPUTES.get(key)
    if remembered is None and before is not None and _GATE_ON:
        # No compute was run for this exact request: run one, so the gate has
        # something to judge. Its report is what the agent should have read.
        text = _post_handler_raw(handler, "compute", request, file)
        try:
            report = json.loads(text)
        except ValueError:
            report = None
        if isinstance(report, dict) and report.get("ok") is False:
            return text
        conflicts = _handler_conflicts(before, report, request) if isinstance(report, dict) else []
        token = spatial_cache.conflict_token(conflicts) if conflicts else None
        remembered = (token, conflicts, time.time())
        _COMPUTES[key] = remembered
    if remembered is not None:
        token, conflicts, _ = remembered
        if conflicts and _GATE_ON and not spatial_cache.acknowledged(file, token):
            return _blocked(conflicts, token, file)
        if token:
            spatial_cache.consume(file, token)

    text = _post_handler_raw(handler, stage, request, file)
    _COMPUTES.pop(key, None)
    try:
        report = json.loads(text)
    except ValueError:
        spatial_cache.invalidate(file)
        return text
    if not isinstance(report, dict):
        spatial_cache.invalidate(file)
        return text
    proposed = report.get("proposed")
    if proposed:
        report["spatial"] = _verify_after(before, file, [{"proposed": proposed, "layer": request.get("layer", "")}],
                                          handles=list(report.get("created") or []))
    else:
        spatial_cache.invalidate(file)      # inverts changed, plan geometry did not: nothing to diff
    return json.dumps(report)


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
      "surfaces", "corridors", "blocks", "layers", "adjacency",
      "circles", "ellipses", "splines", "points", "texts", "dimensions", "hatches"
    Every entity carries a "handle"; handles are what the drafting tools
    (select_entities, offset_entities, trim_entities, ...) take as input.

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
                      surfaces, corridors, blocks, layers, adjacency,
                      circles, ellipses, splines, points, texts, dimensions, hatches"""
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
               visible: bool | None = None, frozen: bool | None = None,
               locked: bool | None = None, plottable: bool | None = None,
               linetype: str | None = None, lineweight: str | float | None = None,
               rgb: list[int] | None = None, description: str | None = None,
               new_name: str | None = None) -> str:
    """Edits an existing layer: color (ACI index) or rgb [r,g,b], visible (on/off),
    frozen, locked, plottable, linetype (loaded from acad.lin if needed), lineweight
    (mm or "bylayer"), description, or new_name to rename it.
    Only call when the user asks to change a layer's properties. Use layer_state to
    change many layers at once and list_layers to see what exists.

    dashboard: REQUIRED id of the dashboard to work through. The drawing behind it
    is set by the user; call list_dashboards() if you are unsure which one."""
    payload = {"intent": "edit_layer", "name": name}
    for key, value in dict(color=color, visible=visible, frozen=frozen, locked=locked,
                           plottable=plottable, linetype=linetype, lineweight=lineweight,
                           rgb=rgb, description=description, new_name=new_name).items():
        if value is not None:
            payload[key] = value
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

    _, report = _post_many(payloads, file)
    if report and "blocked" in report:
        return report["blocked"]
    summary["spatial"] = report
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

    _, report = _post_many(payloads, file)
    if report and "blocked" in report:
        return report["blocked"]
    summary["spatial"] = report
    return json.dumps(summary)


# ── Grading sheet labels ─────────────────────────────────────────────────────
#
# Surface elevation / slope labels on the finished grade surface, worked out in
# the plugin from ordinary 2D linework (Commands/GradingLabels.cs). The plugin
# does every intersection and projection and returns a report; nothing here
# reasons about geometry.

GRADING_LABEL_KINDS = ("pad_fp", "lot_row", "lot_setback", "swale_meet", "swale_high",
                       "lot_corner", "curb_transition", "drive_curb", "drive_slope",
                       "swale_slope")


@mcp.tool()
@observed
def grading_label_context(dashboard: str, surface: str = "") -> str:
    """Read-only: the surfaces in the drawing -- data-shortcut references included,
    each flagged is_reference with reference_valid / reference_stale / source_exists
    -- plus surface_pick (the surface label_grading would use for `surface`, or
    "FG" when blank, or the reason it cannot choose), the surface elevation / slope
    label styles, marker styles and the drawing defaults by exact name, how many
    pieces of linework each grading layer contributes (model space and xrefs
    together), and how many surface labels already exist. Call this first to see
    which FG surface will be labelled, or why a layer is coming up empty.

    surface: optional name to test the pick with, exactly as label_grading takes it.
    dashboard: REQUIRED id of the dashboard whose drawing to read."""
    file = _file_for(dashboard)
    request = {"surface": surface} if surface else {}
    return _post_handler("grading_labels", "context", request, file)


@mcp.tool()
@observed
def label_grading(dashboard: str, apply: bool = False, surface: str = "",
                  kinds: list[str] | None = None, window: list[list[float]] | None = None,
                  layer: str = "0", fp_style: str = "", elevation_style: str = "",
                  tc_style: str = "", slope_style: str = "", marker_style: str = "",
                  existing_tolerance: float = 1.0, pad_reach: float = 60.0,
                  drive_curb_side: str = "right", background: bool = True) -> str:
    """Places the grading-sheet labels on the finished grade (FG) surface, one label
    per target point, from the drawing's 2D linework. Pads, lot lines, curb,
    driveways and swales are found by layer suffix in model space AND inside
    xrefs, so "x-plat (ph1a)|c-lots-line" counts as a lot line.

    Styles are optional. Left blank, every label takes the drawing's default
    surface spot elevation / slope label style (its Surface feature settings).
    Name a style to use it for that group; a name that does not exist is NOT an
    error -- the report warns and the default is used instead.

    Surface elevation labels (SEL):
      pad_fp          FP style     centre of each pad (c-lots-pad*)
      lot_row         elevation    lot line (c-lots-line) meets right of way (c-rway-line)
      lot_setback     elevation    lot line meets setback line (c-lots-stbk)
      swale_meet      elevation    rear swale (c-lots-fl) meets a lot line -- where neighbours' swales meet
      swale_high      elevation    highest FG elevation along each pad's swale
      lot_corner      elevation    lot line meets lot line away from the right of way (rear corners)
      curb_transition TC style     back of curb (c-road-back) changes between arc and straight
      drive_curb      TC style     where the driveway's flare arc lands on the back of curb
                                   (drive_curb_side: right (default, looking from pad to road), left, both)
    Surface slope labels (SSL, two-point, slope style):
      drive_slope     middle of the driveway's pad edge -> straight out to the right of way
      swale_slope     each swale bend -> straight back (away from the road) to the lot line; two per pad

    The plugin does the geometry; YOU read the report. Call with apply=False first:
      * labels=[...]   every point that would be labelled, with kind, style and pad;
      * skipped=[...]  targets left alone and why: "already labelled" (a surface
        label of the same type sits within existing_tolerance), no surface
        coverage, a driveway that never reaches the curb, a pad with no swale...;
      * warnings=[...] a requested style that was not found (with the names that
        exist) and the default used instead, layers that came up empty, pads with
        an odd number of driveway lines or swale bends;
      * styles={...}   per group: requested, used, and whether that is the default;
      * errors=[...]   nothing was placed: no FG surface, no linework at all.
    Then call again with apply=True to create every label in one transaction. Labels
    land on `layer` ("0") and carry a tag naming their kind, which space_labels uses
    to decide how to place them. Placement itself is not done here.

    surface: FG surface name; omit for the one surface whose name contains "FG".
    Surfaces brought in by data reference count -- they are surfaces in this
    drawing -- and the report's surface_info says if the one used is a reference
    and whether it is stale. Several "FG" matches are an error that lists them.
    kinds: subset of the kinds above; omit for all of them.
    window: [[xmin, ymin], [xmax, ymax]] to label one sheet's area only.
    fp_style / elevation_style / tc_style / slope_style / marker_style: optional
    names, matched exactly first, then as a unique case-insensitive substring
    ("TC" finds "Top Curb (TC)"); grading_label_context lists them and the defaults.

    background (default True): this command walks the whole drawing and takes
    minutes, longer than a chat tool call may stay open, so it is handed to the
    dashboard runner and this call returns at once with a run id. Read the
    report with run_results after wait_for_dashboards. background=False runs it
    inline (only for a small window). A window forces inline.

    dashboard: REQUIRED id of the dashboard whose drawing to work on."""
    if background and not window:
        params: dict = {"apply": bool(apply), "layer": layer,
                        "existing_tolerance": existing_tolerance, "pad_reach": pad_reach,
                        "drive_curb_side": drive_curb_side}
        if surface:
            params["surface"] = surface
        for key, value in (("fp_style", fp_style), ("elevation_style", elevation_style),
                           ("tc_style", tc_style), ("slope_style", slope_style),
                           ("marker_style", marker_style)):
            if value and value.strip():
                params[key] = value.strip()
        if kinds:
            unknown = [k for k in kinds if k not in GRADING_LABEL_KINDS]
            if unknown:
                return json.dumps({"error": f"unknown label kind(s) {unknown}; "
                                            f"known: {list(GRADING_LABEL_KINDS)}"})
            params["kinds"] = list(kinds)
        return _run_in_background(dashboard, "label_grading", params,
                                  "grading labels" + (" (apply)" if apply else " (report)"))

    file = _file_for(dashboard)
    request: dict = {
        "layer": layer,
        "existing_tolerance": existing_tolerance,
        "pad_reach": pad_reach,
        "drive_curb_side": drive_curb_side,
    }
    if surface:
        request["surface"] = surface
    for key, value in (("fp_style", fp_style), ("elevation_style", elevation_style),
                       ("tc_style", tc_style), ("slope_style", slope_style),
                       ("marker_style", marker_style)):
        if value and value.strip():
            request[key] = value.strip()
    if kinds:
        unknown = [k for k in kinds if k not in GRADING_LABEL_KINDS]
        if unknown:
            return json.dumps({"error": f"unknown label kind(s) {unknown}; "
                                        f"known: {list(GRADING_LABEL_KINDS)}"})
        request["kinds"] = list(kinds)
    if window:
        request["window"] = window
    return _post_handler("grading_labels", "draw" if apply else "compute", request, file)


@mcp.tool()
@observed
def space_labels(dashboard: str, apply: bool = False, handles: list[str] | None = None,
                 layer: str = "", types: str = "surface", window: list[list[float]] | None = None,
                 mode: str = "auto", kinds: list[str] | None = None, include_other: bool = False,
                 curb_layer: str | list[str] = "c-road-back", curb_tolerance: float = 1.0,
                 curb_side: str = "outside", centreline_layer: str = "c-rway-center",
                 pad_layers: list[str] | None = None, row_layer: str = "c-rway-line",
                 side_reach: float = 80.0, centreline_reach: float = 40.0,
                 include_dragged: bool = False, include_pinned: bool = False,
                 obstacle_layers: list[str] | None = None, ignore_layers: list[str] | None = None,
                 clearance: float = 0.5, cell_size: float = 0.0, label_gap: float = 0.0,
                 leader_lengths: list[float] | None = None, directions: list[float] | None = None,
                 allow_undragged: bool = True, weights: dict | None = None,
                 max_passes: int = 10, background: bool = True) -> str:
    """Puts grading labels where they belong, two treatments chosen per label:
      position  the finished-grade elevation labels (lot_row, lot_setback, swale_meet,
                swale_high, lot_corner) are dragged clear of linework, of each other
                and of each other's leaders;
      align     the top-of-curb labels (curb_transition, drive_curb) are rotated to
                follow the back of curb (curb_layer, a suffix or a list of suffixes)
                at their anchor and, when the style puts the text on the road side,
                flipped in place so the text sits on the outside of the curb. They are
                never dragged. Outside = toward the label's pad (its tagged pad, else the
                nearest on pad_layers within side_reach ft), else away from a
                centreline_layer line within centreline_reach ft (a road's own centreline
                is close; a rear easement's is not this curb's), else toward row_layer
                within side_reach. curb_side="keep" rotates only.
    Which is which comes from the kind tag label_grading writes on its labels; an
    untagged spot elevation is aligned when its anchor sits within curb_tolerance of
    the back of curb and positioned otherwise. Pad (pad_fp) and slope labels are
    outside the spec and are listed as skipped unless include_other=True. mode
    forces one treatment on everything selected; kinds limits the run to those tags.

    Positioning is greedy candidate placement with local-search repair, scored on a
    raster occupancy grid of the linework (model space AND xrefs, every visible layer
    unless obstacle_layers / ignore_layers say otherwise). Labels that are not being
    moved are stamped from their real exploded geometry: text, borders and markers
    block as text, their leaders and slope arrow lines block as leaders. The plugin
    does all of it and reports per label; nothing here reasons about geometry.

    Each label is tried undragged (at its anchor) and dragged in each of 8 directions
    at 1, 2, 3 and 4.5 text heights of leader. Every candidate is measured for real --
    the offset is applied and the extents read back -- so the text box is exactly what
    Civil 3D draws. Cost = linework under the text (x10) + other labels under the
    text (x25) + leaders through the text or this leader through other text (x25)
    + this leader across other leaders (x10) + linework under the leader (x1)
    + leader length in text heights (x1) + direction preference (x0.5), minus 2 for
    staying where it is. Labels are placed most-constrained first, each stamping its
    text box and leader as it goes, then repair passes revisit any that still overlap.
    label_gap: feet a placed text box must keep from other text and leaders (default 0).

    Call with apply=False first and read the report:
      * aligned / side_unresolved / clean / overlapping / leader_conflicts / moved /
        skipped counts, and labels=[...] with, per label, its mode and kind, then for
        aligned ones rotation_deg, curb_distance, side (outside / inside / centred /
        unknown), side_method (already / flipped / rotated_180 / none), outward_ref
        (pad / centreline / row) and outward_detail (what each reference measured:
        pad handle and side, centreline distance and side, row distance and side,
        the text's side, the curb tangent); for positioned ones the chosen offset
        (or undragged), its text box, line_hits, label_hits, leader_label_hits,
        leader_cross_hits and cost -- any hit count > 0 means it could not be made
        clear with these candidates (longer leader_lengths, more directions or a
        smaller clearance usually fix it);
      * skipped_dragged_by_kind / skipped_pinned_by_kind: labels left alone because
        they were already dragged or pinned, so "not rotated" is not "failed";
      * warnings: no linework in reach, no centreline / pad / right of way to tell
        the outside of the curb, fixed labels that would not explode, grid coarsened
        for a huge region (pass a window and work one sheet at a time), labels that
        could not be measured.
    Then apply=True writes every offset, rotation and flip in one transaction.
    Compute leaves the drawing untouched: every temporary measurement is rolled back.

    handles: explicit label handles (e.g. `created` from label_grading); otherwise
    every label matching types ("elevation", "slope", "surface" = both, "all" = any
    Civil 3D label), layer (suffix) and window is taken.
    include_dragged: also move labels somebody already dragged (default: leave them
    and treat them as obstacles). include_pinned: same for pinned labels.
    clearance: feet of padding around linework. cell_size: grid cell in feet
    (default a quarter of the text height). leader_lengths / directions (degrees, in
    preference order) override the candidate set. weights: {line, label, leader,
    length, direction, stay, leader_label, leader_leader}.

    background (default True): measuring every candidate for every label takes
    minutes, longer than a chat tool call may stay open, so the command is
    handed to the dashboard runner and this call returns at once with a run id.
    Read the report with run_results after wait_for_dashboards. Explicit
    handles, window, kinds, directions or weights force an inline call;
    background=False does too.

    dashboard: REQUIRED id of the dashboard whose drawing to work on."""
    if background and not (handles or window or kinds or directions or weights):
        params: dict = {"apply": bool(apply), "types": types, "mode": mode,
                        "include_other": include_other, "curb_layer": curb_layer,
                        "curb_side": curb_side, "centreline_layer": centreline_layer,
                        "row_layer": row_layer, "side_reach": side_reach,
                        "centreline_reach": centreline_reach,
                        "include_dragged": include_dragged, "include_pinned": include_pinned,
                        "clearance": clearance, "label_gap": label_gap, "max_passes": max_passes}
        if layer:
            params["layer"] = layer
        if pad_layers:
            params["pad_layers"] = list(pad_layers)
        if obstacle_layers:
            params["obstacle_layers"] = list(obstacle_layers)
        if ignore_layers:
            params["ignore_layers"] = list(ignore_layers)
        if cell_size and cell_size > 0:
            params["cell_size"] = cell_size
        if leader_lengths:
            params["leader_lengths"] = list(leader_lengths)
        return _run_in_background(dashboard, "space_labels", params,
                                  "label spacing" + (" (apply)" if apply else " (report)"))

    file = _file_for(dashboard)
    request: dict = {
        "types": types,
        "mode": mode,
        "include_other": include_other,
        "curb_layer": curb_layer,
        "curb_tolerance": curb_tolerance,
        "curb_side": curb_side,
        "centreline_layer": centreline_layer,
        "row_layer": row_layer,
        "side_reach": side_reach,
        "centreline_reach": centreline_reach,
        "include_dragged": include_dragged,
        "include_pinned": include_pinned,
        "clearance": clearance,
        "label_gap": label_gap,
        "allow_undragged": allow_undragged,
        "max_passes": max_passes,
    }
    if handles:
        request["handles"] = list(handles)
    if kinds:
        request["kinds"] = list(kinds)
    if layer:
        request["layer"] = layer
    if window:
        request["window"] = window
    if pad_layers:
        request["pad_layers"] = list(pad_layers)
    if obstacle_layers:
        request["obstacle_layers"] = list(obstacle_layers)
    if ignore_layers:
        request["ignore_layers"] = list(ignore_layers)
    if cell_size and cell_size > 0:
        request["cell_size"] = cell_size
    if leader_lengths:
        request["leader_lengths"] = list(leader_lengths)
    if directions:
        request["directions"] = list(directions)
    if weights:
        request["weights"] = weights
    return _post_handler("label_spacing", "draw" if apply else "compute", request, file)


@mcp.tool()
@observed
def pipe_network_context(dashboard: str, structure: str = "", network: str = "") -> str:
    """Read-only snapshot of a pipe network: every structure (rim, sump, surface
    elevation, connected pipes) and every pipe (from/to, diameter, inverts, slope,
    cover at both ends, rule-set minimum cover, and whether Civil 3D flags a cover
    violation). Nothing is modified.

    structure: name or hex handle of a structure -- its network is used and the
    structure itself is described in detail. network: name or handle, used when no
    structure is given. With neither, the first network in the drawing is used.

    dashboard: REQUIRED id of the dashboard whose drawing to read."""
    file = _file_for(dashboard)
    request = {}
    if structure:
        request["structure"] = structure
    if network:
        request["network"] = network
    return _post_handler("pipe_elevation", "context", request, file)


@mcp.tool()
@observed
def solve_pipe_elevations(dashboard: str, structure: str,
                          cover_minimum: float | None = None,
                          offset_mode: str = "MatchCrowns",
                          manual_drop: float = 0.0,
                          pipes: list[str] | None = None,
                          governing_pipe: str | None = None,
                          structure_invert_override: float | None = None,
                          max_depth_below_surface: float = 50.0,
                          slope_units: str = "auto",
                          apply: bool = False) -> str:
    """Solve (and optionally set) the start/end invert elevations of the
    same-diameter pipes meeting at one structure so that minimum cover from the
    network reference surface holds along each pipe (full length) and the pipes
    keep the requested relationship at the structure.

    The plugin does the geometry; YOU do the reasoning about conflicts. Call with
    apply=False first and read the report:
      * ok=true, conflicts=[]  -> safe; call again with apply=True to write all
        pipes in one transaction (all-or-nothing, verified against the Civil 3D
        cover check before commit).
      * conflicts=[...]        -> the solver refused to guess. Each conflict names
        the pipe, both constraints, the delta, and usually a
        suggestedStructureInvert; pass that as structure_invert_override (or pick
        a governing_pipe, relax cover_minimum, change offset_mode) and re-run.
      * errors=[...]           -> precondition failed (mixed diameters, no reference
        surface, no surface coverage at a pipe end, implausible depth). Fix the
        drawing or the inputs; nothing was changed.

    structure: name or hex handle of the shared manhole/junction (from
    get_model_space or pipe_network_context).
    cover_minimum: feet from surface to the OUTSIDE top of the pipe. Omit to use
    the rule set of each pipe (Cover And Slope / Cover Only rule).
    offset_mode: "MatchCrowns" (default), "MatchInverts", or "ManualDrop" with
    manual_drop = feet the outgoing invert sits below the lowest incoming invert.
    pipes: optional explicit pipe names/handles; omit to auto-discover every
    connected pipe of the dominant diameter (others are listed under warnings).
    governing_pipe: pipe that fixes the datum at the structure; omit for the most
    restrictive cover. structure_invert_override: explicit invert for the governing
    pipe at the structure. slope_units: "auto" (calibrated against Pipe.Slope),
    "ratio", or "percent" for how rule-set slope values are read.

    dashboard: REQUIRED id of the dashboard whose drawing to work on."""
    file = _file_for(dashboard)
    request: dict = {
        "structure": structure,
        "offsetMode": offset_mode,
        "manualDrop": manual_drop,
        "maxDepthBelowSurface": max_depth_below_surface,
        "slopeUnits": slope_units,
    }
    if cover_minimum is not None:
        request["coverMinimum"] = cover_minimum
    if pipes:
        request["pipes"] = pipes
    if governing_pipe:
        request["governingPipe"] = governing_pipe
    if structure_invert_override is not None:
        request["structureInvertOverride"] = structure_invert_override
    return _post_handler("pipe_elevation", "draw" if apply else "compute", request, file)


# ── Spatial reasoning ────────────────────────────────────────────────────────
#
# A grid index over the drawing (spatial.py) rendered into words, so the agent
# can orient on a whole site in a few hundred tokens and only reach for handles
# once it has reasoned its way to an area. The index is cached per drawing
# (spatial_cache.py) and rebuilt only when the plugin's change counter moves,
# the vocabulary changes, or a write goes through this process. Nothing here
# draws.


def _index_for(dashboard: str):
    return spatial_cache.get(_file_for(dashboard)).index


@mcp.tool()
@observed
def spatial_map(dashboard: str) -> str:
    """The drawing as a coarse character map, north up, plus its regions.

    Read this FIRST to orient: one glyph per 200ft cell (legend printed with the
    map: curb, pads, lot lines, driveways/swales, utilities ...) with the x/y of
    the top-left, then every corridor (one per alignment) and block with what
    it holds, and the busiest layers the vocabulary has no word for yet (name
    them with define_tag). A whole subdivision fits in a few hundred tokens.
    Drill in with describe_location and find_entities; nothing here draws."""
    try:
        index = _index_for(dashboard)
    except Exception as e:
        return json.dumps({"error": str(e)})
    text = index.site_map() + "\n\n" + index.summary()
    if len(text) > _MAP_MAX_CHARS:          # never hand the chat something it cannot hold
        text = text[:_MAP_MAX_CHARS] + "\n... (map truncated; ask describe_location for detail)"
    return text


_MAP_MAX_CHARS = 60_000


@mcp.tool()
@observed
def list_tags(dashboard: str) -> str:
    """The vocabulary the spatial tools speak. Most layers name themselves from
    the words in their names (layer_words.json): C-WATR-HYDR -> water_hydrant in
    family water, V- layers are existing, -FUTR future. Hand rules
    (layer_tags.json) win over that, and a person can override one family in
    layer_tags.learned.json (edit_learned). This returns the precedence, the
    hand tags, the families, the dictionary and learned-file summaries, and --
    for this dashboard's drawing -- how its layers were named: counts by
    source, the unverified auto-names (no known word in them; correct with
    edit_learned or teach the abbreviation with rename_word), and the layers
    nothing could name. Tags or families are the words describe_location,
    find_entities and checks use."""
    out = spatial.TAGS.describe()
    try:
        index = _index_for(dashboard)
        out["auto_named"] = index.derivation_stats()
        out["unmatched_linework"] = [{"layer": layer, "feet": round(ft, 1)}
                                     for layer, ft in index.unmatched.most_common(25)]
        out["type_tagged_points"] = [{"layer": layer, "count": n}
                                     for layer, n in index.unmatched_points.most_common(25)]
    except Exception as e:
        out["drawing_note"] = str(e)
    return json.dumps(out)


@mcp.tool()
@observed
def explain_layer(layer: str, dashboard: str = "") -> str:
    """Why a layer name gets the tag it gets: the classification (tag, family,
    state, source, confidence), the word-by-word derivation, the learned entry
    for its family if any, and the hand rule that won if one did. With a
    dashboard, adds what the drawing holds on that layer."""
    out = spatial.TAGS.explain(layer)
    if dashboard:
        try:
            index = _index_for(dashboard)
            out["in_drawing"] = index.layer_info.get(layer) or index.layer_info.get(str(layer).lower())
        except Exception as e:
            out["drawing_note"] = str(e)
    return json.dumps(out)


@mcp.tool()
@observed
def rename_word(abbr: str, word: str, kind: str = "major", tag_key: str = "", glyph: str = "") -> str:
    """Teaches the dictionary an abbreviation, for every layer that uses it,
    now and in future drawings. kind: major (a category such as WATR -> water;
    tag_key is the family word, default the word itself; glyph paints the
    family on the map), minor (a part such as HYDR -> hydrant), state (a token
    that sets existing / future / demolition / abandoned / temporary),
    discipline (a leading letter), or annotation (a minor that marks text).
    Saved to layer_words.json; every cached index is rebuilt on its next use.
    Returns the learned families whose names change as a result."""
    try:
        before = {k: spatial.TAGS.classify(e.get("example") or k, record=False).tag
                  for k, e in spatial.TAGS.learned.entries.items() if not e.get("edited")}
        out = spatial.TAGS.rename_word(abbr, word, kind=kind, tag_key=tag_key, glyph=glyph)
    except (ValueError, OSError) as e:
        return _err(e)
    spatial_cache.invalidate_all()
    changed = []
    for k, e in spatial.TAGS.learned.entries.items():
        if e.get("edited"):
            continue
        now = spatial.TAGS.classify(e.get("example") or k, record=False).tag
        if now != before.get(k):
            changed.append({"family": k, "was": before.get(k), "now": now})
    out.update({"vocabulary_version": spatial.TAGS.version, "renamed_families": changed[:40]})
    return json.dumps(out)


@mcp.tool()
@observed
def edit_learned(family: str, tag: str = "", family_of: str = "", state: str = "", note: str = "") -> str:
    """Overrides the auto name of ONE layer family. family is the layer name
    without its xref prefix, numbers or FUTR, as listed by list_tags
    (e.g. "c-misc-stuff"). Give a new tag, the family word it belongs to, a
    state, or just a note; blank fields keep the derived value. The entry is
    marked edited in layer_tags.learned.json and wins over derivation from now
    on. Use rename_word instead when an abbreviation is wrong everywhere."""
    try:
        entry = spatial.TAGS.edit_learned(family, tag=tag or None, family=family_of or None,
                                          state=state or None, note=note if note else None)
    except (ValueError, OSError) as e:
        return _err(e)
    spatial_cache.invalidate_all()
    return json.dumps({"ok": True, "family": family, "entry": entry, "vocabulary_version": spatial.TAGS.version})


@mcp.tool()
@observed
def define_tag(dashboard: str, tag: str, needle: str, match: str = "contains",
               on: str = "layer", glyph: str = "", priority: int = 80,
               persist: bool = True, family: str = "") -> str:
    """Teaches the spatial index a word. From now on an entity whose layer name
    (on="layer"), block name (on="name") or type (on="type": line, arc,
    polyline, block, text, point, circle, dimension, pipe, structure,
    feature_line, label) matches `needle` by `match` (contains | endswith |
    startswith | equals | regex, case-insensitive) is tagged `tag`, shows on the
    site map as `glyph` (one character, optional) and can be asked for by
    find_entities. Lower priority wins over higher; existing rules sit at 10-90.
    An endswith/equals layer rule also joins the xref merge list. persist=True
    saves the rule to layer_tags.json for every later session. Every cached
    index is rebuilt on its next use, so call spatial_map again to see the
    effect. Use list_tags first to see the busiest unnamed layers."""
    try:
        rule = spatial.TAGS.define(tag, needle, match=match, on=on, glyph=glyph,
                                   priority=priority, persist=persist, family=family)
    except (ValueError, OSError) as e:
        return json.dumps({"error": str(e)})
    spatial_cache.invalidate_all()
    return json.dumps({"ok": True, "rule": rule, "vocabulary_version": spatial.TAGS.version,
                       "saved": bool(persist), "tags": spatial.TAGS.tags()})


@mcp.tool()
@observed
def acknowledge_conflicts(dashboard: str, token: str, reason: str = "") -> str:
    """Accepts the conflicts a blocked write reported, so the SAME call succeeds
    when repeated. A write (any draw/modify tool, label_grading or space_labels
    with apply=True, draw_driveways ...) is refused when its proposed geometry
    crosses existing linework, lands inside a pad, or sits too close to
    something; the refusal carries `conflicts` and a `token`. Only call this
    after reading the conflicts and deciding they are acceptable -- say why in
    `reason`; it is logged with the decision. The acknowledgement is good for
    15 minutes and for that exact set of conflicts; change the request and the
    conflicts change with it."""
    file = _file_for(dashboard)
    token = str(token or "").strip()
    if not token:
        return json.dumps({"error": "token is required; it comes from the blocked result"})
    spatial_cache.acknowledge(file, token)
    return json.dumps({"ok": True, "token": token, "reason": reason,
                       "note": "Repeat the blocked call now, unchanged."})


# ── Driving dashboards from the chat ─────────────────────────────────────────
#
# The free worker: a dashboard runs a sequence of blocks on its own drawing as
# its own background task, gated and verified by the spatial index, with no
# model in the loop. The chat composes the sequence (spatial_map, then
# list_blocks, then run_on_dashboard) and reads back later. Claude Desktop
# only acts inside a turn and cannot be pinged, so everything here is a pull:
# a digest per dashboard, the events since a cursor, and an attention queue of
# the things that need a decision. wait_for_dashboards blocks briefly so a
# pull can look like a push.

_DASH_TIMEOUT = 10
_cursor = {"seen": -1}       # last event sequence this process has reported


def _dash_get(path: str, params: dict | None = None, timeout: float = _DASH_TIMEOUT):
    try:
        r = requests.get(f"{dashboard_events.DASHBOARD_BASE}{path}", params=params, timeout=timeout)
    except requests.exceptions.RequestException as e:
        return {"error": f"The dashboard server is not reachable ({e.__class__.__name__}). "
                         "Ask the user to open the dashboard at http://localhost:8765."}
    try:
        body = r.json()
    except ValueError:
        return {"error": f"The dashboard server answered HTTP {r.status_code} without JSON."}
    if r.status_code >= 400 and isinstance(body, dict) and "error" not in body:
        body["error"] = f"HTTP {r.status_code}"
    return body


def _dash_post(path: str, body: dict | None = None, timeout: float = _DASH_TIMEOUT):
    try:
        r = requests.post(f"{dashboard_events.DASHBOARD_BASE}{path}", json=body or {}, timeout=timeout)
    except requests.exceptions.RequestException as e:
        return {"error": f"The dashboard server is not reachable ({e.__class__.__name__}). "
                         "Ask the user to open the dashboard at http://localhost:8765."}
    try:
        out = r.json()
    except ValueError:
        return {"error": f"The dashboard server answered HTTP {r.status_code} without JSON."}
    if r.status_code >= 400 and isinstance(out, dict) and "error" not in out:
        out["error"] = f"HTTP {r.status_code}"
    return out


def _compact_block(spec: dict, with_help: bool) -> dict:
    fields = []
    for f in spec.get("fields") or []:
        c = {"name": f["name"], "type": f.get("type", "text")}
        if "default" in f:
            c["default"] = f["default"]
        elif f.get("optional"):
            c["optional"] = True
        for k in ("options", "min_items", "help"):    # specs say "options"; "choices" never existed
            if k in f and f[k] not in (None, "", []):
                c[k] = f[k]
        fields.append(c)
    out = {"type": spec["type"], "group": spec.get("group"), "description": spec.get("description"),
           "fields": fields}
    if with_help and spec.get("help"):
        out["help"] = str(spec["help"])[:600]
    return out


@mcp.tool()
@observed
def list_blocks(dashboard: str, group: str = "", block_type: str = "", with_help: bool = False) -> str:
    """The blocks a dashboard run can be built from, with their parameters.
    This is the full set; the dashboard UI's "Add block" menu offers only the
    Generated group (the primitive draw / modify / layer blocks are quicker by
    hand), but every block listed here runs through run_on_dashboard and shows
    up in the lane once queued. A block is {"type": ..., "params":
    {...}, "repeat": n}; params use the field names listed here (a "points"
    field takes [[x,y],...], "handles" a list of handle strings, "multichoice"
    a list of names). Generated blocks (draw_driveways, draw_rear_swales,
    label_grading, space_labels, cut_sheets, read_key_map, cut_grading_sheets,
    the profile blocks) read the drawing when they run, so they need no
    coordinates. group narrows the list (Geometry, Generated, Layers, Draw,
    Modify, Annotate, Query ...); block_type returns one block with its full
    help. Read spatial_map for the dashboard first, then compose, then call
    run_on_dashboard."""
    body = _dash_get("/blocks")
    if "error" in body:
        return json.dumps(body)
    specs = body.get("blocks") or []
    if block_type:
        specs = [s for s in specs if s.get("type") == block_type]
        with_help = True
    if group:
        specs = [s for s in specs if str(s.get("group", "")).lower() == group.lower()]
    groups = sorted({str(s.get("group")) for s in (body.get("blocks") or [])})
    return json.dumps({"count": len(specs), "groups": groups, "max_repeat": body.get("max_repeat"),
                       "blocks": [_compact_block(s, with_help) for s in specs]})


@mcp.tool()
@observed
def run_on_dashboard(dashboard: str, blocks: list[dict], on_conflict: str = "pause",
                     label: str = "", checks: str = "") -> str:
    """Commits a sequence of blocks to a dashboard and sets it running on that
    dashboard's drawing, in the background, with no model in the loop. Returns
    at once with the run id; the run continues after this chat turn ends and
    after the user walks away. Several dashboards can run at the same time,
    one run each.

    blocks: [{"type": "draw_driveways", "params": {"alignment": ["RD-1"],
    "width": 20}}, {"type": "draw_rear_swales", "params": {...}}, {"type":
    "label_grading", "params": {"apply": true}}] -- see list_blocks. Blocks run
    top to bottom; generated blocks read the drawing as it is when their turn
    comes, so driveways then swales then labels in one run works.

    on_conflict: what the run does when the pre-run check finds its proposed
    geometry crossing linework or landing inside a pad. "pause" (default,
    for unattended runs): draw nothing, put the conflicts on the attention
    queue and wait for resume_dashboard or stop_dashboard. "warn": log it and
    draw anyway (what the browser's Run button does). "stop": fail the run.
    After the run the index is rebuilt and the change verified; a change
    outside the planned area also lands on the attention queue.

    checks: a plan type from list_playbooks (e.g. "water"). After the run draws,
    that playbook's acceptance checks are evaluated over what the run created;
    results land under the run's `checks` in dashboard_status, and a failed
    error-severity check queues an attention item of kind "checks". Nothing is
    undone. Leave blank for no checks.

    label: a few words naming the run, shown in status. dashboard: REQUIRED id
    of the dashboard; its drawing is set by the user."""
    if not isinstance(blocks, list) or not blocks:
        return json.dumps({"error": "blocks must be a non-empty list of {type, params, repeat}"})
    on_conflict = str(on_conflict or "pause").strip().lower()
    if on_conflict not in ("pause", "warn", "stop"):
        return json.dumps({"error": "on_conflict must be pause, warn or stop"})
    request = {"blocks": blocks, "policy": {"on_conflict": on_conflict}, "label": label, "source": "desktop"}
    if str(checks or "").strip():
        request["checks"] = str(checks).strip()
    body = _dash_post(f"/dashboards/{dashboard}/run", request)
    if "error" not in body:
        body["note"] = ("Running in the background. Check dashboard_status later, or call "
                        "wait_for_dashboards to hold until it needs a decision or finishes.")
    return json.dumps(body)


@mcp.tool()
@observed
def dashboard_status(dashboard: str = "", since: int = -2) -> str:
    """What every dashboard is doing, in a few lines each: idle or the current
    run's status (checking, paused, running, finished, failed, stopped), steps
    done of total, conflicts found, the post-run verification, and any
    decisions taken. Plus `attention`: the open items that need a decision (a
    run paused on conflicts, a failed run, a change outside the planned area)
    with ids for resume_dashboard / resolve_attention, and `events` since the
    last time this was asked (pass since=-1 for the full recent history, or a
    cursor to continue from). dashboard narrows to one. Nothing here can be
    pushed to you -- ask when you want to know."""
    cursor = _cursor["seen"] if since == -2 else since
    params = {"since": cursor}
    if dashboard:
        params["dashboard"] = dashboard
    body = _dash_get("/status", params)
    if "error" in body:
        return json.dumps(body)
    _cursor["seen"] = max(_cursor["seen"], int(body.get("cursor") or -1))
    return json.dumps(_trim_status(body))


def _trim_status(body: dict) -> dict:
    """The digest without the parts a chat never needs to see whole."""
    out = dict(body)
    out["events"] = [{k: v for k, v in e.items() if k in ("seq", "type", "dashboard", "run", "message",
                                                           "error", "ok", "attention", "kind", "completed",
                                                           "total", "tool", "_ts")}
                     for e in body.get("events") or []][-60:]
    out["attention"] = [{k: v for k, v in i.items() if k != "detail"} | {"detail": _preview(i.get("detail"), 600)}
                        for i in body.get("attention") or []]
    return out


@mcp.tool()
@observed
def wait_for_dashboards(dashboard: str = "", timeout: float = 40.0) -> str:
    """Holds until a run pauses, fails, finishes or stops, or an attention item
    appears -- or until `timeout` seconds pass (capped at 45, so the call never
    hangs). Returns the same digest as dashboard_status plus `woke`, the events
    that ended the wait, and `timed_out`. Call it again to keep waiting. Use it
    right after run_on_dashboard when the user wants to watch; otherwise let
    the runs go and use dashboard_status when they ask."""
    params = {"since": _cursor["seen"], "timeout": max(1.0, min(float(timeout), 45.0))}
    if dashboard:
        params["dashboard"] = dashboard
    body = _dash_get("/wait", params, timeout=params["timeout"] + 10)
    if "error" in body:
        return json.dumps(body)
    _cursor["seen"] = max(_cursor["seen"], int(body.get("cursor") or -1))
    return json.dumps(_trim_status(body))


@mcp.tool()
@observed
def run_results(dashboard: str, step: int = 0) -> str:
    """The full plugin reply for each step of the dashboard's latest run: the
    label_grading / space_labels reports (compute and apply stages), created
    handles, per-step errors. dashboard_status only previews these. step
    narrows to one step (1-based); 0 returns all, each reply parsed as JSON
    when it is JSON."""
    params = {"step": step} if step and step > 0 else None
    body = _dash_get(f"/dashboards/{dashboard}/results", params)
    if "error" in body:
        return json.dumps(body)
    budget = 90_000
    out = []
    for r in body.get("results") or []:
        text = r.get("result") or ""
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = text
        entry = {k: v for k, v in r.items() if k != "result"}
        entry["result"] = parsed
        blob = json.dumps(entry)
        if len(blob) > budget:
            entry["result"] = _preview(parsed, max(2000, budget))
            entry["truncated"] = True
            blob = json.dumps(entry)
        budget -= len(blob)
        out.append(entry)
        if budget <= 0:
            out.append({"note": "further results omitted for size; ask for one step at a time"})
            break
    return json.dumps({"run": body.get("run"), "results": out})


def _run_in_background(dashboard: str, block_type: str, params: dict, label: str) -> str:
    """Hands a slow command to the dashboard runner as a one-block run and
    returns at once, so the chat never holds a tool call open for minutes.
    The report comes back through run_results."""
    body = _dash_post(f"/dashboards/{dashboard}/run",
                      {"blocks": [{"type": block_type, "params": params}],
                       "policy": {"on_conflict": "pause"}, "label": label, "source": "desktop"})
    if "error" in body:
        return json.dumps(body)
    body["background"] = True
    body["note"] = ("Started on the dashboard; this call does not wait. Call "
                    "wait_for_dashboards (or dashboard_status later), then run_results to read "
                    "the report. With apply=True the run pauses if the proposed labels conflict "
                    "with the drawing; resume_dashboard lets it go ahead.")
    return json.dumps(body)


@mcp.tool()
@observed
def resume_dashboard(dashboard: str, reason: str) -> str:
    """Lets a run that paused on conflicts go ahead and draw. Only call after
    reading the conflicts in dashboard_status (attention item of kind
    "conflicts") and deciding they are acceptable; `reason` is recorded with
    the decision. To change the plan instead, stop_dashboard and run a new
    sequence."""
    reason = str(reason or "").strip()
    if not reason:
        return json.dumps({"error": "reason is required; it is logged with the decision"})
    return json.dumps(_dash_post(f"/dashboards/{dashboard}/resume", {"reason": reason, "by": "desktop"}))


@mcp.tool()
@observed
def stop_dashboard(dashboard: str) -> str:
    """Stops a dashboard's current run -- paused or running. Steps already
    posted stay in the drawing; the post-run verification still reports what
    they changed."""
    return json.dumps(_dash_post(f"/dashboards/{dashboard}/stop"))


@mcp.tool()
@observed
def resolve_attention(item: str, note: str) -> str:
    """Clears an attention item once it has been dealt with -- a failed run
    that was re-run, an unexpected change that was checked and is fine.
    Paused runs clear themselves on resume or stop; use this for the others.
    `note` says what was decided and is kept with the item."""
    note = str(note or "").strip()
    if not note:
        return json.dumps({"error": "note is required"})
    return json.dumps(_dash_post(f"/attention/{item}/resolve", {"note": note}))


# ── Exemplars and playbooks: representation -> generalization ────────────────
#
# A finished plan of one type is exported as an exemplar bundle (exemplars.py):
# the snapshot split into the base a designer starts from (playbooks/<type>/
# base.json, decided by people) and the delta they drew, both as index cells in
# words, plus a cell-by-cell diff and relations (how far each delta tag sits
# from what was there, where along the alignments, how often). Bundles hold
# client geometry and live under CAD_EXEMPLAR_DIR, outside the repo. From the
# bundles of one type, playbook_brief builds a bounded, coordinate-free digest;
# the chat writes the playbook (markdown + checks) and saves it with
# save_playbook; dashboard runs then evaluate those checks after they draw.


def _err(e) -> str:
    return json.dumps({"error": str(e)})


@mcp.tool()
@observed
def export_exemplar(dashboard: str, plan_type: str, name: str = "", notes: str = "",
                    overwrite: bool = False) -> str:
    """Exports the dashboard's drawing as an exemplar bundle of `plan_type`
    (a folder under playbooks/ with a base.json; see list_playbooks). Reads the
    drawing, splits it into base and delta by that type's base spec, and writes
    snapshot, base, delta, index cells in words for both, the diff, relations
    and the unmatched-layer list to CAD_EXEMPLAR_DIR/<type>/<name>/. Read-only
    on the drawing. Returns the manifest summary plus the busiest delta layers
    the vocabulary has no word for: name those with define_tag and re-export,
    so the bundle is written in the right words. name defaults to the drawing
    stem plus a timestamp; overwrite=True replaces an existing bundle."""
    try:
        file = _file_for(dashboard)
        m = exemplars.export_drawing(file, plan_type, name=name, notes=notes, overwrite=overwrite, base_url=BASE)
    except (exemplars.ExemplarError, playbooks.PlaybookError) as e:
        return _err(e)
    out = {k: m.get(k) for k in ("plan_type", "name", "path", "revision", "vocabulary_version", "alignments",
                                 "counts", "reasons", "cells", "delta_tags", "delta_handles",
                                 "unmatched_delta_layers")}
    if out.get("unmatched_delta_layers"):
        out["hint"] = ("Layers under unmatched_delta_layers landed in the delta with an unverified name: "
                       "either nothing in the layer name was recognisable, or the auto name (shown as tag) "
                       "was built from unknown words. Correct one with edit_learned, teach an abbreviation "
                       "with rename_word, or pin a hand rule with define_tag, then export again with "
                       "overwrite=True. Everything else was named from its words automatically.")
    return json.dumps(out)


@mcp.tool()
@observed
def list_exemplars(plan_type: str = "") -> str:
    """The exemplar bundles on disk (all types, or one): name, drawing, export
    time, revision, vocabulary version, alignments, cell counts, delta tags."""
    try:
        return json.dumps({"root": exemplars.exemplar_root(), "bundles": exemplars.list_bundles(plan_type)})
    except exemplars.ExemplarError as e:
        return _err(e)


@mcp.tool()
@observed
def read_exemplar(plan_type: str, name: str, part: str = "summary", max_chars: int = 30000) -> str:
    """One part of a bundle, for drilling in while writing a playbook. part:
    manifest, summary, site_map, base_summary, base_site_map, relations,
    unmatched, diff, cells, base_cells. Text is cut at max_chars on a line."""
    try:
        return exemplars.read_part(plan_type, name, part, max_chars)
    except exemplars.ExemplarError as e:
        return _err(e)


@mcp.tool()
@observed
def playbook_brief(plan_type: str, max_chars: int = 60000) -> str:
    """The digest of every exemplar of a plan type, bounded and free of
    absolute coordinates: the base spec in words, each exemplar's delta by tag
    (feet, points, region share, station and offset, side, nearest base and
    delta tags, spacing), a few before-and-after cells, the layers still
    unnamed, what holds across exemplars, the blocks that produce those tags,
    and the output contract. Read it, then write the playbook and call
    save_playbook. Use read_exemplar for detail on one bundle."""
    palette = None
    pal = _dash_get("/blocks")
    if "error" not in pal:
        palette = pal.get("blocks") or []
    try:
        return exemplars.build_brief(plan_type, max_chars=max_chars, palette=palette)
    except (exemplars.ExemplarError, playbooks.PlaybookError) as e:
        return _err(e)


@mcp.tool()
@observed
def save_playbook(plan_type: str, markdown: str, checks_json: str) -> str:
    """Saves a plan type's playbook: markdown with the required '## ' sections
    (Scope, Base, Steps, Rules of thumb, Checks, Vocabulary, Open questions)
    and checks_json, a JSON list of acceptance checks (see the output contract
    in playbook_brief). Both are validated first; errors name what is wrong.
    Writes playbooks/<type>/playbook.md and checks.json in the repo. Test the
    checks right away with check_drawing and calibrate_checks."""
    try:
        return json.dumps(playbooks.save_playbook(plan_type, markdown, checks_json))
    except playbooks.PlaybookError as e:
        return _err(e)


@mcp.tool()
@observed
def get_playbook(plan_type: str) -> str:
    """A plan type's playbook markdown, checks and base spec, as saved."""
    try:
        return json.dumps(playbooks.load_playbook(plan_type))
    except playbooks.PlaybookError as e:
        return _err(e)


@mcp.tool()
@observed
def list_playbooks() -> str:
    """Every plan type under playbooks/: whether it has a base spec, a playbook,
    how many checks, version and save time. A type with a base spec but no
    playbook is ready for export_exemplar and playbook_brief."""
    return json.dumps({"playbooks": playbooks.list_playbooks(),
                       "check_kinds": list(checks_mod.KINDS)})


# ── Sheets: layouts cut from model space, with furniture ──────────────────────
#
# A plan type's sheets.json (playbooks/<type>/) says what a sheet of that type
# looks like; sheets.py tiles the designer's work with viewport windows and
# turns each into one all-or-nothing plugin sequence. The cut runs on the
# dashboard like any other block, so it is verified and checked afterwards.


def _sheet_plan_summary(plan: dict) -> dict:
    return {k: plan[k] for k in ("plan_type", "scale", "scale_text", "twist_deg", "count", "grid", "extent",
                                 "extent_source", "subject", "tried", "warnings")}


@mcp.tool()
@observed
def get_sheet_spec(plan_type: str) -> str:
    """A plan type's sheet spec (playbooks/<type>/sheets.json) as saved, or -- when
    none is saved yet -- a template pre-filled from the exemplars' sheets
    (viewport rectangle, scales, overlap, furniture seen on every sheet), with
    the fields explained. Edit and save with save_sheet_spec."""
    try:
        spec = sheets.load_spec(plan_type)
        return json.dumps({"plan_type": plan_type, "saved": True, "path": sheets.spec_path(plan_type),
                           "spec": spec, "warnings": sheets.spec_warnings(spec)})
    except playbooks.PlaybookError as e:
        return _err(e)
    except sheets.SheetError as e:
        note = str(e)
    stats = []
    try:
        for b in exemplars.list_bundles(plan_type):
            try:
                stats.append(json.loads(exemplars.read_part(plan_type, b["name"], "sheets", 200_000)))
            except (exemplars.ExemplarError, ValueError):
                pass
    except exemplars.ExemplarError:
        pass
    spec = sheets.suggest_spec(plan_type, stats) if stats else sheets.default_spec(plan_type)
    return json.dumps({"plan_type": plan_type, "saved": False, "note": note,
                       "from_exemplars": len(stats), "spec": spec})


@mcp.tool()
@observed
def save_sheet_spec(plan_type: str, spec_json: str) -> str:
    """Saves a plan type's sheet spec (validated; errors name the field). Paper
    units are inches, model feet; scales are feet per inch (40 = 1" = 40').
    template.file/layout name the title-block layout to clone; viewport
    is where the window sits on that sheet; visible says what shows in it;
    furniture and tables carry a place and a condition each. Then plan_sheets."""
    try:
        return json.dumps(sheets.save_spec(plan_type, spec_json))
    except (sheets.SheetError, playbooks.PlaybookError) as e:
        return _err(e)


@mcp.tool()
@observed
def plan_sheets(dashboard: str, plan_type: str, scale: float = 0, max_sheets: int = 0,
                twist_deg: float | None = None, title: str = "") -> str:
    """Dry run of cut_sheets: reads the dashboard's drawing and says how many
    sheets it takes at which scale, what each window holds (in the index's
    words), which furniture and tables each sheet would get, which layers are
    frozen in the viewports, and what is missing (template file, block library).
    Nothing is written. scale in feet per inch (0 = pick from the spec),
    max_sheets 0 = the spec's, twist_deg None = the spec's."""
    try:
        spec = sheets.load_spec(plan_type)
        entry = spatial_cache.get(_file_for(dashboard))
        plan, payloads = sheets.plan_and_payloads(
            entry.index, entry.data, spec, plan_type, scale=scale or None, max_sheets=max_sheets or None,
            twist_deg=twist_deg, drawing=_file_for(dashboard), title=title)
    except (sheets.SheetError, playbooks.PlaybookError, spatial_cache.PluginError, DashboardError) as e:
        return _err(e)
    per_sheet = []
    for p, sh in zip(payloads, plan["sheets"]):
        steps = p["steps"]
        per_sheet.append({"layout": sh["layout"], "row": sh["row"] + 1, "col": sh["col"] + 1,
                          "holds": sh["tags"], "entities": len(sh["handles"]),
                          "furniture": [st.get("name") or "mtext" for st in steps if st["intent"] in ("insert_block", "draw_mtext")],
                          "tables": [st.get("title") or "table" for st in steps if st["intent"] == "create_table"],
                          "steps": len(steps)})
    vp = next((st for st in payloads[0]["steps"] if st["intent"] == "create_viewport"), {}) if payloads else {}
    return json.dumps({**_sheet_plan_summary(plan), "template": (spec.get("template") or {}).get("file") or "(plain layouts)",
                       "frozen_in_viewports": len(vp.get("freeze_layers") or []),
                       "words": sheets.describe_plan(entry.index, plan), "sheets": per_sheet,
                       "spec_warnings": sheets.spec_warnings(spec),
                       "next": "cut_sheets(dashboard, plan_type, ...) runs this on the dashboard"})


@mcp.tool()
@observed
def cut_sheets(dashboard: str, plan_type: str, scale: float = 0, max_sheets: int = 0,
               twist_deg: float | None = None, title: str = "", on_conflict: str = "pause") -> str:
    """Cuts the sheets on the dashboard, in the background: one all-or-nothing
    sequence per sheet (template layout cloned, viewport cut with the plan
    type's visible layers, title block filled, furniture placed, tables
    written). Runs like any block: gated, verified afterwards (the new viewport
    windows are planned cells), and the plan type's checks are evaluated when it
    has a playbook -- a `covered` check says nothing fell off the sheets. Call
    plan_sheets first to see the plan. Layouts that already exist with the
    same names make the sheet fail rather than overwrite it."""
    params = {"plan_type": plan_type, "twist_deg": twist_deg if twist_deg is not None else 0, "title": title}
    if scale:
        params["scale"] = scale
    if max_sheets:
        params["max_sheets"] = max_sheets
    checks = ""
    try:
        if any(p["plan_type"] == plan_type and p["checks"] for p in playbooks.list_playbooks()):
            checks = plan_type
    except Exception:
        pass
    return run_on_dashboard(dashboard, [{"type": "cut_sheets", "params": params}], on_conflict=on_conflict,
                            label=f"cut {plan_type} sheets", checks=checks)


# ── Grading sheets from a key map ─────────────────────────────────────────────
#
# A grading set's sheet windows are drawn in a separate key map drawing (a
# rectangle per sheet with its number inside, in the same coordinates as the
# plan). read_key_map keeps its cells for the dashboard; plan_grading_sheets
# and cut_grading_sheets pick cells by key, label each cell's window with the
# grading label commands and cut one sheet per cell.


def _trim_cells(record: dict) -> list:
    return [{k: c.get(k) for k in ("key", "center", "width", "height", "twist_deg", "row", "col", "corners")}
            for c in record.get("cells") or []]


@mcp.tool()
@observed
def read_key_map(dashboard: str, key_map_file: str = "", plan_type: str = "grading", cell_layers: str = "",
                 key_layers: str = "", snap: float = 0, key_pattern: str = "") -> str:
    """Reads the grading key map -- ANOTHER drawing, in the same coordinates as
    the dashboard's, whose model space holds a rectangle per sheet with the
    sheet's number written inside -- and keeps its cells for this dashboard, so
    plan_grading_sheets / cut_grading_sheets can pick them by key. A cell is a
    closed area of the linework (lines need not be closed polylines: endpoints
    are snapped, crossings split, open tails ignored) holding a key text; a key
    names the smallest area it sits in. Writes nothing; Civil 3D opens the key
    map drawing if it is not already open.

    key_map_file blank = key_map.file in playbooks/<plan_type>/sheets.json.
    cell_layers / key_layers: comma-separated layer name suffixes (blank = the
    spec's, else every layer). snap: feet of gap closed between lines (0 = the
    spec's, 0.5). key_pattern: regex the key text must match (blank = the
    spec's). Read the words: closed areas with no key and keys in no closed
    area mean a layer or the snap needs adjusting until every sheet has a cell."""
    body = {"plan_type": plan_type}
    for k, v in (("key_map_file", key_map_file), ("cell_layers", cell_layers), ("key_layers", key_layers),
                 ("key_pattern", key_pattern)):
        if str(v or "").strip():
            body[k] = str(v).strip()
    if snap:
        body["snap"] = snap
    out = _dash_post(f"/dashboards/{dashboard}/keymap", body, timeout=300)
    if "error" in out:
        return json.dumps(out)
    return json.dumps({"file": out.get("file"), "count": len(out.get("cells") or []), "cells": _trim_cells(out),
                       "faces_without_key": out.get("faces_without_key"), "keys_without_cell": out.get("keys_without_cell"),
                       "counts": out.get("counts"), "warnings": out.get("warnings"), "words": out.get("words"),
                       "next": "plan_grading_sheets(dashboard, cells=\"1,2\") to see the sheets, "
                               "cut_grading_sheets to make them"})


@mcp.tool()
@observed
def list_key_map(dashboard: str) -> str:
    """The key map cells read for this dashboard (see read_key_map): key, centre,
    size and twist of each, plus the areas with no key and keys with no area."""
    out = _dash_get(f"/dashboards/{dashboard}/keymap")
    if "error" in out:
        return json.dumps(out)
    if not out.get("cells"):
        return json.dumps({"count": 0, "cells": [], "why": out.get("why") or "no key map read yet"})
    return json.dumps({"file": out.get("file"), "read_at": out.get("read_at"), "count": len(out["cells"]),
                       "cells": _trim_cells(out), "faces_without_key": out.get("faces_without_key"),
                       "keys_without_cell": out.get("keys_without_cell"), "warnings": out.get("warnings"),
                       "words": out.get("words")})


def _cell_keys(cells) -> list:
    if isinstance(cells, (list, tuple)):
        return [str(k).strip() for k in cells if str(k).strip()]
    return [k.strip() for k in str(cells or "").split(",") if k.strip()]


@mcp.tool()
@observed
def plan_grading_sheets(dashboard: str, plan_type: str = "grading", cells: str = "", scale: float = 0,
                        title: str = "") -> str:
    """Dry run of cut_grading_sheets: the sheets the chosen key-map cells make
    in the dashboard's drawing -- layout name (GR-<key>), scale (from the
    cell's size over the viewport, or `scale` in feet per inch), twist, the
    cell window, what each holds, the furniture -- plus the label_grading /
    space_labels parameters the cut runs first (once over the whole drawing).
    Nothing is written. cells: comma-separated keys, blank = every cell. Needs
    read_key_map first."""
    try:
        spec = sheets.load_spec(plan_type)
    except (sheets.SheetError, playbooks.PlaybookError) as e:
        return _err(e)
    rec = _dash_get(f"/dashboards/{dashboard}/keymap")
    if "error" in rec:
        return json.dumps(rec)
    if not rec.get("cells"):
        return json.dumps({"error": rec.get("why") or "no key map read for this dashboard yet: call read_key_map first"})
    try:
        file = _file_for(dashboard)
        entry = spatial_cache.get(file)
        plan = sheets.plan_from_cells(entry.index, spec, rec["cells"], plan_type, scale=scale or None,
                                      keys=_cell_keys(cells) or None)
        payloads = sheets.sheet_payloads(plan, spec, entry.index, entry.data, plan_type, drawing=file, title=title)
    except (sheets.SheetError, spatial_cache.PluginError, DashboardError) as e:
        return _err(e)
    per_sheet = []
    for p, sh in zip(payloads, plan["sheets"]):
        steps = p["steps"]
        per_sheet.append({"key": sh["key"], "layout": sh["layout"], "scale": sh["scale"], "scale_text": sh["scale_text"],
                          "twist_deg": sh["twist_deg"], "window": sh["window"], "size_ft": [sh["width"], sh["height"]],
                          "holds": sh["tags"], "entities": len(sh["handles"]),
                          "furniture": [st.get("name") or "mtext" for st in steps
                                        if st["intent"] in ("insert_block", "draw_mtext", "attach_xref")],
                          "tables": [st.get("title") or "table" for st in steps if st["intent"] == "create_table"],
                          "steps": len(steps)})
    vp = next((st for st in payloads[0]["steps"] if st["intent"] == "create_viewport"), {}) if payloads else {}
    return json.dumps({**_sheet_plan_summary(plan), "key_map": rec.get("file"),
                       "template": (spec.get("template") or {}).get("file") or "(plain layouts)",
                       "frozen_in_viewports": len(vp.get("freeze_layers") or []),
                       "words": sheets.describe_plan(entry.index, plan), "sheets": per_sheet,
                       "label_params": spec.get("grading") or {}, "spec_warnings": sheets.spec_warnings(spec),
                       "next": "cut_grading_sheets(dashboard, cells=...) runs this on the dashboard"})


@mcp.tool()
@observed
def cut_grading_sheets(dashboard: str, plan_type: str = "grading", cells: str = "", scale: float = 0,
                       title: str = "", labels: bool = True, label_scope: str = "drawing",
                       on_conflict: str = "pause") -> str:
    """Makes the grading sheets on the dashboard, in the background: label_grading
    then space_labels once over the whole drawing (the spec's grading.labels /
    grading.spacing parameters, each gated by a compute pass), then one
    all-or-nothing sequence per chosen key-map cell (template layout cloned as
    GR-<key>, viewport cut on the cell at its scale and twist, title block
    filled with the key, furniture placed). cells: comma-separated keys, blank =
    every cell; labels=False cuts the sheets only; label_scope "cells" runs the
    label pair inside each cell's window instead of once over the drawing
    (slower). Needs read_key_map first; call plan_grading_sheets to see the
    plan. A layout that already exists makes that sheet fail rather than
    overwrite it."""
    scope = str(label_scope or "drawing").strip().lower()
    if scope not in ("drawing", "cells"):
        return json.dumps({"error": "label_scope must be drawing or cells"})
    params = {"plan_type": plan_type, "cells": _cell_keys(cells), "title": title, "labels": bool(labels),
              "label_scope": scope}
    if scale:
        params["scale"] = scale
    checks = ""
    try:
        if any(p["plan_type"] == plan_type and p["checks"] for p in playbooks.list_playbooks()):
            checks = plan_type
    except Exception:
        pass
    return run_on_dashboard(dashboard, [{"type": "cut_grading_sheets", "params": params}], on_conflict=on_conflict,
                            label=f"cut {plan_type} sheets", checks=checks)


@mcp.tool()
@observed
def list_sheets(dashboard: str, verify: bool = False) -> str:
    """The sheets the dashboard's drawing already has: every layout with its
    viewports (scale, twist, model window, what the window holds in the index's
    words, frozen layer count), the blocks on the paper with their attributes,
    and the tables. Read-only. verify=True adds, per viewport, the index's
    sentence for the cell under the window's centre and its SW / NE corners
    plus the raw view numbers: open that layout tab and compare -- a match on
    one twisted and one untwisted sheet proves the window transform."""
    try:
        entry = spatial_cache.get(_file_for(dashboard))
    except (spatial_cache.PluginError, DashboardError) as e:
        return _err(e)
    return json.dumps({"layouts": sheets.list_layouts(entry.data, entry.index, verify=verify)})


# ── Redline reviews: scanned reviewer comments -> block chains ──────────────
#
# redline/ reads the handwriting on plotted sheets with a local vision model,
# opens the drawing printed in each sheet's title block, maps every comment
# through the viewport into model space, asks a local language model for a
# chain of dashboard blocks and runs it. It is the one path that assigns a
# drawing to a dashboard without the user doing it: the drawing is the one
# printed on the PDF the user handed over. This tool takes a PDF path only.


@mcp.tool()
@observed
def review_redlines(pdf_path: str, dry_run: bool = False, pages: str = "") -> str:
    """Starts a redline review of a scanned, redlined plan-sheet PDF in the
    background and returns at once. The pipeline reads each comment (local
    vision model), finds the drawing and layout the sheet came from, locates
    the comment in model space, composes a block chain per comment (local
    language model) and runs the chains it can on a dashboard named
    "Redline: <drawing>", skipping the rest with a reason. dry_run plans
    without running. pages is "1,3" to limit the pages. Read the outcome
    with review_status(review_id); runs also appear in dashboard_status.
    Needs Ollama (or another OpenAI-compatible model server) running with
    the configured models; `python -m redline doctor` says what is missing."""
    import subprocess
    import time as _time
    path = str(pdf_path or "").strip().strip('"')
    if not os.path.isfile(path) or not path.lower().endswith(".pdf"):
        return json.dumps({"error": f"pdf_path must be an existing .pdf file, got {path!r}"})
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from redline import config as redline_config
    settings = redline_config.load()
    review_id = _time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(settings.review_dir, review_id)
    os.makedirs(out_dir, exist_ok=True)
    cmd = [sys.executable, "-m", "redline", "review", path, "--review-id", review_id]
    if dry_run:
        cmd.append("--dry-run")
    for p in str(pages or "").split(","):
        if p.strip().isdigit():
            cmd += ["--page", p.strip()]
    log_path = os.path.join(out_dir, "review.log")
    log = open(log_path, "w", encoding="utf-8")
    subprocess.Popen(cmd, cwd=os.path.dirname(os.path.abspath(__file__)), stdout=log, stderr=subprocess.STDOUT,
                     env=os.environ.copy())
    return json.dumps({"review_id": review_id, "background": True, "report_path": os.path.join(out_dir, "report.json"),
                       "log": log_path, "dry_run": dry_run,
                       "note": "Started; this call does not wait. Reading the pages and planning take minutes on a "
                               "local model. Call review_status(review_id) later; the report lists every comment as "
                               "done, skipped (with the reason), failed or needing a person."})


@mcp.tool()
@observed
def review_status(review_id: str = "") -> str:
    """The report of a redline review: per page the drawing and layout matched
    (or why not), per comment the transcription, where it landed, the chain
    or the skip reason and the run's outcome. With review_id blank, lists
    the reviews on disk. A review still running has a review.log but no
    report.json yet."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from redline import config as redline_config
    settings = redline_config.load()
    root = settings.review_dir
    if not review_id:
        out = []
        try:
            names = sorted(os.listdir(root), reverse=True)
        except OSError:
            names = []
        for n in names[:30]:
            rp = os.path.join(root, n, "report.json")
            if os.path.isfile(rp):
                try:
                    with open(rp, "r", encoding="utf-8") as f:
                        rep = json.load(f)
                    out.append({"review_id": n, "counts": rep.get("counts"), "pdfs": rep.get("pdfs"),
                                "unmatched_pages": len(rep.get("unmatched_pages") or [])})
                except (OSError, ValueError):
                    continue
            elif os.path.isfile(os.path.join(root, n, "review.log")):
                out.append({"review_id": n, "status": "running or failed before the report; see review.log"})
        return json.dumps({"reviews": out, "dir": root})
    safe = "".join(ch for ch in str(review_id) if ch.isalnum() or ch in "-_")
    rp = os.path.join(root, safe, "report.json")
    if not os.path.isfile(rp):
        lp = os.path.join(root, safe, "review.log")
        tail = ""
        if os.path.isfile(lp):
            with open(lp, "r", encoding="utf-8", errors="replace") as f:
                tail = f.read()[-1500:]
        return json.dumps({"review_id": safe, "status": "no report yet", "log_tail": tail})
    with open(rp, "r", encoding="utf-8") as f:
        rep = json.load(f)
    # compact: the markdown is easier to read than the full JSON
    md = os.path.join(root, safe, "report.md")
    summary = ""
    if os.path.isfile(md):
        with open(md, "r", encoding="utf-8") as f:
            summary = f.read()[:6000]
    return json.dumps({"review_id": safe, "counts": rep.get("counts"), "sheets": [
        {k: s.get(k) for k in ("page_no", "dwg_path", "layout_name", "how", "reason")} for s in rep.get("sheets") or []],
        "unmatched_pages": rep.get("unmatched_pages"), "errors": rep.get("errors"), "report_md": summary,
        "report_path": rp})


# ── Pressure networks: real water parts ──────────────────────────────────────


@mcp.tool()
@observed
def list_pressure_parts(dashboard: str, parts_list: str = "", full: bool = False) -> str:
    """The drawing's pressure networks (name, parts list, reference surface and
    alignment, part counts) and every pressure parts list with its pipes,
    fittings and appurtenances (description, type, family, nominal diameter,
    material; elbow angle). Read this before lay_pressure_run to pick the
    parts_list, pipe_size and hydrant/valve part names. Read-only."""
    payload = {"intent": "list_pressure_parts", "full": bool(full)}
    if parts_list:
        payload["parts_list"] = parts_list
    return _post(payload, _file_for(dashboard))


@mcp.tool()
@observed
def lay_pressure_run(dashboard: str, alignment: str = "", offset: float = 10.0, side: str = "right",
                     from_station: float | None = None, to_station: float | None = None,
                     pipe_size: str = "", hydrant_every: float = 0.0, hydrant_part: str = "Hydrant",
                     hydrant_size: str = "", network: str = "", parts_list: str = "", layer: str = "",
                     apply: bool = False, background: bool = True) -> str:
    """Lays a run of REAL Civil 3D pressure network parts (a water main): pipes
    along `alignment` offset `offset` ft to `side` (right/left looking
    up-station) between from_station and to_station (blank = the whole
    alignment), bend fittings where the path turns, and a hydrant every
    hydrant_every ft (0 = none). Parts come from the drawing's pressure parts
    list: pipe_size and hydrant_part/hydrant_size match a part description or
    nominal diameter (see list_pressure_parts). The network is created when it
    does not exist.

    apply=False (default) is the compute stage: the plugin resolves every part
    and reports the geometry it would draw under `proposed`, plus the spatial
    gate's verdict on it (crossings, pads) -- nothing is drawn. apply=True
    draws it in one all-or-nothing transaction; with background=True that
    happens as a dashboard run (paused on conflicts until resume_dashboard),
    which is the right way for anything longer than a few pipes."""
    try:
        file = _file_for(dashboard)
        entry = spatial_cache.get(file)
        request, summary = pressure_run.plan(
            entry.data, alignment, offset=offset, side=side, from_station=from_station, to_station=to_station,
            hydrant_every=hydrant_every, hydrant_part=hydrant_part, hydrant_size=hydrant_size, pipe_size=pipe_size,
            network=network, parts_list=parts_list, layer=layer)
    except (pressure_run.PressureRunError, spatial_cache.PluginError, DashboardError) as e:
        return _err(e)
    if apply and background:
        params = {"alignment": alignment, "offset": offset, "side": side, "pipe_size": pipe_size,
                  "hydrant_every": hydrant_every, "hydrant_part": hydrant_part, "network": network,
                  "parts_list": parts_list, "layer": layer}
        if from_station is not None:
            params["from_station"] = from_station
        if to_station is not None:
            params["to_station"] = to_station
        return _run_in_background(dashboard, "lay_pressure_run", params, f"water run along {summary['alignment']}")
    request["apply"] = bool(apply)
    if apply:
        return _post(request, file)             # gated on the path, verified afterwards
    text = _post_raw(request, file)
    try:
        report = json.loads(text)
    except ValueError:
        return text
    if not isinstance(report, dict):
        return text
    report["plan"] = summary
    try:
        conflicts = entry.index.conflicts([request]) if _GATE_ON else []
    except Exception:
        conflicts = []
    if conflicts:
        report["spatial_conflicts"] = conflicts[:60]
        report["spatial_conflict_count"] = len(conflicts)
        report["spatial_note"] = ("The path crosses or lands on things in the drawing (see spatial_conflicts). "
                                  "apply=True on the dashboard pauses on them; move the run or resume_dashboard "
                                  "after reading them.")
    return json.dumps(report)


# ── Sanitary sewer profile views and sheets ──────────────────────────────────
#
# profile_context (plugin, read-only) -> profiles.plan (Python) -> draw_profile_views
# (plugin, one transaction) -> cut_profile_sheets (sheets.profile_sheet_payloads,
# two viewports per sheet). The rules are in profiles.py's docstring.


def _post_long(payload: dict, file: str | None = None) -> str:
    """A plain post with the handler timeout: whole-drawing Civil 3D work."""
    try:
        if file:
            payload = {**payload, "file": file}
        return requests.post(f"{BASE}/execute", json=payload, timeout=HANDLER_TIMEOUT).text
    except requests.exceptions.ConnectionError:
        return _NO_CONNECTION


def _alignment_list(alignments) -> list:
    if isinstance(alignments, str):
        alignments = alignments.split(",")
    return [str(a).strip() for a in alignments or [] if str(a).strip()]


def _profile_context_dict(file: str, alignments="", network: str = "", surface: str = "") -> dict:
    payload = {"intent": "profile_context"}
    names = _alignment_list(alignments)
    if names:
        payload["alignments"] = names
    if network:
        payload["network"] = network
    if surface:
        payload["surface"] = surface
    text = _post_long(payload, file)
    try:
        context = json.loads(text)
    except ValueError:
        raise profiles.ProfileError(f"profile_context did not answer with JSON: {text[:200]}")
    if not isinstance(context, dict) or context.get("error") or context.get("ok") is False:
        raise profiles.ProfileError(str((context or {}).get("error") or (context or {}).get("message") or text[:200]))
    return context


def _profile_spec(plan_type: str) -> dict:
    try:
        return sheets.load_spec(plan_type)
    except (sheets.SheetError, playbooks.PlaybookError):
        return sheets.default_spec(plan_type)


def _profile_plan(dashboard: str, plan_type: str, alignments, span, station_host: str, surface: str,
                  structure_label: str, styles_json: str):
    file = _file_for(dashboard)
    context = _profile_context_dict(file, alignments, surface=surface)
    spec = _profile_spec(plan_type)
    entry = spatial_cache.get(file)
    styles = {}
    if styles_json:
        try:
            styles = dict(json.loads(styles_json))
        except (ValueError, TypeError) as e:
            raise profiles.ProfileError(f"styles_json is not a JSON object: {e}")
    if structure_label:
        styles["structure_label"] = structure_label
    request, summary = profiles.plan(
        context, span=span or None, alignments=_alignment_list(alignments) or None, styles=styles, spec=spec,
        data=entry.data, station_host=station_host or "", surface=surface or "")
    return file, entry, context, request, summary


@mcp.tool()
@observed
def profile_context(dashboard: str, alignments: str = "", network: str = "", surface: str = "") -> str:
    """What the drawing offers for sanitary profile views, read-only: per
    alignment its station range (and whether it starts at 1+00), the EG surface
    elevations sampled along it, the pipes (station, invert, diameter, slope)
    and manholes (station, rim, sump) of the pipe networks that sit on it,
    existing profiles and profile views; every profile view style (with its
    vertical exaggeration), band set, profile style, pipe / structure profile
    label style and major station label style, with the drawing's defaults; the
    drawing extent. Read this to confirm exact style names before drawing.
    alignments: comma separated names (blank = all); network: one network name."""
    try:
        return _post_long({**{"intent": "profile_context"},
                           **({"alignments": _alignment_list(alignments)} if _alignment_list(alignments) else {}),
                           **({"network": network} if network else {}), **({"surface": surface} if surface else {})},
                          _file_for(dashboard))
    except DashboardError as e:
        return _err(e)


@mcp.tool()
@observed
def block_dynamic_properties(dashboard: str, name: str, source_file: str = "") -> str:
    """What a block's grips control: its dynamic properties (name, value, units,
    allowed values, read-only) read from a reference in the drawing, or from a
    temporary reference that is discarded, plus the definition's X extents.
    Read this for the profile dimension-line block (Ano-ProfDimLine) to see
    which property stretches it. Read-only."""
    try:
        payload = {"intent": "block_dynamic_properties", "name": name}
        if source_file:
            payload["source_file"] = source_file
        return _post_long(payload, _file_for(dashboard))
    except DashboardError as e:
        return _err(e)


@mcp.tool()
@observed
def plan_profile_views(dashboard: str, plan_type: str = "sanitary", alignments: str = "", span: float = 0,
                       station_host: str = "", surface: str = "", structure_label: str = "",
                       styles_json: str = "") -> str:
    """Dry run of draw_profile_views: the profile views the drawing's alignments
    get -- 1000 ft windows from 1+00 (the whole length when shorter), a datum
    per view (lowest invert / surface rounded down to an even foot) with the
    36 or 40 ft span, windows split to 500 ft where the span cannot hold the
    elevations, where each view goes in model space (east of the drawing), and
    the gate's verdict on those boxes. Nothing is written. span 0 = the spec's
    (40); station_host flowline|eg; styles_json = {"pipe_label": ..., ...}
    overrides the spec's profile.styles."""
    try:
        file, entry, context, request, summary = _profile_plan(
            dashboard, plan_type, alignments, span, station_host, surface, structure_label, styles_json)
    except (profiles.ProfileError, sheets.SheetError, spatial_cache.PluginError, DashboardError) as e:
        return _err(e)
    try:
        conflicts = entry.index.conflicts([request]) if _GATE_ON else []
    except Exception:
        conflicts = []
    return json.dumps({"plan": summary, "words": profiles.describe(summary),
                       "surface": (context.get("surface_pick") or {}).get("name"),
                       "surface_error": context.get("surface_error"),
                       "style_catalogue": context.get("styles"), "drawing_defaults": context.get("defaults"),
                       "conflicts": conflicts[:60], "conflict_count": len(conflicts),
                       "next": "draw_profile_views(dashboard, plan_type, ..., apply=True) draws this on the dashboard"})


@mcp.tool()
@observed
def draw_profile_views(dashboard: str, plan_type: str = "sanitary", alignments: str = "", span: float = 0,
                       station_host: str = "", surface: str = "", structure_label: str = "",
                       styles_json: str = "", apply: bool = False, background: bool = True) -> str:
    """Draws the sanitary profile views plan_profile_views describes, as REAL
    Civil 3D objects in one all-or-nothing transaction: each alignment
    restationed to 1+00, an EG surface profile (reused when one exists), a
    flowline profile through the pipe inverts (station_host flowline), a profile
    view per window with its station and elevation range, the network's pipes
    and manholes added to it, a pipe label ("Prop -> Pipe Size -> slope") per
    pipe dragged to just above the datum, two labels per manhole (the MH style
    and "MH --> Alignment"), and "Flowline --> Major" station labels every 1+00
    along the bottom. Style names are exact; a missing one falls back to the
    drawing default with a warning.

    apply=False (default): the plugin validates the plan (alignments, surface,
    styles, free names) and reports without writing. apply=True with
    background=True (default) runs it on the dashboard (paused on conflicts
    until resume_dashboard); background=False posts it now, gated. Then
    cut_profile_sheets, or produce_profile_sheets for both in one run."""
    try:
        file, entry, context, request, summary = _profile_plan(
            dashboard, plan_type, alignments, span, station_host, surface, structure_label, styles_json)
    except (profiles.ProfileError, sheets.SheetError, spatial_cache.PluginError, DashboardError) as e:
        return _err(e)
    if apply and background:
        params = {"plan_type": plan_type, "alignment": _alignment_list(alignments),
                  "span": f"{int(round(summary['span']))}", "station_host": summary["station_host"]}
        if surface:
            params["surface"] = surface
        if structure_label:
            params["structure_label"] = structure_label
        return _run_in_background(dashboard, "draw_profile_views", params,
                                  f"profile views ({summary['count']}) along " + ", ".join(a["alignment"] for a in summary["alignments"]))
    request["apply"] = bool(apply)
    if apply:
        return _post(request, file)             # gated on the planned boxes, verified afterwards
    text = _post_long(request, file)
    try:
        report = json.loads(text)
    except ValueError:
        return text
    if not isinstance(report, dict):
        return text
    report["plan"] = summary
    report["words"] = profiles.describe(summary)
    try:
        conflicts = entry.index.conflicts([request]) if _GATE_ON else []
    except Exception:
        conflicts = []
    if conflicts:
        report["spatial_conflicts"] = conflicts[:60]
        report["spatial_conflict_count"] = len(conflicts)
        report["spatial_note"] = ("The planned view boxes land on things in the drawing (see spatial_conflicts); "
                                  "move them with profile.placement in the sheet spec, or resume_dashboard after reading them.")
    return json.dumps(report)


@mcp.tool()
@observed
def cut_profile_sheets(dashboard: str, plan_type: str = "sanitary", alignments: str = "", title: str = "",
                       on_conflict: str = "pause") -> str:
    """Puts the drawing's profile views on sheets, on the dashboard: two stacked
    viewports per sheet (the spec's profile.slots) at 1" = 40' (profile.scale),
    the template layout cloned, the title block filled ({alignment} {stations}
    {views} as well as the usual words), furniture placed. One all-or-nothing
    sequence per sheet; layouts that already exist make the sheet fail rather
    than overwrite. Needs playbooks/<type>/sheets.json (get_sheet_spec /
    save_sheet_spec). alignments: comma separated (blank = every view)."""
    params = {"plan_type": plan_type, "alignment": _alignment_list(alignments), "title": title}
    checks = ""
    try:
        if any(p["plan_type"] == plan_type and p["checks"] for p in playbooks.list_playbooks()):
            checks = plan_type
    except Exception:
        pass
    return run_on_dashboard(dashboard, [{"type": "cut_profile_sheets", "params": params}], on_conflict=on_conflict,
                            label=f"cut {plan_type} profile sheets", checks=checks)


@mcp.tool()
@observed
def produce_profile_sheets(dashboard: str, plan_type: str = "sanitary", alignments: str = "", span: float = 0,
                           station_host: str = "", surface: str = "", structure_label: str = "", title: str = "",
                           on_conflict: str = "pause") -> str:
    """The whole sanitary profile sheet production as one dashboard run: draw
    the profile views (draw_profile_views), then cut the sheets over them
    (cut_profile_sheets, planned at its turn so it sees the new views). Call
    plan_profile_views first to read the plan. span 0 = the spec's."""
    draw = {"plan_type": plan_type, "alignment": _alignment_list(alignments)}
    if span:
        draw["span"] = f"{int(round(float(span)))}"
    if station_host:
        draw["station_host"] = station_host
    if surface:
        draw["surface"] = surface
    if structure_label:
        draw["structure_label"] = structure_label
    cut = {"plan_type": plan_type, "alignment": _alignment_list(alignments), "title": title}
    checks = ""
    try:
        if any(p["plan_type"] == plan_type and p["checks"] for p in playbooks.list_playbooks()):
            checks = plan_type
    except Exception:
        pass
    return run_on_dashboard(dashboard, [{"type": "draw_profile_views", "params": draw},
                                        {"type": "cut_profile_sheets", "params": cut}],
                            on_conflict=on_conflict, label=f"{plan_type} profile sheets", checks=checks)


@mcp.tool()
@observed
def create_sanitary_profile(dashboard: str, plan_type: str = "sanitary", alignments: str = "", span: float = 0,
                            station_host: str = "", surface: str = "", structure_label: str = "", title: str = "",
                            on_conflict: str = "pause") -> str:
    """The whole sanitary profile production as ONE dashboard run in a set order:
    1. draw just the profile views (no labels, no blocks), alphabetical by
    alignment, one transaction per alignment so an alignment that fails is
    skipped and reported; 2. arrange the views two to a sheet window, centred,
    and cut the sheets (template layout by span, border xref, one full-sheet
    viewport); 3. LAST, the labels and the dimension line on every view (pipe
    and 1+00 station labels anchored to the view bottom, the MH and
    MH -> Alignment labels where their styles put them). Alignments whose
    views already exist are left alone. span 0 = the spec's."""
    params = {"plan_type": plan_type, "alignment": _alignment_list(alignments), "title": title}
    if span:
        params["span"] = f"{int(round(float(span)))}"
    if station_host:
        params["station_host"] = station_host
    if surface:
        params["surface"] = surface
    if structure_label:
        params["structure_label"] = structure_label
    checks = ""
    try:
        if any(p["plan_type"] == plan_type and p["checks"] for p in playbooks.list_playbooks()):
            checks = plan_type
    except Exception:
        pass
    return run_on_dashboard(dashboard, [{"type": "create_sanitary_profile", "params": params}],
                            on_conflict=on_conflict, label=f"{plan_type} profile", checks=checks)


@mcp.tool()
@observed
def check_drawing(dashboard: str, plan_type: str = "", checks_json: str = "", scope: str = "all") -> str:
    """Evaluates acceptance checks against the dashboard's drawing as it is now:
    a saved playbook's checks (plan_type) or an inline list (checks_json). scope
    "all" judges the whole drawing; "new" judges only what the dashboard's latest
    run created. Returns pass/fail per check with offenders by handle. This is
    how a playbook's checks are tried right after writing them."""
    try:
        check_list = playbooks.load_checks(plan_type) if plan_type else playbooks._coerce_checks(checks_json)
    except playbooks.PlaybookError as e:
        return _err(e)
    errors = checks_mod.validate(check_list)
    if errors:
        return json.dumps({"error": "checks did not validate", "details": errors})
    try:
        index = _index_for(dashboard)
    except Exception as e:
        return _err(e)
    handles = None
    if scope == "new":
        st = _dash_get(f"/dashboards/{dashboard}/status")
        run = (st.get("run") or {}) if isinstance(st, dict) else {}
        handles = set((run.get("verification") or {}).get("new_handles") or [])
        if not handles:
            return json.dumps({"error": "the latest run has no new handles to scope to; use scope='all'"})
    entry = spatial_cache.peek(_file_for(dashboard))
    results = checks_mod.evaluate(index, check_list, handles, entry.data if entry else None)
    if not getattr(index, "windows", None):
        for r in results:
            if r["kind"] == "covered" and not r["ok"]:
                r["detail"]["note"] = "no sheets cut yet: expected to fail until cut_sheets has run"
    return json.dumps({"scope": scope, **checks_mod.summarize(results), "results": results})


@mcp.tool()
@observed
def calibrate_checks(plan_type: str, checks_json: str = "") -> str:
    """Runs a plan type's checks (saved, or checks_json) over every exemplar
    bundle of that type, scoped to each bundle's delta. A check the exemplars
    themselves fail is a bad check, or an exemplar with a real problem; either
    way it should not go into a playbook unchanged. Returns per-bundle results."""
    try:
        check_list = playbooks._coerce_checks(checks_json) if str(checks_json or "").strip() \
            else playbooks.load_checks(plan_type)
        bundles = exemplars.list_bundles(plan_type)
    except (playbooks.PlaybookError, exemplars.ExemplarError) as e:
        return _err(e)
    errors = checks_mod.validate(check_list)
    if errors:
        return json.dumps({"error": "checks did not validate", "details": errors})
    if not bundles:
        return json.dumps({"error": f"no exemplar bundles for {plan_type!r}"})
    out, failing = [], {}
    for b in bundles:
        bdir = exemplars.bundle_dir(plan_type, b["name"])
        try:
            with open(os.path.join(bdir, "snapshot.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
            with open(os.path.join(bdir, "delta.json"), "r", encoding="utf-8") as f:
                delta = json.load(f)
        except (OSError, ValueError) as e:
            out.append({"bundle": b["name"], "error": str(e)})
            continue
        index = spatial.build(data)
        results = checks_mod.evaluate(index, check_list, exemplars.leaf_handles(delta))
        summary = checks_mod.summarize(results)
        for r in results:
            if not r["ok"]:
                failing.setdefault(r["id"], []).append(b["name"])
        out.append({"bundle": b["name"], **summary,
                    "failed": [{k: r[k] for k in ("id", "expected", "actual", "offenders")} for r in results if not r["ok"]]})
    return json.dumps({"plan_type": plan_type, "bundles": len(bundles), "ok": not failing,
                       "checks_failing_somewhere": failing, "per_bundle": out})


@mcp.tool()
@observed
def describe_location(dashboard: str, x: float, y: float) -> str:
    """What is at a point, in words and in civil terms.

    Returns the 50ft cell's description -- region, nearest alignment with
    station range, side and offset, what is drawn there and which way it runs
    -- plus the same frame as data, the region id, and what the eight
    neighbouring cells hold. Use it to check a spot before drawing there."""
    try:
        index = _index_for(dashboard)
    except Exception as e:
        return json.dumps({"error": str(e)})
    loc = index.locate(x, y)
    return json.dumps({"description": index.describe(loc["key"]),
                       "cell": list(loc["key"]), "bounds": list(loc["bounds"]),
                       "tags": loc["tags"], "frame": loc["frame"],
                       "region": loc["region"], "neighbours": loc["neighbours"]})


@mcp.tool()
@observed
def find_entities(dashboard: str, tag: str, x: float, y: float,
                  radius: float = 100.0, state: str = "") -> str:
    """Handles of entities of one tag or family within `radius` ft of a point.

    tag is a word from list_tags: a hand tag (pad, curb, lot_line, setback,
    centreline, right_of_way, boundary, easement, driveway, swale ...), a full
    auto tag (water_main, water_hydrant, sanitary_pipe, storm_inlet ...), or a
    family word that means all of its parts (water, sanitary, storm ...).
    state narrows to proposed / existing / future / demolition / abandoned /
    temporary. Point entities (labels, blocks, structures, text) match on
    their anchor. Handles are what move_to_layer, edit_layer and the modify
    tools take, so this is how a reasoned-about area becomes something to act on."""
    try:
        index = _index_for(dashboard)
    except Exception as e:
        return json.dumps({"error": str(e)})
    handles = index.entities(tag, point=(x, y), radius=radius, state=state or None)
    return json.dumps({"tag": tag, "count": len(handles), "handles": handles,
                       "region": index.region_of(point=(x, y))})


@mcp.tool()
@observed
def solve_pipe_run(dashboard: str,
                   alignment: str = "",
                   offset_tolerance: float = 25.0,
                   network: str = "",
                   structures: list[str] | None = None,
                   start_structure: str = "",
                   end_structure: str = "",
                   flow_direction: str = "auto",
                   cover_minimum: float | None = None,
                   offset_mode: str = "MatchCrowns",
                   manual_drop: float = 0.0,
                   hold_start: bool = False,
                   hold_end: bool = False,
                   fixed_inverts: dict | None = None,
                   overrides: dict | None = None,
                   max_depth_below_surface: float = 50.0,
                   slope_units: str = "auto",
                   apply: bool = False) -> str:
    """Solve (and optionally set) the inverts of a WHOLE RUN of pipes at once --
    every structure along an alignment, an explicit ordered list of structures,
    or the shortest network path between two structures -- using the same
    criteria as solve_pipe_elevations: minimum cover from the reference surface
    along each pipe, rule-set min/max slope, and the offset rule (MatchCrowns /
    MatchInverts / ManualDrop) at every structure. The result is the SHALLOWEST
    feasible profile: upstream end as high as cover allows, each pipe at minimum
    slope unless cover or a tie-in forces it deeper.

    Pick the run with ONE of: alignment (+ offset_tolerance ft, structures within
    it in station order; network optional), structures (ordered names/handles),
    or start_structure + end_structure (network path).
    flow_direction: "auto" reads the pipes' flow directions, else falls back to
    the ground falling end to end; set "downstation" / "upstation" explicitly
    when the network has no flow directions.
    hold_start / hold_end keep the existing invert at the run ends (tie-ins);
    fixed_inverts = {"MH-7": 94.25} pins the incoming invert at named structures.
    overrides = {"P-3": {"up": 95.1, "down": 94.6}} sets pipes by hand on top of
    the solve; they go through the same cover/slope checks.

    Read the report before applying: ok=true + conflicts=[] means safe (re-run
    with apply=True to write all pipes in ONE transaction, verified against the
    Civil 3D cover check). A "fixed_invert" conflict tells you how far a tie-in is
    above the shallowest reachable profile (delta) -- lower it, relax cover, or
    allow a steeper slope. Structures that also connect pipes outside the run are
    listed under warnings; those laterals are untouched (use
    solve_pipe_elevations there afterwards).

    dashboard: REQUIRED id of the dashboard whose drawing to work on."""
    file = _file_for(dashboard)
    request: dict = {
        "offsetTolerance": offset_tolerance,
        "flowDirection": flow_direction,
        "offsetMode": offset_mode,
        "manualDrop": manual_drop,
        "holdStart": hold_start,
        "holdEnd": hold_end,
        "maxDepthBelowSurface": max_depth_below_surface,
        "slopeUnits": slope_units,
    }
    if alignment:
        request["alignment"] = alignment
    if network:
        request["network"] = network
    if structures:
        request["structures"] = structures
    if start_structure and end_structure:
        request["startStructure"] = start_structure
        request["endStructure"] = end_structure
    if cover_minimum is not None:
        request["coverMinimum"] = cover_minimum
    if fixed_inverts:
        request["fixedInverts"] = fixed_inverts
    if overrides:
        request["overrides"] = overrides
    return _post_handler("pipe_run", "draw" if apply else "compute", request, file)


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


# ── General 2D drafting command set ─────────────────────────────────────────
#
# One tool per intent in Commands/CadCommands.*.cs (draw, modify, annotate,
# organise, query, plus run_sequence for all-or-nothing chains). Registered from a
# separate module so this file keeps the Civil 3D specific tools only.
import drafting_tools
drafting_tools.register(mcp, observed, _post, _file_for)


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
# Point _HEALTH_PATH at a route the CURRENT server has and older ones don't, and
# require the version stamp to match so a server running older code is replaced.
_HEALTH_PATH = "/version"
_DASHBOARD_VERSION = "2026-09-28.2"   # must equal dashboard/server.py DASHBOARD_VERSION

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


def _dashboard_version() -> str | None:
    """The version stamp the server on the port reports, or None when nothing
    there answers like a dashboard server."""
    try:
        response = requests.get(
            f"http://127.0.0.1:{_DASHBOARD_PORT}{_HEALTH_PATH}", timeout=1.0)
        if response.status_code != 200:
            return None
        version = response.json().get("version")
        return str(version) if version else None
    except (requests.exceptions.RequestException, ValueError):
        return None


def _dashboard_healthy() -> bool:
    """True only when the port is owned by a server serving the current routes."""
    return _dashboard_version() == _DASHBOARD_VERSION


def _stop_stale_dashboard() -> bool:
    """Ends a dashboard server left running from an older version of the code.

    Only a process that answers /version with a stamp that is not ours is
    touched -- that is a dashboard server of ours and nothing else -- and it is
    found by the port it holds. Windows only; elsewhere the log line tells the
    user what to stop."""
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             timeout=10, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except Exception as e:
        _log(f"could not list listeners to find the stale dashboard server: {e}")
        return False
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(f":{_DASHBOARD_PORT}") \
                and parts[3].upper() == "LISTENING":
            pids.add(parts[4])
    if not pids:
        return False
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, text=True,
                           timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            _log(f"stopped stale dashboard server (pid {pid}) so the current one can start")
        except Exception as e:
            _log(f"could not stop stale dashboard server pid {pid}: {e}")
            return False
    deadline = time.time() + 5
    while time.time() < deadline and _dashboard_port_open():
        time.sleep(0.25)
    return not _dashboard_port_open()


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


@mcp.tool()
@observed
def open_dashboard() -> str:
    """Opens the dashboard UI (http://localhost:8765) in the user's browser,
    starting the dashboard server first if it is not running. The tab is
    normally opened once when this server process starts, and Claude Desktop
    keeps that process alive across chats and window closes, so call this when
    the user asks for the dashboard and no tab is open."""
    if not _dashboard_healthy():
        _ensure_dashboard_running()
        if not _wait_for_healthy():
            return json.dumps({"ok": False, "url": _DASHBOARD_URL,
                               "error": "The dashboard server did not come up; see "
                                        f"{_LAUNCH_LOG} for why."})
        return json.dumps({"ok": True, "url": _DASHBOARD_URL,
                           "note": "Server started; the launcher opens the tab itself."})
    opened = _open_browser()
    return json.dumps({"ok": opened, "url": _DASHBOARD_URL,
                       "note": None if opened else f"No browser could be opened; see {_LAUNCH_LOG}. "
                                                   f"Ask the user to open {_DASHBOARD_URL}."})


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
            launch_new = not _dashboard_port_open()
            if not launch_new:
                if not _wait_for_healthy():
                    stale = _dashboard_version()
                    if stale is not None and stale != _DASHBOARD_VERSION and _stop_stale_dashboard():
                        # Our own older server: replaced, so the browser gets the
                        # routes this code expects instead of a half-built UI.
                        _log(f"dashboard server {stale} replaced by {_DASHBOARD_VERSION}")
                        launch_new = True
                    else:
                        _log(f"Port {_DASHBOARD_PORT} is held by a process that never "
                             f"served {_HEALTH_PATH} as a dashboard server of ours. Stop "
                             "that process and restart Claude Desktop.")
                        return
            if launch_new:
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