"""Running a chain on the dashboard and reading the outcome back.

One dashboard per drawing, found by its assigned file or created with
source "redline" and the file set (the dashboard server allows that for
this source only; see create_dashboard). Runs go one at a time per
dashboard because the server refuses a second concurrent run. A run that
pauses on conflicts is stopped and recorded as skipped with the conflict
summary: nobody is at the keyboard to resume it, and drawing through a
conflict is not this pipeline's call. `on_conflict=warn` draws anyway.
"""
import os
import time

import requests

from .types import CommentOutcome

TERMINAL = {"finished", "failed", "stopped"}


class DashboardError(RuntimeError):
    pass


class DashboardClient:
    def __init__(self, base: str, timeout: float = 10.0):
        self.base = base.rstrip("/")
        self.timeout = timeout

    # ── http ───────────────────────────────────────────────────────────────

    def _get(self, path, params=None):
        try:
            r = requests.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise DashboardError(f"dashboard server not reachable at {self.base} ({e.__class__.__name__})")
        try:
            body = r.json()
        except ValueError:
            raise DashboardError(f"dashboard answered HTTP {r.status_code} without JSON")
        if r.status_code >= 400:
            raise DashboardError(str(body.get("error") or f"HTTP {r.status_code}"))
        return body

    def _post(self, path, body=None):
        try:
            r = requests.post(f"{self.base}{path}", json=body or {}, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise DashboardError(f"dashboard server not reachable at {self.base} ({e.__class__.__name__})")
        try:
            out = r.json()
        except ValueError:
            raise DashboardError(f"dashboard answered HTTP {r.status_code} without JSON")
        if r.status_code >= 400:
            raise DashboardError(str(out.get("error") or f"HTTP {r.status_code}"))
        return out

    # ── dashboards ─────────────────────────────────────────────────────────

    def dashboards(self) -> list:
        return self._get("/dashboards").get("dashboards") or []

    def ensure_dashboard(self, file: str, name: str | None = None) -> str:
        """The id of the dashboard assigned to `file`, created if none is."""
        want = os.path.normcase(os.path.abspath(file))
        for d in self.dashboards():
            if d.get("file") and os.path.normcase(os.path.abspath(d["file"])) == want:
                return str(d["id"])
        stem = os.path.splitext(os.path.basename(file))[0]
        body = self._post("/dashboards", {"name": name or f"Redline: {stem}", "source": "redline", "file": file})
        if not body.get("file"):
            raise DashboardError("the dashboard server did not accept a file from the redline pipeline "
                                 "(is it the current dashboard/server.py?)")
        return str(body["id"])

    # ── runs ───────────────────────────────────────────────────────────────

    def run(self, dashboard: str, blocks: list, label: str, on_conflict: str = "pause", checks=None) -> dict:
        body = {"blocks": blocks, "policy": {"on_conflict": on_conflict}, "label": label[:80], "source": "redline"}
        if checks:
            body["checks"] = checks
        return self._post(f"/dashboards/{dashboard}/run", body)

    def status(self, dashboard: str) -> dict:
        return self._get(f"/dashboards/{dashboard}/status")

    def results(self, dashboard: str) -> dict:
        return self._get(f"/dashboards/{dashboard}/results")

    def stop(self, dashboard: str) -> dict:
        return self._post(f"/dashboards/{dashboard}/stop")

    def busy(self, dashboard: str) -> bool:
        try:
            return bool(self.status(dashboard).get("running"))
        except DashboardError:
            return False

    def wait_run(self, dashboard: str, run_id: str, timeout: float = 900.0, poll: float = 0.4,
                 stop_on_pause: bool = True) -> dict:
        """Polls until the run is finished, failed or stopped. A paused run
        is stopped (stop_on_pause) and its digest returned with status
        'paused' so the caller can record why."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            st = self.status(dashboard)
            run = st.get("run") or {}
            if run.get("id") == run_id:
                last = run
                if run.get("status") == "paused":
                    if stop_on_pause:
                        self.stop(dashboard)
                        for _ in range(50):
                            time.sleep(0.1)
                            cur = (self.status(dashboard).get("run") or {})
                            if cur.get("id") == run_id and cur.get("status") in TERMINAL:
                                break
                        run = dict(run, status="paused", stopped_by="redline")
                    return run
                if run.get("status") in TERMINAL and not st.get("running"):
                    return run
            time.sleep(poll)
        if last is not None:
            return dict(last, status="timeout")
        raise DashboardError(f"run {run_id} never appeared on dashboard {dashboard}")


def outcome_of(comment_id: str, dashboard: str, digest: dict, results: dict | None = None) -> CommentOutcome:
    status = str(digest.get("status") or "?")
    conflicts = int(digest.get("conflict_count") or 0)
    summary = digest.get("conflict_summary") or {}
    out = CommentOutcome(comment_id=comment_id, status="failed", dashboard=dashboard, run_id=digest.get("id"),
                         conflicts=conflicts, warnings=list(digest.get("warnings") or []),
                         verification=digest.get("verification"))
    preview = None
    if results:
        rs = results.get("results") or []
        if rs:
            preview = str(rs[-1].get("result") or "")[:300]
    out.results_preview = preview or digest.get("last_result")
    if status == "finished":
        out.status = "done"
        if conflicts:
            out.reason = f"drew through {conflicts} conflict(s): {summary}"
    elif status == "paused":
        out.status = "skipped"
        out.reason = f"conflict: {summary or conflicts} (run stopped, nothing drawn)"
    elif status == "failed":
        out.status = "failed"
        out.reason = str(digest.get("error") or "run failed")[:400]
    elif status == "timeout":
        out.status = "failed"
        out.reason = "timed out waiting for the run"
    else:
        out.status = "failed"
        out.reason = f"run ended with status {status}"
    return out
