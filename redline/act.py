"""Running the tool sequence for one located comment, with parameters.

The tools are the MCP server's own functions, called in this process with
the dashboard assigned to the drawing, so every write goes through the
same spatial gate Claude Desktop's calls do: a conflicting write comes back
blocked with a token instead of being drawn. Policy `pause` records that
and stops the comment; `warn` acknowledges the conflicts and repeats the
call. A dry run fills every step's parameters, runs the read-only steps
for real (their results feed the next step) and never runs a write.

Lookups' results feed the handle set the next step may use. Nothing here
raises; every failure is a status on the Action.
"""
import json
import re
import sys
import time

from . import config, fill as fill_mod, tools
from .runner import DashboardClient, DashboardError
from .transform import PaperToModel
from .types import Action, ToolStep

_HEX = re.compile(r"^[0-9A-Fa-f]{1,16}$")
PREVIEW = 1500


def _preview(text: str, limit: int = PREVIEW) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


def _brief(o: dict) -> dict:
    keep = {k: o.get(k) for k in ("handle", "type", "layer", "name", "text", "distance") if o.get(k) is not None}
    return keep


def compact_result(name: str, body) -> object:
    """What the next step (and the model) is shown of a result: the lookups'
    replies are trimmed to what matters so they stay whole within PREVIEW."""
    if not isinstance(body, dict):
        return body
    if name == "object_at":
        return {"object": body.get("object"), "labels": [_brief(l) for l in body.get("labels") or []],
                "candidates": [_brief(c) for c in body.get("candidates") or []],
                "enclosing": [dict(_brief(e), area=e.get("area"), geometry=e.get("geometry")) for e in (body.get("enclosing") or [])[:3]]}
    if name in ("nearby", "find_entities"):
        objs = body.get("objects") or []
        out = {k: body.get(k) for k in ("count", "tag", "region") if k in body}
        out["objects"] = [o if len(objs) <= 4 else _brief(o) for o in objs[:12]]
        if "handles" in body:
            out["handles"] = body["handles"][:40]
        return out
    if name == "describe_location":
        return {k: body.get(k) for k in ("description", "frame", "region", "nearest") if k in body}
    slim = dict(body)
    if isinstance(slim.get("spatial"), dict):
        slim["spatial"] = {k: v for k, v in slim["spatial"].items() if k in ("verified", "ok", "note", "changed")}
    return slim


def handles_in(result) -> list:
    """Handles a tool result carries: under the usual keys, or any list of
    hex-looking strings."""
    out = []

    def walk(v, key=""):
        if isinstance(v, dict):
            for k, vv in v.items():
                walk(vv, k)
        elif isinstance(v, list):
            if v and all(isinstance(x, str) and _HEX.match(x) for x in v) and key in (
                    "handles", "created", "modified", "new_handles", "deleted", "handle", "found", "entities"):
                out.extend(v)
            else:
                for x in v:
                    walk(x, key)
        elif isinstance(v, str) and key in ("handle", "created", "new_handle") and _HEX.match(v):
            out.append(v)
    walk(result)
    seen, uniq = set(), []
    for h in out:
        if h.upper() not in seen:
            seen.add(h.upper())
            uniq.append(h)
    return uniq


def pick_handles(body: dict, tolerance: float) -> list:
    """The handles an object_at result makes usable for the next steps: the
    pick, its labels and the enclosing shapes always; the candidates only
    when something was picked (they are then within tolerance too) or when
    they lie within NEAR_MISS_REACH tolerances of the point. A far near-miss
    stays visible in the result but may not be acted on."""
    if not isinstance(body, dict):
        return handles_in(body)
    out = []
    obj = body.get("object")
    if isinstance(obj, dict) and obj.get("handle"):
        out.append(str(obj["handle"]))
        out += [str(l["handle"]) for l in obj.get("labels") or [] if isinstance(l, dict) and l.get("handle")]
    out += [str(l["handle"]) for l in body.get("labels") or [] if isinstance(l, dict) and l.get("handle")]
    out += [str(e["handle"]) for e in body.get("enclosing") or [] if isinstance(e, dict) and e.get("handle")]
    reach = fill_mod.NEAR_MISS_REACH * float(tolerance or 5.0)
    for c in body.get("candidates") or []:
        if not (isinstance(c, dict) and c.get("handle")):
            continue
        if obj or c.get("distance") is None or float(c["distance"]) <= reach:
            out.append(str(c["handle"]))
    seen, uniq = set(), []
    for h in out:
        if h.upper() not in seen:
            seen.add(h.upper())
            uniq.append(h)
    return uniq


def window_of(match) -> list | None:
    """The model-space box of the sheet's viewports, for the point check."""
    try:
        xs, ys = [], []
        for v in (getattr(match, "viewports", None) or []):
            pm = PaperToModel(tuple(v["paper_center"]), tuple(v["paper_size"]), float(v["scale"]),
                              float(v.get("twist_deg") or 0.0), tuple(v["model_center"]), str(v.get("handle") or ""))
            for x, y in pm.model_ring():
                xs.append(x); ys.append(y)
        if not xs:
            return None
        return [[round(min(xs), 1), round(min(ys), 1)], [round(max(xs), 1), round(max(ys), 1)]]
    except (KeyError, TypeError, ValueError):
        return None


class Executor:
    def __init__(self, settings=None, mode: str = "dry", on_conflict: str = "pause", backend=None, log=None,
                 invoke=None):
        self.settings = settings or config.load()
        self.mode = mode
        self.on_conflict = (on_conflict or "pause").lower()
        self.backend = backend
        self.log = log or (lambda m: None)
        self._invoke = invoke or self._invoke_tool
        self.ack_reason = "redline review, policy warn"    # recorded with every warn-policy acknowledgement
        self.client = DashboardClient(self.settings.dashboard_base)
        self._dash: dict = {}
        self._point_servers()

    def _point_servers(self):
        """The MCP server module reaches the plugin and the dashboard through
        module globals; aim them where the settings say (the stub in tests)."""
        if str(config.ROOT) not in sys.path:
            sys.path.insert(0, str(config.ROOT))
        try:
            import cad_mcp_server
            import dashboard_events
            import spatial_cache
            cad_mcp_server.BASE = self.settings.plugin_base
            dashboard_events.DASHBOARD_BASE = self.settings.dashboard_base
            spatial_cache.PLUGIN_BASE = self.settings.plugin_base
        except Exception as e:
            self.log(f"  (could not point the MCP server at the configured servers: {e})")

    # ── invoking ───────────────────────────────────────────────────────────

    def dashboard_for(self, dwg_path: str) -> str:
        key = dwg_path.lower()
        if key not in self._dash:
            self._dash[key] = self.client.ensure_dashboard(dwg_path)
        return self._dash[key]

    def _invoke_tool(self, name: str, params: dict, dashboard: str):
        t = tools.registry().get(name)
        if t is None:
            raise RuntimeError(f"unknown tool {name}")
        kwargs = dict(params)
        if tools.takes_dashboard(name):
            kwargs["dashboard"] = dashboard
        out = t.fn(**kwargs)
        if hasattr(out, "__await__"):
            import asyncio
            out = asyncio.run(out)
        return out

    @staticmethod
    def _parse(result):
        if isinstance(result, (dict, list)):
            return result
        try:
            return json.loads(result)
        except (TypeError, ValueError):
            return {"result": str(result)}

    def run_step(self, step: ToolStep, dashboard: str) -> ToolStep:
        """Runs one filled step; honours the conflict policy."""
        for attempt in (1, 2):
            t0 = time.time()
            try:
                raw = self._invoke(step.tool, step.params, dashboard)
            except Exception as e:
                step.status, step.error = "failed", f"{e.__class__.__name__}: {str(e)[:300]}"
                step.latency_s += time.time() - t0
                return step
            step.latency_s += time.time() - t0
            body = self._parse(raw)
            step.result_preview = _preview(json.dumps(compact_result(step.tool, body), default=str))
            if isinstance(body, dict) and body.get("blocked"):
                step.conflicts = int(body.get("conflict_count") or len(body.get("conflicts") or []))
                token = body.get("token")
                step.token = str(token) if token else None
                step.blocked = {"count": step.conflicts, "conflicts": list(body.get("conflicts") or [])[:50]}
                if self.on_conflict == "warn" and token and attempt == 1:
                    try:
                        self._invoke("acknowledge_conflicts", {"token": token, "reason": self.ack_reason}, dashboard)
                        self.log(f"    {step.tool}: {step.conflicts} conflict(s) acknowledged (warn), repeating")
                        continue
                    except Exception as e:
                        step.status, step.error = "failed", f"acknowledge failed: {e}"
                        return step
                step.status, step.error = "blocked", f"{step.conflicts} spatial conflict(s); policy {self.on_conflict}"
                return step
            if isinstance(body, dict) and (body.get("error") or body.get("ok") is False):
                step.status, step.error = "failed", _preview(body.get("error") or body.get("message") or "the tool reported a failure", 300)
                return step
            step.created = pick_handles(body, float(step.params.get("tolerance") or 5.0)) if step.tool == "object_at" else handles_in(body)
            step.status = "ran"
            return step
        return step

    # ── one comment ────────────────────────────────────────────────────────

    def act(self, key: str, comment, located, match, sequence: list, unknown_tools=()) -> Action:
        """The sequence filled and run for one located target, every step
        stamped with its bin and risk tier, and the doability record
        (grounded / action covered / parameters determinable / unambiguous)
        for the comment. `unknown_tools` are the names the sequencer
        dropped, which count against action coverage."""
        action = self._act(key, comment, located, match, sequence)
        for s in action.steps:
            s.bin, s.risk = tools.bin_of(s.tool), tools.risk_of(s.tool)
        if located is not None and getattr(located, "space", "none") != "none":
            from . import metrics
            action.doability = metrics.doability(getattr(located, "context", None) or {}, list(sequence or []),
                                                 [s.to_dict() for s in action.steps], list(unknown_tools or []))
        return action

    def _act(self, key: str, comment, located, match, sequence: list) -> Action:
        action = Action(key=key, comment_id=comment.id, model=getattr(self.backend, "name", ""))
        if located is None or located.space == "none":
            action.status, action.reason = "not_located", getattr(located, "reason", None) or "not located"
            return action
        if not sequence:
            action.status, action.reason = "skipped", "no tool sequence"
            return action
        dwg = getattr(match, "dwg_path", None)
        if not dwg:
            action.status, action.reason = "not_located", "no drawing matched"
            return action
        action.dwg_path = dwg
        try:
            action.dashboard = self.dashboard_for(dwg)
        except DashboardError as e:
            action.status, action.reason = "failed", f"dashboard: {e}"
            return action
        if self.mode == "run" and self.client.busy(action.dashboard):
            action.status, action.reason = "failed", "dashboard_busy"
            return action
        context = located.context or {}
        known = fill_mod.usable_handles(context)
        window = window_of(match)
        results = []
        wrote = False
        for i, tool in enumerate(sequence):
            step = fill_mod.fill(comment, located, context, sequence, i, self.backend, known, results, window=window, log=self.log)
            action.steps.append(step)
            if step.status == "not_filled":
                action.status, action.reason = "needs_human", f"{tool}: {step.error}"
                self.log(f"    {tool}: not filled ({step.error})")
                return action
            if tools.is_write(tool) and self.mode != "run":
                step.status = "dry"
                wrote = True
                self.log(f"    {tool} {json.dumps(step.params, default=str)[:160]} (dry run: not executed)")
                results.append({"tool": tool, "preview": "(not run: dry run)"})
                continue
            self.run_step(step, action.dashboard)
            self.log(f"    {tool} {json.dumps(step.params, default=str)[:160]} -> {step.status}"
                     + (f": {step.error}" if step.error else ""))
            if step.status == "failed" and step.error and not str(step.error).startswith("acknowledge"):
                # the tool said what was wrong: one more fill with that message, then one more run
                first_error = step.error
                retry = fill_mod.fill(comment, located, context, sequence, i, self.backend, known, results, window=window,
                                      log=self.log, retry_error=first_error)
                retry.attempt += step.attempt
                if retry.status == "not_filled":
                    retry.error = f"after the tool reported '{first_error}': {retry.error}"
                    action.steps[-1] = retry
                    action.status, action.reason = "needs_human", f"{tool}: {retry.error}"
                    return action
                self.run_step(retry, action.dashboard)
                self.log(f"    {tool} {json.dumps(retry.params, default=str)[:160]} -> {retry.status} (retry after: {first_error[:80]})"
                         + (f": {retry.error}" if retry.error else ""))
                if retry.error and retry.status != "ran":
                    retry.error = f"{retry.error} (first try: {first_error[:120]})"
                action.steps[-1] = step = retry
            if step.status == "blocked":
                action.status, action.reason = "paused", f"{tool}: {step.error}"
                return action
            if step.status != "ran":
                action.status, action.reason = "failed", f"{tool}: {step.error}"
                return action
            known.update(step.created)
            results.append({"tool": tool, "preview": step.result_preview})
            if tools.is_write(tool):
                wrote = True
        action.status = "dry_run" if (self.mode != "run" and wrote) else "done"
        return action
