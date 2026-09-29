"""Where the reviewer's ink is, without a model.

Redlines are ink, almost always red, on a print that is otherwise black,
grey and white. A colour mask picks the ink; the mask is pooled onto a coarse
grid (cells a fraction of an inch across) so nearby strokes of one comment
join, and connected cells become one region. Deterministic and fast, so the
vision model only has to read crops, which small models do well; asking a
7B model for bounding boxes on a 36 x 24 in page does not work.

find_border finds the plotted border rectangle (the largest dark frame) for
the page->paper refinement in transform.PageToPaper.refined.
"""
from collections import deque

import numpy as np

from .types import Page, Region


def _array(page: Page):
    return np.asarray(page.image().convert("RGB"), dtype=np.int16)


def red_mask(rgb: np.ndarray, min_red: int = 110, margin: int = 50) -> np.ndarray:
    """Pixels whose red channel stands well above green and blue."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    return (r > min_red) & (r - np.maximum(g, b) > margin)


def blue_mask(rgb: np.ndarray, min_blue: int = 100, margin: int = 45) -> np.ndarray:
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    return (b > min_blue) & (b - np.maximum(r, g) > margin)


def dark_mask(rgb: np.ndarray, max_level: int = 110) -> np.ndarray:
    return rgb.max(axis=2) < max_level


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
    for r0 in range(rows):
        for c0 in range(cols):
            if not active[r0, c0] or seen[r0, c0]:
                continue
            q = deque([(r0, c0)])
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


def find_regions(page: Page, color: str = "red", min_area_in2: float = 0.15, merge_in: float = 0.5,
                 min_ink: float = 0.002, rgb: np.ndarray | None = None) -> list:
    """Regions of the reviewer's ink on a page, as page-pixel boxes.

    merge_in    strokes closer than this are one comment (cell size is half of it);
                0.5 in keeps the words of one handwritten line together, since a
                word gap in half-inch handwriting is about 0.2 to 0.4 in
    min_area_in2 boxes smaller than this are specks, dropped
    min_ink     fraction of ink a cell needs to count as marked
    """
    rgb = _array(page) if rgb is None else rgb
    mask = {"red": red_mask, "blue": blue_mask, "dark": dark_mask}[color](rgb)
    if not mask.any():
        return []
    cell_px = max(2, int(round(page.dpi * merge_in / 2.0)))
    pooled = _pool(mask, cell_px)
    active = pooled >= min_ink
    regions = []
    for cells, (r0, c0, r1, c1) in _components(active):
        x0, y0 = c0 * cell_px, r0 * cell_px
        x1, y1 = min(page.width_px, (c1 + 1) * cell_px), min(page.height_px, (r1 + 1) * cell_px)
        # tighten to the ink inside the coarse box
        sub = mask[y0:y1, x0:x1]
        ys, xs = np.nonzero(sub)
        if len(xs) == 0:
            continue
        bx0, bx1 = x0 + int(xs.min()), x0 + int(xs.max()) + 1
        by0, by1 = y0 + int(ys.min()), y0 + int(ys.max()) + 1
        area_in2 = (bx1 - bx0) * (by1 - by0) / float(page.dpi * page.dpi)
        if area_in2 < min_area_in2:
            continue
        ink = float(sub.mean())
        regions.append(Region(id="", bbox_px=[bx0, by0, bx1, by1], ink_fraction=round(ink, 4), color=color))
    # reading order: top to bottom, then left to right, in bands of an inch
    band = page.dpi
    regions.sort(key=lambda r: (r.bbox_px[1] // band, r.bbox_px[0]))
    for i, r in enumerate(regions, start=1):
        r.id = f"{page.id}-r{i}"
    return regions


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
