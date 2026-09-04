"""Driveway geometry, shared by the MCP tool and the dashboard block.

Both entry points need the same maths, so it lives here rather than in either
caller: the MCP tool posts the payloads itself, the dashboard block runner
expands a block into them at run time. Nothing here talks to AutoCAD -- it turns
one model space snapshot into draw_line/draw_arc payloads.
"""

import math

# ── Driveway generation ───────────────────────────────────────────────────────
#
# Pure geometry over the model space JSON, so no plugin change is needed: the
# tool works out every line and arc here, then emits the existing draw_line and
# draw_arc payloads.
#
# The whole construction for one pad is:
#
#   1. take the pad's major axis, pointed at the road,
#   2. PROJECT the upstream frontage corner along it until it meets the back of
#      curb -- a ray cast, so the line lands where the driveway actually goes,
#   3. offset that line by the driveway width to get the second edge, and
#      project that one the same way,
#   4. fillet each line into the curb.
#
# Step 2 is a ray cast rather than "nearest point on the road" on purpose.
# Projecting to the nearest point clamps to the ends of a segment, so whenever
# the perpendicular falls off the end of one the foot snaps to a VERTEX -- a PC,
# a PT, a polyline corner -- and every driveway in that stretch converges on the
# same handful of points instead of running square off its own lot. The same
# clamping is why a driveway could stop short of the curb.
#
# Curves are cast against EXACTLY. A curb polyline's arc segments arrive as
# vertices plus bulges and are rebuilt as true circles here; treating them as
# chords between the vertices puts the curb up to a sagitta away from where it
# really is -- fourteen feet on a 50ft radius through 90 degrees -- and every
# driveway on the curve stops short, or lands on the chord instead of the curb.
#
# The alignment answers two questions only: which way the flow runs, so
# "upstream" resolves, and which side of the street a pad is on. That second
# one is a per-driveway test -- the projected line must not cross the
# centreline -- rather than a filter on the curb, because a curb that wraps a
# cul-de-sac bulb straddles the end of its alignment and a filter throws it
# away wholesale, leaving every lot on the bulb with nothing to reach.

# Pads are drawn on a FAMILY of layers: a plain one plus lot-width variants.
# The variants do not end in the plain name, so each has to be listed rather
# than caught by a single suffix. C-LOTS-PAD-NONTYP is deliberately not one of
# them -- non-typical pads are left for someone to look at.
PAD_LAYER_SUFFIXES = (
    "c-lots-pad",
    "c-lots-pad-30",
    "c-lots-pad-40",
    "c-lots-pad-50",
)
PAD_LAYER_SUFFIX = PAD_LAYER_SUFFIXES[0]   # the plain form, kept for callers

ROAD_LAYER       = "c-road-back"

# A pad is served when one of its frontage edges is within this far of one of
# the given alignments. An alignment covers one street while the pad layer
# covers the whole subdivision, so without a limit every lot on site gets a
# driveway off a road it does not front. 150ft clears both sides of a street
# (half right of way + setback) while stopping short of the next street over.
MAX_OFFSET = 150.0

# A curb hit closer than this to the centreline cannot be curb: back of curb is
# never inside half a lane of the crown. What does sit there is the closing
# chord a CLOSED back-of-curb polyline contributes from its last vertex straight
# back to its first, running down the middle of the street.
CENTRELINE_CLEARANCE = 5.0


def _v(a, b):
    return (b[0] - a[0], b[1] - a[1])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def _scale(a, k):
    return (a[0] * k, a[1] * k)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def _norm(a):
    length = math.hypot(a[0], a[1])
    return (a[0] / length, a[1] / length) if length else (0.0, 0.0)


def _pt(raw):
    """Vertices arrive as [x, y] and points as [x, y, z]; keep only the plan."""
    return (float(raw[0]), float(raw[1]))


def is_pad_layer(name) -> bool:
    """Whether a layer holds lot pads.

    Matched on the END of the name so an xref-dependent layer is found without
    the caller knowing the xref: "x-plat (ph1a)|c-lots-pad-40" is a pad layer
    exactly as "c-lots-pad-40" is."""
    lower = str(name).lower()
    return any(lower.endswith(suffix) for suffix in PAD_LAYER_SUFFIXES)


def _mid(edge):
    return ((edge[0][0] + edge[1][0]) / 2.0, (edge[0][1] + edge[1][1]) / 2.0)


def _point_seg_distance(p, a, b):
    """Distance from p to segment ab, plus the foot of that projection."""
    seg = _v(a, b)
    len2 = _dot(seg, seg)
    if len2 == 0:
        return math.dist(p, a), a
    t = max(0.0, min(1.0, _dot(_v(a, p), seg) / len2))
    foot = _add(a, _scale(seg, t))
    return math.dist(p, foot), foot


def _nearest_segment(p, segments):
    """(distance, foot, segment) for the closest segment to p."""
    best = (float("inf"), None, None)
    for seg in segments:
        d, foot = _point_seg_distance(p, seg[0], seg[1])
        if d < best[0]:
            best = (d, foot, seg)
    return best


# ── Reading geometry off the snapshot ────────────────────────────────────────


def _bulge_arc(a, b, bulge):
    """(centre, radius, start, end angle) for a polyline segment with a bulge.

    AutoCAD stores an arc segment as tan(sweep / 4): positive sweeps
    counter-clockwise from a to b, negative clockwise. Either way the result is
    a counter-clockwise sweep, which is how every arc is held here."""
    chord = _v(a, b)
    c = math.hypot(chord[0], chord[1])
    sweep = 4.0 * math.atan(bulge)
    radius = c / (2.0 * math.sin(abs(sweep) / 2.0))

    # The centre sits off the chord's midpoint, on the left for a
    # counter-clockwise sweep; cos goes negative past 180 degrees and puts it
    # on the far side, which is right for a bulge past a semicircle.
    u = _scale(chord, 1.0 / c)
    left = (-u[1], u[0])
    # cos carries the past-180 flip; the bulge's own sign carries handedness.
    # (copysign would discard the first -- a cul-de-sac bulb sweeps ~300deg.)
    offset = radius * math.cos(sweep / 2.0) * (1.0 if bulge > 0 else -1.0)
    centre = _add(_mid((a, b)), _scale(left, offset))

    start, end = (a, b) if bulge > 0 else (b, a)
    a0 = math.atan2(start[1] - centre[1], start[0] - centre[0])
    a1 = math.atan2(end[1] - centre[1], end[0] - centre[0])
    while a1 <= a0:
        a1 += 2 * math.pi
    return centre, radius, a0, a1


def _polyline_pieces(pl):
    """A polyline as ("segment", (a, b)) and ("arc", (centre, r, a0, a1)).

    Bulges are optional in the snapshot: an older plugin sends none, and the
    polyline degrades to chords -- correct on straight runs, wrong on curves."""
    verts = [_pt(v) for v in pl.get("vertices", [])]
    bulges = pl.get("bulges") or []
    n = len(verts)
    if n < 2:
        return
    count = n if (pl.get("closed") and n > 2) else n - 1
    for i in range(count):
        a, b = verts[i], verts[(i + 1) % n]
        bulge = float(bulges[i]) if i < len(bulges) else 0.0
        if abs(bulge) < 1e-9 or math.dist(a, b) < 1e-9:
            yield "segment", (a, b)
        else:
            yield "arc", _bulge_arc(a, b, bulge)


def _chords(centre, r, a0, a1, per_arc):
    """A true arc as a chain of short chords."""
    out, previous = [], None
    for i in range(per_arc + 1):
        a = a0 + (a1 - a0) * i / per_arc
        point = (centre[0] + r * math.cos(a), centre[1] + r * math.sin(a))
        if previous is not None:
            out.append((previous, point))
        previous = point
    return out


def _arcs_on_layer(data, layer_matches):
    """Every true arc on the layer -- arc entities and polyline arc segments --
    as (centre, radius, start, end angle), counter-clockwise."""
    out = []
    for arc in data.get("arcs", []):
        if not layer_matches(arc.get("layer", "")):
            continue
        a0, a1 = float(arc["start_angle"]), float(arc["end_angle"])
        if a1 < a0:                      # arcs are stored counter-clockwise
            a1 += 2 * math.pi
        out.append((_pt(arc["center"]), float(arc["radius"]), a0, a1))

    for pl in data.get("polylines", []):
        if not layer_matches(pl.get("layer", "")):
            continue
        for kind, piece in _polyline_pieces(pl):
            if kind == "arc":
                out.append(piece)
    return out


def _segments_on_layer(data, layer_matches, per_arc=32, include_arcs=True):
    """Every straight segment on the layer, from lines and polylines.

    With include_arcs, curves are CHORDED in as well -- fine for callers that
    only need approximate geometry. Driveway casting passes False and handles
    arcs exactly through _arcs_on_layer instead, so a curve is never hit twice
    (once as a chord, once as the true circle)."""
    segments = []
    for ln in data.get("lines", []):
        if layer_matches(ln.get("layer", "")):
            segments.append((_pt(ln["start_point"]), _pt(ln["end_point"])))

    for pl in data.get("polylines", []):
        if not layer_matches(pl.get("layer", "")):
            continue
        for kind, piece in _polyline_pieces(pl):
            if kind == "segment":
                segments.append(piece)
            elif include_arcs:
                segments.extend(_chords(*piece, per_arc))

    if include_arcs:
        for arc in data.get("arcs", []):
            if not layer_matches(arc.get("layer", "")):
                continue
            a0, a1 = float(arc["start_angle"]), float(arc["end_angle"])
            if a1 < a0:
                a1 += 2 * math.pi
            segments.extend(_chords(_pt(arc["center"]), float(arc["radius"]),
                                    a0, a1, per_arc))
    return segments


# ── Ray casting ───────────────────────────────────────────────────────────────


def _ray_segments(origin, direction, segments):
    """Nearest crossing of a ray with any of `segments`: (distance, point,
    unit direction of the segment), or None."""
    best = None
    for a, b in segments:
        edge = _v(a, b)
        denom = direction[0] * edge[1] - direction[1] * edge[0]
        if abs(denom) < 1e-12:
            continue                     # parallel
        w = _v(origin, a)
        t = (w[0] * edge[1] - w[1] * edge[0]) / denom          # along the ray
        u = (w[0] * direction[1] - w[1] * direction[0]) / denom  # along the edge
        if t > 1e-9 and -1e-9 <= u <= 1 + 1e-9:
            if best is None or t < best[0]:
                best = (t, _add(origin, _scale(direction, t)), _norm(edge))
    return best


def _ray_circle(origin, direction, centre, radius, a0, a1):
    """Ray/circle crossings that fall inside the arc's own sweep.

    `direction` must be a unit vector, so the returned parameter is a distance.
    """
    f = _v(centre, origin)
    b = _dot(f, direction)
    disc = b * b - (_dot(f, f) - radius * radius)
    if disc < 0:
        return []

    root = math.sqrt(disc)
    hits = []
    for t in (-b - root, -b + root):
        if t <= 1e-9:
            continue                     # behind the pad, not in front of it
        point = _add(origin, _scale(direction, t))
        angle = math.atan2(point[1] - centre[1], point[0] - centre[0])
        while angle < a0 - 1e-9:
            angle += 2 * math.pi
        if angle <= a1 + 1e-9:
            hits.append((t, point))
    return hits


def _ray_road(origin, direction, segments, arcs, centreline_segments):
    """Where a ray leaving `origin` first meets the back of curb.

    Returns (distance, point, curb direction there, circle or None), or None
    when the ray never meets curb it may use. A ray cast, not a nearest-point
    search: the driveway follows the pad's major axis and stops where that line
    actually meets the curb, so it can never be pulled sideways onto a vertex.

    A driveway never crosses the road it connects to, so a curb hit is refused
    when the centreline lies between the pad and it, or when the hit sits so
    close to the centreline that it cannot be curb at all."""
    best = _ray_segments(origin, direction, segments)
    if best is not None:
        best = best + (None,)

    for centre, r, a0, a1 in arcs:
        for t, point in _ray_circle(origin, direction, centre, r, a0, a1):
            if best is None or t < best[0]:
                radial = _norm(_v(centre, point))
                best = (t, point, (-radial[1], radial[0]), (centre, r))

    if best is None:
        return None

    crossing = _ray_segments(origin, direction, centreline_segments)
    if crossing is not None and crossing[0] < best[0]:
        return None                      # the curb across the street
    if _nearest_segment(best[1], centreline_segments)[0] < CENTRELINE_CLEARANCE:
        return None                      # geometry on the crown, not curb
    return best


# ── Alignments ────────────────────────────────────────────────────────────────


def _alignment_points(align, per_arc=24):
    """Flattens an alignment into an ordered point list.

    Only used to answer "which way is downstream" and "which side of the street
    is this", so approximating arcs with chords is plenty -- no drawn geometry
    comes from this."""
    pts = []
    for ent in align.get("entities", []):
        kind = ent.get("type")
        if kind == "Tangent":
            start, end = _pt(ent["start"]), _pt(ent["end"])
            if not pts:
                pts.append(start)
            pts.append(end)
        elif kind == "Arc":
            centre, r = _pt(ent["center"]), float(ent["radius"])
            start, end = _pt(ent["start"]), _pt(ent["end"])
            a0 = math.atan2(start[1] - centre[1], start[0] - centre[0])
            a1 = math.atan2(end[1] - centre[1], end[0] - centre[0])
            if ent.get("clockwise"):
                while a1 > a0:
                    a1 -= 2 * math.pi
            else:
                while a1 < a0:
                    a1 += 2 * math.pi
            if not pts:
                pts.append(start)
            for i in range(1, per_arc + 1):
                a = a0 + (a1 - a0) * i / per_arc
                pts.append((centre[0] + r * math.cos(a), centre[1] + r * math.sin(a)))
    return pts


def _station_and_heading(pts, p):
    """(station, unit heading of increasing station) at the point on the
    alignment nearest p. Heading is what orders a frontage's corners into
    upstream and downstream."""
    best = (float("inf"), 0.0, (1.0, 0.0))
    run = 0.0
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        seg = _v(a, b)
        len2 = _dot(seg, seg)
        if len2 == 0:
            continue
        length = math.sqrt(len2)
        t = max(0.0, min(1.0, _dot(_v(a, p), seg) / len2))
        foot = _add(a, _scale(seg, t))
        d = math.dist(p, foot)
        if d < best[0]:
            best = (d, run + t * length, _norm(seg))
        run += length
    return best[1], best[2]


def _offset_from(pts, p):
    """Distance from p to the alignment."""
    return _nearest_segment(p, [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)])[0]


def _major_axis(verts):
    """Unit direction of the pad's long axis, taken from its longest edge."""
    edges = [(verts[i], verts[(i + 1) % len(verts)]) for i in range(len(verts))]
    longest = max(edges, key=lambda e: math.dist(e[0], e[1]))
    return _norm(_v(longest[0], longest[1]))


def _front_edges(verts):
    """The pad edges that cross its major axis -- the frontage, not the sides.

    A lot pad here is about 80ft deep and 30-50ft wide, so the major axis runs
    from street to rear and the driveway runs ALONG it, entering over a short
    frontage edge. Falls back to every edge on a pad too irregular to have a
    clear axis."""
    edges = [(verts[i], verts[(i + 1) % len(verts)]) for i in range(len(verts))]
    major = _major_axis(verts)
    square = math.cos(math.radians(45))
    fronts = [e for e in edges
              if abs(_dot(_norm(_v(e[0], e[1])), major)) < square]
    return fronts or edges


# ── Fillets ───────────────────────────────────────────────────────────────────


def _fillet(apex, back_along_drive, along_road, radius):
    """The arc of `radius` tangent to both rays leaving `apex`.

    back_along_drive points from the road back toward the pad; along_road points
    away from the driveway, so the two flares open outward. Returns the tangent
    point on each ray plus the arc centre, or None when the rays are collinear."""
    cos_a = max(-1.0, min(1.0, _dot(back_along_drive, along_road)))
    theta = math.acos(cos_a)
    if theta < 1e-6 or abs(theta - math.pi) < 1e-6:
        return None  # collinear: no arc is defined

    setback = radius / math.tan(theta / 2)
    tangent_drive = _add(apex, _scale(back_along_drive, setback))
    tangent_road = _add(apex, _scale(along_road, setback))
    bisector = _norm(_add(back_along_drive, along_road))
    centre = _add(apex, _scale(bisector, radius / math.sin(theta / 2)))
    return tangent_drive, tangent_road, centre


def _line_arc_fillet(origin, direction, reach, centre, r_road, radius, lateral):
    """Exact fillet where a straight driveway edge meets a CURVED curb.

    The fillet centre sits `radius` off the driveway line on the flare side, and
    (r_road +/- radius) from the curb's centre. Walking the offset line therefore
    reduces to one quadratic. Solving it exactly avoids the ~r^2/2R error of
    filleting against a chord.

    `reach` is how far along the ray the curb actually is, which picks the root:
    the fillet has to sit short of where the driveway would have met the curb."""
    base = _add(origin, _scale(lateral, radius))    # the offset line's origin
    q = _v(centre, base)

    # Pad outside the curve -> fillet centre further out; inside -> nearer in.
    hyp = r_road + radius if math.dist(origin, centre) > r_road else r_road - radius
    if hyp <= 0:
        return None

    b = _dot(q, direction)
    disc = b * b - (_dot(q, q) - hyp * hyp)
    if disc < 0:
        return None
    root = math.sqrt(disc)

    reachable = [t for t in (-b - root, -b + root) if t < reach + 1e-9]
    if not reachable:
        return None
    t = max(reachable)

    fillet_centre = _add(base, _scale(direction, t))
    tangent_drive = _add(origin, _scale(direction, t))   # lateral is square to d
    tangent_road = _add(centre, _scale(_norm(_v(centre, fillet_centre)), r_road))
    return tangent_drive, tangent_road, fillet_centre


def _arc_angles(centre, p_from, p_to):
    """Start/end angles in degrees for the MINOR sweep, ordered the way draw_arc
    expects (it always sweeps counter-clockwise from start to end)."""
    a = math.degrees(math.atan2(p_from[1] - centre[1], p_from[0] - centre[0])) % 360
    b = math.degrees(math.atan2(p_to[1] - centre[1], p_to[0] - centre[0])) % 360
    return (a, b) if (b - a) % 360 <= 180 else (b, a)


def _driveway_for_pad(pad, segments, arcs, align, width, radius):
    """Works out one pad's driveway. Returns (payloads, note): the lines and arcs
    to draw, or an empty list plus the reason it was skipped."""
    verts = [_pt(v) for v in pad.get("vertices", [])]
    if len(verts) < 3:
        return [], "fewer than 3 vertices"

    major = _major_axis(verts)
    centroid = (sum(v[0] for v in verts) / len(verts),
                sum(v[1] for v in verts) / len(verts))
    fronts = _front_edges(verts)
    centreline = align["segments"]

    # Which way along the major axis the curb lies. Both ways are tried rather
    # than assumed, so pads facing either way down a street work the same. The
    # frontage midpoints are probed alongside the centre because a ray from the
    # centre can slip past the end of a short run of curb that the frontage
    # itself sits square to.
    probes = [centroid] + [_mid(edge) for edge in fronts]
    toward, nearest = None, None
    for direction in (major, _scale(major, -1.0)):
        for probe in probes:
            hit = _ray_road(probe, direction, segments, arcs, centreline)
            if hit and (nearest is None or hit[0] < nearest):
                nearest, toward = hit[0], direction
    if toward is None:
        return [], "the pad's major axis never meets the back of curb"

    # The frontage edge on the curb side of the pad.
    front = max(fronts, key=lambda e: _dot(_v(centroid, _mid(e)), toward))

    # Upstream corner first, so the width is offset downstream from it. Judged
    # by the flow heading where the pad sits rather than by station, which
    # ties for every lot around a cul-de-sac bulb.
    _, heading = _station_and_heading(align["points"], _mid(front))
    upstream, downstream = ((front[0], front[1])
                            if _dot(_v(front[0], front[1]), heading) >= 0
                            else (front[1], front[0]))

    across = _v(upstream, downstream)
    frontage = math.hypot(across[0], across[1])
    if frontage < width:
        return [], (f"frontage is {frontage:.1f}ft, narrower than the "
                    f"{width:.0f}ft driveway")
    along = _scale(across, 1.0 / frontage)

    payloads, runs = [], []
    # One edge at the upstream corner and one offset `width` from it, each
    # flaring away from the other so the two arcs open outward.
    for start, lateral in ((upstream, _scale(along, -1.0)),
                           (_add(upstream, _scale(along, width)), along)):
        hit = _ray_road(start, toward, segments, arcs, centreline)
        if hit is None:
            return [], "a driveway edge ran off the end of the back of curb"
        reach, apex, road_dir, circle = hit
        runs.append(reach)

        if circle is not None:
            fillet = _line_arc_fillet(start, toward, reach, circle[0], circle[1],
                                      radius, lateral)
        else:
            flare = (road_dir if _dot(road_dir, lateral) >= 0
                     else _scale(road_dir, -1.0))
            fillet = _fillet(apex, _scale(toward, -1.0), flare, radius)
        if fillet is None:
            return [], "no fillet fits where the driveway meets the curb"
        tangent_drive, tangent_road, centre = fillet

        # The line stops at its tangent point; the arc carries it to the curb,
        # so the two meet cleanly instead of overlapping.
        payloads.append({"intent": "draw_line",
                         "start_point": [start[0], start[1], 0],
                         "end_point": [tangent_drive[0], tangent_drive[1], 0]})

        a0, a1 = _arc_angles(centre, tangent_drive, tangent_road)
        payloads.append({"intent": "draw_arc",
                         "center": [centre[0], centre[1], 0],
                         "radius": radius,
                         "start_angle": math.radians(a0),
                         "end_angle": math.radians(a1)})

    return payloads, {"corner": [round(upstream[0], 3), round(upstream[1], 3)],
                      "frontage": round(frontage, 3),
                      "run": round(max(runs), 3)}


def _resolve_alignments(data: dict, alignment) -> list:
    """The alignments to work from.

    Accepts one name, a comma separated list, a list of names, or nothing at
    all -- which means every alignment in the drawing, so a whole subdivision
    can be done in one go without naming each street."""
    available = data.get("alignments") or []
    if not available:
        raise ValueError("The drawing has no alignments.")

    if alignment is None:
        wanted = []
    elif isinstance(alignment, str):
        wanted = [n.strip() for n in alignment.split(",") if n.strip()]
    else:
        wanted = [str(n).strip() for n in alignment if str(n).strip()]

    if not wanted:
        return list(available)

    by_name = {str(a.get("name", "")).strip().lower(): a for a in available}
    chosen, missing = [], []
    for name in wanted:
        found = by_name.get(name.lower())
        if found is None:
            missing.append(name)
        elif found not in chosen:
            chosen.append(found)
    if missing:
        raise ValueError(f"No alignment named {missing}. Available: "
                         f"{[a.get('name') for a in available]}")
    return chosen


def _prepare(align: dict):
    """One alignment's flattened centreline, as points and as segments."""
    points = _alignment_points(align)
    if len(points) < 2:
        return None
    return {"name": align.get("name"),
            "points": points,
            "segments": [(points[i], points[i + 1]) for i in range(len(points) - 1)]}


def _fronting(prepared: list, front_edges: list):
    """(distance, alignment) for the street a pad fronts.

    Measured from the FRONTAGE edge midpoints rather than the pad centre, which
    is what gets corner lots right: such a lot sits nearer the side street, but
    the edge its driveway leaves from faces the street it actually fronts."""
    best = None
    for align in prepared:
        distance = min(_offset_from(align["points"], _mid(edge))
                       for edge in front_edges)
        if best is None or distance < best[0]:
            best = (distance, align)
    return best


def plan(data: dict, alignment=None, layer: str = "0",
         width: float = 20.0, radius: float = 5.0,
         max_offset: float = MAX_OFFSET):
    """Works out every driveway in a model space snapshot.

    Returns (payloads, summary). payloads are ready-to-post draw_line/draw_arc
    dicts; summary reports what was drawn and why anything was skipped. Raises
    ValueError when the drawing or the request can't support the job at all --
    no pads, no road, unknown alignment -- since those are worth telling the
    user about rather than silently drawing nothing.
    """
    pads = [pl for pl in data.get("polylines", []) if is_pad_layer(pl.get("layer", ""))]
    if not pads:
        raise ValueError("No pads found: no polylines on a layer ending in any "
                         f"of {list(PAD_LAYER_SUFFIXES)}.")

    on_road = lambda name: str(name).lower() == ROAD_LAYER
    # Arcs are kept out of the segment list and cast against exactly, so a
    # curve is never hit twice -- once as a chord and once as the true circle.
    road_segments = _segments_on_layer(data, on_road, include_arcs=False)
    road_arcs = _arcs_on_layer(data, on_road)
    if not road_segments and not road_arcs:
        raise ValueError(f"No back of curb found on layer '{ROAD_LAYER}'.")

    chosen = _resolve_alignments(data, alignment)
    prepared = [p for p in (_prepare(a) for a in chosen) if p]
    if not prepared:
        raise ValueError("None of the requested alignments have usable geometry: "
                         f"{[a.get('name') for a in chosen]}")

    payloads, drawn, skipped, unserved = [], [], [], 0
    per_alignment = {p["name"]: 0 for p in prepared}
    for pad in pads:
        verts = [_pt(v) for v in pad.get("vertices", [])]
        if len(verts) < 3:
            skipped.append({"pad": pad.get("handle"), "why": "fewer than 3 vertices"})
            continue

        # Each pad is worked against the ONE street it fronts, so a pad within
        # reach of two of the given alignments still gets a single driveway.
        distance, align = _fronting(prepared, _front_edges(verts))

        # Pads fronting none of the given alignments belong to another street:
        # passed over quietly and counted apart from the skips, since choosing
        # not to serve a pad is not a failure to draw it.
        if distance > max_offset:
            unserved += 1
            continue

        built, note = _driveway_for_pad(pad, road_segments, road_arcs, align,
                                        width, radius)
        if not built:
            skipped.append({"pad": pad.get("handle"), "why": note})
            continue
        payloads.extend({**p, "layer": layer} for p in built)
        per_alignment[align["name"]] += 1
        drawn.append({"pad": pad.get("handle"), "alignment": align["name"], **note})

    return payloads, {
        "drawn": len(drawn),
        "skipped": len(skipped),
        "not_on_these_alignments": unserved,
        "pads_seen": len(pads),
        "curb_arcs": len(road_arcs),
        "layer": layer,
        "width": width,
        "radius": radius,
        "max_offset": max_offset,
        "alignments": [p["name"] for p in prepared],
        "drawn_per_alignment": per_alignment,
        "details": drawn[:20],
        "skipped_details": skipped[:20],
    }
