"""Talks to the local dashboard server on behalf of cad_mcp_server.py.

Two jobs:
  1. emit()          -- best-effort activity reporting, tagged with the dashboard.
  2. resolve_file()  -- looks up the drawing a dashboard is assigned to.

The drawing assignment is set by the USER in the dashboard UI. The agent names a
dashboard; it never supplies or sees a path, so it cannot redirect work to another
drawing. Emitting is best-effort and mirrors the _NO_CONNECTION swallow pattern in
cad_mcp_server.py, but resolve_file() deliberately FAILS LOUD: without a known
drawing there is no safe default to fall back on.
"""

import os
import time

import requests

# 127.0.0.1, not "localhost": localhost resolves to IPv6 ::1 first on this machine
# and stalls ~2s per request before falling back to IPv4 (measured 2.06s vs 0.015s).
# Every tool call emits twice, so this overhead would be paid three times per call.
import cadagent_config
DASHBOARD_BASE = cadagent_config.get("dashboard_base")
SESSION_ID = os.getpid()
_TIMEOUT = 1.5
_FILE_TTL = 2.0

_file_cache: dict = {}  # dashboard id -> (fetched_at, path)


class DashboardError(Exception):
    """Raised when a dashboard's drawing can't be resolved. The message is
    returned to the agent so it can tell the user what to fix."""


def emit(event_type: str, **fields) -> None:
    try:
        requests.post(f"{DASHBOARD_BASE}/events",
                      json={"type": event_type, "session": SESSION_ID, **fields},
                      timeout=_TIMEOUT)
    except requests.exceptions.RequestException:
        pass


def resolve_file(dashboard: str) -> str:
    """The drawing the user assigned to this dashboard. Raises DashboardError if
    the dashboard is unknown, has no drawing assigned, or the server is down --
    never guesses, so a call can't land in the wrong drawing."""
    key = str(dashboard).strip()

    cached = _file_cache.get(key)
    if cached and time.time() - cached[0] < _FILE_TTL:
        return cached[1]

    try:
        response = requests.get(f"{DASHBOARD_BASE}/dashboards/{key}/file", timeout=_TIMEOUT)
    except requests.exceptions.RequestException:
        raise DashboardError(
            "The dashboard server isn't reachable, so the assigned drawing can't be "
            "looked up. Ask the user to open the dashboard at http://localhost:8765."
        )

    if response.status_code == 404:
        raise DashboardError(
            f"There is no dashboard '{dashboard}'. Call list_dashboards() to see which "
            "dashboards exist, and ask the user which one to work in."
        )

    try:
        path = response.json().get("file")
    except ValueError:
        raise DashboardError("The dashboard server returned an unreadable response.")

    if not path:
        raise DashboardError(
            f"Dashboard '{dashboard}' has no drawing assigned yet. Ask the user to set "
            "one in the dashboard UI — you cannot choose a drawing yourself."
        )

    _file_cache[key] = (time.time(), path)
    return path


def list_dashboards() -> list:
    try:
        response = requests.get(f"{DASHBOARD_BASE}/dashboards", timeout=_TIMEOUT)
        return response.json().get("dashboards", [])
    except (requests.exceptions.RequestException, ValueError):
        return []
