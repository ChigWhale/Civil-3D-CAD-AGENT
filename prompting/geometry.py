"""Vectors and points over the spatial index: where a thing is, which way
is "toward the road", what "10 ft north of the hydrant" means in feet.

Everything is 2D model feet. Directions are unit vectors (the index's
nearest() takes vectors, not words). Left and right against a road,
alignment, curb or centreline follow the civil convention: left of
increasing station (spatial._signed_offset side +1); against any other line
they follow the line's own start -> end direction; against a point, west
and east.
"""
import math

import spatial
import spatial_summary

COMPASS = {"north": (0.0, 1.0), "n": (0.0, 1.0), "up": (0.0, 1.0), "above": (0.0, 1.0),
           "south": (0.0, -1.0), "s": (0.0, -1.0), "down": (0.0, -1.0), "below": (0.0, -1.0),
           "east": (1.0, 0.0), "e": (1.0, 0.0), "west": (-1.0, 0.0), "w": (-1.0, 0.0),
           "northeast": (0.7071, 0.7071), "ne": (0.7071, 0.7071), "northwest": (-0.7071, 0.7071), "nw": (-0.7071, 0.7071),
           "southeast": (0.7071, -0.7071), "se": (0.7071, -0.7071), "southwest": (-0.7071, -0.7071), "sw": (-0.7071, -0.7071)}
ROAD_TAGS = ("curb", "centreline", "alignment", "right_of_way")
ROAD_KINDS = ("road", "street", "roadway", "alignment", "curb", "centreline", "centerline", "right of way", "row")
NEAR_FT = 50.0          # "near / next to X" as a filter
ALONG_FT = 15.0         # "along X" as a filter
CLEARANCE_FT = 5.0      # "next to X" as a place: this far from X
ROAD_REACH_FT = 200.0


def compass_vector(word) -> tuple | None:
    return COMPASS.get(str(word or "").strip().lower())


def _rnd(p) -> tuple:
    x, y = round(float(p[0]), 3), round(float(p[1]), 3)
    return (x + 0.0 if x != 0 else 0.0, y + 0.0 if y != 0 else 0.0)


def point_at(p, vec, dist: float) -> tuple:
    u = spatial._norm(vec)
    return _rnd((p[0] + u[0] * dist, p[1] + u[1] * dist))


def centroid(points) -> tuple | None:
    pts = [p for p in points if p is not None]
    if not pts:
        return None
    return _rnd((sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)))


def bbox(index, handle) -> tuple | None:
    """(minx, miny, maxx, maxy) of an entity from its pieces."""
    xs, ys = [], []
    for p in index.pieces_for(str(handle)):
        if p.kind == "pt":
            xs.append(p.geom[0]); ys.append(p.geom[1])
        else:
            for a, b in p.chords():
                xs += [a[0], b[0]]; ys += [a[1], b[1]]
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _alignment_points(index, handle):
    for a in getattr(index, "alignments", None) or []:
        if str(a.get("handle") or a.get("name")) == str(handle) or str(a.get("name")) == str(handle):
            return a.get("points") or None
    return None


def rep_point(index, data, handle, lookup: dict | None = None) -> tuple | None:
    """One point that stands for an entity: its position (blocks, texts,
    parts), a line's midpoint, a closed shape's vertex centroid, half way
    along an open curve, else the centre of its extent."""
    h = str(handle)
    s = spatial_summary.summarize(data or {}, index, h, lookup) if data is not None else None
    g = (s or {}).get("geometry") or {}
    for k in ("position", "anchor", "center"):
        if g.get(k):
            return _rnd(g[k])
    if g.get("start") and g.get("end"):
        a, b = g["start"], g["end"]
        return _rnd(((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0))
    ring = index.rings.get(h)
    if ring and ring[0]:
        return centroid(ring[0])
    pts = _alignment_points(index, h)
    if pts:
        return _rnd(spatial.point_at_station(pts, spatial._centreline_length(pts) / 2.0))
    pieces = [p for p in index.pieces_for(h) if p.kind != "pt"]
    if pieces:
        chords = [c for p in pieces for c in p.chords()]
        total = sum(math.dist(a, b) for a, b in chords)
        if total > 0:
            run, half = 0.0, total / 2.0
            for a, b in chords:
                seg = math.dist(a, b)
                if run + seg >= half and seg > 0:
                    t = (half - run) / seg
                    return _rnd((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
                run += seg
    box = bbox(index, h)
    if box:
        return _rnd(((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0))
    return None


def foot_on(index, handle, p) -> tuple | None:
    """The nearest point of an entity to p."""
    best = None
    for piece in index.pieces_for(str(handle)):
        if piece.kind == "pt":
            d, foot = math.dist(p, piece.geom), piece.geom
            if best is None or d < best[0]:
                best = (d, foot)
            continue
        for a, b in piece.chords():
            d, foot = spatial._point_seg_distance(p, a, b)
            if best is None or d < best[0]:
                best = (d, foot)
    return _rnd(best[1]) if best else None


def direction_at(index, handle, p) -> tuple | None:
    """The unit tangent of an entity at its point nearest p (start -> end
    sense; an alignment's by increasing station). None for a point entity."""
    pts = _alignment_points(index, handle)
    if pts:
        _, s, _ = spatial._signed_offset(pts, p)
        a, b = spatial.point_at_station(pts, max(0.0, s - 1.0)), spatial.point_at_station(pts, s + 1.0)
        v = spatial._norm(spatial._v(a, b))
        return v if v != (0, 0) else None
    best = None
    for piece in index.pieces_for(str(handle)):
        if piece.kind == "pt":
            continue
        for a, b in piece.chords():
            d, _ = spatial._point_seg_distance(p, a, b)
            if best is None or d < best[0]:
                best = (d, a, b)
    if best is None:
        return None
    v = spatial._norm(spatial._v(best[1], best[2]))
    return v if v != (0, 0) else None


def _nearest_foot(index, p, handles) -> tuple | None:
    best = None
    for h in handles or ():
        f = foot_on(index, h, p)
        if f is None:
            continue
        d = math.dist(p, f)
        if best is None or d < best[0]:
            best = (d, f, h)
    return best


def toward(index, p, ref_handles) -> tuple | None:
    hit = _nearest_foot(index, p, ref_handles)
    if hit is None or hit[0] < 1e-6:
        return None
    return spatial._norm(spatial._v(p, hit[1]))


def away(index, p, ref_handles) -> tuple | None:
    v = toward(index, p, ref_handles)
    return (-v[0], -v[1]) if v else None


def along(index, ref_handles, p) -> tuple | None:
    hit = _nearest_foot(index, p, ref_handles)
    if hit is None:
        return None
    return direction_at(index, hit[2], p)


def nearest_alignment(index, p):
    """(points, distance, station, side) of the alignment nearest p, or None."""
    best = None
    for a in getattr(index, "alignments", None) or []:
        pts = a.get("points") or []
        if len(pts) < 2:
            continue
        d, s, side = spatial._signed_offset(pts, p)
        if best is None or d < best[1]:
            best = (pts, d, s, side)
    return best


def side_vector(index, ref_handles, p, side: str, alignment_frame: bool) -> tuple | None:
    """The unit vector "to the left/right" at p. With alignment_frame, left is
    the +1 side of the nearest alignment (left of increasing station); else
    the left normal of the reference line's direction at p; for a point
    reference, west."""
    left = str(side or "").lower().startswith("l")
    if alignment_frame:
        hit = nearest_alignment(index, p)
        if hit:
            pts, _, s, _ = hit
            a, b = spatial.point_at_station(pts, max(0.0, s - 1.0)), spatial.point_at_station(pts, s + 1.0)
            u = spatial._norm(spatial._v(a, b))
            if u != (0, 0):
                n = (-u[1], u[0])
                return _rnd(n) if left else _rnd((-n[0], -n[1]))
    nf = _nearest_foot(index, p, ref_handles)
    if nf is not None:
        u = direction_at(index, nf[2], p)
        if u:
            n = (-u[1], u[0])
            return _rnd(n) if left else _rnd((-n[0], -n[1]))
    return (-1.0, 0.0) if left else (1.0, 0.0)


def nearest_road(index, p) -> tuple | None:
    """(handle, foot, distance) of the nearest road piece within reach."""
    best = None
    for tag in ROAD_TAGS:
        try:
            hit = index.nearest(tag, p, max_distance=ROAD_REACH_FT)
        except Exception:
            hit = None
        if hit and (best is None or hit[2] < best[2]):
            best = (hit[0], _rnd(hit[1]), round(float(hit[2]), 3))
    return best


def clearance_direction(index, p) -> tuple:
    """Away from the nearest road, so a note never lands on the pavement; north when none is near."""
    road = nearest_road(index, p)
    if road and math.dist(p, road[1]) > 1e-6:
        v = spatial._norm(spatial._v(road[1], p))
        return _rnd(v)
    return (0.0, 1.0)


def is_areal(index, handle) -> bool:
    return bool(index.rings.get(str(handle)))


def placement(index, data, ref_handles, relation: str, direction: str | None, distance_ft: float | None,
              anchor_xy, lookup: dict | None = None) -> tuple | None:
    """The point "<relation> [direction] [distance] of REF" means.

    base: a point entity's position; a line's foot of the anchor (else its
    midpoint); a closed shape's edge on the compass axis (or its centroid).
    at/on/to -> base.  compass -> base + d * vec (d default CLEARANCE_FT).
    left/right -> base + d * side vector.  toward/away -> from the anchor.
    near/next to/from -> base + d * clearance direction."""
    refs = [str(h) for h in (ref_handles or [])]
    if not refs:
        return None
    rel = str(relation or "at").lower()
    d = float(distance_ft) if distance_ft is not None else CLEARANCE_FT
    ref = refs[0]
    base = None
    if anchor_xy is not None and not any(p.kind == "pt" for p in index.pieces_for(ref)):
        base = foot_on(index, ref, anchor_xy)
    if base is None:
        base = rep_point(index, data, ref, lookup)
    if base is None:
        return None
    if rel in ("at", "on", "to", "onto", "over"):
        return _rnd(base)
    if rel == "compass":
        vec = compass_vector(direction)
        if vec is None:
            return _rnd(base)
        if is_areal(index, ref):
            box = bbox(index, ref)
            if box:
                cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
                edge = (box[2] if vec[0] > 0 else box[0] if vec[0] < 0 else cx,
                        box[3] if vec[1] > 0 else box[1] if vec[1] < 0 else cy)
                base = edge
        return point_at(base, vec, d)
    if rel == "side":
        frame = bool(nearest_alignment(index, base)) and (not refs or any(index.entity_tag.get(h) in ROAD_TAGS for h in refs))
        vec = side_vector(index, refs, base, direction or "left", frame)
        return point_at(base, vec, d) if vec else _rnd(base)
    if rel in ("toward", "away"):
        if anchor_xy is None:
            return None
        vec = toward(index, anchor_xy, refs) if rel == "toward" else away(index, anchor_xy, refs)
        return point_at(anchor_xy, vec, d) if vec else None
    # near / next to / from / within
    return point_at(base, clearance_direction(index, base), d)


def site_extent(index) -> tuple:
    """(cx, cy, radius) covering every level-0 cell a non-viewport entity occupies."""
    keys = set()
    for h, cells in index.entity_cells.items():
        if index.entity_tag.get(h) == "viewport":
            continue
        keys |= {k for k in cells if k[0] == 0}
    if not keys:
        return (0.0, 0.0, spatial.BASE_SIZE)
    bs = [spatial.bounds(k) for k in keys]
    x0, y0 = min(b[0] for b in bs), min(b[1] for b in bs)
    x1, y1 = max(b[2] for b in bs), max(b[3] for b in bs)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    radius = math.ceil(math.hypot(x1 - cx, y1 - cy) + spatial.BASE_SIZE)
    return (round(cx, 3), round(cy, 3), float(radius))
