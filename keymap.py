"""Key map: the sheet cells of a site, read off a drawing's linework.

A grading key map is a drawing whose model space shows the whole site at a
small scale with a rectangle per grading sheet and the sheet's number (the
key) written inside it. The rectangles are seldom closed polylines: cells
share edges, some are open polylines, some are plain lines, and a line may
stop a hair short of the next. So this planner does not look for closed
shapes. It builds the planar graph of the linework -- endpoints snapped
together, edges split where they cross or touch, dangling tails pruned --
and takes the graph's faces. A key names the smallest face that contains it.

The key map is in the same coordinates as the plans, so a cell's ring IS the
model-space window of its sheet; sheets.plan_from_cells turns cells into a
plan. Nothing here talks to the plugin; the dashboard hands in a snapshot.
"""

import math
import re
from collections import defaultdict

import profiles
import spatial
from spatial import _point_in_polygon

DEFAULT_SNAP = 0.5          # ft: endpoints this close are one node, gaps this wide close
MIN_FACE_AREA = 4.0         # sq ft: a sliver between near-duplicate lines is not a cell
COLLINEAR_TOL_DEG = 1.0     # a turn smaller than this is not a corner of the cell
MAX_KEY_LEN = 16


class KeyMapError(RuntimeError):
    pass


def layer_list(v) -> list:
    """Layer suffixes from a list or a comma-separated string, lowercased."""
    if v is None:
        return []
    if isinstance(v, str):
        v = v.split(",")
    return [str(x).strip().lower() for x in v if str(x).strip()]


def _on_layers(layer, suffixes) -> bool:
    if not suffixes:
        return True
    name = str(layer or "").lower()
    return any(name == s or name.endswith(s) for s in suffixes)


# ── The planar graph ─────────────────────────────────────────────────────────


def _segments(data: dict, cell_layers, snap: float) -> list:
    """Every chord of the linework on the cell layers: lines, arcs (sampled),
    polylines (bulges sampled), circles."""
    out = []
    for piece in spatial._snapshot_pieces(data):
        if piece.kind not in ("seg", "arc") or not _on_layers(piece.layer, cell_layers):
            continue
        for a, b in piece.chords():
            if math.dist(a, b) >= snap:
                out.append(((float(a[0]), float(a[1])), (float(b[0]), float(b[1]))))
    return out


class _Nodes:
    """Points snapped to within `snap` of each other are one node (the first
    seen keeps its coordinates). A hash grid of cell `snap` makes the lookup
    local: a point within snap of another is in the same or a neighbouring cell."""

    def __init__(self, snap):
        self.snap = float(snap)
        self.pts = []
        self.grid = defaultdict(list)

    def _cell(self, p):
        return (int(math.floor(p[0] / self.snap)), int(math.floor(p[1] / self.snap)))

    def of(self, p) -> int:
        cx, cy = self._cell(p)
        best, best_d = None, self.snap
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self.grid.get((cx + dx, cy + dy), ()):
                    d = math.dist(p, self.pts[i])
                    if d <= best_d:
                        best, best_d = i, d
        if best is not None:
            return best
        self.pts.append((float(p[0]), float(p[1])))
        i = len(self.pts) - 1
        self.grid[(cx, cy)].append(i)
        return i


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _noded_edges(segs: list, nodes: _Nodes, snap: float) -> tuple:
    """The segments as edges between nodes, split wherever two of them cross
    or one ends on another (a T-junction, or a collinear overlap). Returns
    (edge set, duplicates removed)."""
    ends = [(nodes.of(a), nodes.of(b)) for a, b in segs]
    pts = nodes.pts
    boxes = []
    for u, v in ends:
        a, b = pts[u], pts[v]
        boxes.append((min(a[0], b[0]) - snap, min(a[1], b[1]) - snap, max(a[0], b[0]) + snap, max(a[1], b[1]) + snap))
    splits = [[] for _ in segs]

    def foot(e, a, d, length2):
        """Parameter of e's projection on a + t*d, and the distance to it."""
        t = ((e[0] - a[0]) * d[0] + (e[1] - a[1]) * d[1]) / length2
        f = (a[0] + t * d[0], a[1] + t * d[1])
        return t, math.dist(e, f)

    n = len(segs)
    for i in range(n):
        ui, vi = ends[i]
        if ui == vi:
            continue
        a, b = pts[ui], pts[vi]
        d1 = (b[0] - a[0], b[1] - a[1])
        len1 = math.hypot(*d1)
        eps1 = snap / len1
        bi = boxes[i]
        for j in range(i + 1, n):
            uj, vj = ends[j]
            if uj == vj:
                continue
            bj = boxes[j]
            if bi[2] < bj[0] or bj[2] < bi[0] or bi[3] < bj[1] or bj[3] < bi[1]:
                continue
            c, d = pts[uj], pts[vj]
            d2 = (d[0] - c[0], d[1] - c[1])
            len2 = math.hypot(*d2)
            eps2 = snap / len2
            den = _cross(d1, d2)
            shared = {ui, vi} & {uj, vj}
            if abs(den) > 1e-9 * len1 * len2 and not shared:
                ac = (c[0] - a[0], c[1] - a[1])
                t, s = _cross(ac, d2) / den, _cross(ac, d1) / den
                if eps1 < t < 1 - eps1 and eps2 < s < 1 - eps2:
                    k = nodes.of((a[0] + t * d1[0], a[1] + t * d1[1]))
                    splits[i].append((t, k))
                    splits[j].append((s, k))
                    continue
            # an endpoint of one on the other's interior: the other is split there
            for k in (uj, vj):
                if k in (ui, vi):
                    continue
                t, dist = foot(pts[k], a, d1, len1 * len1)
                if eps1 < t < 1 - eps1 and dist <= snap:
                    splits[i].append((t, k))
            for k in (ui, vi):
                if k in (uj, vj):
                    continue
                s, dist = foot(pts[k], c, d2, len2 * len2)
                if eps2 < s < 1 - eps2 and dist <= snap:
                    splits[j].append((s, k))

    edges = {}
    for i, (u, v) in enumerate(ends):
        chain = [u] + [k for _, k in sorted(splits[i], key=lambda x: x[0])] + [v]
        for p, q in zip(chain, chain[1:]):
            if p == q:
                continue
            key = (min(p, q), max(p, q))
            edges[key] = edges.get(key, 0) + 1
    duplicates = sum(c - 1 for c in edges.values())
    return set(edges), duplicates


def _prune_dangling(edges: set) -> tuple:
    """Removes every edge that ends in a degree-one node, repeatedly: open tails,
    stray lines and isolated segments never bound a cell. Returns (adjacency,
    edges removed)."""
    adj = defaultdict(set)
    for u, v in edges:
        adj[u].add(v)
        adj[v].add(u)
    removed = 0
    stack = [n for n in adj if len(adj[n]) == 1]
    while stack:
        n = stack.pop()
        if len(adj[n]) != 1:
            continue
        (m,) = tuple(adj[n])
        adj[n].discard(m)
        adj[m].discard(n)
        removed += 1
        if len(adj[m]) == 1:
            stack.append(m)
    return {n: s for n, s in adj.items() if s}, removed


def _shoelace(ring) -> float:
    s = 0.0
    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        s += a[0] * b[1] - b[0] * a[1]
    return s / 2.0


def _faces(adj: dict, pts: list, edge_count: int, warnings: list) -> list:
    """The bounded faces of the planar graph: walk every half-edge keeping the
    face on its left (at each node take the neighbour just clockwise of the
    way back). Bounded faces come out counter-clockwise (positive area); each
    component's outer boundary comes out clockwise and is dropped."""
    nbrs = {}
    for n, s in adj.items():
        nbrs[n] = sorted(s, key=lambda m: math.atan2(pts[m][1] - pts[n][1], pts[m][0] - pts[n][0]))
    visited = set()
    faces = []
    limit = 2 * edge_count + 2
    for u in nbrs:
        for v in nbrs[u]:
            if (u, v) in visited:
                continue
            ring, cu, cv, steps, closed = [], u, v, 0, False
            while True:
                if (cu, cv) in visited:
                    closed = (cu, cv) == (u, v)
                    break
                visited.add((cu, cv))
                ring.append(cu)
                lst = nbrs[cv]
                nxt = lst[(lst.index(cu) - 1) % len(lst)]
                cu, cv = cv, nxt
                steps += 1
                if steps > limit:
                    break
            if not closed:
                warnings.append("the linework graph has a walk that never closes; some cells may be missing")
                continue
            outline = [pts[i] for i in ring]
            area = _shoelace(outline)
            if area > MIN_FACE_AREA:
                faces.append({"outline": outline, "area": area, "keys": []})
    return faces


# ── Keys ─────────────────────────────────────────────────────────────────────


_MTEXT_CODE = re.compile(r"\\[A-Za-z][^;]*;")


def _clean_text(raw) -> str:
    s = str(raw or "")
    s = s.replace("\\P", " ")            # paragraph break, before the code regex eats it
    s = _MTEXT_CODE.sub("", s)
    s = s.replace("{", "").replace("}", "")
    s = re.sub(r"%%[a-zA-Z]", "", s)
    return " ".join(s.split())


def _key_texts(data: dict, key_layers, pattern) -> list:
    rx = re.compile(pattern) if pattern else None
    out = []
    for tx in data.get("texts") or []:
        kind = str(tx.get("type") or "")
        if "leader" in kind.lower():
            continue
        if not _on_layers(tx.get("layer"), key_layers):
            continue
        text = _clean_text(tx.get("text"))
        p = spatial._xy(tx.get("position"))
        if not text or p is None or len(text) > MAX_KEY_LEN:
            continue
        if rx and not rx.match(text):
            continue
        out.append({"key": text, "handle": str(tx.get("handle") or ""), "layer": str(tx.get("layer") or ""),
                    "position": [round(p[0], 3), round(p[1], 3)]})
    return out


# ── Cell geometry ────────────────────────────────────────────────────────────


def _rot(p, c, deg):
    a = math.radians(deg)
    dx, dy = p[0] - c[0], p[1] - c[1]
    return (c[0] + dx * math.cos(a) - dy * math.sin(a), c[1] + dx * math.sin(a) + dy * math.cos(a))


def _simplify(ring: list, snap: float) -> list:
    """Drops repeated points and the nodes that sit on a straight edge (where
    another line met it), so the corners are the cell's real corners."""
    pts = [ring[0]]
    for p in ring[1:]:
        if math.dist(p, pts[-1]) >= snap:
            pts.append(p)
    if len(pts) > 1 and math.dist(pts[0], pts[-1]) < snap:
        pts.pop()
    n = len(pts)
    if n < 3:
        return pts
    out = []
    for i in range(n):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
        u, v = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
        turn = math.degrees(math.atan2(_cross(u, v), u[0] * v[0] + u[1] * v[1]))
        if abs(turn) >= COLLINEAR_TOL_DEG:
            out.append(b)
    return out if len(out) >= 3 else pts


def _cell_geometry(outline: list, snap: float) -> dict:
    """The cell as a viewport window: the angle of its longest edge is the
    frame, the frame's box around the outline is the ring (SW, SE, NE, NW),
    and the twist a viewport needs to show that ring is the negative of the
    frame angle (spatial.viewport_ring rotates the paper by -twist)."""
    pts = _simplify(outline, snap)
    best, theta = -1.0, 0.0
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        length = math.dist(a, b)
        if length > best:
            best, theta = length, math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
    theta = ((theta + 90.0) % 180.0) - 90.0        # fold to (-90, 90]
    if abs(theta) < 0.05 or abs(abs(theta) - 90.0) < 0.05:
        theta = 0.0                                # north-up (a portrait cell is still north-up)
    c = (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)
    frame = [_rot(p, c, -theta) for p in pts]
    x0, x1 = min(p[0] for p in frame), max(p[0] for p in frame)
    y0, y1 = min(p[1] for p in frame), max(p[1] for p in frame)
    centre = _rot(((x0 + x1) / 2.0, (y0 + y1) / 2.0), c, theta)
    ring = [_rot(p, c, theta) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    if theta == 0.0:
        ring = [(x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        centre = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    twist = 0.0 if theta == 0.0 else -theta
    return {"outline": [[round(p[0], 3), round(p[1], 3)] for p in pts],
            "corners": n,
            "ring": [[round(p[0], 3), round(p[1], 3)] for p in ring],
            "center": [round(centre[0], 3), round(centre[1], 3)],
            "bbox": [[round(min(xs), 3), round(min(ys), 3)], [round(max(xs), 3), round(max(ys), 3)]],
            "twist_deg": round(twist, 3),
            "width": round(x1 - x0, 3), "height": round(y1 - y0, 3),
            "area": round(abs(_shoelace(pts)), 2)}


def _page_order(cells: list) -> None:
    """row / col on every cell: rows from the top, columns west to east, in the
    frame of the largest cell (so a twisted set still reads in page order)."""
    if not cells:
        return
    ref = max(cells, key=lambda c: c["area"])
    theta = -ref["twist_deg"]
    heights = sorted(c["height"] for c in cells)
    tol = 0.5 * heights[len(heights) // 2]
    framed = []
    for c in cells:
        fx, fy = _rot(c["center"], (0.0, 0.0), -theta)
        framed.append((fy, fx, c))
    framed.sort(key=lambda t: (-t[0], t[1]))
    row, row_y, rows = -1, None, defaultdict(list)
    for fy, fx, c in framed:
        if row_y is None or row_y - fy > tol:
            row += 1
            row_y = fy
        rows[row].append((fx, c))
    for r, members in rows.items():
        members.sort(key=lambda t: t[0])
        for k, (_, c) in enumerate(members):
            c["row"], c["col"] = r, k


# ── The planner ──────────────────────────────────────────────────────────────


def cells_from_snapshot(data: dict, cell_layers=None, key_layers=None, snap=DEFAULT_SNAP,
                        key_pattern=None) -> dict:
    """The key map's cells: every face of the linework on `cell_layers` (blank
    = all linework) that holds a key text from `key_layers` (blank = all text)
    matching `key_pattern` (blank = any short text). Faces without a key and
    keys in no face are reported, never fatal; nothing is fatal but an empty
    snapshot."""
    if not isinstance(data, dict):
        raise KeyMapError("the key map snapshot is not a drawing")
    snap = float(snap or DEFAULT_SNAP)
    if snap <= 0:
        raise KeyMapError("snap must be a positive distance in feet")
    cell_layers, key_layers = layer_list(cell_layers), layer_list(key_layers)
    warnings = []
    segs = _segments(data, cell_layers, snap)
    if not segs:
        raise KeyMapError("the key map has no linework" + (f" on layers ending {', '.join(cell_layers)}" if cell_layers else ""))
    nodes = _Nodes(snap)
    edges, duplicates = _noded_edges(segs, nodes, snap)
    adj, dangling = _prune_dangling(edges)
    faces = _faces(adj, nodes.pts, len(edges), warnings)
    keys = _key_texts(data, key_layers, key_pattern)
    if not keys:
        warnings.append("no key text found" + (f" on layers ending {', '.join(key_layers)}" if key_layers else "") +
                        (f" matching {key_pattern}" if key_pattern else ""))

    keys_without_cell = []
    for k in keys:
        holders = [f for f in faces if _point_in_polygon(k["position"], f["outline"])]
        if not holders:
            keys_without_cell.append(dict(k))
            continue
        min(holders, key=lambda f: f["area"])["keys"].append(k)

    cells, faces_without_key, seen = [], [], defaultdict(list)
    for f in faces:
        geom = _cell_geometry(f["outline"], snap)
        if not f["keys"]:
            faces_without_key.append({"center": geom["center"], "area": geom["area"], "corners": geom["corners"],
                                      "width": geom["width"], "height": geom["height"]})
            continue
        f["keys"].sort(key=lambda k: profiles.natural_key(k["key"]))
        primary = f["keys"][0]
        if len(f["keys"]) > 1:
            warnings.append(f"cell {primary['key']} holds {len(f['keys'])} keys "
                            f"({', '.join(k['key'] for k in f['keys'])}); using {primary['key']}")
        if geom["corners"] != 4:
            warnings.append(f"cell {primary['key']} has {geom['corners']} corners; its sheet shows the box around it")
        cell = {"key": primary["key"], **geom, "text_handle": primary["handle"], "text_layer": primary["layer"],
                "row": 0, "col": 0}
        cells.append(cell)
        seen[primary["key"]].append(cell)
    for key, group in seen.items():
        if len(group) > 1:
            warnings.append(f"key {key} appears in {len(group)} cells; give each cell its own key")
    _page_order(cells)
    cells.sort(key=lambda c: (profiles.natural_key(c["key"]), c["row"], c["col"]))
    return {"cells": cells, "faces_without_key": faces_without_key, "keys_without_cell": keys_without_cell,
            "counts": {"segments": len(segs), "nodes": len(nodes.pts), "edges": len(edges), "faces": len(faces),
                       "dangling_removed": dangling, "duplicates_removed": duplicates, "keys": len(keys)},
            "warnings": warnings}


def describe_cells(result: dict) -> list:
    """One line per cell, in the words a person would use."""
    cells = result.get("cells") or []
    lines = [f"{len(cells)} cell(s)" + (": " + ", ".join(c["key"] for c in cells) if cells else "")]
    for c in cells:
        extra = f", twist {c['twist_deg']:g} deg" if c.get("twist_deg") else ""
        lines.append(f"  {c['key']}: {c['width']:,.0f} x {c['height']:,.0f} ft, centre "
                     f"{c['center'][0]:,.0f}, {c['center'][1]:,.0f} (row {c.get('row', 0) + 1}, "
                     f"column {c.get('col', 0) + 1}){extra}")
    for f in result.get("faces_without_key") or []:
        lines.append(f"  a closed area with no key at {f['center'][0]:,.0f}, {f['center'][1]:,.0f} "
                     f"({f['width']:,.0f} x {f['height']:,.0f} ft)")
    for k in result.get("keys_without_cell") or []:
        lines.append(f"  key {k['key']!r} is inside no closed area (at {k['position'][0]:,.0f}, {k['position'][1]:,.0f})")
    counts = result.get("counts") or {}
    if counts.get("dangling_removed"):
        lines.append(f"  {counts['dangling_removed']} open tail(s) ignored")
    lines.extend("  note: " + w for w in result.get("warnings") or [])
    return lines
