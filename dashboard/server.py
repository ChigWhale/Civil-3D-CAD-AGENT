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
import json
import sys
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

# Shared with the MCP tool of the same name, so both draw identical geometry.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import driveways
import spatial
import spatial_cache
import swales

# Overridable so the command runner can be exercised against a stub instead of a
# live drawing (CAD_PLUGIN_BASE=http://127.0.0.1:5399).
# 127.0.0.1, not "localhost": localhost resolves to IPv6 ::1 first on this machine
# and stalls ~2s per request before falling back to IPv4 (measured 2.06s vs 0.015s).
PLUGIN_BASE = os.environ.get("CAD_PLUGIN_BASE", "http://127.0.0.1:5199")
PLUGIN_TIMEOUT = 30
# Whole-drawing commands measure thousands of labels or walk every xref; they take minutes,
# not seconds, and a 30 s read timeout was being reported as "not reachable" while the
# plugin was still busy finishing the job.
LONG_PLUGIN_TIMEOUT = 900
LONG_INTENTS = {"label_grading", "space_labels", "list_surface_label_styles"}

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
                 label: str = "", source: str = "browser"):
        global _run_counter
        _run_counter += 1
        self.id = f"{dashboard_id}-{_run_counter}"
        self.dashboard = dashboard_id
        self.file = file
        self.commands = commands
        self.policy = policy
        self.label = label
        self.source = source
        self.status = "queued"          # queued | checking | paused | running | finished | failed | stopped
        self.total = len(commands)
        self.done = 0
        self.conflicts: list = []
        self.warnings: list = []
        self.results: list = []         # per step: the plugin's full reply (what a chat reads back)
        self.verification: dict | None = None
        self.error: str | None = None
        self.decisions: list = []
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
DASHBOARD_VERSION = "2026-09-10.3"


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
        return str(body.get("message") or "the plugin reported a failure")
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
        resolved = []
        for payload in commands:
            if payload.get("intent") == "__expand__":
                resolved.extend(await _expand(payload, file, dashboard_id))
            else:
                resolved.append(payload)
        commands = resolved
        run.total = len(commands)

        # The index from before the run: the gate for every payload, expanded
        # or hand-built, and the "before" half of the post-run check. Built
        # once per run, however many steps follow.
        writes = [p for p in commands
                  if str(p.get("intent") or "") not in spatial.READ_ONLY_INTENTS]
        conflicts = []
        if writes:
            try:
                before = (await asyncio.to_thread(spatial_cache.get, file, None, PLUGIN_BASE)).index
                conflicts = await _warn_conflicts(dashboard_id, "sequence", before, writes)
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

            body = {k: v for k, v in payload.items() if k not in ("_block", "_expanded")}
            body["file"] = file
            timeout = LONG_PLUGIN_TIMEOUT if tool in LONG_INTENTS else PLUGIN_TIMEOUT

            # Label commands carry no geometry until the plugin has computed
            # it, so they are gated at their turn: a compute pass first, its
            # proposed labels checked against the drawing, the run's policy
            # applied, and only then the real (apply) step.
            if tool in _COMPUTE_FIRST and _truthy(body.get("apply", True)) and before is not None:
                await _gate_compute_first(run, before, body, index, timeout)

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
        if completed and writes:
            run.status = "verifying"
            _touch(run)
            await _verify_run(run, before, writes)
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


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


async def _gate_compute_first(run: "Run", before, body: dict, index: int, timeout: float) -> None:
    """Runs a label command's compute stage, checks what it proposes against
    the drawing, and applies the run's conflict policy before the apply step.
    The compute report is kept on the run record so a chat can read it."""
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
        return
    try:
        conflicts = before.conflicts([{"proposed": proposed, "layer": body.get("layer", "")}])
    except Exception as e:
        run.warnings.append(f"step {index}: proposed-geometry check skipped: {e}")
        return
    if not conflicts:
        return
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


async def _verify_run(run: "Run", before, payloads: list) -> None:
    """After a run: rebuild the index and say what changed, in the words the
    run was planned in, flagging any changed cell outside the area the
    payloads should have touched."""
    spatial_cache.invalidate(run.file)
    if before is None:
        return
    try:
        after = (await asyncio.to_thread(spatial_cache.get, run.file, None, PLUGIN_BASE)).index
        report = before.verify(after, payloads)
    except Exception as e:
        run.warnings.append(f"post-run check skipped: {e}")
        await _broadcast({"type": "run_warning", "dashboard": run.dashboard, "tool": "verify",
                          "message": f"post-run check skipped: {e}", "_ts": time.time()})
        return
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

    body = await request.json()
    try:
        # "blocks" is what the dashboard's sequence builder sends; "script" is the
        # equivalent typed syntax, kept for scripting the server without a browser.
        if body.get("blocks") is not None:
            commands = blocks.compile_blocks(body["blocks"])
        else:
            commands = blocks.parse_script(body.get("script", ""))
        policy = _policy_of(body.get("policy"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    run = Run(dashboard_id, d["file"], commands, policy,
              label=str(body.get("label") or "").strip()[:80],
              source=str(body.get("source") or "browser").strip()[:20])
    _runs[dashboard_id] = run
    run.task = asyncio.create_task(_run_sequence(run))
    _touch(run)
    return {"queued": len(commands), "run": run.id, "policy": policy}


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
    return _digest(None if since < 0 else since, dashboard)


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
            out = _digest(None if since < 0 else since, dashboard)
            out["woke"] = [{"type": e["type"], "dashboard": e.get("dashboard"),
                            "message": e.get("message") or e.get("error")} for e in hits[-10:]]
            out["timed_out"] = not hits
            return out
        try:
            await asyncio.wait_for(_signal.wait(), timeout=max(0.05, deadline - time.time()))
        except asyncio.TimeoutError:
            pass


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
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
