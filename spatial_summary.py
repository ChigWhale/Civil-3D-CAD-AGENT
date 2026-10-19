"""Objects, not just handles: what one entity is, in a few numbers, and
which entity a point is on.

Built on a drawing's snapshot (spatial_cache.Entry.data) and its spatial
index (spatial.build). Nothing here talks to the plugin.

    summarize(data, index, handle)   -> {handle, type, layer, tag, state, name, text, geometry, labels}
    object_at(data, index, (x, y))   -> the one entity under a point (a reviewer's arrow tip)
    nearby(data, index, (x, y), r)   -> every entity within reach, summarised, nearest first
"""
import math

import spatial

# snapshot category -> the word a summary calls that kind of object
_CATEGORY_TYPE = {
    "lines": "line", "arcs": "arc", "polylines": "polyline", "circles": "circle", "blocks": "block",
    "texts": "text", "points": "point", "dimensions": "dimension", "labels": "label",
    "feature_lines": "feature_line", "alignments": "alignment", "parcels": "parcel",
    "cogo_points": "cogo_point", "intersections": "intersection", "surfaces": "surface",
    "corridor_lines": "corridor_line", "sample_lines": "sample_line", "survey_figures": "survey_figure",
    "catchments": "catchment", "gradings": "grading", "view_frames": "view_frame",
}
_NETWORKS = (("pipe_networks", (("pipes", "pipe"), ("structures", "structure"))),
             ("pressure_networks", (("pipes", "pressure_pipe"), ("fittings", "fitting"), ("appurtenances", "appurtenance"))))
TIE_FT = 0.5          # within this, a point object beats linework passing through the same spot
MAX_ATTR = 12


def by_handle(data: dict) -> dict:
    """{handle: (type, raw dict)} for everything in the snapshot the index
    reads, nested network parts included (their layer falls back to the
    network's name, as the index does)."""
    out = {}
    for cat, word in _CATEGORY_TYPE.items():
        for it in data.get(cat) or []:
            if isinstance(it, dict):
                h = it.get("handle") or (it.get("name") if cat == "alignments" else None)
                if h is not None:
                    out.setdefault(str(h), (word, it))
    for cat, kinds in _NETWORKS:
        for net in data.get(cat) or []:
            if not isinstance(net, dict):
                continue
            net_name = str(net.get("name") or "")
            for key, word in kinds:
                for part in net.get(key) or []:
                    if isinstance(part, dict) and part.get("handle") is not None:
                        raw = dict(part)
                        raw.setdefault("layer", net_name)
                        raw["_network"] = net_name
                        out.setdefault(str(part["handle"]), (word, raw))
    return out


def _xy(raw):
    p = spatial._xy(raw)
    return [round(p[0], 3), round(p[1], 3)] if p else None


def _len(a, b):
    return round(math.dist(a, b), 3) if a and b else None


def _geometry(kind: str, raw: dict) -> dict:
    g = {}
    if kind in ("line", "pipe", "pressure_pipe"):
        a, b = _xy(raw.get("start_point")), _xy(raw.get("end_point"))
        g.update({"start": a, "end": b, "length": _len(a, b)})
        if a and b:
            g["angle_deg"] = round(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 360.0, 2)
        for k in ("size", "part_size", "diameter", "inner_diameter", "material", "slope", "start_invert", "end_invert"):
            if raw.get(k) is not None:
                g[k] = raw[k]
    elif kind in ("arc", "circle"):
        g.update({"center": _xy(raw.get("center")), "radius": raw.get("radius")})
        if kind == "arc":
            for k, out in (("start_angle", "start_angle_deg"), ("end_angle", "end_angle_deg")):
                if raw.get(k) is not None:
                    g[out] = round(math.degrees(float(raw[k])), 2)
    elif kind == "polyline":
        pts = [q for q in (_xy(v) for v in raw.get("vertices") or []) if q]
        if pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            g.update({"vertex_count": len(pts), "closed": bool(raw.get("closed")), "first": pts[0], "last": pts[-1],
                      "bbox": [[min(xs), min(ys)], [max(xs), max(ys)]]})
            g["length"] = round(sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
                                + (math.dist(pts[-1], pts[0]) if raw.get("closed") and len(pts) > 2 else 0.0), 3)
    elif kind == "block":
        g.update({"position": _xy(raw.get("position")), "name": raw.get("name"), "rotation_deg": raw.get("rotation_deg"),
                  "scale": raw.get("scale")})
        attrs = raw.get("attributes") or {}
        if isinstance(attrs, dict) and attrs:
            g["attributes"] = {str(k): str(v)[:60] for k, v in list(attrs.items())[:MAX_ATTR]}
    elif kind == "text":
        g.update({"position": _xy(raw.get("position")), "text": str(raw.get("text") or "")[:120],
                  "height": raw.get("height"), "rotation_deg": raw.get("rotation_deg")})
    elif kind == "point" or kind == "cogo_point" or kind == "intersection":
        g.update({"position": _xy(raw.get("position"))})
        for k in ("number", "description", "elevation", "name"):
            if raw.get(k) is not None:
                g[k] = raw[k]
    elif kind == "dimension":
        g.update({"text_position": _xy(raw.get("text_position")), "text": raw.get("text"), "measurement": raw.get("measurement")})
    elif kind == "label":
        g.update({"anchor": _xy(raw.get("anchor")), "kind": raw.get("kind") or raw.get("type"), "target": raw.get("target"),
                  "text": str(raw.get("text") or "")[:120] if raw.get("text") else None})
    elif kind in ("structure", "fitting", "appurtenance"):
        g.update({"position": _xy(raw.get("position") or raw.get("insertion_point")), "part_type": raw.get("part_type"),
                  "description": raw.get("description"), "network": raw.get("_network")})
        for k in ("rim", "sump", "surface_elevation", "part_size"):
            if raw.get(k) is not None:
                g[k] = raw[k]
    elif kind == "alignment":
        g.update({"name": raw.get("name"), "length": raw.get("length"), "start_station": raw.get("start_station"),
                  "end_station": raw.get("end_station")})
    elif kind == "feature_line":
        pts = [q for q in (_xy(p) for p in raw.get("points") or []) if q]
        if pts:
            g.update({"vertex_count": len(pts), "first": pts[0], "last": pts[-1]})
    elif kind == "parcel":
        g.update({"name": raw.get("name"), "area": raw.get("area"), "number": raw.get("number")})
    return {k: v for k, v in g.items() if v is not None}


def summarize(data: dict, index, handle, lookup: dict | None = None) -> dict | None:
    """One entity in a few numbers, or None for an unknown handle."""
    lookup = lookup if lookup is not None else by_handle(data)
    hit = lookup.get(str(handle))
    if hit is None:
        return None
    kind, raw = hit
    out = {"handle": str(handle), "type": kind, "layer": str(raw.get("layer") or ""),
           "tag": index.entity_tag.get(str(handle)) if index is not None else None,
           "state": index.entity_state.get(str(handle)) if index is not None else None}
    name = raw.get("name") or raw.get("part_type") or raw.get("description")
    if name and kind != "text":
        out["name"] = str(name)[:80]
    if kind == "text" and raw.get("text") is not None:
        out["text"] = str(raw["text"])[:120]
    out["geometry"] = _geometry(kind, raw)
    if index is not None:
        labels = sorted(index.labels_of(str(handle)))
        if labels:
            out["labels"] = [{"handle": l, **({"kind": lookup[l][1].get("kind") or lookup[l][1].get("type")} if l in lookup else {})}
                             for l in labels]
    return {k: v for k, v in out.items() if v is not None}


def distance_to(index, handle, xy) -> float | None:
    """The true distance from a point to an entity's geometry."""
    best = None
    for piece in index.pieces_for(str(handle)):
        if piece.kind == "pt":
            d = math.dist(xy, piece.geom)
        else:
            chords = piece.chords()
            if not chords:
                continue
            d = min(spatial._point_seg_distance(xy, a, b)[0] for a, b in chords)
        if best is None or d < best:
            best = d
    return round(best, 3) if best is not None else None


def _frozen(layer: str, frozen) -> bool:
    if not layer or not frozen:
        return False
    lay = str(layer).lower()
    return lay in frozen or lay.split("|")[-1] in frozen


def _candidates(data, index, xy, radius, frozen, lookup):
    frozen = {str(f).lower() for f in (frozen or [])}
    out = []
    for h in index.entities(point=xy, radius=radius):
        if index.entity_tag.get(h) == "viewport":
            continue
        s = summarize(data, index, h, lookup)
        if s is None or _frozen(s.get("layer"), frozen):
            continue
        d = distance_to(index, h, xy)
        if d is None:
            continue
        s["distance"] = d
        out.append(s)
    return out


def _point_in_ring(xy, pts) -> bool:
    x, y = xy
    inside = False
    n = len(pts)
    for i in range(n):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xi > x:
                inside = not inside
    return inside


def enclosing(data: dict, index, xy, frozen=None, lookup: dict | None = None) -> list:
    """Closed shapes the point is inside: circles and closed polylines (and
    parcels' first loops), smallest first. "Draw a triangle inside" is about
    one of these, which no distance-to-edge search finds."""
    xy = (float(xy[0]), float(xy[1]))
    lookup = lookup if lookup is not None else by_handle(data)
    frozen = {str(f).lower() for f in (frozen or [])}
    out = []
    for ci in data.get("circles") or []:
        c, r = spatial._xy(ci.get("center")), ci.get("radius")
        if c is None or not r or _frozen(ci.get("layer"), frozen):
            continue
        if math.dist(xy, c) <= float(r):
            s = summarize(data, index, ci.get("handle"), lookup)
            if s:
                s["area"] = round(math.pi * float(r) ** 2, 1)
                out.append(s)
    for pl in data.get("polylines") or []:
        if not pl.get("closed") or _frozen(pl.get("layer"), frozen):
            continue
        pts = [q for q in (spatial._xy(v) for v in pl.get("vertices") or []) if q]
        if len(pts) >= 3 and _point_in_ring(xy, pts):
            s = summarize(data, index, pl.get("handle"), lookup)
            if s:
                s["area"] = round(abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                                         for i in range(len(pts)))) / 2.0, 1)
                out.append(s)
    for pc in data.get("parcels") or []:
        loops = pc.get("loops") or []
        if not loops or _frozen(pc.get("layer"), frozen):
            continue
        pts = [q for q in (spatial._xy(seg.get("start")) for seg in loops[0] or []) if q]
        if len(pts) >= 3 and _point_in_ring(xy, pts):
            s = summarize(data, index, pc.get("handle"), lookup)
            if s:
                s["area"] = pc.get("area")
                out.append(s)
    out.sort(key=lambda s: (s.get("area") if isinstance(s.get("area"), (int, float)) else 1e18))
    return out


CANDIDATE_REACH = 10          # with nothing under the point, look this many tolerances out for candidates


def object_at(data: dict, index, xy, tolerance: float = 5.0, frozen=None, lookup: dict | None = None) -> dict:
    """The one entity under a point. Labels never win (they are reported
    under the winner's `labels`); within TIE_FT a point object beats linework
    passing through the same spot; then the nearest wins. `candidates` are
    the next few, so a caller can see what else was close; when nothing is
    within tolerance they are the nearest objects within ten tolerances, so
    a near miss is visible. `enclosing` lists the closed shapes the point is
    inside, smallest first."""
    xy = (float(xy[0]), float(xy[1]))
    lookup = lookup if lookup is not None else by_handle(data)
    cands = _candidates(data, index, xy, tolerance, frozen, lookup)
    labels = [c for c in cands if c["type"] == "label"]
    others = [c for c in cands if c["type"] != "label"]
    point_like = {"block", "text", "point", "cogo_point", "intersection", "dimension", "structure", "fitting", "appurtenance"}

    def rank(c):
        return (round(c["distance"] / TIE_FT), 0 if c["type"] in point_like else 1, c["distance"])
    others.sort(key=rank)
    labels.sort(key=lambda c: c["distance"])
    inside = enclosing(data, index, xy, frozen, lookup)
    if not others:
        if labels:         # only a label here: it is the object
            return {"object": labels[0], "labels": [], "candidates": labels[1:4], "enclosing": inside}
        far = _candidates(data, index, xy, max(tolerance * CANDIDATE_REACH, 50.0), frozen, lookup)
        far = [c for c in far if c["type"] != "label"]
        far.sort(key=lambda c: c["distance"])
        return {"object": None, "labels": [], "candidates": far[:3], "enclosing": inside}
    winner = others[0]
    own = {l["handle"] for l in winner.get("labels") or []}
    return {"object": winner,
            "labels": [l for l in labels if l["handle"] in own] or labels[:3],
            "candidates": others[1:4], "enclosing": inside}


def nearby(data: dict, index, xy, radius: float = 50.0, limit: int = 20, types=None, frozen=None,
           lookup: dict | None = None) -> dict:
    """Every entity within `radius` of a point, summarised, nearest first.
    `types` keeps only those type words (line, block, text, pipe ...)."""
    xy = (float(xy[0]), float(xy[1]))
    lookup = lookup if lookup is not None else by_handle(data)
    cands = _candidates(data, index, xy, radius, frozen, lookup)
    if types:
        want = {str(t).lower() for t in types}
        cands = [c for c in cands if c["type"] in want]
    cands.sort(key=lambda c: (c["distance"], c["type"], c["handle"]))
    return {"count": len(cands), "objects": cands[:limit], "truncated": len(cands) > limit}


def brief(summary: dict) -> dict:
    """The short form for lists: handle, type, layer, what it is, distance."""
    g = summary.get("geometry") or {}
    out = {"handle": summary["handle"], "type": summary["type"], "layer": summary.get("layer")}
    what = summary.get("text") or summary.get("name") or g.get("part_type") or g.get("kind")
    if what:
        out["what"] = str(what)[:60]
    if "distance" in summary:
        out["distance"] = summary["distance"]
    return out
