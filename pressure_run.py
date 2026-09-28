"""The path of a pressure run: an alignment offset to one side, cut to a
station range, with appurtenances (hydrants, valves) every so many feet. Pure
geometry over the snapshot's alignments; the plugin's `lay_pressure_run`
intent turns the result into real Civil 3D pressure network parts.

Offsets follow spatial._signed_offset: `side` "left" is to the left when
walking up-station, "right" to the right; a positive `offset` is the distance.
"""

import math

import spatial
from spatial import _alignment_points, _centreline_length, point_at_station


class PressureRunError(ValueError):
    pass


def _alignment(data: dict, name: str) -> dict:
    aligns = []
    for a in data.get("alignments", []) or []:
        pts = _alignment_points(a)
        if len(pts) >= 2:
            aligns.append({"name": str(a.get("name")), "points": pts, "length": _centreline_length(pts)})
    if not aligns:
        raise PressureRunError("the drawing has no alignment to lay the run along")
    want = str(name or "").strip().lower()
    if not want:
        if len(aligns) == 1:
            return aligns[0]
        raise PressureRunError("alignment is required; the drawing has " + ", ".join(a["name"] for a in aligns))
    for a in aligns:
        if a["name"].lower() == want:
            return a
    for a in aligns:
        if want in a["name"].lower():
            return a
    raise PressureRunError(f"no alignment named {name!r}; the drawing has " + ", ".join(a["name"] for a in aligns))


def _dedupe(pts: list, tol: float = 1e-6) -> list:
    out = []
    for p in pts:
        if not out or math.dist(out[-1], p) > tol:
            out.append(p)
    return out


def offset_polyline(pts: list, distance: float, side: str = "right") -> list:
    """A parallel offset of a polyline: each vertex moved along the bisector of
    its two edge normals (mitred corners, capped so a sharp bend cannot throw
    the point far out). `side` left/right is relative to the direction of
    travel, as _signed_offset reports it."""
    pts = _dedupe([(float(p[0]), float(p[1])) for p in pts])
    if len(pts) < 2:
        raise PressureRunError("an offset needs at least two distinct points")
    sign = -1.0 if str(side).lower().startswith("r") else 1.0
    d = float(distance) * sign
    normals = []
    for a, b in zip(pts, pts[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(dx, dy)
        normals.append((-dy / n, dx / n))       # left-hand normal of the edge
    out = []
    for i, p in enumerate(pts):
        if i == 0:
            n = normals[0]
        elif i == len(pts) - 1:
            n = normals[-1]
        else:
            n1, n2 = normals[i - 1], normals[i]
            bx, by = n1[0] + n2[0], n1[1] + n2[1]
            bl = math.hypot(bx, by)
            if bl < 1e-9:                        # a U-turn: fall back to the incoming normal
                n = n1
            else:
                cos_half = (n1[0] * bx + n1[1] * by) / bl
                scale = 1.0 / max(cos_half, 0.25)   # mitre limit: never more than 4x the offset
                n = (bx / bl * scale, by / bl * scale)
        out.append((p[0] + d * n[0], p[1] + d * n[1]))
    return out


def _cut(pts: list, from_station, to_station, length: float) -> list:
    """The part of a polyline between two stations, measured along it."""
    s0 = 0.0 if from_station in (None, "") else max(0.0, float(from_station))
    s1 = length if to_station in (None, "") else min(length, float(to_station))
    if s1 <= s0:
        raise PressureRunError(f"to_station ({s1:g}) must be past from_station ({s0:g})")
    out = [point_at_station(pts, s0)]
    run = 0.0
    for a, b in zip(pts, pts[1:]):
        seg = math.dist(a, b)
        if s0 < run + seg < s1 and run + seg > s0:
            out.append(b)
        run += seg
    out.append(point_at_station(pts, s1))
    return _dedupe(out, 1e-3)


def plan(data: dict, alignment: str = "", offset: float = 10.0, side: str = "right",
         from_station=None, to_station=None, hydrant_every: float = 0.0, hydrant_part: str = "Hydrant",
         hydrant_size: str = "", first_hydrant_at: float = None, pipe_size: str = "", network: str = "",
         parts_list: str = "", layer: str = "", bend_tolerance_deg: float = 2.0) -> tuple:
    """(request, summary): the plugin request for lay_pressure_run and a
    summary in words. The path is the alignment between the stations, offset
    to `side`; hydrants every `hydrant_every` ft along the alignment from
    `first_hydrant_at` (default half a spacing in), placed on the path."""
    a = _alignment(data, alignment)
    length = a["length"]
    s0 = 0.0 if from_station in (None, "") else max(0.0, float(from_station))
    s1 = length if to_station in (None, "") else min(length, float(to_station))
    centre = _cut(a["points"], s0, s1, length)
    path = offset_polyline(centre, float(offset), side)
    appurtenances = []
    if hydrant_every and float(hydrant_every) > 0:
        step = float(hydrant_every)
        s = s0 + (step / 2.0 if first_hydrant_at is None else float(first_hydrant_at))
        # stations are measured along the alignment; the point is offset like the path
        while s < s1 - 1e-6:                 # never at the dead end of the run
            p = point_at_station(a["points"], s)
            q = point_at_station(a["points"], min(length, s + 1.0))
            dx, dy = q[0] - p[0], q[1] - p[1]
            n = math.hypot(dx, dy) or 1.0
            sign = -1.0 if str(side).lower().startswith("r") else 1.0
            pt = (p[0] + sign * float(offset) * (-dy / n), p[1] + sign * float(offset) * (dx / n))
            entry = {"point": [round(pt[0], 4), round(pt[1], 4)], "part": hydrant_part, "station": round(s, 2)}
            if hydrant_size:
                entry["size"] = hydrant_size
            appurtenances.append(entry)
            s += step
    request = {
        "intent": "lay_pressure_run",
        "network": network or "",
        "parts_list": parts_list or "",
        "path": [[round(p[0], 4), round(p[1], 4)] for p in path],
        "pipe_size": pipe_size or "",
        "fittings": "auto",
        "bend_tolerance_deg": float(bend_tolerance_deg),
        "appurtenances": appurtenances,
        "layer": layer or "",
        "alignment": a["name"],
        "stations": [round(s0, 2), round(s1, 2)],
        "offset": float(offset), "side": side,
    }
    summary = {"alignment": a["name"], "from_station": round(s0, 2), "to_station": round(s1, 2),
               "length": round(sum(math.dist(p, q) for p, q in zip(path, path[1:])), 2),
               "vertices": len(path), "appurtenances": len(appurtenances), "offset": float(offset), "side": side}
    return request, summary


def proposed(request: dict) -> dict:
    """What the gate checks before the plugin has run: the path's segments and
    the appurtenance points, in the same shape a handler report carries."""
    path = [tuple(p) for p in request.get("path") or []]
    return {"segments": [[list(a), list(b)] for a, b in zip(path, path[1:])],
            "points": [list(e["point"]) for e in request.get("appurtenances") or []]}
