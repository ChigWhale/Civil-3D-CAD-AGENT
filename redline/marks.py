"""Where the reviewer's ink is, without a model.

Redlines are ink, almost always red, on a print that is otherwise black,
grey and white. A colour mask picks the ink. The ink is then split into two
kinds of thing before anything is grouped:

  text        letters, stars, X's, short ticks: small dense components
  connectors  long thin strokes (arrows, leaders) and rings (circles, clouds)

Text is pooled onto a coarse grid so the words of one handwritten line join
into one region: the crop the vision model reads. Connectors are NOT pooled
into regions: a long arrow would otherwise weld its note to whatever it
touches, and several notes joined by arrows would come out as one giant
crop that the model reads one text from. Instead each connector is traced
on a fine grid to its far end(s); a connector whose near end sits beside a
text region gives that region one target point per far end. A ring around
something gives the point at its centre.

Deterministic and fast, so the vision model only has to read crops, which
small models do well; asking a 7B model for bounding boxes on a 36 x 24 in
page does not work.

find_border finds the plotted border rectangle (the largest dark frame) for
the page->paper refinement in transform.PageToPaper.refined.
"""
import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .types import Page, Region


def _array(page: Page):
    return np.asarray(page.image().convert("RGB"), dtype=np.int16)


def red_mask(rgb: np.ndarray, min_red: int = 100, margin: int = 30, rel: float = 0.2) -> np.ndarray:
    """Pixels whose red channel stands above green and blue, by an absolute
    margin and by a fraction of red, so pink and faint scanned ink count
    while grey and black do not."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    lead = r - np.maximum(g, b)
    return (r > min_red) & (lead > margin) & (lead > rel * r)


def blue_mask(rgb: np.ndarray, min_blue: int = 100, margin: int = 45) -> np.ndarray:
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    return (b > min_blue) & (b - np.maximum(r, g) > margin)


def dark_mask(rgb: np.ndarray, max_level: int = 110) -> np.ndarray:
    return rgb.max(axis=2) < max_level


MASKS = {"red": red_mask, "blue": blue_mask, "dark": dark_mask}


def _pool(mask: np.ndarray, cell_px: int) -> np.ndarray:
    """Fraction of ink per coarse cell."""
    h, w = mask.shape
    ch, cw = -(-h // cell_px), -(-w // cell_px)
    padded = np.zeros((ch * cell_px, cw * cell_px), dtype=np.float32)
    padded[:h, :w] = mask
    return padded.reshape(ch, cell_px, cw, cell_px).mean(axis=(1, 3))


def _components(active: np.ndarray) -> list:
    """8-connected components of a boolean grid: [(cells, [r0, c0, r1, c1])]."""
    seen = np.zeros_like(active, dtype=bool)
    rows, cols = active.shape
    out = []
    for r0, c0 in zip(*np.nonzero(active)):
        if seen[r0, c0]:
            continue
        q = deque([(int(r0), int(c0))])
        seen[r0, c0] = True
        cells = []
        while q:
            r, c = q.popleft()
            cells.append((r, c))
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < rows and 0 <= cc < cols and active[rr, cc] and not seen[rr, cc]:
                        seen[rr, cc] = True
                        q.append((rr, cc))
        rs = [c[0] for c in cells]
        cs = [c[1] for c in cells]
        out.append((cells, [min(rs), min(cs), max(rs), max(cs)]))
    return out


# ── fine components: text vs connectors ─────────────────────────────────────

@dataclass
class Stroke:
    """One connected piece of thin ink on the fine grid."""
    kind: str                       # stroke | ring | ring_absorbed
    cells: list                     # (row, col) on the fine grid
    bbox_px: list
    length_in: float
    fill: float
    leaves: list = field(default_factory=list)    # tip cells
    source_px: list | None = None   # where it meets its text region
    tips_px: list = field(default_factory=list)   # far ends (stroke) or [centre] (ring)
    attached: int | None = None     # index of the text region it belongs to


def _bfs(cells: set, start) -> dict:
    dist = {start: 0}
    q = deque([start])
    while q:
        r, c = q.popleft()
        d = dist[(r, c)] + 1
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                n = (r + dr, c + dc)
                if n in cells and n not in dist:
                    dist[n] = d
                    q.append(n)
    return dist


def _leaves(cells: list, fine_in: float, sep_in: float = 0.4) -> list:
    """The tips of a thin component: cells that are local maxima of the
    ink-path distance from the component's middle (and, so a hook's own end
    is not missed, from the far end of that). Nearby maxima on one thick tip
    are one leaf."""
    cellset = set(cells)
    rows = [c[0] for c in cells]
    cols = [c[1] for c in cells]
    cr, cc = sum(rows) / len(rows), sum(cols) / len(cols)
    start = min(cells, key=lambda c: (c[0] - cr) ** 2 + (c[1] - cc) ** 2)
    found = {}
    d0 = _bfs(cellset, start)
    far = max(d0.items(), key=lambda kv: kv[1])[0]
    sep = max(2, sep_in / fine_in)
    rad = int(math.ceil(sep))
    for root, dist in ((start, d0), (far, _bfs(cellset, far))):
        for cell, d in dist.items():
            if d == 0:
                continue
            r, c = cell
            # a true tip beats every cell of the stroke within sep of it; a
            # discretisation bump on a thick curve does not
            if all(dist.get((r + dr, c + dc), -1) <= d
                   for dr in range(-rad, rad + 1) for dc in range(-rad, rad + 1)
                   if dr * dr + dc * dc <= sep * sep):
                found[cell] = max(found.get(cell, 0), d)
    leaves = []
    for cell, d in sorted(found.items(), key=lambda kv: -kv[1]):
        if all(math.hypot(cell[0] - k[0], cell[1] - k[1]) > sep for k in leaves):
            leaves.append(cell)
    return leaves


def _is_loop(cells: list) -> bool:
    """Closed ring or open stroke? From the middle, walk the ink to the far
    end F, then from F to its far end G. On a loop G comes back beside the
    middle; on a line or a U it is the other tip, far away."""
    cellset = set(cells)
    rows = [c[0] for c in cells]
    cols = [c[1] for c in cells]
    cr, cc = sum(rows) / len(rows), sum(cols) / len(cols)
    start = min(cells, key=lambda c: (c[0] - cr) ** 2 + (c[1] - cc) ** 2)
    d0 = _bfs(cellset, start)
    f = max(d0.items(), key=lambda kv: kv[1])
    df = _bfs(cellset, f[0])
    g = max(df.items(), key=lambda kv: kv[1])[0]
    return d0.get(g, 0) <= max(3, 0.2 * f[1])


def _classify(cells: list, bbox_cells: list, fine_in: float, stroke_min_in: float) -> tuple:
    """(kind, leaves) for one fine component."""
    r0, c0, r1, c1 = bbox_cells
    h, w = (r1 - r0 + 1), (c1 - c0 + 1)
    length_in = max(h, w) * fine_in
    fill = len(cells) / float(h * w)
    thin = fill < 0.3
    if thin and min(h, w) * fine_in >= 0.5 and _is_loop(cells):
        return "ring", []
    if thin and length_in > stroke_min_in and fill < 0.25:
        return "stroke", _leaves(cells, fine_in)
    return "text", []


def _ring_end(cellset: set, leaf, fine_in: float, reach_in: float = 1.25) -> tuple | None:
    """When an arrow's head touches the circle it points at, arrow and circle
    are one piece of ink and the far tip lands on the circle's far side. Look
    at the ink within reach of the tip: a thin loop that surrounds its own
    middle on nearly every side is a ring, and its centre (as a fine cell)
    is the real target."""
    reach = reach_in / fine_in
    sub = [c for c in cellset if abs(c[0] - leaf[0]) <= reach and abs(c[1] - leaf[1]) <= reach]
    if len(sub) < 12:
        return None
    r0, r1 = min(c[0] for c in sub), max(c[0] for c in sub)
    c0, c1 = min(c[1] for c in sub), max(c[1] for c in sub)
    h, w = r1 - r0 + 1, c1 - c0 + 1
    if min(h, w) * fine_in < 0.4 or len(sub) / float(h * w) >= 0.3:
        return None
    rc, cc = (r0 + r1) / 2.0, (c0 + c1) / 2.0
    rad = 0.6 * max(h, w)
    ring = [c for c in sub if math.hypot(c[0] - rc, c[1] - cc) <= rad]
    if any(math.hypot(c[0] - rc, c[1] - cc) <= 0.2 * max(h, w) for c in ring):
        return None                                   # ink through the middle: not a ring
    bins = {int((math.atan2(c[0] - rc, c[1] - cc) + math.pi) / (2 * math.pi) * 12) % 12 for c in ring}
    if len(bins) < 10:
        return None                                   # a hook or a bend, not a loop
    rr0, rr1 = min(c[0] for c in ring), max(c[0] for c in ring)
    cc0, cc1 = min(c[1] for c in ring), max(c[1] for c in ring)
    return ((rr0 + rr1) / 2.0, (cc0 + cc1) / 2.0)


def _rect_distance_px(px, rect) -> float:
    x, y = px
    dx = max(rect[0] - x, 0, x - rect[2])
    dy = max(rect[1] - y, 0, y - rect[3])
    return math.hypot(dx, dy)


def _local_ink(cells: set, cell, fine_in: float, radius_in: float = 0.25) -> int:
    rad = max(1, int(round(radius_in / fine_in)))
    r0, c0 = cell
    return sum(1 for (r, c) in cells if abs(r - r0) <= rad and abs(c - c0) <= rad)


def analyse(page: Page, color: str = "red", min_area_in2: float = 0.04, merge_in: float = 0.5,
            min_ink: float = 0.002, fine_in: float = 0.05, stroke_min_in: float = 1.25, attach_in: float = 0.5,
            source_zone_in: float = 1.0, dedupe_in: float = 0.75, min_px_at_150: int = 40,
            rgb: np.ndarray | None = None) -> tuple:
    """(regions, orphans): the reviewer's text regions with their target
    points, and the long connectors that met no text (kept for the overlay
    and the report's missed-comment hints).

    merge_in        text strokes closer than this are one comment (cell = half of it)
    min_area_in2    text boxes smaller than this are specks
    fine_in         cell size of the grid connectors are traced on
    stroke_min_in   a thin component longer than this is a connector, not writing
    attach_in       a connector end within this of a text box belongs to it
    source_zone_in  a connector end within this of its own text is never a target
    dedupe_in       two targets closer than this are one
    """
    rgb = _array(page) if rgb is None else rgb
    mask = MASKS[color](rgb)
    if not mask.any():
        return [], []
    dpi = float(page.dpi)
    fine_px = max(2, int(round(dpi * fine_in)))
    fine_in = fine_px / dpi
    fine = _pool(mask, fine_px) > 0

    # 1. every piece of ink on the fine grid, sorted into text / stroke / ring
    strokes = []
    text_active = np.zeros_like(fine)
    for cells, bb in _components(fine):
        kind, leaves = _classify(cells, bb, fine_in, stroke_min_in)
        r0, c0, r1, c1 = bb
        if kind == "text":
            for r, c in cells:
                text_active[r, c] = True
            continue
        bbox = [c0 * fine_px, r0 * fine_px, min(page.width_px, (c1 + 1) * fine_px), min(page.height_px, (r1 + 1) * fine_px)]
        strokes.append(Stroke(kind, cells, bbox, max(r1 - r0 + 1, c1 - c0 + 1) * fine_in,
                              len(cells) / float((r1 - r0 + 1) * (c1 - c0 + 1)), leaves=leaves))

    # 2. text regions: the writing pooled at the merge distance, tightened to ink
    text_mask = np.zeros_like(mask)
    for r, c in zip(*np.nonzero(text_active)):
        y0, x0 = int(r) * fine_px, int(c) * fine_px
        text_mask[y0:y0 + fine_px, x0:x0 + fine_px] = mask[y0:y0 + fine_px, x0:x0 + fine_px]
    cell_px = max(2, int(round(dpi * merge_in / 2.0)))
    min_px = int(min_px_at_150 * (dpi / 150.0) ** 2)
    regions = []
    if text_mask.any():
        pooled = _pool(text_mask, cell_px)
        for cells, (r0, c0, r1, c1) in _components(pooled >= min_ink):
            x0, y0 = c0 * cell_px, r0 * cell_px
            x1, y1 = min(page.width_px, (c1 + 1) * cell_px), min(page.height_px, (r1 + 1) * cell_px)
            sub = text_mask[y0:y1, x0:x1]
            sy, sx = np.nonzero(sub)
            if len(sx) < max(1, min_px):
                continue
            bx0, bx1 = x0 + int(sx.min()), x0 + int(sx.max()) + 1
            by0, by1 = y0 + int(sy.min()), y0 + int(sy.max()) + 1
            if (bx1 - bx0) * (by1 - by0) / (dpi * dpi) < min_area_in2:
                continue
            regions.append(Region(id="", bbox_px=[bx0, by0, bx1, by1], ink_fraction=round(float(sub.mean()), 4),
                                  color=color, kind_hint="text"))

    # 3. connectors: which text each one leaves from, and where it goes
    attach_px = attach_in * dpi
    source_zone_px = max(attach_px, source_zone_in * dpi)
    dedupe_px = dedupe_in * dpi

    def px_of(cell):
        return [round((cell[1] + 0.5) * fine_px, 1), round((cell[0] + 0.5) * fine_px, 1)]

    for s in strokes:
        if s.kind == "ring":
            cx, cy = (s.bbox_px[0] + s.bbox_px[2]) / 2.0, (s.bbox_px[1] + s.bbox_px[3]) / 2.0
            s.tips_px = [[round(cx, 1), round(cy, 1)]]
            grown = [s.bbox_px[0] - attach_px, s.bbox_px[1] - attach_px, s.bbox_px[2] + attach_px, s.bbox_px[3] + attach_px]
            for i, reg in enumerate(regions):
                b = reg.bbox_px
                if b[0] < grown[2] and grown[0] < b[2] and b[1] < grown[3] and grown[1] < b[3]:
                    s.attached = i
                    break
            continue
        if not s.leaves:
            continue
        cellset = set(s.cells)
        # the source end: the tip beside a text box; when two qualify, the one
        # with less ink around it (an arrowhead is the denser end)
        cands = []
        for leaf in s.leaves:
            p = px_of(leaf)
            for i, reg in enumerate(regions):
                d = _rect_distance_px(p, reg.bbox_px)
                if d <= attach_px:
                    cands.append((_local_ink(cellset, leaf, fine_in), d, i, leaf))
        if cands:
            cands.sort()
            _, _, s.attached, source = cands[0]
        else:
            source = min(s.leaves, key=lambda leaf: _local_ink(cellset, leaf, fine_in))
        s.source_px = px_of(source)
        for leaf in s.leaves:
            if leaf == source:
                continue
            ring_centre = _ring_end(cellset, leaf, fine_in)
            tip = px_of(ring_centre if ring_centre is not None else leaf)
            # an end within an inch of its own words is where an arrow starts
            # (or an underline comes back), never what it points at
            if s.attached is not None and _rect_distance_px(tip, regions[s.attached].bbox_px) <= source_zone_px:
                continue
            s.tips_px.append(tip)

    # 4. rings that sit where an arrow lands belong to that arrow's note
    for s in strokes:
        if s.kind != "ring" or s.attached is not None:
            continue
        grown = [s.bbox_px[0] - attach_px, s.bbox_px[1] - attach_px, s.bbox_px[2] + attach_px, s.bbox_px[3] + attach_px]
        for t in strokes:
            if t.kind != "stroke" or t.attached is None:
                continue
            for k, tip in enumerate(t.tips_px):
                if grown[0] <= tip[0] <= grown[2] and grown[1] <= tip[1] <= grown[3]:
                    t.tips_px[k] = list(s.tips_px[0])
                    s.attached = t.attached
                    s.kind = "ring_absorbed"
                    break
            if s.kind == "ring_absorbed":
                break

    # 5. hand the targets to the text regions; connectors with no text stand alone
    orphans = []
    for s in strokes:
        if s.kind == "ring_absorbed":
            regions[s.attached].connector_boxes.append(list(s.bbox_px))
            continue
        if s.attached is not None:
            reg = regions[s.attached]
            reg.connector_boxes.append(list(s.bbox_px))
            for tip in s.tips_px:
                if all(math.hypot(tip[0] - t[0], tip[1] - t[1]) > dedupe_px for t in reg.targets_px):
                    reg.targets_px.append(tip)
            if reg.kind_hint == "text":
                reg.kind_hint = "arrow" if s.kind == "stroke" else "ring"
            continue
        if s.kind == "ring" or s.length_in <= 3.0:
            # a ring round an area, or a short lone arrow, is a comment in itself
            regions.append(Region(id="", bbox_px=list(s.bbox_px), ink_fraction=round(s.fill, 4), color=color,
                                  kind_hint=s.kind, targets_px=[list(t) for t in s.tips_px],
                                  connector_boxes=[list(s.bbox_px)]))
        else:
            orphans.append({"kind": s.kind, "bbox_px": list(s.bbox_px), "tips_px": [list(t) for t in s.tips_px],
                            "length_in": round(s.length_in, 2)})

    # reading order: top to bottom, then left to right, in bands of an inch
    band = page.dpi
    regions.sort(key=lambda r: (r.bbox_px[1] // band, r.bbox_px[0]))
    for i, r in enumerate(regions, start=1):
        r.id = f"{page.id}-r{i}"
    return regions, orphans


def find_regions(page: Page, color: str = "red", min_area_in2: float = 0.04, merge_in: float = 0.5,
                 min_ink: float = 0.002, rgb: np.ndarray | None = None, **kw) -> list:
    """Regions of the reviewer's ink on a page, as page-pixel boxes with their
    target points. See analyse() for the parameters."""
    return analyse(page, color=color, min_area_in2=min_area_in2, merge_in=merge_in, min_ink=min_ink, rgb=rgb, **kw)[0]


def debug_image(page: Page, regions: list, orphans: list | None = None):
    """The page with region boxes (green), connector boxes (orange), target
    dots (blue) and orphan connectors (magenta) drawn on, for checking a run
    by eye."""
    from PIL import ImageDraw
    img = page.image().copy()
    d = ImageDraw.Draw(img)
    lw = max(2, page.dpi // 50)
    for reg in regions:
        for cb in reg.connector_boxes:
            d.rectangle(cb, outline=(255, 140, 0), width=lw)
        d.rectangle(reg.bbox_px, outline=(0, 170, 0), width=lw)
        d.text((reg.bbox_px[0], max(0, reg.bbox_px[1] - 6 * lw)), reg.id, fill=(0, 120, 0))
        for t in reg.targets_px:
            r = 3 * lw
            d.ellipse([t[0] - r, t[1] - r, t[0] + r, t[1] + r], fill=(0, 90, 255))
            d.line([(reg.bbox_px[0] + reg.bbox_px[2]) / 2, (reg.bbox_px[1] + reg.bbox_px[3]) / 2, t[0], t[1]],
                   fill=(0, 90, 255), width=1)
    for o in orphans or []:
        d.rectangle(o["bbox_px"], outline=(220, 0, 220), width=lw)
    return img


def find_border(page: Page, rgb: np.ndarray | None = None, min_span: float = 0.6) -> tuple | None:
    """The plotted border: the outermost long dark horizontal and vertical
    runs. Row and column projections of the dark mask are scanned for the
    first and last lines that span at least `min_span` of the page. Returns
    (x0, y0, x1, y1) in page pixels, or None when no frame is found."""
    rgb = _array(page) if rgb is None else rgb
    dark = dark_mask(rgb)
    h, w = dark.shape
    rows = dark.sum(axis=1) / float(w)
    cols = dark.sum(axis=0) / float(h)
    ys = np.nonzero(rows >= min_span)[0]
    xs = np.nonzero(cols >= min_span)[0]
    if len(ys) < 2 or len(xs) < 2:
        return None
    y0, y1 = int(ys.min()), int(ys.max())
    x0, x1 = int(xs.min()), int(xs.max())
    if y1 - y0 < h * 0.3 or x1 - x0 < w * 0.3:
        return None
    return (x0, y0, x1, y1)


def crop_box(bbox_px: list, page: Page, margin_in: float) -> list:
    m = int(round(margin_in * page.dpi))
    return [max(0, bbox_px[0] - m), max(0, bbox_px[1] - m),
            min(page.width_px, bbox_px[2] + m), min(page.height_px, bbox_px[3] + m)]
