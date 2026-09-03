"""FastAPI backend for the CAD-Agent dashboards.

A "dashboard" is a user-created workspace with a drawing file assigned to it BY THE
USER. The agent never chooses or changes a file path: its tool calls name a
dashboard, and this server resolves that dashboard's user-assigned drawing. Two
dashboards assigned to different drawings can be worked in parallel, because the
plugin locks each drawing independently.

Why dashboards rather than chats: Claude Desktop reuses one cad_mcp_server.py
process across every chat and exposes no chat identity, so a chat cannot be
identified. A dashboard is an explicit workspace the user creates instead.

Run with: uvicorn dashboard.server:app --port 8765
"""

import asyncio
import hashlib
import os
import shutil
import time
from collections import deque
from pathlib import Path

import requests
from fastapi import FastAPI, File, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Works both as a package ("uvicorn dashboard.server:app") and when this file's
# own directory is on sys.path (running server.py directly).
try:
    from . import blocks
except ImportError:  # pragma: no cover - depends only on how the app was started
    import blocks

# Overridable so the command runner can be exercised against a stub instead of a
# live drawing (CAD_PLUGIN_BASE=http://127.0.0.1:5399).
# 127.0.0.1, not "localhost": localhost resolves to IPv6 ::1 first on this machine
# and stalls ~2s per request before falling back to IPv4 (measured 2.06s vs 0.015s).
PLUGIN_BASE = os.environ.get("CAD_PLUGIN_BASE", "http://127.0.0.1:5199")
PLUGIN_TIMEOUT = 30

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR.mkdir(exist_ok=True)

_HISTORY_LEN = 400

app = FastAPI(title="CAD Agent Dashboards")

_history: deque = deque(maxlen=_HISTORY_LEN)
_clients: set[WebSocket] = set()

# dashboard id -> {"name": str, "file": str | None, "created": float}
_dashboards: dict[str, dict] = {}
_next_id = 1


def _avatar_path(dashboard_id: str) -> Path:
    safe = hashlib.md5(dashboard_id.encode("utf-8")).hexdigest()[:16]
    return UPLOAD_DIR / safe / "current.jpg"


async def _broadcast(event: dict) -> None:
    """Sends to every client concurrently, with a per-client timeout. Command
    sequences broadcast twice per command, so a single stalled browser tab must
    never be able to slow the run down -- a slow client gets dropped instead."""
    if not _clients:
        return

    async def send(ws: WebSocket):
        try:
            await asyncio.wait_for(ws.send_json(event), timeout=1.0)
        except Exception:
            return ws  # unreachable or too slow — drop it
        return None

    results = await asyncio.gather(*(send(ws) for ws in list(_clients)),
                                   return_exceptions=True)
    for result in results:
        if isinstance(result, WebSocket):
            _clients.discard(result)


# ── Dashboards (user-created workspaces) ──────────────────────────────────────


@app.get("/dashboards")
async def list_dashboards():
    return {
        "dashboards": [
            {"id": did, "name": d["name"], "file": d["file"]}
            for did, d in sorted(_dashboards.items(), key=lambda kv: kv[1]["created"])
        ]
    }


@app.post("/dashboards")
async def create_dashboard(request: Request):
    global _next_id
    try:
        body = await request.json()
    except Exception:
        body = {}

    did = str(_next_id)
    _next_id += 1
    _dashboards[did] = {
        "name": (body.get("name") or f"Dashboard {did}").strip(),
        "file": None,
        "created": time.time(),
    }
    await _broadcast({"type": "dashboard_created", "dashboard": did,
                      "name": _dashboards[did]["name"], "_ts": time.time()})
    return {"id": did, **{k: v for k, v in _dashboards[did].items() if k != "created"}}


@app.delete("/dashboards/{dashboard_id}")
async def delete_dashboard(dashboard_id: str):
    _dashboards.pop(dashboard_id, None)
    await _broadcast({"type": "dashboard_deleted", "dashboard": dashboard_id,
                      "_ts": time.time()})
    return {"ok": True}


@app.get("/dashboards/{dashboard_id}/file")
async def get_dashboard_file(dashboard_id: str):
    """Polled by cad_mcp_server.py to resolve a dashboard's user-assigned drawing."""
    d = _dashboards.get(dashboard_id)
    if d is None:
        return JSONResponse({"error": f"No dashboard '{dashboard_id}'."}, status_code=404)
    return {"id": dashboard_id, "name": d["name"], "file": d["file"]}


@app.post("/dashboards/{dashboard_id}/file")
async def set_dashboard_file(dashboard_id: str, request: Request):
    """User-only: the agent has no tool that reaches this endpoint."""
    d = _dashboards.get(dashboard_id)
    if d is None:
        return JSONResponse({"error": f"No dashboard '{dashboard_id}'."}, status_code=404)

    path = ((await request.json()).get("file") or "").strip()
    if not path:
        d["file"] = None
        return {"id": dashboard_id, "file": None}

    for other_id, other in _dashboards.items():
        if other_id != dashboard_id and other["file"] and other["file"].lower() == path.lower():
            return JSONResponse(
                {"error": f"'{path}' is already assigned to {other['name']}."},
                status_code=400,
            )

    d["file"] = path
    await _broadcast({"type": "dashboard_file_set", "dashboard": dashboard_id,
                      "file": path, "_ts": time.time()})
    return {"id": dashboard_id, "file": path}


# ── Direct command sequences (no chat involved) ─────────────────────────────
#
# A dashboard's sequence is a list of function blocks, each with its own
# parameters and repeat count, assembled in the browser and run in order against
# that dashboard's assigned drawing. This is the only way to get two drawings
# worked simultaneously: Claude Desktop only drives the chat you're looking at,
# so chat-driven work can't overlap. Each dashboard's sequence runs as its own
# task, so several can run at once.
#
# What a block is, which parameters it takes and how it becomes a plugin payload
# all live in blocks.py — this module only sequences and dispatches them.

# dashboard id -> asyncio.Task
_runs: dict[str, asyncio.Task] = {}


@app.get("/blocks")
async def list_blocks():
    """The block palette. The browser builds every parameter form from this, so
    adding a block to blocks.py is all it takes to expose it in the UI."""
    return {"blocks": blocks.palette(), "max_repeat": blocks.MAX_REPEAT}



async def _run_sequence(dashboard_id: str, commands: list, file: str) -> None:
    await _broadcast({"type": "run_started", "dashboard": dashboard_id,
                      "total": len(commands), "_ts": time.time()})
    completed = 0
    try:
        for index, payload in enumerate(commands, start=1):
            tool = payload["intent"]
            started = time.time()
            # "block" travels with every event so the browser can highlight the
            # block a step came from — a repeat fans one block out into many steps.
            block_id = payload.get("_block")
            await _broadcast({"type": "tool_call_started", "dashboard": dashboard_id,
                              "tool": tool, "block": block_id,
                              "args": {"step": f"{index}/{len(commands)}"},
                              "_ts": started})

            body = {k: v for k, v in payload.items() if k != "_block"}
            body["file"] = file
            try:
                response = await asyncio.to_thread(
                    requests.post, f"{PLUGIN_BASE}/execute", json=body, timeout=PLUGIN_TIMEOUT
                )
                result, error = response.text, None
            except requests.exceptions.RequestException:
                result, error = None, ("Civil 3D is not reachable — make sure AutoCAD is "
                                       "open with the CADAgent plugin loaded.")

            await _broadcast({"type": "tool_call_finished", "dashboard": dashboard_id,
                              "tool": tool, "block": block_id,
                              "result": (result or "")[:300], "error": error,
                              "duration_ms": round((time.time() - started) * 1000),
                              "_ts": time.time()})
            if error:
                raise RuntimeError(error)
            completed += 1
    except asyncio.CancelledError:
        await _broadcast({"type": "run_stopped", "dashboard": dashboard_id,
                          "completed": completed, "_ts": time.time()})
        raise
    except Exception as e:
        await _broadcast({"type": "run_failed", "dashboard": dashboard_id,
                          "completed": completed, "error": str(e), "_ts": time.time()})
    else:
        await _broadcast({"type": "run_finished", "dashboard": dashboard_id,
                          "completed": completed, "_ts": time.time()})
    finally:
        _runs.pop(dashboard_id, None)


@app.post("/dashboards/{dashboard_id}/run")
async def run_script(dashboard_id: str, request: Request):
    d = _dashboards.get(dashboard_id)
    if d is None:
        return JSONResponse({"error": f"No dashboard '{dashboard_id}'."}, status_code=404)
    if not d["file"]:
        return JSONResponse(
            {"error": "Assign a drawing to this dashboard before running commands."},
            status_code=400,
        )
    if dashboard_id in _runs and not _runs[dashboard_id].done():
        return JSONResponse({"error": "This dashboard is already running a sequence."},
                            status_code=409)

    body = await request.json()
    try:
        # "blocks" is what the dashboard's sequence builder sends; "script" is the
        # equivalent typed syntax, kept for scripting the server without a browser.
        if body.get("blocks") is not None:
            commands = blocks.compile_blocks(body["blocks"])
        else:
            commands = blocks.parse_script(body.get("script", ""))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    _runs[dashboard_id] = asyncio.create_task(
        _run_sequence(dashboard_id, commands, d["file"])
    )
    return {"queued": len(commands)}


@app.get("/dashboards/{dashboard_id}/status")
async def run_status(dashboard_id: str):
    task = _runs.get(dashboard_id)
    return {"running": bool(task and not task.done())}


@app.post("/dashboards/{dashboard_id}/stop")
async def stop_script(dashboard_id: str):
    task = _runs.get(dashboard_id)
    if task and not task.done():
        task.cancel()
        return {"stopped": True}
    return {"stopped": False}


# ── Activity ──────────────────────────────────────────────────────────────────


@app.post("/events")
async def post_event(request: Request):
    """Ingested from cad_mcp_server.py's dashboard_events.emit() calls."""
    event = await request.json()
    event["_ts"] = time.time()
    _history.append(event)
    await _broadcast(event)
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    _clients.add(websocket)
    try:
        for event in _history:
            await websocket.send_json(event)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(websocket)


# ── Avatars ───────────────────────────────────────────────────────────────────


@app.post("/avatar")
async def upload_avatar(dashboard: str = Query(...), file: UploadFile = File(...)):
    target = _avatar_path(dashboard)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"ok": True}


@app.get("/avatar")
async def get_avatar(dashboard: str = Query(...)):
    path = _avatar_path(dashboard)
    if path.exists():
        return FileResponse(path)
    return FileResponse(STATIC_DIR / "default-avatar.svg")


# Catch-all static file mount for the frontend; must come after the routes above.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
