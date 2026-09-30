"""Reading the ink: a vision-language model on crops, or a fixture.

read_comments  one call per ink region: a crop with a margin (so an arrow
               that leaves the box is seen) at the crop DPI, asked for the
               text, the kind of mark, whether it points at something and
               where. Small models transcribe a crop well; they do not find
               boxes on a whole sheet, which is why marks.py does that.
read_text      printed text (the title block strip).
find_marks     the fallback for a scan with no colour (a grey photocopy):
               the page in tiles, the model asked for boxes. Coarse.
"""
import json
import os
from typing import Protocol

from . import marks, openai_compat
from . import pdf as pdf_mod
from .types import Comment, Page, Region, scope_of

KINDS = ("note", "arrow", "circle", "cloud", "strike", "dimension", "unknown")

GLOSSARY = ("Civil plan shorthand: PROP. = proposed, EX. or EXIST. = existing, TYP. = typical, R.O.W. = right of way, "
            "FH or HYD = fire hydrant, MH = manhole, SS or SAN = sanitary sewer, STM = storm, WL or W = water line, "
            "INL = inlet, CL = centerline, BOC = back of curb, FL = flowline, TC = top of curb, FG = finished grade, "
            "STA = station, ' = feet, \" = inches, Ø = diameter, N/S/E/W = compass directions.")

READ_SCHEMA = {
    "title": "comment",
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "kind": {"type": "string", "enum": list(KINDS)},
        "targets_geometry": {"type": "boolean"},
        "target_offset": {"type": "string", "enum": ["on", "above", "below", "left", "right", "none"]},
        "mark_center": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
        "starred": {"type": "boolean"},
        "is_directive": {"type": "boolean"},
        "confidence": {"type": "number"},
    },
    "required": ["text", "kind", "targets_geometry", "mark_center", "starred", "is_directive", "confidence"],
}

TOP_BAND = 0.12   # fraction of the page height that counts as the top margin


def in_top_band(bbox_px: list, page: Page) -> bool:
    return (bbox_px[1] + bbox_px[3]) / 2.0 <= TOP_BAND * page.height_px

MARKS_SCHEMA = {
    "title": "marks",
    "type": "object",
    "properties": {"marks": {"type": "array", "items": {"type": "object", "properties": {
        "x0": {"type": "number"}, "y0": {"type": "number"}, "x1": {"type": "number"}, "y1": {"type": "number"},
        "text": {"type": "string"}}, "required": ["x0", "y0", "x1", "y1"]}}},
    "required": ["marks"],
}

SWEEP_MIN_PX_AT_150 = 40


def _boxes_overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def sweep(page: Page, regions: list, ocr, rgb=None, tiles=(3, 2)) -> tuple:
    """A second, independent look for handwriting: the model scans the page in
    tiles and every box it reports that the ink detector has no region or
    connector for becomes an extra region (when there is red ink in it), and
    a tile where the model counts clearly more comments than were detected
    becomes a warning. (extra_regions, warnings)."""
    rgb = marks._array(page) if rgb is None else rgb
    mask = marks.red_mask(rgb)
    min_px = int(SWEEP_MIN_PX_AT_150 * (page.dpi / 150.0) ** 2)
    covered = [list(r.bbox_px) for r in regions] + [cb for r in regions for cb in r.connector_boxes]
    found = ocr.find_marks(page, tiles=tiles) if hasattr(ocr, "find_marks") else []
    extra = []
    for m in found:
        if any(_boxes_overlap(m.bbox_px, c) for c in covered):
            continue
        x0, y0, x1, y1 = [int(v) for v in m.bbox_px]
        if int(mask[y0:y1, x0:x1].sum()) < max(1, min_px):
            continue
        extra.append(Region(id="", bbox_px=[x0, y0, x1, y1], color="model", kind_hint="model"))
    warnings = []
    nx, ny = tiles
    tw, th = page.width_px / nx, page.height_px / ny
    for j in range(ny):
        for i in range(nx):
            tile = [i * tw, j * th, (i + 1) * tw, (j + 1) * th]

            def inside(b):
                cx, cy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
                return tile[0] <= cx < tile[2] and tile[1] <= cy < tile[3]
            detected = sum(1 for r in regions + extra if inside(r.bbox_px))
            counted = sum(1 for m in found if inside(m.bbox_px))
            if counted >= detected + 2:
                warnings.append({"page_no": page.page_no, "kind": "model_count", "tile": [i + 1, j + 1],
                                 "detail": f"model saw {counted} mark(s) in tile {i + 1},{j + 1}; detector has {detected}"})
    return extra, warnings


class OcrBackend(Protocol):
    name: str

    def read_comments(self, page: Page, regions: list, out_dir: str | None = None) -> list: ...

    def read_text(self, image, hint: str = "") -> str: ...

    def find_marks(self, page: Page) -> list: ...


# ── Fixture ──────────────────────────────────────────────────────────────────

class FakeOcr:
    """Answers from a fixture:
        {"title_text": "...",                       # or "title_text_by_page": {"1": ...}
         "comments": {"1": [{"text":..., "kind":..., "targets_geometry":..., "confidence":...,
                             "bbox_px": [..] (optional)}, ...]},
         "regions": {"1": [[x0,y0,x1,y1], ...]}}    # for find_marks
    Comments pair with regions by order; a comment with its own bbox_px is
    placed there whatever the regions say."""
    name = "fake"

    def __init__(self, fixture: dict | str):
        if isinstance(fixture, str):
            with open(fixture, "r", encoding="utf-8") as f:
                fixture = json.load(f)
        self.fixture = fixture or {}
        self.text_calls = 0
        self.current_page = None

    def read_comments(self, page, regions, out_dir=None):
        self.current_page = page.page_no
        wanted = (self.fixture.get("comments") or {}).get(str(page.page_no)) or []
        out = []
        for i, spec in enumerate(wanted):
            region = regions[i] if i < len(regions) else None
            bbox = spec.get("bbox_px") or (region.bbox_px if region else None)
            if bbox is None:
                continue
            targets = spec.get("targets_px")
            if targets is None:
                targets = list(region.targets_px) if region else []
            is_dir = bool(spec.get("is_directive", False))
            starred = bool(spec.get("starred", is_dir))
            out.append(Comment(id=f"{page.id}-c{i + 1}", page_no=page.page_no, bbox_px=list(bbox),
                               text=str(spec.get("text", "")), kind=spec.get("kind", "note"),
                               targets_geometry=bool(spec.get("targets_geometry", False)),
                               target_offset=spec.get("target_offset"),
                               target_bbox_px=spec.get("target_bbox_px"),
                               confidence=float(spec.get("confidence", 0.9)), raw_model_reply="fixture",
                               targets_px=[list(t) for t in targets], starred=starred, is_directive=is_dir,
                               scope=scope_of(targets, is_dir, starred, in_top_band(list(bbox), page))))
        return out

    def read_text(self, image, hint=""):
        self.text_calls += 1
        by_page = self.fixture.get("title_text_by_page") or {}
        if str(self.current_page) in by_page:
            return str(by_page[str(self.current_page)])
        return str(self.fixture.get("title_text") or next(iter(by_page.values()), ""))

    def find_marks(self, page, **kw):
        self.current_page = page.page_no
        boxes = (self.fixture.get("regions") or {}).get(str(page.page_no)) or []
        return [Region(id=f"{page.id}-r{i + 1}", bbox_px=list(b)) for i, b in enumerate(boxes)]


# ── Vision-language model over the OpenAI-compatible endpoint ────────────────

class OpenAICompatVlm:
    name = "vlm"

    def __init__(self, base: str, model: str, crop_dpi: int = 300, margin_in: float = 0.75, timeout: float = 600.0,
                 max_image_px: int = 1280):
        self.base, self.model, self.crop_dpi, self.margin_in, self.timeout = base, model, crop_dpi, margin_in, timeout
        self.max_image_px = max_image_px
        self.name = model

    def _ask(self, image, prompt: str, schema: dict | None, max_tokens: int = 600) -> str:
        messages = [{"role": "user", "content": [{"type": "text", "text": prompt},
                                                 openai_compat.image_part(image, self.max_image_px)]}]
        return openai_compat.chat(self.base, self.model, messages, schema=schema, temperature=0.0,
                                  timeout=self.timeout, max_tokens=max_tokens)

    def read_comments(self, page, regions, out_dir=None):
        out = []
        for i, region in enumerate(regions, start=1):
            box = marks.crop_box(region.bbox_px, page, self.margin_in)
            img = pdf_mod.crop(page, box, dpi=self.crop_dpi)
            crop_path = ""
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
                crop_path = os.path.join(out_dir, f"{page.id}_c{i}.png")
                img.save(crop_path)
            prompt = ("This is a crop of a plotted civil engineering plan sheet with a reviewer's handwritten "
                      "redline mark in the middle (the printed drawing is black; the reviewer's ink is coloured). "
                      "Transcribe the handwriting exactly as written (keep abbreviations, feet and inch marks). "
                      "Say what kind of mark it is: note (words only), arrow (points at something), circle or cloud "
                      "(rings something), strike (crosses something out), dimension (a measured distance). "
                      "targets_geometry is true when the mark points at, rings or crosses out a drawn thing; "
                      "target_offset says where that thing sits relative to the handwriting (on, above, below, "
                      "left, right, none). mark_center is where the mark itself sits (the circle, the arrow's tip, "
                      "the X, the cloud; for a note with no mark, the middle of the words) as fractions of this "
                      "image, [x, y] from the top-left corner, 0 to 1. starred is true only when a drawn star or "
                      "asterisk stands right before the first word. is_directive is true only when the words are "
                      "a rule for the whole sheet (like 'all valves to face the street'), not an instruction about "
                      "the thing they sit beside. confidence is 0 to 1 for the transcription. "
                      + GLOSSARY +
                      ' Answer as JSON: {"text": "...", "kind": "...", "targets_geometry": true/false, '
                      '"target_offset": "...", "mark_center": [0.5, 0.5], "starred": false, "is_directive": false, '
                      '"confidence": 0.0}')
            raw = ""
            try:
                raw = self._ask(img, prompt, READ_SCHEMA)
                rep = openai_compat.parse_json(raw)
            except (openai_compat.ModelError, ValueError) as e:
                # keep what the model actually said: it is the only clue when tuning
                out.append(Comment(id=f"{page.id}-c{i}", page_no=page.page_no, bbox_px=list(region.bbox_px),
                                   text="", kind="unknown", confidence=0.0, crop_path=crop_path,
                                   raw_model_reply=f"error: {e}" + (f" | reply: {str(raw)[:600]}" if raw else "")))
                continue
            kind = str(rep.get("kind") or "unknown").lower()
            target_offset = str(rep.get("target_offset") or "none").lower()
            c = Comment(id=f"{page.id}-c{i}", page_no=page.page_no, bbox_px=list(region.bbox_px),
                        text=str(rep.get("text") or "").strip(), kind=kind if kind in KINDS else "unknown",
                        targets_geometry=bool(rep.get("targets_geometry")),
                        target_offset=None if target_offset == "none" else target_offset,
                        confidence=max(0.0, min(1.0, float(rep.get("confidence") or 0.0))),
                        crop_path=crop_path, raw_model_reply=str(raw)[:2000],
                        targets_px=[list(t) for t in region.targets_px], starred=bool(rep.get("starred")),
                        is_directive=bool(rep.get("is_directive")))
            c.scope = scope_of(c.targets_px, c.is_directive, c.starred, in_top_band(region.bbox_px, page))
            # Where the comment is about, best evidence first: an arrow tip or
            # ring centre traced by the ink detector; else the mark's own spot
            # as the model saw it in the crop; else the ink box shifted the way
            # the model said the target lies.
            if not c.targets_px:
                c.target_bbox_px = _mark_box(rep.get("mark_center"), box, page) or _target_box(c, page)
            out.append(c)
        return out

    def read_text(self, image, hint=""):
        # Phrased as a reading task with an explicit "nothing here" answer: on a
        # blank tile a small model otherwise echoes the instructions back.
        prompt = ("Read every piece of printed text in this image and write it out exactly, one line per item, "
                  "keeping file paths, numbers and codes verbatim. " + (hint or "") +
                  " If the image has no text, answer exactly: NONE")
        try:
            raw = self._ask(image, prompt, None, max_tokens=800)
        except openai_compat.ModelError as e:
            return f"(read_text failed: {e})"
        lines = []
        for ln in str(raw).splitlines():
            t = ln.strip()
            if not t or t.upper() == "NONE" or t in prompt or (hint and t in hint):
                continue
            lines.append(t)
        return "\n".join(lines)

    def find_marks(self, page, tiles=(3, 2), overlap: float = 0.1):
        """Coarse boxes from the model on page tiles; used only when the
        colour mask found nothing."""
        img = page.image()
        W, H = img.width, img.height
        nx, ny = tiles
        tw, th = W / nx, H / ny
        out = []
        for j in range(ny):
            for i in range(nx):
                x0 = max(0, int(i * tw - overlap * tw)); y0 = max(0, int(j * th - overlap * th))
                x1 = min(W, int((i + 1) * tw + overlap * tw)); y1 = min(H, int((j + 1) * th + overlap * th))
                tile = img.crop((x0, y0, x1, y1))
                prompt = ("This is part of a plotted engineering plan sheet. Find every handwritten mark a reviewer "
                          "added (notes, arrows, circles, clouds, strike-outs). For each, give its bounding box as "
                          "fractions of this image (x0,y0 top-left to x1,y1 bottom-right, 0 to 1) and the text. "
                          'Answer as JSON: {"marks": [{"x0":0.1,"y0":0.2,"x1":0.3,"y1":0.25,"text":"..."}]}')
                try:
                    rep = openai_compat.parse_json(self._ask(tile, prompt, MARKS_SCHEMA, max_tokens=1200))
                except (openai_compat.ModelError, ValueError):
                    continue
                for m in rep.get("marks") or []:
                    try:
                        bx0 = x0 + float(m["x0"]) * (x1 - x0); by0 = y0 + float(m["y0"]) * (y1 - y0)
                        bx1 = x0 + float(m["x1"]) * (x1 - x0); by1 = y0 + float(m["y1"]) * (y1 - y0)
                    except (KeyError, TypeError, ValueError):
                        continue
                    if bx1 - bx0 < 4 or by1 - by0 < 4:
                        continue
                    out.append(Region(id="", bbox_px=[int(bx0), int(by0), int(bx1), int(by1)], color="model"))
        out = _merge_overlapping(out)
        for k, r in enumerate(out, start=1):
            r.id = f"{page.id}-r{k}"
        return out


def _mark_box(center, crop_box_px: list, page: Page, half_in: float = 0.1) -> list | None:
    """The model's mark_center (fractions of the crop) as a small page-pixel
    box, or None when the answer is missing or not sensible."""
    try:
        fx, fy = float(center[0]), float(center[1])
    except (TypeError, ValueError, IndexError):
        return None
    if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
        return None
    x0, y0, x1, y1 = crop_box_px
    px, py = x0 + fx * (x1 - x0), y0 + fy * (y1 - y0)
    h = half_in * page.dpi
    return [max(0, px - h), max(0, py - h), min(page.width_px, px + h), min(page.height_px, py + h)]


def _target_box(c: Comment, page: Page) -> list | None:
    """Where the thing a mark points at sits: the ink box shifted one box
    width/height in target_offset's direction, clipped to the page."""
    if not c.targets_geometry or not c.target_offset or c.target_offset == "on":
        return None
    x0, y0, x1, y1 = c.bbox_px
    w, h = x1 - x0, y1 - y0
    dx, dy = {"above": (0, -h), "below": (0, h), "left": (-w, 0), "right": (w, 0)}.get(c.target_offset, (0, 0))
    box = [x0 + dx, y0 + dy, x1 + dx, y1 + dy]
    return [max(0, box[0]), max(0, box[1]), min(page.width_px, box[2]), min(page.height_px, box[3])]


def _merge_overlapping(regions: list) -> list:
    merged = []
    for r in sorted(regions, key=lambda r: (r.bbox_px[1], r.bbox_px[0])):
        for m in merged:
            a, b = m.bbox_px, r.bbox_px
            if a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]:
                m.bbox_px = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                break
        else:
            merged.append(Region(id="", bbox_px=list(r.bbox_px), color=r.color))
    return merged


# ── Factory ──────────────────────────────────────────────────────────────────

def backend(settings, fixture=None):
    kind = (settings.ocr_backend or "openai").lower()
    if kind == "fake":
        return FakeOcr(fixture if fixture is not None else (settings.fixture or {}))
    if kind in ("openai", "ollama", "vlm"):
        return OpenAICompatVlm(settings.model_base, settings.vlm, crop_dpi=settings.crop_dpi,
                               timeout=settings.model_timeout, max_image_px=settings.max_image_px)
    raise ValueError(f"unknown OCR backend {kind!r} (openai | fake)")
