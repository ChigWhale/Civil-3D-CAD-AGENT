"""The plan phase, and what a plan looks like.

A Planner fills the steps one at a time: prefill (the anchor's x/y), then
the helpers, then the redline LLM filler for whatever is left. Read-only
tools run for real as they are filled, so their handles and results feed
the steps after them; writes are only filled and shown. A write that
cannot be filled because its handle will only exist once an earlier write
has run is marked `needs_fill` and filled at execute time; any other
unfillable step ends the plan as `needs_human` with the reason.

The plan record travels back to the browser, lives in the block's
parameters, and comes back on the next Run:

  {"version": 1, "prompt", "anchor", "tools_version", "model", "created",
   "status": ready | needs_human | executed | failed, "reason",
   "sequence": {"tools", "unknown_tools", "error", "attempt", "latency_s"},
   "labelset_id", "steps": [step ...], "executed": null | {...}}

  step = {"n", "tool", "role": read | write, "params", "status",
          "filled_by": prefill | helper | llm | human | none, "helper_note",
          "missing", "result_preview", "error", "created", "conflicts",
          "latency_s", "bin", "risk"}

  read statuses: ran | failed | not_filled
  write statuses: planned | needs_fill | not_filled, then ran | failed | blocked
"""
import json
import re
import time

from redline import fill as fill_mod, locate, tools
from redline.types import Located, ToolStep

from . import compose, helpers

PLAN_VERSION = 1
PREVIEW = 400

_HANDLE_WORDS = re.compile(r"\b(handles?|targets?|entities|objects|sources?|boundary|curve|reference|primary)\b", re.I)


# ── pieces ───────────────────────────────────────────────────────────────────

def context_for(index, data, anchor_xy, radius: float = 100.0) -> dict:
    """The anchor's digest (pick, nearby objects, enclosing shapes, labels,
    texts, cell) the way a redline comment's arrow tip gets one; {} without
    an anchor or an index."""
    if not anchor_xy or index is None or data is None:
        return {}
    try:
        return locate.comment_context(index, data, anchor_xy, radius=radius)
    except Exception:
        return {}


def located_for(anchor_xy, context: dict) -> Located:
    xy = [round(float(anchor_xy[0]), 3), round(float(anchor_xy[1]), 3)] if anchor_xy else None
    return Located(comment_id="prompt", space="model", model_xy=xy, context=dict(context or {}))


def role_of(tool: str) -> str:
    return "write" if tools.is_write(tool) else "read"


def _xy(v):
    """(x, y) rounded, from a pair or an 'x,y' string; None for nothing."""
    if isinstance(v, str):
        v = [p for p in v.replace(";", ",").split(",") if p.strip()]
    try:
        return (round(float(v[0]), 3), round(float(v[1]), 3)) if v else None
    except (TypeError, ValueError, IndexError):
        return None


NO_LOCATION = "no location: name an object or give an anchor"


def fill_step(comment, located, context: dict, names: list, i: int, filler, known: set, results: list,
              text: str, index=None, data=None, log=None, retry_error: str | None = None):
    """(ToolStep, filled_by, helper_note, helper_params) for step i: prefill,
    helpers, then the model only for what they left open. A helper's refusal
    ("no hydrant found") is the step's error and the model is never asked.
    Handles the helpers read from the drawing join `known` (the caller's set)
    so this and later steps may name them."""
    tool = names[i]
    schema = tools.schema(tool)
    pre = fill_mod.prefill(tool, schema, located) if schema else {}
    h = None
    if schema:
        req = helpers.Request(tool=tool, schema=schema, text=text, anchor_xy=located.model_xy, context=context,
                              index=index, data=data, sequence=list(names), position=i, results=list(results),
                              known=set(known), prefilled=dict(pre))
        h = helpers.run_helpers(req)
    if h and h.refuse:
        step = ToolStep(tool=tool, status="not_filled", error=h.refuse)
        return step, "none", h.note, dict(h.params or {})
    if h and h.grounds:
        known.update(str(x) for x in h.grounds)
    # a lookup whose required parameters prefill alone satisfied needs no model either
    prefilled_lookup = bool(schema) and not tools.is_write(tool) and not helpers.required_missing(schema, pre)
    skip = bool(h and h.complete) or prefilled_lookup
    step = fill_mod.fill(comment, located, context, names, i, filler, known, results, log=log,
                         retry_error=retry_error, prefilled=(h.params if h else None), skip_model=skip)
    if step.status == "not_filled":
        by = "none"
    elif step.attempt == 0:
        by = "helper" if (h and h.params) else "prefill"
    else:
        by = "llm"
    return step, by, (h.note if h else ""), dict((h.params if h else None) or {})


def ungrounded_points(tool: str, params: dict, helper_params: dict, anchor) -> list:
    """Point parameters a model chose with nothing in the drawing to stand
    on: without an anchor, every point the helpers did not supply; a draw
    tool with no point at all is ungrounded too (where would it go?)."""
    if anchor:
        return []
    pts = _point_params(params)
    guessed = [k for k in pts if (helper_params or {}).get(k) != (params or {}).get(k)]
    if guessed:
        return guessed
    if not pts and tools.bin_of(tool) in ("draw", "text"):
        schema = tools.schema(tool)
        keys = [k for k in (schema.get("properties") or {})
                if fill_mod._POINT_KEY.search(k) and k not in ("offset", "scale", "angle_deg", "distance", "radius")]
        return keys[:1] or ["location"]
    return []


def step_record(n: int, step: ToolStep, filled_by: str, note: str) -> dict:
    return {"n": n, "tool": step.tool, "role": role_of(step.tool), "params": dict(step.params or {}),
            "status": step.status, "filled_by": filled_by, "helper_note": note or "", "missing": [],
            "result_preview": (step.result_preview or "")[:PREVIEW], "error": step.error,
            "created": list(step.created or []), "conflicts": int(step.conflicts or 0),
            "latency_s": round(float(step.latency_s or 0.0), 3),
            "bin": tools.bin_of(step.tool), "risk": tools.risk_of(step.tool)}


def _handle_trouble(error: str | None) -> bool:
    """Was the only thing wrong a handle the step could not name yet?"""
    e = str(error or "")
    if "is not in CONTEXT or RESULTS" in e:
        return True
    m = re.search(r"missing required parameter\(s\): (.*)$", e)
    if m:
        return all(_HANDLE_WORDS.search(k.strip()) for k in m.group(1).split(",") if k.strip())
    return bool(re.search(r"\b\w*handle\w*\b is empty|\b(handles|targets|entities|objects) is empty", e))


def _point_params(params: dict) -> list:
    """The parameter names that carry coordinates (a center, a position, points)."""
    out = []
    for k, v in (params or {}).items():
        if k in ("offset", "scale", "angle_deg", "distance", "radius") or not fill_mod._POINT_KEY.search(k):
            continue
        if isinstance(v, (list, tuple)) and v and all(isinstance(x, (int, float)) or isinstance(x, (list, tuple)) for x in v):
            out.append(k)
    return out


def _required_handle_keys(tool: str) -> list:
    schema = tools.schema(tool)
    return [k for k in (schema.get("required") or []) if k in fill_mod.HANDLE_KEYS or re.search("handle", k, re.I)]


# ── the plan phase ───────────────────────────────────────────────────────────

class Planner:
    def __init__(self, text: str, anchor_xy, sequence_tools: list, filler, executor, dashboard: str,
                 index=None, data=None, log=None, model: str = "", sequence_info: dict | None = None):
        self.text = str(text or "").strip()
        self.anchor = list(_xy(anchor_xy)) if _xy(anchor_xy) else None
        self.names = list(sequence_tools or [])
        self.filler, self.executor, self.dashboard = filler, executor, dashboard
        self.index, self.data = index, data
        self.log = log or (lambda m: None)
        self.model = model
        self.sequence_info = dict(sequence_info or {})
        self.context = context_for(index, data, self.anchor)
        self.located = located_for(self.anchor, self.context)
        self.comment = compose.comment_for(self.text)
        self.known = set(fill_mod.usable_handles(self.context))
        self.results: list = []
        self.steps: list = []
        self.i = 0
        self.stopped: str | None = None      # needs_human once a step cannot be filled or a read fails
        self.reason: str | None = None

    def next(self) -> dict | None:
        """Fills (and, for a read, runs) the next step; None when there are
        no more or the plan stopped."""
        if self.stopped or self.i >= len(self.names):
            return None
        i, tool = self.i, self.names[self.i]
        t0 = time.time()
        step, by, note, helper_params = fill_step(self.comment, self.located, self.context, self.names, i, self.filler,
                                                  self.known, self.results, self.text, self.index, self.data, log=self.log)
        rec = step_record(i + 1, step, by, note)
        if step.status == "not_filled":
            earlier_write = any(tools.is_write(t) for t in self.names[:i])
            if rec["role"] == "write" and earlier_write and _handle_trouble(step.error):
                rec["status"], rec["missing"] = "needs_fill", _required_handle_keys(tool)
                rec["error"] = None
                rec["helper_note"] = (note + "; " if note else "") + "handle comes from an earlier write: filled when it runs"
                self.results.append({"tool": tool, "preview": "(filled at run time)", "created": []})
            else:
                self.stopped, self.reason = "needs_human", f"{tool}: {step.error}"
        elif rec["role"] == "read":
            self.executor.run_step(step, self.dashboard)
            rec.update(status=step.status, error=step.error, result_preview=(step.result_preview or "")[:PREVIEW],
                       created=list(step.created or []), conflicts=int(step.conflicts or 0))
            if step.status == "ran":
                self.known.update(step.created or [])
                self.results.append({"tool": tool, "preview": step.result_preview, "created": list(step.created or [])})
            else:
                self.stopped, self.reason = "needs_human", f"{tool}: {step.error}"
        elif by == "llm" and ungrounded_points(tool, step.params, helper_params, self.anchor):
            # nothing in the drawing told the model where: a guessed point is
            # never drawn; the run fails here with the reason on the card
            rec.update(status="not_filled", error=NO_LOCATION, filled_by="none")
            self.stopped, self.reason = "needs_human", f"{tool}: {NO_LOCATION}"
        else:
            rec["status"] = "planned"
            self.results.append({"tool": tool, "preview": "(planned: " + json.dumps(step.params, default=str)[:200] + ")",
                                 "created": []})
        rec["latency_s"] = round(time.time() - t0, 3)
        self.steps.append(rec)
        self.i += 1
        return rec

    def plan(self) -> dict:
        return {"version": PLAN_VERSION, "prompt": self.text, "anchor": self.anchor,
                "tools_version": tools.version(), "model": self.model, "created": time.time(),
                "status": "needs_human" if self.stopped else "ready", "reason": self.reason,
                "sequence": self.sequence_info, "labelset_id": None, "steps": list(self.steps), "executed": None}


# ── which phase a block is in ────────────────────────────────────────────────

def why_replan(params: dict, tools_version: str | None = None) -> str | None:
    """None when the block's plan can be executed as it stands; else the
    one-line reason a fresh plan is made instead."""
    params = params or {}
    plan = params.get("plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        return "no plan yet"
    status = str(plan.get("status") or "")
    if status == "executed":
        return "the plan was already executed"
    if status == "failed":
        return "the last run of this plan failed"
    if status != "ready":
        return f"the plan is not ready ({status or 'no status'}): {plan.get('reason') or 'see the steps'}"
    if str(plan.get("prompt") or "").strip() != str(params.get("prompt") or "").strip():
        return "the prompt changed since the plan was made"
    if _xy(plan.get("anchor")) != _xy(params.get("anchor")):
        return "the anchor changed since the plan was made"
    if tools_version and plan.get("tools_version") and plan["tools_version"] != tools_version:
        return "the tool vocabulary changed since the plan was made"
    return None


def phase_of(params: dict, tools_version: str | None = None) -> str:
    return "run" if why_replan(params, tools_version) is None else "plan"
