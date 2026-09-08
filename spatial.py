"""A spatial index over a model space snapshot, built for reading.

The C# SpatialHash is a broad phase: cells are bags of ids, consumed once to
find adjacency edges and thrown away. This module keeps the same floor-division
grid but promotes the cell to something with content and position, so an agent
-- or a planner -- can ask "what is here", "where is this relative to the
street", and "does what I am about to draw cross anything", without reading
raw coordinates.

Structure, bottom up:

  grid      fixed origin, three levels (50 / 200 / 800 ft). Keys are a pure
            function of coordinates, so the same cell has the same key in
            every snapshot and snapshots diff cell by cell.
  cell      a record: distinct entities per tag, CLIPPED length per tag, area
            fraction covered by closed shapes, a length-weighted structure
            tensor (orientation that ADDS, so parents are exact sums of their
            children), a content hash, and a station/offset frame against the
            nearest alignment.
  region    corridors straight from the alignments (station range x offset
            band) and blocks as connected runs of what is left between them.
  render    fixed templates that turn a record into a sentence, and the coarse
            level into a character map. No model writes these: they have to be
            identical every run for the same drawing.

Geometry is inserted only at level 0, by exact grid traversal (a segment marks
the cells it crosses, not its bounding box). Everything above is aggregation.

Nothing here decides what an operation acts on. Queries return handles; the
operations take handles.
"""

import hashlib
import math
from collections import defaultdict, Counter
from dataclasses import dataclass, field

from driveways import (
    ROAD_LAYER,
    _add,
    _alignment_points,
    _chords,
    _dot,
    _mid,
    _norm,
    _point_seg_distance,
    _polyline_pieces,
    _pt,
    _ray_circle,
    _ray_segments,
    _scale,
    _v,
    is_pad_layer,
)


def _centreline_length(pts) -> float:
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def _signed_offset(pts, p):
    """(distance, station, side) of p against a flattened alignment.

    side is +1 left of increasing station and -1 right of it. Station clamps at
    the ends, which the frame reports as beyond_end rather than hiding."""
    best = (float("inf"), 0.0, 1.0)
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
            u = _norm(seg)
            cross = u[0] * (p[1] - foot[1]) - u[1] * (p[0] - foot[0])
            best = (d, run + t * length, 1.0 if cross >= 0 else -1.0)
        run += length
    return best

# ── Grid constants ────────────────────────────────────────────────────────────
#
# The origin is a CONSTANT, not the drawing's extents. Derive it from extents
# and every key shifts the moment someone draws a north arrow off in a corner,
# and no two snapshots of the same site can be compared.
ORIGIN = (0.0, 0.0)
BASE_SIZE = 50.0          # ft, level 0 -- about one lot width
FACTOR = 4                # each level is 4x4 children of the one below
LEVELS = 3                # 50, 200, 800 ft
SUBSAMPLE = 4             # covered-fraction sampling grid per cell (4x4)
CORRIDOR_HALF_WIDTH = 40.0  # ft off an alignment that still counts as road

# ── Tags ──────────────────────────────────────────────────────────────────────
#
# The vocabulary an agent sees. Layer names map to tags by the same suffix
# habits the planners use, so "x-plat (ph1a)|c-lots-line" is a lot line here
# exactly as it is there. First rule to match wins; pads are checked first
# through is_pad_layer so the width variants are covered. Extend by adding a
# row -- the renderer picks new tags up without change.
TAG_RULES = (
    ("curb",         "equals",   ROAD_LAYER),
    ("lot_line",     "endswith", "c-lots-line"),
    ("setback",      "endswith", "c-lots-stbk"),
    ("centreline",   "endswith", "c-rway-center"),
    ("right_of_way", "endswith", "c-rway-line"),
    ("boundary",     "contains", "c-bndy"),
    ("easement",     "contains", "c-esmt"),
    ("driveway",     "contains", "drive"),
    ("swale",        "contains", "swale"),
)

MAP_GLYPHS = (  # first matching rule paints the cell; order is priority
    ("+", lambda c: c.length.get("curb", 0) > 0 and c.count("pad") > 0),
    ("R", lambda c: c.length.get("curb", 0) > 0),
    ("P", lambda c: c.count("pad") > 0),
    ("L", lambda c: c.length.get("lot_line", 0) > 0),
    ("d", lambda c: c.count("driveway") > 0 or c.count("swale") > 0),
    ("o", lambda c: c.total_length() > 0),
)


def tag_for(layer) -> str:
    lower = str(layer or "").lower()
    if is_pad_layer(lower):
        return "pad"
    for tag, kind, needle in TAG_RULES:
        if (kind == "equals" and lower == needle) or \
           (kind == "endswith" and lower.endswith(needle)) or \
           (kind == "contains" and needle in lower):
            return tag
    return "other"


# ── Grid maths ────────────────────────────────────────────────────────────────


def cell_size(level: int) -> float:
    return BASE_SIZE * (FACTOR ** level)


def key_at(x: float, y: float, level: int = 0) -> tuple:
    s = cell_size(level)
    return (level,
            int(math.floor((x - ORIGIN[0]) / s)),
            int(math.floor((y - ORIGIN[1]) / s)))


def parent_key(key: tuple) -> tuple:
    level, cx, cy = key
    return (level + 1, cx // FACTOR, cy // FACTOR)   # floor division: exact for negatives too


def bounds(key: tuple) -> tuple:
    level, cx, cy = key
    s = cell_size(level)
    x0, y0 = ORIGIN[0] + cx * s, ORIGIN[1] + cy * s
    return (x0, y0, x0 + s, y0 + s)


def centre(key: tuple) -> tuple:
    x0, y0, x1, y1 = bounds(key)
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _traverse(a, b, level: int = 0):
    """Cells a segment crosses, with the length inside each: Amanatides & Woo.

    This is the whole reason a cell can be trusted: a diagonal road marks the
    cells it passes through, not the rectangle its bounding box covers."""
    total = math.dist(a, b)
    if total < 1e-12:
        return [(key_at(a[0], a[1], level), 0.0)]

    s = cell_size(level)
    dx, dy = (b[0] - a[0]) / total, (b[1] - a[1]) / total
    _, cx, cy = key_at(a[0], a[1], level)

    def first_boundary(pos, origin, c, d):
        if d > 0:
            return (origin + (c + 1) * s - pos) / d
        if d < 0:
            return (origin + c * s - pos) / d
        return math.inf

    t_max_x = first_boundary(a[0], ORIGIN[0], cx, dx)
    t_max_y = first_boundary(a[1], ORIGIN[1], cy, dy)
    t_delta_x = s / abs(dx) if dx else math.inf
    t_delta_y = s / abs(dy) if dy else math.inf
    step_x = 1 if dx > 0 else -1
    step_y = 1 if dy > 0 else -1

    out, t = [], 0.0
    for _ in range(100000):                     # guard, never reached in practice
        t_exit = min(t_max_x, t_max_y, total)
        out.append(((level, cx, cy), max(0.0, t_exit - t)))
        if t_exit >= total - 1e-9:
            break
        if t_max_x < t_max_y:
            cx += step_x
            t_max_x += t_delta_x
        elif t_max_y < t_max_x:
            cy += step_y
            t_max_y += t_delta_y
        else:                                   # exactly through a corner
            cx += step_x
            cy += step_y
            t_max_x += t_delta_x
            t_max_y += t_delta_y
        t = t_exit
    return out


def _clip(a, b, box):
    """Segment ab clipped to an axis-aligned box (Liang-Barsky), or None."""
    x0, y0, x1, y1 = box
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if abs(p) < 1e-12:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return None
            t0 = max(t0, r)
        else:
            if r < t0:
                return None
            t1 = min(t1, r)
    if t1 - t0 < 1e-9:
        return None
    return ((a[0] + t0 * dx, a[1] + t0 * dy), (a[0] + t1 * dx, a[1] + t1 * dy))


def _point_in_polygon(p, verts) -> bool:
    inside = False
    n = len(verts)
    for i in range(n):
        a, b = verts[i], verts[(i + 1) % n]
        if (a[1] > p[1]) != (b[1] > p[1]):
            x = a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if p[0] < x:
                inside = not inside
    return inside


# ── Pieces ────────────────────────────────────────────────────────────────────
#
# Everything is reduced to segments and true arcs, carrying tag and handle.


@dataclass(frozen=True)
class Piece:
    kind: str          # "seg" | "arc"
    geom: tuple        # seg: (a, b)   arc: (centre, r, a0, a1)
    tag: str
    handle: str
    layer: str

    def chords(self, per_arc=None):
        if self.kind == "seg":
            return [self.geom]
        c, r, a0, a1 = self.geom
        n = per_arc or max(8, int(math.ceil(r * (a1 - a0) / (BASE_SIZE / 2.0))))
        return _chords(c, r, a0, a1, n)

    def endpoints(self):
        if self.kind == "seg":
            return self.geom
        c, r, a0, a1 = self.geom
        return ((c[0] + r * math.cos(a0), c[1] + r * math.sin(a0)),
                (c[0] + r * math.cos(a1), c[1] + r * math.sin(a1)))

    def length(self):
        if self.kind == "seg":
            return math.dist(*self.geom)
        c, r, a0, a1 = self.geom
        return r * (a1 - a0)

    def descriptor(self):
        """Geometry only, rounded -- what the content hash is over."""
        if self.kind == "seg":
            (ax, ay), (bx, by) = self.geom
            return f"{self.tag}|seg|{ax:.3f},{ay:.3f}|{bx:.3f},{by:.3f}"
        (cx, cy), r, a0, a1 = self.geom
        return f"{self.tag}|arc|{cx:.3f},{cy:.3f}|{r:.3f}|{a0:.5f}|{a1:.5f}"


def _snapshot_pieces(data: dict):
    for ln in data.get("lines", []) or []:
        yield Piece("seg", (_pt(ln["start_point"]), _pt(ln["end_point"])),
                    tag_for(ln.get("layer")), str(ln.get("handle")), str(ln.get("layer")))
    for arc in data.get("arcs", []) or []:
        a0, a1 = float(arc["start_angle"]), float(arc["end_angle"])
        if a1 < a0:
            a1 += 2 * math.pi
        yield Piece("arc", (_pt(arc["center"]), float(arc["radius"]), a0, a1),
                    tag_for(arc.get("layer")), str(arc.get("handle")), str(arc.get("layer")))
    for pl in data.get("polylines", []) or []:
        tag, handle, layer = tag_for(pl.get("layer")), str(pl.get("handle")), str(pl.get("layer"))
        for kind, geom in _polyline_pieces(pl):
            yield Piece("segment" if False else ("seg" if kind == "segment" else "arc"),
                        geom, tag, handle, layer)


def _snapshot_polygons(data: dict):
    """Closed polylines as chorded vertex rings, for covered-fraction."""
    for pl in data.get("polylines", []) or []:
        if not pl.get("closed"):
            continue
        ring = []
        for kind, geom in _polyline_pieces(pl):
            if kind == "segment":
                ring.append(geom[0])
            else:
                for a, _ in _chords(*geom, 12):
                    ring.append(a)
        if len(ring) >= 3:
            yield ring, tag_for(pl.get("layer")), str(pl.get("handle"))


def _payload_pieces(payloads: list):
    """Proposed draw_* payloads reduced to pieces, for the dry-run gate."""
    for index, p in enumerate(payloads):
        intent = p.get("intent")
        tag = tag_for(p.get("layer", ""))
        handle = f"payload:{index}"
        if intent == "draw_line":
            yield index, Piece("seg", (_pt(p["start_point"]), _pt(p["end_point"])), tag, handle, str(p.get("layer")))
        elif intent == "draw_arc":
            a0, a1 = float(p["start_angle"]), float(p["end_angle"])
            if a1 < a0:
                a1 += 2 * math.pi
            yield index, Piece("arc", (_pt(p["center"]), float(p["radius"]), a0, a1), tag, handle, str(p.get("layer")))
        elif intent == "draw_shape":
            verts = [_pt(v) for v in p.get("vertices", [])]
            count = len(verts) if p.get("closed") and len(verts) > 2 else len(verts) - 1
            for i in range(max(0, count)):
                yield index, Piece("seg", (verts[i], verts[(i + 1) % len(verts)]), tag, handle, str(p.get("layer")))


# ── Cells ─────────────────────────────────────────────────────────────────────


@dataclass
class Cell:
    key: tuple
    handles: dict = field(default_factory=lambda: defaultdict(set))   # tag -> {handle}
    length: dict = field(default_factory=lambda: defaultdict(float))  # tag -> clipped ft
    covered: dict = field(default_factory=lambda: defaultdict(float)) # tag -> area fraction
    tensor: list = field(default_factory=lambda: [0.0, 0.0, 0.0])     # sxx, sxy, syy
    vertices: set = field(default_factory=set)                        # rounded endpoints inside
    pieces: list = field(default_factory=list)                        # level 0 only
    clipped: list = field(default_factory=list)                       # level 0: (tag, a, b) inside the cell
    frame: dict = None
    children: list = field(default_factory=list)
    hash: str = ""

    # -- derived, never stored ------------------------------------------------

    def count(self, tag) -> int:
        return len(self.handles.get(tag, ()))

    def tags(self) -> list:
        return sorted(t for t in set(self.handles) | set(self.length) | set(self.covered)
                      if self.count(t) or self.length.get(t, 0) > 0 or self.covered.get(t, 0) > 0)

    def total_length(self) -> float:
        return sum(self.length.values())

    def orientation(self):
        """(bearing degrees 0-180, anisotropy 0-1) from the structure tensor."""
        sxx, sxy, syy = self.tensor
        trace = sxx + syy
        if trace <= 1e-9:
            return None, 0.0
        angle = 0.5 * math.degrees(math.atan2(2 * sxy, sxx - syy)) % 180.0
        half = math.sqrt(((sxx - syy) / 2) ** 2 + sxy ** 2)
        lam1, lam2 = trace / 2 + half, trace / 2 - half
        return angle, (lam1 - lam2) / (lam1 + lam2) if (lam1 + lam2) > 0 else 0.0

    def all_handles(self) -> set:
        out = set()
        for s in self.handles.values():
            out |= s
        return out


# ── Index ─────────────────────────────────────────────────────────────────────


class SpatialIndex:
    def __init__(self, data: dict):
        self.cells: dict = {}
        self.entity_cells: dict = defaultdict(set)     # handle -> {level-0 key}
        self.alignments = self._prepare_alignments(data)
        self._insert(data)
        self._frames()
        self._aggregate()
        self._hash()
        self.regions = self._regions()
        self.site = self._site_frame()

    # -- build ----------------------------------------------------------------

    @staticmethod
    def _prepare_alignments(data):
        out = []
        for a in data.get("alignments", []) or []:
            pts = _alignment_points(a)
            if len(pts) >= 2:
                out.append({"name": str(a.get("name")), "points": pts,
                            "length": _centreline_length(pts)})
        return out

    def _cell(self, key) -> Cell:
        c = self.cells.get(key)
        if c is None:
            c = self.cells[key] = Cell(key)
        return c

    def _insert(self, data):
        for piece in _snapshot_pieces(data):
            for a, b in piece.chords():
                seg = _v(a, b)
                length = math.hypot(seg[0], seg[1])
                if length < 1e-12:
                    continue
                ux, uy = seg[0] / length, seg[1] / length
                for key, clipped in _traverse(a, b):
                    if clipped <= 0:
                        continue
                    c = self._cell(key)
                    c.handles[piece.tag].add(piece.handle)
                    c.length[piece.tag] += clipped
                    c.tensor[0] += clipped * ux * ux
                    c.tensor[1] += clipped * ux * uy
                    c.tensor[2] += clipped * uy * uy
                    if not c.pieces or c.pieces[-1] is not piece:
                        c.pieces.append(piece)
                    span = _clip(a, b, bounds(key))
                    if span:
                        c.clipped.append((piece.tag, span[0], span[1]))
                    self.entity_cells[piece.handle].add(key)
            for p in piece.endpoints():
                self._cell(key_at(*p)).vertices.add((round(p[0], 3), round(p[1], 3)))

        for ring, tag, handle in _snapshot_polygons(data):
            xs, ys = [p[0] for p in ring], [p[1] for p in ring]
            _, cx0, cy0 = key_at(min(xs), min(ys))
            _, cx1, cy1 = key_at(max(xs), max(ys))
            s = BASE_SIZE
            for cx in range(cx0, cx1 + 1):
                for cy in range(cy0, cy1 + 1):
                    key = (0, cx, cy)
                    x0, y0 = ORIGIN[0] + cx * s, ORIGIN[1] + cy * s
                    inside = 0
                    for i in range(SUBSAMPLE):
                        for j in range(SUBSAMPLE):
                            p = (x0 + (i + 0.5) * s / SUBSAMPLE, y0 + (j + 0.5) * s / SUBSAMPLE)
                            if _point_in_polygon(p, ring):
                                inside += 1
                    if inside:
                        c = self._cell(key)
                        c.covered[tag] = min(1.0, c.covered[tag] + inside / (SUBSAMPLE ** 2))
                        c.handles[tag].add(handle)
                        self.entity_cells[handle].add(key)

    def _frames(self):
        if not self.alignments:
            return
        for key, cell in self.cells.items():
            if key[0] != 0:
                continue
            x0, y0, x1, y1 = bounds(key)
            probes = ((x0, y0), (x1, y0), (x1, y1), (x0, y1), ((x0 + x1) / 2, (y0 + y1) / 2))
            per = []
            for align in self.alignments:
                hits = [_signed_offset(align["points"], p) for p in probes]
                dist = [h[0] for h in hits]
                sta = [h[1] for h in hits]
                sides = {h[2] for h in hits}
                per.append({
                    "alignment": align["name"],
                    "offset": [round(min(dist), 2), round(max(dist), 2)],
                    "station": [round(min(sta), 2), round(max(sta), 2)],
                    "side": "mixed" if len(sides) > 1 else ("left" if 1.0 in sides else "right"),
                    "beyond_end": min(sta) <= 1e-6 or max(sta) >= align["length"] - 1e-6,
                })
            per.sort(key=lambda f: f["offset"][0])
            cell.frame = {"nearest": per[0], "second": per[1] if len(per) > 1 else None}

    def _aggregate(self):
        for level in range(1, LEVELS):
            groups = defaultdict(list)
            for key, cell in list(self.cells.items()):
                if key[0] == level - 1:
                    groups[parent_key(key)].append(cell)
            for pkey, kids in groups.items():
                parent = self._cell(pkey)
                parent.children = [k.key for k in kids]
                child_area = cell_size(level - 1) ** 2
                parent_area = cell_size(level) ** 2
                for k in kids:
                    for tag, hs in k.handles.items():
                        parent.handles[tag] |= hs
                    for tag, ln in k.length.items():
                        parent.length[tag] += ln
                    for tag, cov in k.covered.items():
                        parent.covered[tag] += cov * child_area / parent_area
                    for i in range(3):
                        parent.tensor[i] += k.tensor[i]
                    parent.vertices |= k.vertices
                parent.frame = self._envelope([k.frame for k in kids if k.frame])

    @staticmethod
    def _envelope(frames):
        """A parent's frame: the alignment most children name, its ranges unioned.
        Side is 'mixed' when children disagree -- the honest answer for a cell
        a centreline passes through."""
        if not frames:
            return None
        by_name = defaultdict(list)
        for f in frames:
            by_name[f["nearest"]["alignment"]].append(f["nearest"])
            if f.get("second"):
                by_name[f["second"]["alignment"]].append(f["second"])
        ranked = sorted(by_name.items(), key=lambda kv: -len(kv[1]))

        def fold(name, parts):
            sides = {p["side"] for p in parts}
            return {"alignment": name,
                    "offset": [min(p["offset"][0] for p in parts), max(p["offset"][1] for p in parts)],
                    "station": [min(p["station"][0] for p in parts), max(p["station"][1] for p in parts)],
                    "side": sides.pop() if len(sides) == 1 else "mixed",
                    "beyond_end": any(p["beyond_end"] for p in parts)}

        nearest = fold(*ranked[0])
        second = fold(*ranked[1]) if len(ranked) > 1 else None
        return {"nearest": nearest, "second": second}

    def _hash(self):
        for level in range(LEVELS):
            for key, cell in self.cells.items():
                if key[0] != level:
                    continue
                if level == 0:
                    # Over the geometry INSIDE the cell, not the whole entity:
                    # shortening a pad's far end must not change the hash of
                    # the cells its near end sits in.
                    text = "\n".join(sorted(
                        f"{t}|{a[0]:.3f},{a[1]:.3f}|{b[0]:.3f},{b[1]:.3f}"
                        for t, a, b in cell.clipped))
                    text += "\n" + "\n".join(f"{t}:{v:.3f}" for t, v in sorted(cell.covered.items()))
                else:
                    text = "\n".join(sorted(self.cells[k].hash for k in cell.children))
                cell.hash = hashlib.sha1(text.encode()).hexdigest()[:16]

    # -- regions --------------------------------------------------------------

    def _regions(self):
        # Regions are assigned at LEVEL 0. A 200ft cell that touches an
        # alignment would count as corridor wholesale and swallow the lots
        # behind the curb; 50ft cells keep road and lot apart.
        level = 0
        cells = {k: c for k, c in self.cells.items() if k[0] == level}
        regions, member = {}, {}

        # Corridors come straight from the alignments: station range x offset band.
        for align in self.alignments:
            keys = [k for k, c in cells.items()
                    if c.frame and c.frame["nearest"]["alignment"] == align["name"]
                    and c.frame["nearest"]["offset"][0] <= CORRIDOR_HALF_WIDTH]
            if not keys:
                continue
            rid = f"{align['name']} corridor"
            regions[rid] = self._region(rid, "corridor", align["name"], keys)
            for k in keys:
                member[k] = rid

        # Blocks: connected runs of whatever occupied cells are left.
        remaining = [k for k in cells if k not in member]
        seen, n = set(), 0
        for start in sorted(remaining, key=lambda k: (-k[2], k[1])):    # north-west first
            if start in seen:
                continue
            comp, stack = [], [start]
            seen.add(start)
            while stack:
                k = stack.pop()
                comp.append(k)
                for nk in self._neighbours(k, diagonal=False):
                    if nk in cells and nk not in member and nk not in seen:
                        seen.add(nk)
                        stack.append(nk)
            n += 1
            rid = f"Block {n}"
            regions[rid] = self._region(rid, "block", None, comp)
            for k in comp:
                member[k] = rid

        # Relations: what each region touches.
        for rid, region in regions.items():
            touches = set()
            for k in region["cells"]:
                for nk in self._neighbours(k):
                    other = member.get(nk)
                    if other and other != rid:
                        touches.add(other)
            region["touches"] = sorted(touches)
        self._member = member
        return regions

    def _region(self, rid, kind, source, keys):
        handles = set()
        for k in keys:
            handles |= self.cells[k].all_handles()
        xs0 = [bounds(k)[0] for k in keys]; ys0 = [bounds(k)[1] for k in keys]
        xs1 = [bounds(k)[2] for k in keys]; ys1 = [bounds(k)[3] for k in keys]
        by_tag, length = defaultdict(set), Counter()
        for k in keys:
            c = self.cells[k]
            for t, hs in c.handles.items():
                by_tag[t] |= hs
            for t, ln in c.length.items():
                length[t] += ln
        counts = {t: len(hs) for t, hs in by_tag.items() if hs}
        return {"id": rid, "kind": kind, "source": source, "cells": sorted(keys),
                "handles": sorted(handles),
                "bounds": (min(xs0), min(ys0), max(xs1), max(ys1)),
                "count_by_tag": dict(counts), "length_by_tag": {t: round(v, 1) for t, v in length.items()},
                "touches": []}

    @staticmethod
    def _neighbours(key, diagonal=True):
        level, cx, cy = key
        out = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                if not diagonal and dx and dy:
                    continue
                out.append((level, cx + dx, cy + dy))
        return out

    def _site_frame(self):
        """Row/column presentation over the OCCUPIED extents. This is a view
        for rendering; keys never move with it."""
        level = 1 if LEVELS > 1 else 0
        keys = [k for k in self.cells if k[0] == level]
        if not keys:
            return {"level": level, "cols": 0, "rows": 0}
        cxs, cys = [k[1] for k in keys], [k[2] for k in keys]
        return {"level": level, "cx0": min(cxs), "cx1": max(cxs), "cy0": min(cys), "cy1": max(cys),
                "cols": max(cxs) - min(cxs) + 1, "rows": max(cys) - min(cys) + 1}

    # -- queries --------------------------------------------------------------

    def cell(self, key):
        return self.cells.get(key)

    def _region_of_key(self, key):
        """A cell's region; for a parent, the region most of its base cells are in."""
        if key[0] == 0:
            return self._member.get(key)
        hits, stack = Counter(), [key]
        while stack:
            k = stack.pop()
            c = self.cells.get(k)
            if not c:
                continue
            if k[0] == 0:
                r = self._member.get(k)
                if r:
                    hits[r] += 1
            else:
                stack.extend(c.children)
        return hits.most_common(1)[0][0] if hits else None

    def region_of(self, handle=None, point=None):
        if handle is not None:
            keys = self.entity_cells.get(str(handle), ())
            hits = Counter(self._member.get(k) for k in keys)
            hits.pop(None, None)
            return hits.most_common(1)[0][0] if hits else None
        if point is not None:
            return self._member.get(key_at(point[0], point[1]))
        return None

    def locate(self, x, y):
        key = key_at(x, y)
        cell = self.cells.get(key)
        neighbours = {}
        for name, (dx, dy) in (("N", (0, 1)), ("NE", (1, 1)), ("E", (1, 0)), ("SE", (1, -1)),
                               ("S", (0, -1)), ("SW", (-1, -1)), ("W", (-1, 0)), ("NW", (-1, 1))):
            nc = self.cells.get((0, key[1] + dx, key[2] + dy))
            neighbours[name] = nc.tags() if nc else []
        return {"key": key, "bounds": bounds(key), "empty": cell is None,
                "tags": cell.tags() if cell else [],
                "frame": cell.frame if cell else None,
                "region": self.region_of(point=(x, y)),
                "neighbours": neighbours}

    def _keys_in(self, bbox, level=0):
        x0, y0, x1, y1 = bbox
        _, cx0, cy0 = key_at(min(x0, x1), min(y0, y1), level)
        _, cx1, cy1 = key_at(max(x0, x1), max(y0, y1), level)
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                if (level, cx, cy) in self.cells:
                    yield (level, cx, cy)

    def entities(self, tag=None, bbox=None, point=None, radius=None):
        """Handles of entities of `tag` touching a bbox, or within `radius` of a point."""
        if point is not None and radius is not None:
            bbox = (point[0] - radius, point[1] - radius, point[0] + radius, point[1] + radius)
        if bbox is None:
            raise ValueError("entities() needs a bbox or a point and radius")
        out = set()
        box = (min(bbox[0], bbox[2]), min(bbox[1], bbox[3]), max(bbox[0], bbox[2]), max(bbox[1], bbox[3]))
        for key in self._keys_in(box):
            cell = self.cells[key]
            for piece in cell.pieces:
                if tag and piece.tag != tag:
                    continue
                chords = piece.chords()
                if radius is not None:
                    if min(_point_seg_distance(point, a, b)[0] for a, b in chords) > radius:
                        continue
                elif not any(_clip(a, b, box) for a, b in chords):
                    continue                     # in a touched cell, but outside the box
                out.add(piece.handle)
            if radius is None:
                # Closed shapes that COVER the cell without any edge in it (a pad
                # around a whole cell) have no pieces here; count them when the
                # cell's centre is inside the box.
                cx, cy = centre(key)
                if box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
                    for t, hs in cell.handles.items():
                        if not tag or t == tag:
                            out |= hs
        return sorted(out)

    def nearest(self, tag, point, direction=None, max_distance=2000.0):
        """The nearest piece of `tag`, optionally only looking along `direction`.
        Returns (handle, hit point, distance) or None."""
        if direction is not None:
            d = _norm(direction)
            far = _add(point, _scale(d, max_distance))
            best = None
            for key, _ in _traverse(point, far):
                cell = self.cells.get(key)
                if not cell:
                    continue
                for piece in cell.pieces:
                    if piece.tag != tag:
                        continue
                    if piece.kind == "seg":
                        hit = _ray_segments(point, d, [piece.geom])
                        if hit and (best is None or hit[0] < best[2]):
                            best = (piece.handle, hit[1], hit[0])
                    else:
                        for t, p in _ray_circle(point, d, *piece.geom):
                            if best is None or t < best[2]:
                                best = (piece.handle, p, t)
                if best and best[2] <= math.dist(point, centre(key)) + cell_size(0):
                    break                       # nothing further along can be nearer
            return best

        ring, best = 0, None
        _, cx, cy = key_at(*point)
        while ring * BASE_SIZE <= max_distance:
            for dx in range(-ring, ring + 1):
                for dy in range(-ring, ring + 1):
                    if max(abs(dx), abs(dy)) != ring:
                        continue
                    cell = self.cells.get((0, cx + dx, cy + dy))
                    if not cell:
                        continue
                    for piece in cell.pieces:
                        if piece.tag != tag:
                            continue
                        for a, b in piece.chords():
                            dist, foot = _point_seg_distance(point, a, b)
                            if best is None or dist < best[2]:
                                best = (piece.handle, foot, dist)
            if best and best[2] <= ring * BASE_SIZE:
                return best
            ring += 1
        return best

    def between(self, a, b, tag=None):
        """Pieces the segment ab crosses in its interior: [(handle, tag, point)]."""
        return self._crossings_of(Piece("seg", (a, b), "query", "query", ""), tag)

    def _crossings_of(self, probe: Piece, tag=None, eps=1e-3):
        out, seen = [], set()
        for a, b in probe.chords():
            seg = _v(a, b)
            length = math.hypot(seg[0], seg[1])
            if length < 1e-12:
                continue
            d = (seg[0] / length, seg[1] / length)
            for key, _ in _traverse(a, b):
                cell = self.cells.get(key)
                if not cell:
                    continue
                for piece in cell.pieces:
                    if tag and piece.tag != tag:
                        continue
                    if piece.kind == "seg":
                        hit = _ray_segments(a, d, [piece.geom])
                        hits = [(hit[0], hit[1])] if hit else []
                    else:
                        hits = _ray_circle(a, d, *piece.geom)
                    for t, p in hits:
                        if not (eps < t < length - eps):
                            continue                 # touches at an end are not crossings
                        if piece.kind == "seg":
                            pa, pb = piece.geom
                            u = _dot(_v(pa, p), _v(pa, pb)) / max(1e-12, _dot(_v(pa, pb), _v(pa, pb)))
                            if not (eps / max(1e-9, math.dist(pa, pb)) < u < 1 - eps / max(1e-9, math.dist(pa, pb))):
                                continue
                        sig = (piece.handle, round(p[0], 3), round(p[1], 3))
                        if sig in seen:
                            continue
                        seen.add(sig)
                        out.append((piece.handle, piece.tag, p))
        return out

    def crossings(self, payloads: list, tags=None):
        """The dry-run gate. Which proposed payloads cross existing geometry of
        the given tags (all tags if None), and where. Touching at an endpoint
        -- a fillet landing on the curb, a leg landing on a lot line -- is not
        a crossing."""
        out = []
        for index, piece in _payload_pieces(payloads):
            for t in (tags or [None]):
                for handle, tag, p in self._crossings_of(piece, t):
                    if tags is None or tag in tags:
                        out.append({"payload": index, "intent": payloads[index].get("intent"),
                                    "crosses": handle, "tag": tag,
                                    "at": [round(p[0], 3), round(p[1], 3)]})
        return out

    def diff(self, other: "SpatialIndex"):
        """Level-0 cells added, removed or changed, found coarse-to-fine so an
        unchanged parent's children are never visited."""
        added, removed, changed = [], [], []

        def walk(level, keys_a, keys_b):
            for k in sorted(set(keys_a) | set(keys_b)):
                ca, cb = self.cells.get(k), other.cells.get(k)
                if ca is None:
                    (removed if level == 0 else None) is not None and removed.append(k)
                    if level > 0:
                        walk(level - 1, [], cb.children)
                elif cb is None:
                    if level == 0:
                        added.append(k)
                    else:
                        walk(level - 1, ca.children, [])
                elif ca.hash != cb.hash:
                    if level == 0:
                        changed.append(k)
                    else:
                        walk(level - 1, ca.children, cb.children)

        top = LEVELS - 1
        walk(top, [k for k in self.cells if k[0] == top], [k for k in other.cells if k[0] == top])
        return {"added": added, "removed": removed, "changed": changed}

    # -- rendering ------------------------------------------------------------

    @staticmethod
    def _station(value):
        return f"{int(value // 100)}+{value % 100:05.2f}"

    @staticmethod
    def _bearing_words(angle, anisotropy):
        if angle is None:
            return "no linework"
        if anisotropy < 0.4:
            return "no dominant direction"
        for name, centre_deg in (("E-W", 0), ("NE-SW", 45), ("N-S", 90), ("NW-SE", 135), ("E-W", 180)):
            if abs(angle - centre_deg) <= 22.5:
                return f"running {name}"
        return "running E-W"

    def _edge_words(self, cell, tag):
        """Where a tag's linework sits within the cell, as a compass edge."""
        pts = [_mid((a, b)) for t, a, b in cell.clipped if t == tag]
        if not pts:
            return ""
        cx, cy = centre(cell.key)
        mx = sum(p[0] for p in pts) / len(pts) - cx
        my = sum(p[1] for p in pts) / len(pts) - cy
        s = cell_size(cell.key[0])
        if max(abs(mx), abs(my)) < 0.25 * s:
            return "through the middle"
        ns = "north" if my > 0.25 * s else ("south" if my < -0.25 * s else "")
        ew = "east" if mx > 0.25 * s else ("west" if mx < -0.25 * s else "")
        return f"along the {(ns + ' ' + ew).strip()} edge"

    def _frame_words(self, frame):
        if not frame:
            return "no alignment nearby"
        n = frame["nearest"]
        sta = f"sta {self._station(n['station'][0])} to {self._station(n['station'][1])}"
        if n["beyond_end"]:
            sta += " (at the end)"
        side = {"left": "left side", "right": "right side", "mixed": "passes through"}[n["side"]]
        off = f"{n['offset'][0]:.0f} to {n['offset'][1]:.0f} ft off"
        return f"{n['alignment']}, {sta}, {side}, {off}"

    def describe(self, key) -> str:
        cell = self.cells.get(key)
        if cell is None:
            return f"cell {key}: empty"
        region = self._region_of_key(key)
        head = f"{region + ', ' if region else ''}{self._frame_words(cell.frame)}."
        parts = []
        pads = cell.count("pad")
        if pads:
            cov = cell.covered.get("pad", 0.0)
            parts.append(f"{pads} pad{'s' if pads != 1 else ''}" + (f" ({cov:.0%} covered)" if cov else ""))
        angle, aniso = cell.orientation()
        for tag in ("curb", "lot_line", "centreline", "driveway", "swale", "setback",
                    "easement", "boundary", "right_of_way", "other"):
            ln = cell.length.get(tag, 0.0)
            if ln <= 0:
                continue
            n = cell.count(tag)
            words = f"{tag.replace('_', ' ')} {ln:.0f} ft"
            if n > 1:
                words += f" ({n} pieces)"
            if key[0] == 0 and tag in ("curb", "lot_line", "driveway", "swale"):
                edge = self._edge_words(cell, tag)
                if edge:
                    words += f" {edge}"
            parts.append(words)
        body = ", ".join(parts) if parts else "nothing drawn"
        orient = self._bearing_words(angle, aniso)
        cont = ""
        if key[0] == 0 and cell.length.get("curb", 0) > 0:
            dirs = [d for d, (dx, dy) in (("E", (1, 0)), ("W", (-1, 0)), ("N", (0, 1)), ("S", (0, -1)))
                    if (0, key[1] + dx, key[2] + dy) in self.cells
                    and self.cells[(0, key[1] + dx, key[2] + dy)].length.get("curb", 0) > 0]
            if dirs:
                cont = f" Curb continues {' and '.join(dirs)}."
        return f"{head} {body[0].upper() + body[1:]}; linework {orient}.{cont}"

    def describe_region(self, rid) -> str:
        r = self.regions.get(rid)
        if not r:
            return f"{rid}: no such region"
        x0, y0, x1, y1 = r["bounds"]
        counts = ", ".join(f"{n} {t.replace('_', ' ')}{'s' if n != 1 and t == 'pad' else ''}"
                           for t, n in sorted(r["count_by_tag"].items()) if n)
        lengths = ", ".join(f"{t.replace('_', ' ')} {v:.0f} ft" for t, v in sorted(r["length_by_tag"].items()) if v)
        touch = f" Touches {', '.join(r['touches'])}." if r["touches"] else ""
        return (f"{rid} ({r['kind']}): {len(r['cells'])} cells, {x1 - x0:.0f} x {y1 - y0:.0f} ft. "
                f"{counts}. {lengths}.{touch}")

    def site_map(self, level=None) -> str:
        """The coarse grid as characters, north up, with a legend."""
        level = self.site["level"] if level is None else level
        keys = [k for k in self.cells if k[0] == level]
        if not keys:
            return "(empty)"
        cxs, cys = [k[1] for k in keys], [k[2] for k in keys]
        cx0, cx1, cy0, cy1 = min(cxs), max(cxs), min(cys), max(cys)
        rows = []
        for cy in range(cy1, cy0 - 1, -1):
            row = []
            for cx in range(cx0, cx1 + 1):
                cell = self.cells.get((level, cx, cy))
                glyph = "."
                if cell:
                    for g, test in MAP_GLYPHS:
                        if test(cell):
                            glyph = g
                            break
                row.append(glyph)
            rows.append("".join(row))
        s = cell_size(level)
        legend = ("legend: R curb  P pads  + curb+pads  L lot lines  d driveways/swales  "
                  "o other linework  . empty")
        head = (f"{len(rows)} rows x {cx1 - cx0 + 1} cols of {s:.0f} ft cells, north up; "
                f"column 0 is x={ORIGIN[0] + cx0 * s:.0f}, top row is y={ORIGIN[1] + cy1 * s:.0f}")
        return "\n".join([head, legend, ""] + rows)

    def summary(self) -> str:
        lines = [f"{len([k for k in self.cells if k[0] == 0])} occupied cells at {BASE_SIZE:.0f} ft, "
                 f"{len(self.alignments)} alignments, {len(self.regions)} regions."]
        for rid in self.regions:
            lines.append("  " + self.describe_region(rid))
        return "\n".join(lines)


def build(data: dict) -> SpatialIndex:
    return SpatialIndex(data)
