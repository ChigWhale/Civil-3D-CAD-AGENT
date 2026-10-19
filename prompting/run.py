"""The execute phase: a plan's writes, in order, through the Executor.

Reads already ran while planning (their handles are in the plan's steps);
here only the write steps with status planned or needs_fill run. A
needs_fill step is filled first, with every handle the earlier steps
created now in hand. Blocked and failed steps come back as such; the
dashboard decides (pause and ask, acknowledge and repeat, or stop).
"""
import time

from redline import fill as fill_mod
from redline.types import ToolStep

from . import compose
from .plan import NO_LOCATION, PREVIEW, context_for, fill_step, located_for, ungrounded_points


class Runner:
    def __init__(self, plan: dict, text: str, anchor_xy, filler, executor, dashboard: str,
                 index=None, data=None, log=None):
        self.plan = plan
        self.steps = plan.get("steps") or []
        self.text = str(text or "").strip()
        self.filler, self.executor, self.dashboard = filler, executor, dashboard
        self.index, self.data = index, data
        self.log = log or (lambda m: None)
        self.context = context_for(index, data, anchor_xy)
        self.located = located_for(anchor_xy, self.context)
        self.comment = compose.comment_for(self.text)
        self.known = set(fill_mod.usable_handles(self.context))
        self.results: list = []
        for s in self.steps:
            if s.get("status") == "ran" or s.get("created"):
                self.known.update(s.get("created") or [])
            if s.get("role") == "read" and s.get("status") == "ran":
                self.results.append({"tool": s["tool"], "preview": s.get("result_preview") or "", "created": list(s.get("created") or [])})
        self.created: list = []
        # the writes still to run; a read a person edited after planning
        # (status planned, not ran) runs too, so its handles are in hand
        self._queue = [i for i, s in enumerate(self.steps)
                       if (s.get("role") == "write" and s.get("status") in ("planned", "needs_fill"))
                       or (s.get("role") == "read" and s.get("status") == "planned")]

    def pending(self) -> list:
        return list(self._queue)

    def _out(self, idx: int, rec: dict, **extra) -> dict:
        out = {"idx": idx, **rec}
        out.update(extra)
        return out

    def next(self) -> dict | None:
        """Runs the next pending write. Returns the step record with
        status ran | failed | blocked | needs_human (plus idx, token,
        blocked conflicts); None when nothing is left."""
        if not self._queue:
            return None
        idx = self._queue.pop(0)
        rec = self.steps[idx]
        tool = rec["tool"]
        if rec.get("status") == "needs_fill":
            names = [s["tool"] for s in self.steps]
            step, by, note, helper_params = fill_step(self.comment, self.located, self.context, names, idx, self.filler,
                                                      self.known, self.results, self.text, self.index, self.data, log=self.log)
            if step.status == "not_filled":
                rec.update(status="not_filled", error=step.error, filled_by="none")
                return self._out(idx, rec, status="needs_human")
            if by == "llm" and ungrounded_points(tool, step.params, helper_params, self.located.model_xy):
                rec.update(status="not_filled", error=NO_LOCATION, filled_by="none")
                return self._out(idx, rec, status="needs_human")
            rec.update(params=dict(step.params), filled_by=by, helper_note=note or rec.get("helper_note") or "",
                       missing=[], status="planned")
        return self._run(idx, rec, ToolStep(tool=tool, params=dict(rec.get("params") or {})))

    def retry_after_ack(self, idx: int, reason: str) -> dict:
        """Acknowledges the step's conflicts with the decision's reason and
        runs it once more."""
        rec = self.steps[idx]
        token = rec.get("token")
        if token:
            self.executor._invoke("acknowledge_conflicts", {"token": token, "reason": str(reason or "resumed")[:400]},
                                  self.dashboard)
        return self._run(idx, rec, ToolStep(tool=rec["tool"], params=dict(rec.get("params") or {})))

    def _run(self, idx: int, rec: dict, step: ToolStep) -> dict:
        t0 = time.time()
        self.executor.run_step(step, self.dashboard)
        rec.update(status=step.status, error=step.error, result_preview=(step.result_preview or "")[:PREVIEW],
                   created=list(step.created or []), conflicts=int(step.conflicts or 0),
                   latency_s=round(float(step.latency_s or (time.time() - t0)), 3), token=step.token)
        rec["blocked"] = ({"count": step.blocked.get("count", 0), "conflicts": list(step.blocked.get("conflicts") or [])[:10]}
                          if step.blocked else None)
        if step.status == "ran":
            self.created.extend(step.created or [])
            self.known.update(step.created or [])
            self.results.append({"tool": step.tool, "preview": step.result_preview, "created": list(step.created or [])})
        return self._out(idx, rec, blocked=step.blocked)
