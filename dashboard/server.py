"""FastAPI backend for the CAD-Agent dashboards.

A "dashboard" is a user-created workspace with a drawing file assigned to it BY THE
USER. The agent never chooses or changes a file path: its tool calls name a
dashboard, and this server resolves that dashboard's user-assigned drawing. Two
dashboards assigned to different drawings can be worked in parallel, because the
plugin locks each drawing independently.

One labelled exception: the redline pipeline (redline/), launched by the user
on a scanned PDF, assigns the drawing whose name is printed in that PDF's title
block. It creates its dashboard with source "redline" and the file in the same
request; no chat tool reaches that path.

Why dashboards rather than chats: Claude Desktop reuses one cad_mcp_server.py
process across every chat and exposes no chat identity, so a chat cannot be
identified. A dashboard is an explicit workspace the user creates instead.

Run with: uvicorn dashboard.server:app --port 8765
"""

import asyncio
import hashlib
import json
import sys
import os
import shutil
import time
from collections import deque
from pathlib import Path

import requests
from fastapi import FastAPI, File, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

# Works both as a package ("uvicorn dashboard.server:app") and when this file's
# own directory is on sys.path (running server.py directly).
try:
    from . import blocks
except ImportError:  # pragma: no cover - depends only on how the app was started
    import blocks

# Shared with the MCP tool of the same name, so both draw identical geometry.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import checks as checks_mod
import driveways
import keymap
import playbooks
import pressure_run
import profiles
import sheets
import spatial
import spatial_cache
import swales

# Overridable so the command runner can be exercised against a stub instead of a
# live drawing (CAD_PLUGIN_BASE=http://127.0.0.1:5399).
# 127.0.0.1, not "localhost": localhost resolves to IPv6 ::1 first on this machine
# and stalls ~2s per request before falling back to IPv4 (measured 2.06s vs 0.015s).
import cadagent_config
PLUGIN_BASE = cadagent_config.get("plugin_base")
PLUGIN_TIMEOUT = 30
# Whole-drawing commands measure thousands of labels or walk every xref; they take minutes,
# not seconds, and a 30 s read timeout was being reported as "not reachable" while the
# plugin was still busy finishing the job.
LONG_PLUGIN_TIMEOUT = 900
LONG_INTENTS = {"label_grading", "space_labels", "list_surface_label_styles", "lay_pressure_run",
                "profile_context", "draw_profile_views", "label_profile_views", "arrange_profile_views",
                "drag_profile_labels", "block_dynamic_properties"}
# Generated blocks that must read the drawing as EARLIER STEPS of the same run
# left it (sheets over profile views the run itself draws) are expanded at
# their turn, not before the run starts.
DEFERRED_EXPANDERS = {"profile_sheets", "profile_place", "profile_labels", "prompt"}

# This server's own address as a client last reached it, for the prompt block:
# its tool calls are the MCP server's functions run in this process, and they
# resolve the dashboard's drawing over HTTP from the dashboard server -- us.
_SELF_BASE: str | None = None

BASE_DIR = Path(__file__).resolve().parent
# Runs, attention items and avatars live here. A server aimed at a stub plugin
# (CAD_PLUGIN_BASE set) keeps its own folder, so a test run can never append to
# the live server's history or leave attention items on real dashboards;
# CAD_DASHBOARD_STATE points either kind of server somewhere else.
# A packaged install (cad-agent.json with data_dir) keeps it under the data dir
# instead, so the program folder stays read-only.
UPLOAD_DIR = Path(os.environ.get("CAD_DASHBOARD_STATE")
                  or cadagent_config.writable("dashboard",
                                              str(BASE_DIR / ("uploads-test" if os.environ.get("CAD_PLUGIN_BASE") else "uploads"))))
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR.mkdir(exist_ok=True)
if os.environ.get("CAD_PLUGIN_BASE"):
    # a stub-aimed server must not write stub layer families into the repo's learned file
    import layer_words
    spatial.TAGS.learned = layer_words.Learned(str(UPLOAD_DIR / "layer_tags.learned.json"))
    spatial.TAGS._bump()

_HISTORY_LEN = 400

app = FastAPI(title="CAD Agent Dashboards")


@app.middleware("http")
async def _no_stale_pages(request: Request, call_next):
    """The page, its script and its stylesheet are revalidated on every load.
    Without this a browser keeps index.html by its own heuristic (a tenth of
    the file's age, days for a file months old), and a kept index.html loads
    the old app.js under the old ?v=, so a new block renders without its UI
    and the server sees requests the page cannot answer. no-cache still
    lets the ETag answer 304, so a reload costs nothing."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache"
    return response


_history: deque = deque(maxlen=_HISTORY_LEN)
_clients: set[WebSocket] = set()

# dashboard id -> {"name": str, "file": str | None, "created": float}
_dashboards: dict[str, dict] = {}
_next_id = 1


def _avatar_path(dashboard_id: str) -> Path:
    safe = hashlib.md5(dashboard_id.encode("utf-8")).hexdigest()[:16]
    return UPLOAD_DIR / safe / "current.jpg"


async def _broadcast(event: dict) -> None:
    """Records the event (with a sequence number, so a pulling client can ask
    for everything since N), wakes long-pollers, then sends to every browser
    concurrently with a per-client timeout. Command sequences broadcast twice
    per command, so a single stalled browser tab must never be able to slow
    the run down -- a slow client gets dropped instead."""
    global _seq
    _seq += 1
    event["seq"] = _seq
    event.setdefault("_ts", time.time())
    _history.append(event)
    _signal.set()
    _signal.clear()
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


# ── Browser-open arbitration ──────────────────────────────────────────────────

_browser_claim_at = 0.0
_BROWSER_CLAIM_WINDOW = 10.0


@app.post("/claim-browser")
async def claim_browser():
    """Decides which cad_mcp_server.py process opens the browser window.

    Claude Desktop starts two of them about a second apart and both reach the
    point of wanting to open a tab. A marker file cannot settle this: check-then-
    write is not atomic, so both can pass the check before either writes, and the
    user gets two windows. This handler runs on the server's single event loop
    with no await between the check and the update, so exactly one caller wins.
    """
    global _browser_claim_at
    now = time.time()
    if now - _browser_claim_at < _BROWSER_CLAIM_WINDOW:
        return {"open": False}
    _browser_claim_at = now
    return {"open": True}


@app.post("/release-browser")
async def release_browser():
    """Hands the claim back when the winner could not actually open a browser, so
    its sibling a second later still gets a chance instead of no tab opening."""
    global _browser_claim_at
    _browser_claim_at = 0.0
    return {"ok": True}


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

    source = str(body.get("source") or "browser").strip()[:20]
    file = None
    if source == "redline":
        # the redline pipeline names the drawing printed on the sheet it was given
        file = str(body.get("file") or "").strip() or None
        if file:
            for other in _dashboards.values():
                if other["file"] and other["file"].lower() == file.lower():
                    return JSONResponse({"error": f"'{file}' is already assigned to {other['name']}."},
                                        status_code=400)
    did = str(_next_id)
    _next_id += 1
    _dashboards[did] = {
        "name": (body.get("name") or f"Dashboard {did}").strip(),
        "file": file,
        "source": source,
        "created": time.time(),
    }
    await _broadcast({"type": "dashboard_created", "dashboard": did,
                      "name": _dashboards[did]["name"], "_ts": time.time()})
    if file:
        await _broadcast({"type": "dashboard_file_set", "dashboard": did, "file": file, "_ts": time.time()})
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

# ── Runs ──────────────────────────────────────────────────────────────────────
#
# A run is one sequence on one dashboard: its commands, its conflict policy,
# what it found before drawing, how far it got, and what the drawing looked
# like afterwards. The record is the source of truth a pulling client reads;
# the websocket events are how the browser follows along live. Runs that need
# a decision -- paused on conflicts, failed, or changed something outside the
# planned area -- put an item on the attention queue, which is what a client
# that cannot be pinged asks for when it comes back.

RUN_POLICIES = ("warn", "pause", "stop")
_RUNS_FILE = UPLOAD_DIR / "runs.json"
# The grading key map read for a dashboard (its sheet cells), kept in memory by
# dashboard id and mirrored to disk by the TARGET drawing's path: dashboard ids
# start over when the server restarts, the drawing's path does not.
_KEYMAPS_FILE = UPLOAD_DIR / "keymaps.json"
_keymaps: dict[str, dict] = {}               # dashboard id -> key map record

_run_counter = 0
_runs: dict[str, "Run"] = {}                 # dashboard id -> its latest run
_run_history: deque = deque(maxlen=200)      # digests of finished runs
_attention: dict[str, dict] = {}             # attention id -> item
_attention_counter = 0

# Every broadcast event carries a sequence number so a client can ask for
# "everything since N"; _signal wakes long-pollers.
_seq = 0
_signal = asyncio.Event()


class Run:
    def __init__(self, dashboard_id: str, file: str, commands: list, policy: dict,
                 label: str = "", source: str = "browser", checks_spec: dict | None = None):
        global _run_counter
        _run_counter += 1
        self.id = f"{dashboard_id}-{_run_counter}"
        self.dashboard = dashboard_id
        self.file = file
        self.commands = commands
        self.policy = policy
        self.label = label
        self.source = source
        self.checks_spec = checks_spec     # {"plan_type": str, "checks": [...]} or None
        self.checks: dict | None = None    # results after the run
        self.status = "queued"          # queued | checking | paused | running | finished | failed | stopped
        self.total = len(commands)
        self.done = 0
        self.conflicts: list = []
        self.warnings: list = []
        self.results: list = []         # per step: the plugin's full reply (what a chat reads back)
        self.verification: dict | None = None
        self.error: str | None = None
        self.decisions: list = []
        self.prompt_created: list = []   # handles the prompt block's tool calls made or changed (verified by handle)
        self.prompt_version: str | None = None   # the tool vocabulary the prompt blocks were classified against
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.updated = self.created
        self.resume = asyncio.Event()
        self.task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    def block_counts(self) -> dict:
        counts: dict = {}
        for c in self.commands:
            name = c.get("expander") if c.get("intent") == "__expand__" else c.get("intent")
            name = str(name or "?")
            counts[name] = counts.get(name, 0) + 1
        return counts

    def digest(self) -> dict:
        return {
            "id": self.id, "dashboard": self.dashboard, "label": self.label, "source": self.source,
            "status": self.status, "policy": self.policy,
            "total": self.total, "done": self.done, "blocks": self.block_counts(),
            "conflict_count": len(self.conflicts),
            "conflict_summary": _conflict_summary(self.conflicts) if self.conflicts else {},
            "warnings": self.warnings[-5:],
            "verification": self.verification,
            "results": len(self.results),
            "last_result": (self.results[-1]["result"][:300] if self.results else None),
            "checks_plan": (self.checks_spec or {}).get("plan_type"),
            "checks": self.checks,
            "error": self.error,
            "decisions": self.decisions[-5:],
            "created": self.created, "started": self.started, "finished": self.finished,
            "updated": self.updated,
        }


def _touch(run: "Run") -> None:
    run.updated = time.time()


async def _attend(run: "Run", kind: str, message: str, detail: dict) -> dict:
    """Puts something that needs a decision on the attention queue and says so.
    Awaited, so the attention event lands before whatever the caller
    broadcasts next and one pause wakes a long-poller once, not twice."""
    global _attention_counter
    _attention_counter += 1
    item = {"id": f"a{_attention_counter}", "dashboard": run.dashboard, "run": run.id,
            "kind": kind, "message": message, "detail": detail,
            "created": time.time(), "resolved": None, "resolution": None}
    _attention[item["id"]] = item
    await _broadcast({"type": "attention", "dashboard": run.dashboard, "run": run.id,
                      "attention": item["id"], "kind": kind, "message": message,
                      "_ts": item["created"]})
    _save_runs()
    return item


def _resolve_attention(item_id: str, note: str) -> None:
    item = _attention.get(item_id)
    if item and not item["resolved"]:
        item["resolved"] = time.time()
        item["resolution"] = note
        _save_runs()


def _save_runs() -> None:
    """Best-effort: finished runs and the attention queue survive a restart so
    a client coming back later still sees what happened."""
    try:
        _RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _RUNS_FILE.open("w", encoding="utf-8") as f:
            json.dump({"runs": list(_run_history)[-200:],
                       "attention": list(_attention.values())[-200:]}, f)
    except Exception:
        pass


def _load_runs() -> None:
    global _attention_counter, _run_counter
    try:
        with _RUNS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    for r in data.get("runs") or []:
        if isinstance(r, dict):
            _run_history.append(r)
            try:
                _run_counter = max(_run_counter, int(str(r.get("id", "0-0")).rsplit("-", 1)[-1]))
            except ValueError:
                pass
    for i in data.get("attention") or []:
        if isinstance(i, dict) and i.get("id"):
            _attention[i["id"]] = i
            try:
                _attention_counter = max(_attention_counter, int(str(i["id"]).lstrip("a")))
            except ValueError:
                pass


_load_runs()


def _keymap_record(dashboard_id: str) -> dict | None:
    """The key map read for this dashboard: from memory, else the one saved for
    the dashboard's drawing (promoted into memory)."""
    rec = _keymaps.get(dashboard_id)
    if rec:
        return rec
    d = _dashboards.get(dashboard_id)
    if not d or not d.get("file"):
        return None
    try:
        with _KEYMAPS_FILE.open("r", encoding="utf-8") as f:
            stored = json.load(f)
    except Exception:
        return None
    rec = stored.get(spatial_cache.drawing_key(d["file"])) if isinstance(stored, dict) else None
    if isinstance(rec, dict) and rec.get("cells"):
        _keymaps[dashboard_id] = rec
        return rec
    return None


def _store_keymap(dashboard_id: str, record: dict) -> None:
    _keymaps[dashboard_id] = record
    try:
        try:
            with _KEYMAPS_FILE.open("r", encoding="utf-8") as f:
                stored = json.load(f)
        except Exception:
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        stored[spatial_cache.drawing_key(record["target"])] = record
        _KEYMAPS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _KEYMAPS_FILE.open("w", encoding="utf-8") as f:
            json.dump(stored, f)
    except Exception:
        pass    # best effort, like the run history


async def _read_key_map(dashboard_id: str, file: str, params: dict) -> dict:
    """Reads the key map drawing (params.key_map_file, else the plan type's
    key_map.file) through the plugin, turns its linework into cells, and keeps
    the result for the dashboard. Raises RuntimeError with the reason."""
    plan_type = str(params.get("plan_type") or "grading").strip() or "grading"
    km_spec: dict = {}
    try:
        km_spec = dict(sheets.load_spec(plan_type).get("key_map") or {})
    except (sheets.SheetError, playbooks.PlaybookError) as e:
        if not params.get("key_map_file"):
            raise RuntimeError(f"no key map file given and the {plan_type} sheet spec cannot name one: {e}")
    path = str(params.get("key_map_file") or km_spec.get("file") or "").strip()
    if not path:
        raise RuntimeError("say which drawing is the key map: key_map_file on the block, or key_map.file "
                           f"in playbooks/{plan_type}/sheets.json")
    if spatial_cache.drawing_key(path) == spatial_cache.drawing_key(file):
        raise RuntimeError("the key map must be a different drawing from the one the sheets go in")

    def pick(name, default):
        v = params.get(name)
        return v if v not in (None, "", [], 0) else km_spec.get(name, default)

    cell_layers = keymap.layer_list(pick("cell_layers", []))
    key_layers = keymap.layer_list(pick("key_layers", []))
    try:
        snap = float(pick("snap", keymap.DEFAULT_SNAP) or keymap.DEFAULT_SNAP)
    except (TypeError, ValueError):
        raise RuntimeError("snap must be a number of feet")
    pattern = str(pick("key_pattern", "") or "")
    try:
        # an empty layer list: model space only, no /nested merge (the cells are drawn here)
        entry = await asyncio.to_thread(spatial_cache.get, path, [], PLUGIN_BASE)
    except spatial_cache.PluginError as e:
        raise RuntimeError(f"could not read the key map {path}: {e}")
    try:
        result = keymap.cells_from_snapshot(entry.data, cell_layers, key_layers, snap, pattern or None)
    except keymap.KeyMapError as e:
        raise RuntimeError(f"{path}: {e}")
    words = keymap.describe_cells(result)
    if not result["cells"]:
        raise RuntimeError(f"no cell with a key found in {path}: " + "; ".join(w.strip() for w in words[1:8]))
    record = {"file": path, "target": file, "read_at": time.time(), "revision": entry.revision,
              "params": {"plan_type": plan_type, "cell_layers": cell_layers, "key_layers": key_layers,
                         "snap": snap, "key_pattern": pattern},
              "cells": result["cells"], "faces_without_key": result["faces_without_key"],
              "keys_without_cell": result["keys_without_cell"], "counts": result["counts"],
              "warnings": result["warnings"], "words": words}
    _store_keymap(dashboard_id, record)
    return record


@app.post("/dashboards/{dashboard_id}/keymap")
async def read_keymap(dashboard_id: str, request: Request):
    """Reads the grading key map for this dashboard and keeps its cells.
    Body: {key_map_file?, plan_type?, cell_layers?, key_layers?, snap?, key_pattern?}."""
    d = _dashboards.get(dashboard_id)
    if d is None:
        return JSONResponse({"error": f"No dashboard '{dashboard_id}'."}, status_code=404)
    if not d["file"]:
        return JSONResponse({"error": "Assign a drawing to this dashboard first: the key map's cells "
                                      "are kept for that drawing."}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        return await _read_key_map(dashboard_id, d["file"], body if isinstance(body, dict) else {})
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/dashboards/{dashboard_id}/keymap")
async def get_keymap(dashboard_id: str):
    d = _dashboards.get(dashboard_id)
    if d is None:
        return JSONResponse({"error": f"No dashboard '{dashboard_id}'."}, status_code=404)
    rec = _keymap_record(dashboard_id)
    if rec is None:
        return {"cells": [], "why": "no key map read for this dashboard yet: run the Grading Key Map block "
                                    "or read_key_map first"}
    return rec


@app.get("/dashboards/{dashboard_id}/keymap_cells")
async def list_keymap_cells(dashboard_id: str):
    """The keys of the cells read for this dashboard, for the Cut Grading Sheets
    picker. Lists what was read; it never opens the key map on its own, so
    drawing a form cannot open a drawing in Civil 3D."""
    d = _dashboards.get(dashboard_id)
    if d is None:
        return JSONResponse({"error": f"No dashboard '{dashboard_id}'."}, status_code=404)
    if not d["file"]:
        return {"keymap_cells": [], "why": "no drawing assigned yet"}
    rec = _keymap_record(dashboard_id)
    if rec is None:
        return {"keymap_cells": [], "why": "run the Grading Key Map block first"}
    return {"keymap_cells": [c["key"] for c in rec.get("cells") or []]}


@app.get("/dashboards/{dashboard_id}/alignments")
async def list_alignment_names(dashboard_id: str):
    """Alignment names in the dashboard's assigned drawing.

    Backs the block palette's alignment picker, so streets are chosen from what
    the drawing actually contains instead of being typed from memory."""
    d = _dashboards.get(dashboard_id)
    if d is None:
        return JSONResponse({"error": f"No dashboard '{dashboard_id}'."},
                            status_code=404)
    if not d["file"]:
        return {"alignments": [], "why": "no drawing assigned yet"}

    try:
        response = await asyncio.to_thread(
            requests.get, f"{PLUGIN_BASE}/modelspace",
            params={"file": d["file"]}, timeout=PLUGIN_TIMEOUT)
        data = response.json()
    except requests.exceptions.RequestException:
        return {"alignments": [], "why": "Civil 3D is not reachable"}
    except ValueError:
        return {"alignments": [], "why": "the drawing data came back unreadable"}

    if isinstance(data, dict) and "error" in data:
        return {"alignments": [], "why": str(data["error"])}

    names = [a.get("name") for a in (data.get("alignments") or []) if a.get("name")]
    return {"alignments": names}


# Bumped whenever the server or its static files change in a way a running
# instance cannot pick up. cad_mcp_server.py probes /version on startup and
# treats a server that doesn't answer with this value as stale.
DASHBOARD_VERSION = "2026-10-19.4"


@app.get("/version")
async def version():
    return {"version": DASHBOARD_VERSION, "blocks": len(blocks.BLOCK_SPECS)}


@app.get("/blocks")
async def list_blocks():
    """The block palette. The browser builds every parameter form from this, so
    adding a block to blocks.py is all it takes to expose it in the UI."""
    return {"blocks": blocks.palette(), "max_repeat": blocks.MAX_REPEAT}



async def _expand(payload: dict, file: str, dashboard_id: str = None) -> list:
    """Turns an "__expand__" placeholder into real payloads.

    Blocks like draw_driveways cannot be compiled up front: what they draw
    depends on what is in the drawing right now. So the runner reads the model
    space when it reaches one and expands it in place, keeping the block id on
    every payload so the browser still highlights the block it came from."""
    expander = payload.get("expander")
    if expander == "sheets":
        return await _expand_sheets(payload, file)
    if expander == "pressure_run":
        return await _expand_pressure_run(payload, file)
    if expander == "profile_views":
        return await _expand_profile_views(payload, file)
    if expander == "profile_sheets":
        return await _expand_profile_sheets(payload, file)
    if expander == "profile_place":
        return await _expand_profile_place(payload, file)
    if expander == "sanitary_profile":
        return await _expand_sanitary_profile(payload, file)
    if expander == "profile_labels":
        return await _expand_profile_labels(payload, file)
    if expander == "key_map":
        return await _expand_key_map(payload, file, dashboard_id)
    if expander == "grading_sheets":
        return await _expand_grading_sheets(payload, file, dashboard_id)
    planners = {"driveways": driveways.plan, "swales": swales.plan}
    if expander not in planners:
        raise RuntimeError(f"Unknown expanding block '{expander}'.")

    # The cached snapshot: model space plus the xrefed layers the index merges
    # (pads, road, lot lines ... from layer_tags.json), reused as long as the
    # drawing's revision stands. Pads are usually XREFED in, so model space
    # alone holds only the xref's block reference; the merge is what lets the
    # planner see them.
    params = payload.get("params") or {}
    lot_line_layer = str(params.get("lot_line_layer")
                         or swales.LOT_LINE_SUFFIX).strip().lower()
    layers = list(spatial.merge_layers())
    if lot_line_layer and lot_line_layer not in layers:
        layers.append(lot_line_layer)
    try:
        entry = await asyncio.to_thread(spatial_cache.get, file, layers, PLUGIN_BASE)
    except spatial_cache.PluginError as e:
        raise RuntimeError(str(e))
    data, index = entry.data, entry.index

    try:
        if expander == "driveways":
            built, summary = planners[expander](
                data,
                params.get("alignment"),
                params.get("layer", "0"),
                float(params.get("width", 20.0)),
                float(params.get("radius", 5.0)),
                float(params.get("max_offset", driveways.MAX_OFFSET)),
            )
        else:
            built, summary = planners[expander](
                data,
                params.get("alignment"),
                params.get("layer", "0"),
                float(params.get("offset", 5.0)),
                float(params.get("first_leg", 10.0)),
                swales.LEG_ANGLE,
                lot_line_layer,
                float(params.get("max_offset", driveways.MAX_OFFSET)),
            )
    except ValueError as e:
        raise RuntimeError(str(e))  # no pads, no road, unknown alignment

    if not built:
        raise RuntimeError(f"Nothing to draw: {summary['skipped']} pad(s) skipped.")

    # The dry-run gate: the planner's own output checked against the drawing
    # before anything is posted. A warning, not a refusal -- a crossing is worth
    # a look, and the first false positive would otherwise get the gate switched
    # off. Touching at an end (a fillet on the curb, a leg on a lot line) does
    # not count; only geometry that actually passes through something does.
    # Gating happens in the run loop, over expanded and hand-built payloads
    # together, so one policy decision covers the whole run.
    block_id = payload.get("_block")
    return [{**p, "_block": block_id, "_expanded": True} for p in built]


async def _expand_pressure_run(payload: dict, file: str) -> list:
    """The lay_pressure_run block: the alignment offset to a path with hydrant
    points (pressure_run.plan), as one plugin request with apply=true. The
    gate reads the path from the payload itself."""
    params = payload.get("params") or {}
    try:
        entry = await asyncio.to_thread(spatial_cache.get, file, None, PLUGIN_BASE)
    except spatial_cache.PluginError as e:
        raise RuntimeError(str(e))

    def num(k, default=None):
        v = params.get(k)
        return default if v in (None, "") else float(v)

    try:
        request, _ = pressure_run.plan(
            entry.data, params.get("alignment") or "", offset=num("offset", 10.0), side=params.get("side") or "right",
            from_station=num("from_station"), to_station=num("to_station"), hydrant_every=num("hydrant_every", 0.0),
            hydrant_part=params.get("hydrant_part") or "Hydrant", pipe_size=params.get("pipe_size") or "",
            network=params.get("network") or "", parts_list=params.get("parts_list") or "", layer=params.get("layer") or "")
    except pressure_run.PressureRunError as e:
        raise RuntimeError(str(e))
    request["apply"] = True
    return [{**request, "_block": payload.get("_block"), "_expanded": True}]


async def _expand_key_map(payload: dict, file: str, dashboard_id: str) -> list:
    """The read_key_map block: reads the key map drawing, keeps its cells for
    the dashboard, tells the run what it found, and adds no plugin step."""
    started = time.time()
    record = await _read_key_map(dashboard_id, file, payload.get("params") or {})
    keys = [c["key"] for c in record["cells"]]
    summary = {"file": record["file"], "count": len(keys), "keys": keys, "counts": record["counts"],
               "faces_without_key": record["faces_without_key"], "keys_without_cell": record["keys_without_cell"],
               "warnings": record["warnings"], "words": record["words"]}
    run = _runs.get(dashboard_id)
    if run is not None:
        run.results.append({"step": 0, "tool": "read_key_map", "block": payload.get("_block"), "stage": "read",
                            "ok": True, "error": None, "result": json.dumps(summary)[:_RESULT_KEEP],
                            "duration_ms": round((time.time() - started) * 1000)})
        run.warnings.extend(f"key map: {w}" for w in record["warnings"])
    shown = ", ".join(keys[:12]) + (", ..." if len(keys) > 12 else "")
    await _broadcast({"type": "keymap_read", "dashboard": dashboard_id, "run": run.id if run else None,
                      "block": payload.get("_block"), "count": len(keys), "keys": keys[:40],
                      "message": f"key map: {len(keys)} cell(s) ({shown})",
                      "duration_ms": round((time.time() - started) * 1000), "_ts": time.time()})
    return []


async def _expand_grading_sheets(payload: dict, file: str, dashboard_id: str) -> list:
    """The cut_grading_sheets block: label_grading then space_labels over the
    whole drawing (the spec's grading.labels / grading.spacing) -- one pass
    each, which is far faster than a pass per sheet -- then one sequence per
    chosen key-map cell. label_scope "cells" runs the label pair inside each
    cell's window instead (all the label steps first, then the sheets)."""
    params = payload.get("params") or {}
    plan_type = str(params.get("plan_type") or "grading").strip() or "grading"
    try:
        spec = sheets.load_spec(plan_type)
    except (sheets.SheetError, playbooks.PlaybookError) as e:
        raise RuntimeError(str(e))
    record = _keymap_record(dashboard_id)
    if not record:
        raise RuntimeError("no key map read for this dashboard yet: run the Grading Key Map block "
                           "(or read_key_map) first")
    try:
        entry = await asyncio.to_thread(spatial_cache.get, file, None, PLUGIN_BASE)
    except spatial_cache.PluginError as e:
        raise RuntimeError(str(e))
    keys = params.get("cells") or []
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",") if k.strip()]
    scale = params.get("scale")
    try:
        plan = sheets.plan_from_cells(entry.index, spec, record["cells"], plan_type,
                                      scale=float(scale) if scale not in (None, "", 0) else None,
                                      keys=[str(k) for k in keys] or None)
        built = sheets.sheet_payloads(plan, spec, entry.index, entry.data, plan_type, drawing=file,
                                      title=str(params.get("title") or ""))
    except sheets.SheetError as e:
        raise RuntimeError(str(e))
    block_id = payload.get("_block")
    out = []
    if _truthy(params.get("labels", True)):
        grading = spec.get("grading") or {}

        def label_pair():
            for block_type, section in (("label_grading", "labels"), ("space_labels", "spacing")):
                try:
                    yield blocks.build_block(block_type, {**(grading.get(section) or {}), "apply": True})
                except ValueError as e:
                    raise RuntimeError(f"grading.{section} in the {plan_type} sheet spec: {e}")

        # the label steps are part of the sheet cut (the _sheet marker keeps the run
        # a sheet cut, so its checks judge the whole drawing)
        if str(params.get("label_scope") or "drawing").lower() == "cells":
            for sheet, seq in zip(plan["sheets"], built):
                for p in label_pair():
                    p["window"] = [list(sheet["window"][0]), list(sheet["window"][1])]
                    out.append({**p, "_sheet": seq["_sheet"], "_block": block_id, "_expanded": True})
        else:
            for p in label_pair():
                out.append({**p, "_sheet": {"n": 0, "layout": "", "scope": "drawing"},
                            "_block": block_id, "_expanded": True})
    out.extend({**p, "_block": block_id, "_expanded": True} for p in built)
    run = _runs.get(dashboard_id)
    if plan["warnings"]:
        if run is not None:
            run.warnings.extend(f"grading sheets: {w}" for w in plan["warnings"])
        await _broadcast({"type": "run_warning", "dashboard": dashboard_id, "tool": "cut_grading_sheets",
                          "message": "; ".join(plan["warnings"])[:400], "_ts": time.time()})
    return out


async def _expand_sheets(payload: dict, file: str) -> list:
    """The cut_sheets block: the plan type's sheet spec, the drawing's index,
    then one sequence per sheet (see sheets.py). The plan itself is kept on
    every payload as _sheet so the browser and the verifier can name sheets."""
    params = payload.get("params") or {}
    plan_type = str(params.get("plan_type") or "").strip()
    if not plan_type:
        raise RuntimeError("cut_sheets needs a plan_type (a type under playbooks/ with a sheets.json).")
    try:
        spec = sheets.load_spec(plan_type)
    except (sheets.SheetError, playbooks.PlaybookError) as e:
        raise RuntimeError(str(e))
    try:
        entry = await asyncio.to_thread(spatial_cache.get, file, None, PLUGIN_BASE)
    except spatial_cache.PluginError as e:
        raise RuntimeError(str(e))
    scale = params.get("scale")
    max_sheets = params.get("max_sheets")
    twist = params.get("twist_deg")
    try:
        plan, built = sheets.plan_and_payloads(
            entry.index, entry.data, spec, plan_type,
            scale=float(scale) if scale not in (None, "", 0) else None,
            max_sheets=int(float(max_sheets)) if max_sheets not in (None, "", 0) else None,
            twist_deg=float(twist) if twist not in (None, "") else None,
            drawing=file, title=str(params.get("title") or ""))
    except sheets.SheetError as e:
        raise RuntimeError(str(e))
    block_id = payload.get("_block")
    return [{**p, "_block": block_id, "_expanded": True} for p in built]


def _alignment_names(params: dict) -> list:
    raw = params.get("alignment")
    if isinstance(raw, str):
        raw = raw.split(",")
    return [str(a).strip() for a in raw or [] if str(a).strip()]


async def _plan_profile_views(params: dict, file: str, skip_existing: bool = False) -> tuple:
    """(draw request, spec): the plugin's profile_context read and the views planned
    (profiles.plan: 1000 ft windows from 1+00, 36/40 ft spans, split to 500 ft, two to a
    sheet window). skip_existing: an alignment whose views exist is left out, not refused."""
    plan_type = str(params.get("plan_type") or "sanitary").strip()
    try:
        spec = sheets.load_spec(plan_type)
    except (sheets.SheetError, playbooks.PlaybookError):
        spec = sheets.default_spec(plan_type)      # views need no template; the profile defaults do
    request = {"intent": "profile_context", "file": file}
    names = _alignment_names(params)
    if names:
        request["alignments"] = names
    if params.get("surface"):
        request["surface"] = str(params["surface"])
    try:
        response = await asyncio.to_thread(requests.post, f"{PLUGIN_BASE}/execute", json=request, timeout=LONG_PLUGIN_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"profile_context: the plugin did not answer ({e})")
    error = _plugin_failure(response)
    if error:
        raise RuntimeError(f"profile_context: {error}")
    try:
        context = response.json()
    except ValueError:
        raise RuntimeError("profile_context: the plugin did not answer with JSON")
    try:
        entry = await asyncio.to_thread(spatial_cache.get, file, None, PLUGIN_BASE)
        data = entry.data
    except spatial_cache.PluginError:
        data = None
    styles = {}
    if params.get("structure_label"):
        styles["structure_label"] = str(params["structure_label"])
    span = params.get("span")
    try:
        draw, _summary = profiles.plan(
            context, span=float(span) if span not in (None, "") else None, alignments=names or None,
            styles=styles, spec=spec, data=data, station_host=str(params.get("station_host") or ""),
            surface=str(params.get("surface") or ""), skip_existing=skip_existing)
    except profiles.ProfileError as e:
        raise RuntimeError(str(e))
    draw["apply"] = True
    return draw, spec


async def _expand_profile_views(payload: dict, file: str) -> list:
    """The draw_profile_views block: the views planned and drawn (apply=true), then
    the deferred placement (arrange + labels). The gate reads the planned boxes
    from the payload's proposed."""
    params = payload.get("params") or {}
    draw, _spec = await _plan_profile_views(params, file)
    # the order is draw the views -> arrange them -> place labels and dimension lines: the
    # draw creates views and labels only; the placement is a deferred block, planned at its
    # turn from the drawing as the draw left it (an arrange first, then the placement pass),
    # so nothing placed is left behind by a move
    drag = {"intent": "drag_profile_labels", "views": [v["name"] for v in draw.get("views") or []],
            "styles": draw.get("styles"), "dim_lines": draw.get("dim_lines"), "drag_to_datum": draw.get("drag_to_datum"),
            "dimension_anchor": {**(draw.get("dimension_anchor") or {}), "at_creation": False},
            "station_host": draw.get("station_host"), "station_increment": draw.get("station_increment"),
            # anchors: the per-label DimensionAnchorOption/Value pair (verified live 2026-09-17,
            # 62/62, no crash); the label-group Default* pair is the one that crashed Civil 3D
            # the MH labels are created with their styles and left alone (user's call 2026-09-17;
            # anchor values are FEET in the API -- the spec's -3.0 in drew a 36 in tail)
            "do": {"dim_lines": True, "anchors": False, "reset_structure_anchors": True, "pipes": True, "stations": True},
            "proposed": draw.get("proposed")}
    place = {"intent": "__expand__", "expander": "profile_place", "params": {**params, "drag": drag}}
    return [{**draw, "_block": payload.get("_block"), "_expanded": True},
            {**place, "_block": payload.get("_block")}]


async def _expand_profile_place(payload: dict, file: str) -> list:
    """The second half of the draw_profile_views block, planned at its turn: the drawing's
    profile views arranged into their sheet windows (two per sheet, alphabetical -- the views
    the draw just made and any unpaired ones), THEN the placement pass that anchors the
    structure labels, inserts and stretches the dimension lines and seats the pipe and
    station labels on the views where they now stand."""
    params = payload.get("params") or {}
    plan_type = str(params.get("plan_type") or "sanitary").strip()
    try:
        spec = sheets.load_spec(plan_type)
    except (sheets.SheetError, playbooks.PlaybookError):
        spec = sheets.default_spec(plan_type)
    block_id = payload.get("_block")
    out = []
    try:
        entry = await asyncio.to_thread(spatial_cache.get, file, None, PLUGIN_BASE)
    except spatial_cache.PluginError as e:
        raise RuntimeError(str(e))
    try:
        arrange, _groups = sheets.arrange_profile_views(entry.data, spec, plan_type, alignments=_alignment_names(params) or None)
        if arrange.get("views"):
            arrange["dim_lines"] = dict(profiles.profile_spec(spec).get("dim_line") or {})
            out.append({**arrange, "_block": block_id, "_expanded": True})
    except sheets.SheetError as e:
        if not str(e).startswith("no profile views to arrange"):
            raise RuntimeError(str(e))
    drag = params.get("drag")
    if isinstance(drag, dict) and drag.get("views"):
        out.append({**drag, "_block": block_id, "_expanded": True})
    return out


async def _expand_sanitary_profile(payload: dict, file: str) -> list:
    """The Create Sanitary Profile block, in this SET order:
    1. draw JUST the profile views (no labels, no blocks), alphabetical by alignment,
       one transaction per alignment so an alignment that fails is skipped;
       alignments whose views already exist are left out;
    2. arrange the views two to a sheet window, centred, and cut the sheets
       (deferred profile_sheets, planned once the views exist);
    3. LAST, create and place the labels and the dimension line on every view
       (deferred profile_labels): pipe and station labels anchored to the view
       bottom, the "MH" and "MH -> Alignment" labels left where their styles put
       them."""
    params = payload.get("params") or {}
    block_id = payload.get("_block")
    draw, _spec = await _plan_profile_views(params, file, skip_existing=True)
    out = []
    if draw.get("views"):
        draw["skip_failures"] = True
        out.append({**draw, "_block": block_id, "_expanded": True})
    sheet_params = {k: params.get(k) for k in ("plan_type", "alignment", "alignments", "title") if k in params}
    out.append({"intent": "__expand__", "expander": "profile_sheets", "params": {**sheet_params, "tolerant": True}, "_block": block_id})
    label_params = {k: params.get(k) for k in ("plan_type", "alignment", "alignments", "station_host", "structure_label") if k in params}
    label_params["styles"] = draw.get("styles") or {}
    out.append({"intent": "__expand__", "expander": "profile_labels", "params": label_params, "_block": block_id})
    return out


async def _expand_profile_labels(payload: dict, file: str) -> list:
    """The last step of Create Sanitary Profile, planned at its turn: one
    drag_profile_labels over the drawing's profile views of the chosen
    alignments. It creates the labels a view lacks and the dimension line, and
    anchors the pipe and station labels to the view bottom; the MH labels are
    created and left alone (do.anchors off, no placement default at creation)."""
    params = payload.get("params") or {}
    plan_type = str(params.get("plan_type") or "sanitary").strip()
    try:
        spec = sheets.load_spec(plan_type)
    except (sheets.SheetError, playbooks.PlaybookError):
        spec = sheets.default_spec(plan_type)
    ps = profiles.profile_spec(spec)
    try:
        entry = await asyncio.to_thread(spatial_cache.get, file, None, PLUGIN_BASE)
    except spatial_cache.PluginError as e:
        raise RuntimeError(str(e))
    pvs = sheets.profile_views_in(entry.data, _alignment_names(params) or None)
    if not pvs:
        return []
    boxes = []
    for pv in pvs:
        try:
            x0, y0, x1, y1 = sheets._ring_box(pv["extents"])
            boxes.append({"min": [x0, y0 - 60.0], "max": [x1, y1]})
        except (KeyError, TypeError, ValueError):
            pass
    styles = dict(ps.get("styles") or {})
    styles.update({k: v for k, v in (params.get("styles") or {}).items() if v})
    if params.get("structure_label"):
        styles["structure_label"] = str(params["structure_label"])
    host = str(params.get("station_host") or ps.get("station_host") or "eg").strip().lower()
    drag = {"intent": "drag_profile_labels", "views": [pv.get("name") for pv in pvs],
            "styles": styles, "dim_lines": dict(ps.get("dim_line") or {}),
            "drag_to_datum": {"pipe": True, "station": True, "lift": ps["drag_lift"]},
            "dimension_anchor": {**dict(ps.get("dimension_anchor") or {}), "at_creation": False},
            "station_host": host, "station_increment": ps["station_increment"],
            "do": {"dim_lines": True, "anchors": False, "reset_structure_anchors": True, "pipes": True, "stations": True},
            "proposed": {"boxes": boxes, "segments": []}}
    return [{**drag, "_block": payload.get("_block"), "_expanded": True}]


async def _expand_profile_sheets(payload: dict, file: str) -> list:
    """The cut_profile_sheets block: the drawing's profile views (as the drawing
    is NOW -- this block is expanded at its turn) on sheets, two per sheet."""
    params = payload.get("params") or {}
    plan_type = str(params.get("plan_type") or "sanitary").strip()
    try:
        spec = sheets.load_spec(plan_type)
    except (sheets.SheetError, playbooks.PlaybookError) as e:
        raise RuntimeError(str(e))
    try:
        entry = await asyncio.to_thread(spatial_cache.get, file, None, PLUGIN_BASE)
    except spatial_cache.PluginError as e:
        raise RuntimeError(str(e))
    tolerant = bool(params.get("tolerant"))
    try:
        # the views are first moved into their sheet windows (two per sheet, alphabetical),
        # then every sheet is built from those planned windows
        arrange, groups = sheets.arrange_profile_views(entry.data, spec, plan_type, alignments=_alignment_names(params) or None)
        arrange["dim_lines"] = dict(profiles.profile_spec(spec).get("dim_line") or {})
    except sheets.SheetError as e:
        if not (tolerant and str(e).startswith("no profile views to arrange")):
            raise RuntimeError(str(e))
        return []            # every view is on a sheet already: nothing to arrange, nothing to cut
    try:
        _plan, built = sheets.profile_sheet_payloads(
            entry.data, entry.index, spec, plan_type, drawing=file, title=str(params.get("title") or ""),
            alignments=_alignment_names(params) or None, groups=groups)
    except sheets.SheetError as e:
        if not tolerant:
            raise RuntimeError(str(e))
        built = []
    block_id = payload.get("_block")
    out = [{**arrange, "_block": block_id, "_expanded": True}] if arrange.get("views") else []
    out.extend({**p, "_block": block_id, "_expanded": True} for p in built)
    return out


async def _warn_conflicts(dashboard_id: str, tool: str, index, payloads: list) -> list:
    """The dry-run gate for a run: every payload's proposed geometry checked
    against the cached index. On the dashboard this is a warning, not a
    refusal -- a person is watching and a crossing is worth a look, while the
    first false positive would otherwise get the gate switched off."""
    try:
        conflicts = index.conflicts(payloads)
    except Exception as e:  # the gate must never take the run down with it
        await _broadcast({"type": "run_warning", "dashboard": dashboard_id,
                          "tool": tool, "message": f"dry-run check skipped: {e}",
                          "_ts": time.time()})
        return []
    if conflicts:
        by = {}
        for c in conflicts:
            k = (c["kind"], c["tag"])
            by[k] = by.get(k, 0) + 1
        words = ", ".join(
            f"{n} {'cross' if kind == 'crossing' else 'land inside' if kind == 'inside' else 'sit within reach of'} "
            f"a {tag.replace('_', ' ')}" for (kind, tag), n in sorted(by.items()))
        await _broadcast({"type": "run_warning", "dashboard": dashboard_id, "tool": tool,
                          "message": f"{len(payloads)} payloads planned; {words}",
                          "crossings": conflicts[:50], "conflicts": conflicts[:50],
                          "_ts": time.time()})
    return conflicts


def _plugin_failure(response) -> str | None:
    """The plugin answers a failed command with HTTP 200 and a JSON body that says
    so ({"ok": false, "message": ...} from the drafting command set, or
    {"error": ...}), or with a 500. Any of those is a failed step: later blocks
    usually depend on the earlier ones, so the run stops there."""
    if response.status_code >= 400:
        return f"plugin returned HTTP {response.status_code}: {response.text[:200]}"
    try:
        body = response.json()
    except ValueError:
        return None  # legacy plain-text replies ("Line created.") are successes
    if not isinstance(body, dict):
        return None
    if body.get("error"):
        return str(body["error"])
    if body.get("ok") is False:
        message = str(body.get("message") or "the plugin reported a failure")
        # a failed sequence names the step; its own message says why
        step = body.get("failed_step")
        results = body.get("results")
        if isinstance(step, int) and isinstance(results, list) and 0 <= step < len(results) \
                and isinstance(results[step], dict) and results[step].get("message"):
            message += f" Step {step}: {results[step]['message']}"
        return message
    return None


async def _run_sequence(run: "Run") -> None:
    """One run, start to finish: expand the generated blocks, check everything
    against the drawing, apply the run's conflict policy, execute step by step,
    then rebuild the index and report what changed. The run record is the
    source of truth for status; events are how the browser and the status
    routes learn about it."""
    dashboard_id, file = run.dashboard, run.file
    completed, before, writes, commands = 0, None, [], run.commands
    try:
        run.status = "checking"
        _touch(run)
        # the tool vocabulary the prompt blocks' plans are checked against
        has_prompt = any(p.get("intent") == "__expand__" and p.get("expander") == "prompt" for p in commands)
        if has_prompt:
            run.prompt_version = await asyncio.to_thread(_prompt_tools_version)
        resolved = []
        for payload in commands:
            if payload.get("intent") == "__expand__" and payload.get("expander") not in DEFERRED_EXPANDERS:
                resolved.extend(await _expand(payload, file, dashboard_id))
            else:
                resolved.append(payload)
        commands = resolved
        run.total = len(commands)
        # A sheet cut draws nothing in model space, so its acceptance checks
        # judge the whole drawing rather than "what the run created". (A grading
        # cut's label steps carry _sheet too: they belong to a sheet.)
        run.sheet_cut = bool(commands) and all(
            isinstance(p, dict) and ("_sheet" in p or (p.get("expander") in DEFERRED_EXPANDERS and p.get("expander") != "prompt"))
            for p in commands)

        # The index from before the run: the gate for every payload, expanded
        # or hand-built, and the "before" half of the post-run check. Built
        # once per run, however many steps follow.
        writes = [p for p in commands
                  if str(p.get("intent") or "") not in spatial.READ_ONLY_INTENTS and p.get("intent") != "__expand__"]
        conflicts = []
        # a prompt block writes through the MCP gate, not through payloads:
        # nothing to check up front, but the "before" index is still wanted
        # so what it creates can be verified afterwards by handle
        if writes or has_prompt:
            try:
                before = (await asyncio.to_thread(spatial_cache.get, file, None, PLUGIN_BASE)).index
                conflicts = await _warn_conflicts(dashboard_id, "sequence", before, writes) if writes else []
            except Exception as e:
                run.warnings.append(f"dry-run check skipped: {e}")
                await _broadcast({"type": "run_warning", "dashboard": dashboard_id,
                                  "tool": "sequence", "message": f"dry-run check skipped: {e}",
                                  "_ts": time.time()})
        run.conflicts = conflicts
        if conflicts:
            if run.policy["on_conflict"] == "stop":
                raise RuntimeError(f"stopped before drawing anything: {len(conflicts)} conflict(s) "
                                   f"with the drawing ({_conflict_words(conflicts)})")
            if run.policy["on_conflict"] == "pause":
                await _pause_for_decision(run, conflicts)

        run.status = "running"
        run.started = time.time()
        _touch(run)
        await _broadcast({"type": "run_started", "dashboard": dashboard_id, "run": run.id,
                          "total": len(commands), "_ts": time.time()})
        i = 0
        while i < len(commands):
            payload = commands[i]
            if payload.get("intent") == "__expand__" and payload.get("expander") == "prompt":
                # the prompt block plans or executes at its turn through the
                # MCP server's own functions; it never becomes plugin payloads
                completed += await _run_prompt_block(run, payload, i + 1, len(commands))
                run.done = completed
                i += 1
                continue
            if payload.get("intent") == "__expand__":
                # deferred: planned against the drawing as the earlier steps left it
                try:
                    expanded = await _expand(payload, file, dashboard_id)
                except Exception as e:
                    raise RuntimeError(f"{payload.get('expander')} could not be planned at its turn: {e}")
                commands[i:i + 1] = expanded
                run.total = len(commands)
                writes.extend(p for p in expanded if str(p.get("intent") or "") not in spatial.READ_ONLY_INTENTS)
                if not expanded:
                    continue
                payload = commands[i]
            index = i + 1
            tool = payload["intent"]
            started = time.time()
            # "block" travels with every event so the browser can highlight the
            # block a step came from — a repeat fans one block out into many steps.
            block_id = payload.get("_block")
            await _broadcast({"type": "tool_call_started", "dashboard": dashboard_id,
                              "tool": tool, "block": block_id,
                              "args": {"step": f"{index}/{len(commands)}"},
                              "_ts": started})

            body = {k: v for k, v in payload.items() if not str(k).startswith("_")}
            body["file"] = file
            timeout = LONG_PLUGIN_TIMEOUT if tool in LONG_INTENTS else PLUGIN_TIMEOUT

            # Label commands carry no geometry until the plugin has computed
            # it, so they are gated at their turn: a compute pass first, its
            # proposed labels checked against the drawing, the run's policy
            # applied, and only then the real (apply) step.
            if tool in _COMPUTE_FIRST and _truthy(body.get("apply", True)) and before is not None:
                proposed = await _gate_compute_first(run, before, body, index, timeout)
                if proposed:
                    # what the compute pass proposed is what the apply pass draws:
                    # the verifier plans those cells, so the labels are not "unexpected"
                    writes.append({"proposed": proposed, "layer": body.get("layer", "")})

            try:
                response = await asyncio.to_thread(
                    requests.post, f"{PLUGIN_BASE}/execute", json=body, timeout=timeout
                )
                result, error = response.text, _plugin_failure(response)
            except requests.exceptions.ConnectionError as e:
                if isinstance(e, requests.exceptions.ConnectTimeout):
                    result, error = None, (f"Civil 3D did not accept the connection within {timeout} s.")
                else:
                    result, error = None, ("Civil 3D is not reachable — make sure AutoCAD is "
                                           "open with the CADAgent plugin loaded.")
            except requests.exceptions.Timeout:
                # The plugin keeps working after we stop waiting; the step may well finish on its own.
                result, error = None, (f"{tool} did not finish within {timeout} s. The plugin may still "
                                       "be working on it — check the drawing before running it again.")
            except requests.exceptions.RequestException as e:
                result, error = None, f"request to the plugin failed: {e}"

            await _broadcast({"type": "tool_call_finished", "dashboard": dashboard_id,
                              "tool": tool, "block": block_id,
                              "result": (result or "")[:300], "error": error,
                              "duration_ms": round((time.time() - started) * 1000),
                              "_ts": time.time()})
            run.results.append({"step": index, "tool": tool, "block": block_id, "stage": "run",
                                "ok": not error, "error": error,
                                "result": (result or "")[:_RESULT_KEEP],
                                "duration_ms": round((time.time() - started) * 1000)})
            if error:
                raise RuntimeError(error)
            completed += 1
            run.done = completed
            i += 1
    except asyncio.CancelledError:
        outcome = "stopped"
        raise
    except Exception as e:
        outcome = "failed"
        run.error = str(e)
    else:
        outcome = "finished"
    finally:
        # Verify first, then announce the outcome: a client woken by
        # run_finished must find the verification already on the record.
        after = None
        if completed and (writes or run.prompt_created):
            run.status = "verifying"
            _touch(run)
            after = await _verify_run(run, before, writes, handles=run.prompt_created or None)
        if run.checks_spec:
            await _check_run(run, after)
        run.status = outcome
        run.finished = time.time()
        _touch(run)
        if outcome == "stopped":
            await _broadcast({"type": "run_stopped", "dashboard": dashboard_id, "run": run.id,
                              "completed": completed, "_ts": time.time()})
        elif outcome == "failed":
            await _broadcast({"type": "run_failed", "dashboard": dashboard_id, "run": run.id,
                              "completed": completed, "error": run.error, "_ts": time.time()})
            await _attend(run, "failed", f"run failed after {completed} of {run.total} step(s): {run.error}",
                          {"completed": completed, "error": run.error, "conflicts": run.conflicts[:20]})
        else:
            await _broadcast({"type": "run_finished", "dashboard": dashboard_id, "run": run.id,
                              "completed": completed, "_ts": time.time()})
        _run_history.append(run.digest())
        _save_runs()


_COMPUTE_FIRST = {"label_grading", "space_labels"}
_RESULT_KEEP = 48_000       # characters of each step's reply kept on the run record


# ── The prompt block ──────────────────────────────────────────────────────────
#
# prompting/ does the work (compose the tool sequence, fill the parameters,
# run the steps through redline.act.Executor, record the label); this is the
# dashboard's side of it: which phase a block is in, the events the browser
# reads, the run's conflict policy, and the thread boundary. Everything that
# imports cad_mcp_server, calls a tool, calls the model, reads the spatial
# cache or writes the label set runs in a worker thread; everything that
# touches the run, broadcasts, or waits on a decision runs on the loop.


def _prompt_tools_version() -> str:
    from redline import tools
    return tools.version()


async def _run_prompt_block(run: "Run", payload: dict, step_index: int, total: int) -> int:
    """The prompt block at its turn, start to finish: compose the tool
    sequence, fill the steps (reads run live), record the plan and hand it to
    the browser (prompt_planned), then run the plan's writes in order through
    the Executor, pausing on or stopping at conflicts as the run's policy
    says, and hand the outcome back (prompt_executed). A block that arrives
    with a plan still good for its prompt (one a person edited on the card)
    skips the planning and runs that plan as it is. A plan that needs a
    person fails the run with the reason. Returns 1 when the block completed;
    raises to fail the run."""
    from prompting import backends as prompt_backends, compose as prompt_compose, plan as prompt_plan
    from prompting import record as prompt_record, run as prompt_run, session as prompt_session

    params = payload.get("params") or {}
    block_id = payload.get("_block")
    text = str(params.get("prompt") or "").strip()
    anchor = params.get("anchor")
    policy = run.policy["on_conflict"]
    label = f"{step_index}/{total}"

    why = prompt_plan.why_replan(params, run.prompt_version)
    if why and isinstance(params.get("plan"), dict):
        message = f"prompt block: planning again -- {why}"
        run.warnings.append(message)
        await _broadcast({"type": "run_warning", "dashboard": run.dashboard, "tool": "prompt",
                          "message": message, "_ts": time.time()})

    session = await asyncio.to_thread(prompt_session.open, run.dashboard, run.file, policy, _SELF_BASE, PLUGIN_BASE)
    reason = await asyncio.to_thread(prompt_backends.preflight, session.settings)
    if reason:
        raise RuntimeError(f"prompt block: {reason}")
    try:
        entry = await asyncio.to_thread(spatial_cache.get, run.file, None, PLUGIN_BASE)
    except spatial_cache.PluginError as e:
        raise RuntimeError(f"prompt block: {e}")
    index, data = entry.index, entry.data

    async def started(tool: str, stage: str, **args) -> float:
        t0 = time.time()
        await _broadcast({"type": "tool_call_started", "dashboard": run.dashboard, "tool": tool, "block": block_id,
                          "inner": True, "args": {"step": label, "stage": stage, **args}, "_ts": t0})
        return t0

    async def finished(tool: str, t0: float, result="", error=None) -> None:
        await _broadcast({"type": "tool_call_finished", "dashboard": run.dashboard, "tool": tool, "block": block_id,
                          "inner": True, "result": str(result or "")[:300], "error": error,
                          "duration_ms": round((time.time() - t0) * 1000), "_ts": time.time()})

    def record(stage: str, tool: str, ok: bool, error, body, t0: float) -> None:
        run.results.append({"step": step_index, "tool": tool, "block": block_id, "stage": stage,
                            "ok": bool(ok), "error": error, "result": json.dumps(body, default=str)[:_RESULT_KEEP],
                            "duration_ms": round((time.time() - t0) * 1000)})

    # ── plan phase ──────────────────────────────────────────────────────
    if why is not None:
        t0 = await started("compose", "plan", prompt=text[:80])
        seq = await asyncio.to_thread(prompt_compose.compose, text, session.sequencer)
        seq_info = {"tools": list(seq.tools), "unknown_tools": list(seq.unknown_tools), "error": seq.error,
                    "attempt": seq.attempt, "latency_s": round(seq.latency_s, 3)}
        await finished("compose", t0, result=", ".join(seq.tools), error=seq.error)
        record("plan", "compose", not seq.error, seq.error, seq_info, t0)

        planner = prompt_plan.Planner(text, anchor, seq.tools, session.filler, session.executor, run.dashboard,
                                      index, data, model=session.model, sequence_info=seq_info)
        while planner.i < len(planner.names) and not planner.stopped:
            tool = planner.names[planner.i]
            t0 = await started(tool, "plan")
            rec = await asyncio.to_thread(planner.next)
            if rec is None:
                break
            preview = rec["result_preview"] if rec["role"] == "read" else \
                f"{rec['status']}: {json.dumps(rec['params'], default=str)[:200]}"
            await finished(tool, t0, result=preview, error=rec.get("error"))
            record("plan", tool, rec["status"] not in ("not_filled", "failed"), rec.get("error"), rec, t0)
        plan = planner.plan()
        if not seq.tools:
            plan["status"], plan["reason"] = "needs_human", f"no tool sequence: {seq.error or 'the model named no tools'}"
        if seq.tools:
            plan["labelset_id"] = await asyncio.to_thread(prompt_record.record_plan, session.settings, text, seq.tools,
                                                          f"planned by {session.model}; status {plan['status']}")
        n_write = sum(1 for s in plan["steps"] if s["role"] == "write")
        if plan["status"] != "ready":
            message = f"needs a person: {plan['reason']}"
        elif n_write:
            message = f"planned {len(plan['steps'])} step(s): {', '.join(s['tool'] for s in plan['steps'])} -- executing"
        else:
            message = f"planned {len(plan['steps'])} lookup step(s) and ran them; nothing to write"
        # a snapshot: the execute phase goes on changing this dict, and the
        # event history must keep saying what the plan was when it was made
        await _broadcast({"type": "prompt_planned", "dashboard": run.dashboard, "run": run.id, "block": block_id,
                          "plan": json.loads(json.dumps(plan, default=str)), "message": message,
                          "ok": plan["status"] == "ready", "_ts": time.time()})
        _touch(run)
        if plan["status"] != "ready":
            # the card shows the plan and the step that stopped it; the run fails with the reason
            raise RuntimeError(f"prompt block needs a person: {plan['reason']}")
        if not n_write:
            return 1
    else:
        plan = params["plan"]

    # ── execute phase ───────────────────────────────────────────────────
    runner = prompt_run.Runner(plan, text, anchor, session.filler, session.executor, run.dashboard, index, data)
    pending = runner.pending()

    async def fail(reason: str) -> None:
        plan["status"], plan["reason"] = "failed", reason
        plan["executed"] = {"at": time.time(), "run": run.id, "created": list(runner.created),
                            "steps": _prompt_outcomes(plan)}
        await asyncio.to_thread(prompt_record.record_outcome, session.settings, plan.get("labelset_id"), f"run failed: {reason}"[:400])
        await _broadcast({"type": "prompt_executed", "dashboard": run.dashboard, "run": run.id, "block": block_id,
                          "plan": json.loads(json.dumps(plan, default=str)), "message": f"failed: {reason}",
                          "ok": False, "_ts": time.time()})
        _touch(run)

    for _ in range(len(pending)):
        idx = runner.pending()[0]
        tool = plan["steps"][idx]["tool"]
        t0 = await started(tool, "run")
        out = await asyncio.to_thread(runner.next)
        if out is None:
            break
        if out["status"] == "needs_human":
            await finished(tool, t0, error=out.get("error"))
            record("run", tool, False, out.get("error"), _prompt_slim(out), t0)
            reason = f"{tool}: {out.get('error')}"
            await fail(reason)
            raise RuntimeError(reason)
        if out["status"] == "blocked":
            found = out.get("blocked") or {}
            conflicts = list(found.get("conflicts") or [])
            count = int(found.get("count") or len(conflicts))
            if policy == "stop":
                await finished(tool, t0, error=out.get("error"))
                record("run", tool, False, out.get("error"), _prompt_slim(out), t0)
                reason = f"stopped at {tool}: {count} conflict(s) ({_conflict_words(conflicts)})"
                await fail(reason)
                raise RuntimeError(reason)
            # pause: the attention queue and a resume with its reason, then the
            # conflicts acknowledged with that reason and the step repeated
            # (warn never gets here: the Executor acknowledged and repeated itself)
            run.resume = asyncio.Event()
            await _pause_for_decision(run, conflicts)
            run.status = "running"
            _touch(run)
            decision = run.decisions[-1]["reason"] if run.decisions else "resumed"
            out = await asyncio.to_thread(runner.retry_after_ack, out["idx"], decision)
            if out["status"] == "blocked":
                out["status"] = "failed"
                out["error"] = f"still blocked after the acknowledgement: {out.get('error')}"
                plan["steps"][out["idx"]].update(status="failed", error=out["error"])
        await finished(tool, t0, result=out.get("result_preview"), error=out.get("error"))
        record("run", tool, out["status"] == "ran", out.get("error"), _prompt_slim(out), t0)
        run.prompt_created.extend(out.get("created") or [])
        if out["status"] != "ran":
            reason = f"{tool}: {out.get('error')}"
            await fail(reason)
            raise RuntimeError(reason)

    plan["status"] = "executed"
    plan["reason"] = None
    plan["executed"] = {"at": time.time(), "run": run.id, "created": list(runner.created), "steps": _prompt_outcomes(plan)}
    n_created = len(runner.created)
    if pending:
        message = f"executed {len(pending)} step(s)" + (f"; {n_created} object(s) created or changed" if n_created else "")
    else:
        message = "nothing to write: the plan held lookups only"
    await asyncio.to_thread(prompt_record.record_outcome, session.settings, plan.get("labelset_id"), f"executed OK: {message}"[:400])
    await _broadcast({"type": "prompt_executed", "dashboard": run.dashboard, "run": run.id, "block": block_id,
                      "plan": json.loads(json.dumps(plan, default=str)), "message": message, "ok": True,
                      "_ts": time.time()})
    _touch(run)
    return 1


def _prompt_outcomes(plan: dict) -> list:
    return [{k: s.get(k) for k in ("n", "tool", "status", "error", "result_preview", "created", "conflicts")}
            for s in plan.get("steps") or [] if s.get("role") == "write" or s.get("status") in ("ran", "failed")]


def _prompt_slim(out: dict) -> dict:
    return {k: v for k, v in out.items() if k not in ("blocked",)}


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


async def _gate_compute_first(run: "Run", before, body: dict, index: int, timeout: float):
    """Runs a label command's compute stage, checks what it proposes against
    the drawing, and applies the run's conflict policy before the apply step.
    The compute report is kept on the run record so a chat can read it.
    Returns the proposed geometry (None when the compute pass gave none)."""
    started = time.time()
    compute = {**body, "apply": False}
    try:
        response = await asyncio.to_thread(
            requests.post, f"{PLUGIN_BASE}/execute", json=compute, timeout=timeout)
        text = response.text
    except requests.exceptions.RequestException as e:
        run.warnings.append(f"step {index}: compute pass skipped: {e}")
        return
    run.results.append({"step": index, "tool": body.get("intent"), "block": body.get("_block"),
                        "stage": "compute", "ok": _plugin_failure(response) is None,
                        "error": _plugin_failure(response), "result": text[:_RESULT_KEEP],
                        "duration_ms": round((time.time() - started) * 1000)})
    try:
        report = json.loads(text)
    except ValueError:
        return
    if not isinstance(report, dict):
        return
    if report.get("ok") is False:
        raise RuntimeError(f"step {index} ({body.get('intent')}): compute failed: "
                           f"{report.get('message') or report.get('error') or 'see results'}")
    proposed = report.get("proposed")
    if not proposed:
        return None
    try:
        conflicts = before.conflicts([{"proposed": proposed, "layer": body.get("layer", "")}])
    except Exception as e:
        run.warnings.append(f"step {index}: proposed-geometry check skipped: {e}")
        return proposed
    if not conflicts:
        return proposed
    run.conflicts = list(run.conflicts) + conflicts
    await _broadcast({"type": "run_warning", "dashboard": run.dashboard, "tool": body.get("intent"),
                      "message": f"step {index}: {len(conflicts)} conflict(s) -- {_conflict_words(conflicts)}",
                      "conflicts": conflicts[:50], "_ts": time.time()})
    if run.policy["on_conflict"] == "stop":
        raise RuntimeError(f"stopped at step {index} before drawing: {len(conflicts)} conflict(s) "
                           f"({_conflict_words(conflicts)})")
    if run.policy["on_conflict"] == "pause":
        run.resume = asyncio.Event()
        await _pause_for_decision(run, conflicts)
        run.status = "running"
        _touch(run)
    return proposed


async def _pause_for_decision(run: "Run", conflicts: list) -> None:
    """The unattended answer to a conflict: stop before anything is drawn, put
    an item on the attention queue, and wait for a resume (or a stop). The
    resume carries a reason, which is logged with the decision."""
    run.status = "paused"
    _touch(run)
    item = await _attend(run, "conflicts",
                         f"paused before drawing: {len(conflicts)} conflict(s) -- {_conflict_words(conflicts)}",
                         {"conflicts": conflicts[:50], "conflict_count": len(conflicts),
                          "summary": _conflict_summary(conflicts)})
    await _broadcast({"type": "run_paused", "dashboard": run.dashboard, "run": run.id,
                      "attention": item["id"], "message": item["message"],
                      "conflicts": conflicts[:50], "_ts": time.time()})
    await run.resume.wait()
    _resolve_attention(item["id"], run.decisions[-1]["reason"] if run.decisions else "resumed")


async def _verify_run(run: "Run", before, payloads: list, handles: list | None = None):
    """After a run: rebuild the index and say what changed, in the words the
    run was planned in, flagging any changed cell outside the area the
    payloads should have touched. `handles` are objects the run made or
    changed without a payload (the prompt block's tool calls); their cells
    count as planned too. Returns the rebuilt index (or None)."""
    spatial_cache.invalidate(run.file)
    if before is None:
        return None
    try:
        after = (await asyncio.to_thread(spatial_cache.get, run.file, None, PLUGIN_BASE)).index
        report = before.verify(after, payloads, handles=handles)
    except Exception as e:
        run.warnings.append(f"post-run check skipped: {e}")
        await _broadcast({"type": "run_warning", "dashboard": run.dashboard, "tool": "verify",
                          "message": f"post-run check skipped: {e}", "_ts": time.time()})
        return None
    n_new, n_gone = len(report["new_handles"]), len(report["removed_handles"])
    message = (f"{report['changed_cells']} cell(s) changed, {n_new} new object(s), {n_gone} removed"
               + ("" if report["ok"] else
                  f"; {len(report['unexpected'])} changed OUTSIDE the planned area -- check the drawing"))
    run.verification = {"ok": report["ok"], "message": message,
                        "changed_cells": report["changed_cells"],
                        "changed": report["changed"][:12], "unexpected": report["unexpected"][:12],
                        "new_handles": report["new_handles"][:200],
                        "removed_handles": report["removed_handles"][:200]}
    await _broadcast({"type": "run_verified", "dashboard": run.dashboard, "run": run.id,
                      "ok": report["ok"], "message": message, "changed": report["changed"][:12],
                      "unexpected": report["unexpected"][:12],
                      "new_handles": report["new_handles"][:100], "_ts": time.time()})
    if not report["ok"]:
        await _attend(run, "unexpected", message,
                      {"unexpected": report["unexpected"][:12], "new_handles": report["new_handles"][:200]})
    return after


async def _check_run(run: "Run", after) -> None:
    """Acceptance checks from the run's playbook, over what the run created
    (the verification's new handles) or the whole drawing when there is no
    verification. Failures never change the run's outcome: they land on the
    record, in a run_checked event, and on the attention queue."""
    spec = run.checks_spec or {}
    try:
        if after is None:
            spatial_cache.invalidate(run.file)
            after = (await asyncio.to_thread(spatial_cache.get, run.file, None, PLUGIN_BASE)).index
        scope = None
        note = "scope: whole drawing (no verification for this run)"
        if getattr(run, "sheet_cut", False):
            note = "scope: whole drawing (a sheet cut draws nothing in model space)"
        elif run.verification and run.verification.get("new_handles") is not None:
            scope = set(run.verification.get("new_handles") or [])
            note = f"scope: the {len(scope)} handle(s) this run created"
        entry = spatial_cache.peek(run.file)
        data = entry.data if entry is not None else None      # the snapshot, for sheet_has
        results = await asyncio.to_thread(checks_mod.evaluate, after, spec.get("checks") or [], scope, data)
        summary = checks_mod.summarize(results)
        run.checks = {"plan_type": spec.get("plan_type"), **summary, "note": note, "results": results[:50]}
    except Exception as e:
        run.warnings.append(f"acceptance checks skipped: {e}")
        await _broadcast({"type": "run_warning", "dashboard": run.dashboard, "tool": "checks",
                          "message": f"acceptance checks skipped: {e}", "_ts": time.time()})
        return
    _touch(run)
    await _broadcast({"type": "run_checked", "dashboard": run.dashboard, "run": run.id,
                      "ok": run.checks["ok"], "message": run.checks["message"],
                      "failed": run.checks["failed_ids"], "warned": run.checks["warned_ids"], "_ts": time.time()})
    if not run.checks["ok"]:
        failed = [r for r in results if not r["ok"] and r["severity"] == "error"]
        await _attend(run, "checks", f"{spec.get('plan_type')} checks: {run.checks['message']}",
                      {"plan_type": spec.get("plan_type"),
                       "failed": [{k: r[k] for k in ("id", "kind", "tag", "expected", "actual", "offenders", "why")}
                                  for r in failed][:20]})


def _checks_of(raw) -> dict | None:
    """The run's acceptance checks: a plan type (its saved checks.json), an
    inline list, or {plan_type, checks} with inline checks overriding the file."""
    if raw in (None, "", [], {}):
        return None
    if isinstance(raw, str):
        return {"plan_type": raw.strip(), "checks": playbooks.load_checks(raw.strip())}
    if isinstance(raw, list):
        check_list = raw
        plan_type = "inline"
    elif isinstance(raw, dict):
        plan_type = str(raw.get("plan_type") or "inline")
        check_list = raw.get("checks")
        if check_list is None:
            check_list = playbooks.load_checks(plan_type)
    else:
        raise ValueError("checks must be a plan type, a list of checks, or {plan_type, checks}")
    errors = checks_mod.validate(check_list)
    if errors:
        raise ValueError("checks did not validate: " + "; ".join(errors[:8]))
    return {"plan_type": plan_type, "checks": check_list}


def _conflict_summary(conflicts: list) -> dict:
    by = {}
    for c in conflicts:
        k = f"{c.get('kind')}:{c.get('tag')}"
        by[k] = by.get(k, 0) + 1
    return by


def _conflict_words(conflicts: list) -> str:
    verbs = {"crossing": "cross", "inside": "land inside", "proximity": "sit within reach of"}
    by = {}
    for c in conflicts:
        k = (c.get("kind"), c.get("tag"))
        by[k] = by.get(k, 0) + 1
    return ", ".join(f"{n} {verbs.get(kind, kind)} a {str(tag).replace('_', ' ')}"
                     for (kind, tag), n in sorted(by.items(), key=lambda kv: str(kv[0])))


def _policy_of(raw) -> dict:
    """Normalises a run's policy. on_conflict: warn (draw anyway, log it -- the
    browser default), pause (stop before drawing and wait for a decision -- the
    unattended default), stop (fail the run)."""
    raw = raw if isinstance(raw, dict) else {}
    on_conflict = str(raw.get("on_conflict") or "warn").strip().lower()
    if on_conflict not in RUN_POLICIES:
        raise ValueError(f"policy.on_conflict must be one of {', '.join(RUN_POLICIES)}")
    return {"on_conflict": on_conflict}


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
    current = _runs.get(dashboard_id)
    if current is not None and current.active:
        return JSONResponse({"error": f"This dashboard is already running a sequence "
                                      f"(run {current.id}, {current.status})."},
                            status_code=409)

    global _SELF_BASE
    # how this client reached us is how the prompt block's in-process tool
    # calls will reach us (127.0.0.1, never localhost: see PLUGIN_BASE above)
    _SELF_BASE = str(request.base_url).rstrip("/").replace("//localhost", "//127.0.0.1")

    body = await request.json()
    try:
        # "blocks" is what the dashboard's sequence builder sends; "script" is the
        # equivalent typed syntax, kept for scripting the server without a browser.
        if body.get("blocks") is not None:
            commands = blocks.compile_blocks(body["blocks"])
        else:
            commands = blocks.parse_script(body.get("script", ""))
        policy = _policy_of(body.get("policy"))
        checks_spec = _checks_of(body.get("checks"))
    except (ValueError, playbooks.PlaybookError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    run = Run(dashboard_id, d["file"], commands, policy,
              label=str(body.get("label") or "").strip()[:80],
              source=str(body.get("source") or "browser").strip()[:20],
              checks_spec=checks_spec)
    _runs[dashboard_id] = run
    run.task = asyncio.create_task(_run_sequence(run))
    _touch(run)
    return {"queued": len(commands), "run": run.id, "policy": policy,
            "checks": checks_spec["plan_type"] if checks_spec else None}


@app.get("/dashboards/{dashboard_id}/status")
async def run_status(dashboard_id: str):
    run = _runs.get(dashboard_id)
    return {"running": bool(run and run.active), "run": run.digest() if run else None}


@app.get("/dashboards/{dashboard_id}/results")
async def run_results(dashboard_id: str, step: int | None = None):
    """The plugin's full reply for every step of the dashboard's latest run --
    the label reports, the created handles -- which the live events only
    carry a preview of."""
    run = _runs.get(dashboard_id)
    if run is None:
        return {"run": None, "results": []}
    results = run.results if step is None else [r for r in run.results if r["step"] == step]
    return {"run": run.digest(), "results": results}


@app.post("/dashboards/{dashboard_id}/stop")
async def stop_script(dashboard_id: str):
    run = _runs.get(dashboard_id)
    if run and run.active:
        run.task.cancel()
        return {"stopped": True, "run": run.id}
    return {"stopped": False}


@app.post("/dashboards/{dashboard_id}/resume")
async def resume_script(dashboard_id: str, request: Request):
    """Lets a paused run go ahead. The reason is recorded with the decision."""
    run = _runs.get(dashboard_id)
    if run is None or run.status != "paused":
        return JSONResponse({"error": "This dashboard has no paused run."}, status_code=409)
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = str((body or {}).get("reason") or "resumed").strip()[:400]
    run.decisions.append({"at": time.time(), "action": "resume", "reason": reason,
                          "by": str((body or {}).get("by") or "unknown")[:40]})
    run.resume.set()
    return {"resumed": True, "run": run.id}


# ── Status for a client that pulls ───────────────────────────────────────────
#
# Claude Desktop only acts inside a turn and cannot be pinged, so it reads:
# a digest per dashboard, the events since a cursor, and the open attention
# items. /wait blocks (briefly) until something needs a decision, so a pull
# can look like a push from the user's chair.

_ATTENTION_EVENTS = {"run_paused", "run_failed", "run_finished", "run_stopped", "attention"}


def _digest(since: int | None = None, dashboard_id: str | None = None, limit: int = 200) -> dict:
    boards = []
    for did, d in sorted(_dashboards.items(), key=lambda kv: kv[1]["created"]):
        if dashboard_id is not None and did != dashboard_id:
            continue
        run = _runs.get(did)
        boards.append({"id": did, "name": d["name"], "file": d["file"],
                       "status": run.status if run else "idle",
                       "run": run.digest() if run else None})
    events = []
    if since is not None:
        events = [e for e in _history if e.get("seq", 0) > since
                  and (dashboard_id is None or e.get("dashboard") == dashboard_id)
                  and e.get("type") not in ("tool_call_started", "tool_call_finished")]
        events = events[-limit:]
    open_items = [i for i in _attention.values() if not i["resolved"]
                  and (dashboard_id is None or i["dashboard"] == dashboard_id)]
    return {"cursor": _seq, "dashboards": boards, "events": events,
            "attention": sorted(open_items, key=lambda i: i["created"]),
            "recent_runs": [r for r in list(_run_history)[-20:]
                            if dashboard_id is None or r["dashboard"] == dashboard_id]}


@app.get("/status")
async def status_all(since: int = -1, dashboard: str | None = None):
    """since=-1 (the default) returns the whole recent history; a cursor from
    an earlier answer returns only what happened after it."""
    return _digest(since, dashboard)


@app.get("/wait")
async def wait_for_attention(since: int = -1, timeout: float = 40.0, dashboard: str | None = None):
    """Long poll: returns as soon as an event that needs a look (a pause, a
    failure, a finish, an attention item) lands after `since`, or when the
    timeout passes. Capped well under a minute so an MCP client never gives
    up on the call."""
    timeout = max(1.0, min(float(timeout), 45.0))
    deadline = time.time() + timeout
    while True:
        hits = [e for e in _history if e.get("seq", 0) > since and e.get("type") in _ATTENTION_EVENTS
                and (dashboard is None or e.get("dashboard") == dashboard)]
        if hits or time.time() >= deadline:
            out = _digest(since, dashboard)
            out["woke"] = [{"type": e["type"], "dashboard": e.get("dashboard"),
                            "message": e.get("message") or e.get("error")} for e in hits[-10:]]
            out["timed_out"] = not hits
            return out
        try:
            await asyncio.wait_for(_signal.wait(), timeout=max(0.05, deadline - time.time()))
        except asyncio.TimeoutError:
            pass


@app.post("/_test/playbooks_dir")
async def set_playbooks_dir(request: Request):
    """Test hook: point playbook lookups at a temporary folder so an end-to-end
    test never writes into the repo's playbooks/. Only honoured when the server
    is aimed at a stub plugin (CAD_PLUGIN_BASE set), never in normal use."""
    if not os.environ.get("CAD_PLUGIN_BASE"):
        return JSONResponse({"error": "test hooks are disabled"}, status_code=403)
    body = await request.json()
    path = str((body or {}).get("path") or "").strip()
    if not path or not os.path.isdir(path):
        return JSONResponse({"error": "path must be an existing folder"}, status_code=400)
    playbooks.PLAYBOOKS_DIR = path
    return {"ok": True, "playbooks_dir": path}


@app.get("/attention")
async def list_attention(include_resolved: bool = False):
    items = [i for i in _attention.values() if include_resolved or not i["resolved"]]
    return {"attention": sorted(items, key=lambda i: i["created"])}


@app.post("/attention/{item_id}/resolve")
async def resolve_attention(item_id: str, request: Request):
    if item_id not in _attention:
        return JSONResponse({"error": f"No attention item '{item_id}'."}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    _resolve_attention(item_id, str((body or {}).get("note") or "resolved")[:400])
    return {"resolved": True, "item": _attention[item_id]}


# ── Activity ──────────────────────────────────────────────────────────────────


@app.post("/events")
async def post_event(request: Request):
    """Ingested from cad_mcp_server.py's dashboard_events.emit() calls."""
    event = await request.json()
    # The prompt block calls the MCP server's functions inside THIS process;
    # their @observed wrapper reports here like any other caller's would. The
    # block already announces every step with its block id, so these twins
    # (tagged with our own pid) are dropped rather than counted twice.
    if event.get("type") in ("tool_call_started", "tool_call_finished") and event.get("session") == os.getpid():
        return {"ok": True, "dropped": "own process"}
    event["_ts"] = time.time()
    await _broadcast(event)          # records into the history as well
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
# ── Redline review results: the page at /redline.html reads these ───────────
#
# The review folder is the pipeline's own (redline/config.py resolves it:
# CAD_REDLINE_DIR, else the data dir's reviews folder, else
# dashboard/uploads[-test]/reviews), never UPLOAD_DIR/reviews. Everything is
# served through redline.view's guards: a review id is one or two plain path
# segments, a file is a relative path under that folder with an allowed suffix.
# These routes sit above the catch-all static mount and are registered most
# specific first because {review_id:path} is greedy.

def _rv():
    from redline import view as redline_view
    return redline_view


def _review_root() -> str:
    return _rv().review_dir()


def _review_404(review_id: str):
    return JSONResponse({"error": f"no review {review_id!r} under {_review_root()}"}, status_code=404)


def _labels_response(folder: str, review_id: str, extra: dict | None = None):
    from redline import labels as labels_mod
    v = _rv()
    report = v.load_report(folder) or {}
    labs = labels_mod.load(folder)
    from redline import metrics
    body = {"ok": True, "labels": labs.get("comments") or {},
            "counts": labels_mod.counts(labs, len(report.get("comments") or [])),
            "metrics": metrics.review_metrics(report, labs.get("comments") or {})}
    body.update(extra or {})
    return JSONResponse(body)


@app.get("/redline/reviews")
async def redline_reviews():
    from redline import labels as labels_mod
    root = _review_root()
    return {"review_dir": root, "whoami": labels_mod.whoami(), "reviews": _rv().list_reviews(root)}


@app.get("/redline/metrics")
async def redline_metrics(limit: int = Query(50, ge=1, le=500)):
    return _rv().aggregate_payload(_review_root(), limit=limit)


@app.get("/redline/reviews/{review_id:path}/file/{rel:path}")
async def redline_review_file(review_id: str, rel: str):
    v = _rv()
    folder = v.resolve(_review_root(), review_id)
    path = v.safe_file(folder, rel) if folder else None
    if not path:
        return JSONResponse({"error": "no such file"}, status_code=404)
    return FileResponse(path)


@app.get("/redline/reviews/{review_id:path}/labels")
async def redline_labels_get(review_id: str):
    folder = _rv().resolve(_review_root(), review_id)
    if not folder:
        return _review_404(review_id)
    return _labels_response(folder, review_id)


@app.put("/redline/reviews/{review_id:path}/labels/{comment_id}")
async def redline_label_put(review_id: str, comment_id: str, request: Request):
    from redline import labels as labels_mod
    v = _rv()
    folder = v.resolve(_review_root(), review_id)
    if not folder:
        return _review_404(review_id)
    report = v.load_report(folder) or {}
    if comment_id not in {c.get("id") for c in report.get("comments") or []}:
        return JSONResponse({"error": f"no comment {comment_id!r} in review {review_id!r}"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    labeller = str(body.get("labeller") or "").strip()
    if not labeller:
        return JSONResponse({"error": "labeller is required"}, status_code=400)
    known = v.vocabulary(folder) or None
    try:
        rec = labels_mod.set_label(folder, report.get("review_id") or review_id, comment_id, labeller,
                                   verdict=body.get("verdict"), tools=body.get("tools"), note=body.get("note") or "",
                                   tools_version=(report.get("vocabulary") or {}).get("version"), known=known)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return _labels_response(folder, review_id, {"label": rec})


@app.delete("/redline/reviews/{review_id:path}/labels/{comment_id}")
async def redline_label_delete(review_id: str, comment_id: str, labeller: str | None = Query(None)):
    from redline import labels as labels_mod
    folder = _rv().resolve(_review_root(), review_id)
    if not folder:
        return _review_404(review_id)
    labels_mod.clear_label(folder, comment_id, labeller or None)
    return _labels_response(folder, review_id)


@app.delete("/redline/reviews/{review_id:path}/labels")
async def redline_labels_discard(review_id: str):
    from redline import labels as labels_mod
    folder = _rv().resolve(_review_root(), review_id)
    if not folder:
        return _review_404(review_id)
    cleared = labels_mod.discard(folder)
    return _labels_response(folder, review_id, {"cleared_rows": cleared})


@app.get("/redline/reviews/{review_id:path}")
async def redline_review(review_id: str):
    body = _rv().payload(_review_root(), review_id)
    if body is None:
        return _review_404(review_id)
    return JSONResponse(body)


# ── Label set for fine-tuning: the page at /labelset.html reads these ────────
#
# A person types a comment and drags tool-name blocks into the chain that
# addresses it (redline/labelset.py). Kept in its own folder
# (CAD_REDLINE_LABELSET_DIR, else <review dir>/labelset), never in a review.
# No metrics, and no parameter is ever stored.

def _ls():
    from redline import labelset
    return labelset


def _labelset_payload(extra: dict | None = None):
    ls = _ls()
    body = {"ok": True}
    body.update(ls.payload(ls.folder()))
    body.update(extra or {})
    return JSONResponse(body)


async def _labelset_body(request: Request):
    try:
        body = await request.json()
    except Exception:
        return None, JSONResponse({"error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return None, JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    return body, None


@app.get("/redline/labelset")
async def labelset_get():
    return _labelset_payload()


@app.get("/redline/labelset/export")
async def labelset_export():
    ls = _ls()
    text = ls.jsonl_text(ls.load(ls.folder()))
    return Response(content=text, media_type="application/x-ndjson",
                    headers={"Content-Disposition": f'attachment; filename="{ls.JSONL}"'})


@app.post("/redline/labelset/examples")
async def labelset_add(request: Request):
    ls = _ls()
    body, err = await _labelset_body(request)
    if err:
        return err
    fold = ls.folder()
    try:
        ex = ls.add(fold, body.get("text"), body.get("tools"), labeller=body.get("labeller") or "",
                    note=body.get("note") or "", source=body.get("source") or "typed", known=ls.known_tools(fold))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return _labelset_payload({"example": ex})


@app.put("/redline/labelset/examples/{ex_id}")
async def labelset_update(ex_id: str, request: Request):
    ls = _ls()
    body, err = await _labelset_body(request)
    if err:
        return err
    fold = ls.folder()
    try:
        ex = ls.update(fold, ex_id, text=body.get("text"), tool_names=body.get("tools"), labeller=body.get("labeller"),
                       note=body.get("note"), source=body.get("source"), known=ls.known_tools(fold))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if ex is None:
        return JSONResponse({"error": f"no example {ex_id!r}"}, status_code=404)
    return _labelset_payload({"example": ex})


@app.delete("/redline/labelset/examples/{ex_id}")
async def labelset_delete(ex_id: str):
    ls = _ls()
    if not ls.remove(ls.folder(), ex_id):
        return JSONResponse({"error": f"no example {ex_id!r}"}, status_code=404)
    return _labelset_payload()


@app.delete("/redline/labelset")
async def labelset_discard():
    ls = _ls()
    n = ls.discard(ls.folder())
    return _labelset_payload({"discarded": n})


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
