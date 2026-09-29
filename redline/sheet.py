"""Which drawing, and which layout of it, a scanned page is.

The title block on a plotted sheet carries the sheet number, the project
number and, on the border, the drawing's file path. The strip is
cropped (fractions of the page, so a shrunk scan still lands on it), read
by the OCR backend, and parsed. Candidates for the .dwg come from three
places, scored together: drawings Civil 3D has open (GET /documents), .dwg
files under CAD_PROJECT_ROOT whose name matches, and the printed path when
it exists. The best candidate is opened by asking the plugin for its
snapshot with the file named; the plugin opens a drawing it does not have
(HttpBridge.ResolveDocument). The layout is the one whose title block
attribute '#' equals the sheet number.

This is the one place the pipeline chooses a drawing path. The dashboard's
rule that the chat agent never does still holds: this code runs on a PDF
the user handed over, and the drawing it opens is the one printed on it.
"""
import difflib
import os
import re

import requests

from . import pdf as pdf_mod
from .types import SheetMatch, TitleBlockRead, ViewportRef
from .transform import PageToPaper, PaperToModel

DWG_RE = re.compile(r"((?:[A-Za-z]:[\\/]|\\\\)[^\n\r\"<>|*?]*?\.dwg\b|[\w \-()&.'#]+?\.dwg\b)", re.I)
PROJECT_RE = re.compile(r"\b(\d{4}-\d{4}-\d{2})\b")
SHEET_WORD_RE = re.compile(r"\b(?:SHEET|SHT)\.?\s*(?:NO\.?|NUMBER|#)?\s*[:.]?\s*([A-Z]{0,2}-?\d{1,3}[A-Z]?)\b", re.I)
SHEET_CODE_RE = re.compile(r"\b([A-Z]{1,2}-\d{1,3}[A-Z]?)\b")
OF_RE = re.compile(r"\b(\d{1,3})\s+OF\s+(\d{1,3})\b", re.I)
TITLE_KEYS = ("PLAN", "PROFILE", "DETAIL", "GRADING", "WATER", "SANITARY", "STORM", "PAVING", "DRAINAGE",
              "UTILITY", "EROSION", "SITE", "LAYOUT", "NOTES", "SECTION")

NAME_MIN_RATIO = 0.85
AMBIGUITY = 0.05


# ── Title block text ────────────────────────────────────────────────────────

def parse_title_text(text: str) -> TitleBlockRead:
    text = str(text or "")
    read = TitleBlockRead(raw_text=text)
    m = DWG_RE.search(text)
    if m:
        raw = m.group(1).strip()
        read.printed_path = raw if re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", raw) else None
        read.printed_file = os.path.basename(raw.replace("/", "\\"))
    m = PROJECT_RE.search(text)
    if m:
        read.project_no = m.group(1)
    cands = []
    for m in SHEET_WORD_RE.finditer(text):
        cands.append(m.group(1).upper())
    for m in OF_RE.finditer(text):
        cands.append(m.group(1))
    for m in SHEET_CODE_RE.finditer(text):
        cands.append(m.group(1).upper())
    seen = []
    for c in cands:
        if c and c not in seen:
            seen.append(c)
    read.sheet_no_candidates = seen
    read.sheet_no = seen[0] if seen else None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    titled = [ln for ln in lines if any(k in ln.upper() for k in TITLE_KEYS) and len(ln) < 60
              and not DWG_RE.search(ln)]
    if titled:
        read.sheet_title = titled[0]
    return read


TITLE_HINT = ("engineering plan title block: transcribe every printed word and number exactly, including any "
              "file path ending in .dwg, the sheet number, project number and sheet title")


def _tiles(img, max_px: int, overlap: float = 0.08) -> list:
    """The image cut along its long axis into pieces no longer than max_px,
    overlapping a little so a line of text on a seam is whole in one piece."""
    if not max_px or max(img.width, img.height) <= max_px:
        return [img]
    vertical = img.height >= img.width
    long = img.height if vertical else img.width
    n = -(-long // max_px)
    step = long / n
    pad = int(step * overlap)
    out = []
    for k in range(n):
        a = max(0, int(k * step) - pad)
        b = min(long, int((k + 1) * step) + pad)
        out.append(img.crop((0, a, img.width, b)) if vertical else img.crop((a, 0, b, img.height)))
    return out


def _has_print(tile, min_fraction: float = 0.0005) -> bool:
    """Is there anything printed here beyond border lines? Rows and columns
    that are mostly dark (the border) are discounted before counting."""
    try:
        import numpy as np
        from . import marks
        dark = marks.dark_mask(np.asarray(tile.convert("RGB"), dtype=np.int16))
    except Exception:
        return True
    if dark.size == 0:
        return False
    rows = dark.mean(axis=1) >= 0.5
    cols = dark.mean(axis=0) >= 0.5
    dark[rows, :] = False
    dark[:, cols] = False
    return float(dark.mean()) >= min_fraction


def read_title_block(page, ocr, settings, out_dir: str | None = None) -> TitleBlockRead:
    """OCR the title strips (page fractions in settings.title_rects) and parse.

    Each strip is rendered at settings.title_dpi and read in tiles no longer
    than settings.max_image_px, since one call with a whole 24 in strip does
    not fit a small model's context. Reading stops early once a drawing file
    name and a sheet number have both been seen."""
    texts, crops = [], []
    dpi = getattr(settings, "title_dpi", None) or settings.crop_dpi
    max_px = getattr(settings, "max_image_px", 0)
    done = False
    # title_rects describe an unrotated plot. A sheet plotted at 90, 270 or
    # 180 degrees (or scanned upside down) carries the same strips on other
    # edges; those are tried, in that order, only while the configured ones
    # have yielded neither a file name nor a sheet number. Blank strips cost
    # no model calls, so a miss is cheap.
    base = list(settings.title_rects)
    rects = base[:]
    rects += [(fy0, 1 - fx1, fy1, 1 - fx0) for (fx0, fy0, fx1, fy1) in base]      # plot rotation 90
    rects += [(1 - fy1, fx0, 1 - fy0, fx1) for (fx0, fy0, fx1, fy1) in base]      # 270
    rects += [(1 - fx1, 1 - fy1, 1 - fx0, 1 - fy0) for (fx0, fy0, fx1, fy1) in base]  # 180
    for i, (fx0, fy0, fx1, fy1) in enumerate(rects, start=1):
        if done:
            break
        if i > len(base) and (i - 1) % len(base) == 0:
            so_far = parse_title_text("\n".join(texts))
            if so_far.printed_file or so_far.sheet_no:
                break
        box = [int(fx0 * page.width_px), int(fy0 * page.height_px), int(fx1 * page.width_px), int(fy1 * page.height_px)]
        if box[2] - box[0] < 4 or box[3] - box[1] < 4:
            continue
        img = pdf_mod.crop(page, box, dpi=dpi)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, f"{page.id}_title{i}.png")
            img.save(path)
            crops.append(path)
        for tile in _tiles(img, max_px):
            if not _has_print(tile):
                continue                      # blank paper: no model call
            texts.append(ocr.read_text(tile, hint=TITLE_HINT))
            so_far = parse_title_text("\n".join(texts))
            if so_far.printed_file and so_far.sheet_no:
                done = True
                break
    read = parse_title_text("\n".join(texts))
    read.crops = crops
    return read


# ── Candidate drawings ───────────────────────────────────────────────────────

def open_documents(plugin_base: str, timeout: float = 5.0) -> list:
    try:
        r = requests.get(f"{plugin_base}/documents", timeout=timeout)
        return [d.get("path") for d in (r.json().get("drawings") or []) if d.get("path")]
    except (requests.exceptions.RequestException, ValueError, AttributeError):
        return []


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(str(path).replace("/", "\\")))[0].strip().lower()


def name_ratio(a: str, b: str) -> float:
    a, b = _stem(a), _stem(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def walk_dwgs(roots: list, limit: int = 20000) -> list:
    out = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in ("backup", "bak", "archive")]
            for fn in filenames:
                if fn.lower().endswith(".dwg"):
                    out.append(os.path.join(dirpath, fn))
                    if len(out) >= limit:
                        return out
    return out


def find_candidates(read: TitleBlockRead, open_paths: list, roots: list) -> list:
    """Scored [{path, how, score, why}], best first."""
    cands = {}

    def add(path, how, score, why):
        key = os.path.normcase(os.path.abspath(path))
        cur = cands.get(key)
        if cur is None or score > cur["score"]:
            cands[key] = {"path": os.path.abspath(path), "how": how, "score": round(score, 3), "why": why}

    wanted = read.printed_file or ""
    proj = (read.project_no or "").lower()
    if read.printed_path and os.path.isfile(read.printed_path):
        add(read.printed_path, "printed_path", 1.3, "the printed path exists")
    for p in open_paths:
        r = name_ratio(p, wanted) if wanted else 0.0
        if r >= NAME_MIN_RATIO:
            add(p, "open_document", r + 0.2 + (0.2 if proj and proj in p.lower() else 0.0), f"open in Civil 3D, name match {r:.2f}")
    if wanted:
        for p in walk_dwgs(roots):
            r = name_ratio(p, wanted)
            if r >= NAME_MIN_RATIO:
                add(p, "project_root", r + (0.2 if proj and proj in p.lower() else 0.0), f"under project root, name match {r:.2f}")
    elif proj:
        # no file name read: a project folder with exactly one plan-looking drawing is still a lead
        hits = [p for p in walk_dwgs(roots) if proj in p.lower()]
        if len(hits) == 1:
            add(hits[0], "project_root", 0.5, "only drawing under the project number")
    return sorted(cands.values(), key=lambda c: -c["score"])


def choose_candidate(cands: list) -> tuple:
    """(candidate, reason). reason is None on a clear choice."""
    if not cands:
        return None, "no_drawing_candidate"
    if len(cands) > 1 and cands[0]["score"] - cands[1]["score"] < AMBIGUITY \
            and os.path.normcase(cands[0]["path"]) != os.path.normcase(cands[1]["path"]):
        return None, "ambiguous_drawing"
    return cands[0], None


# ── Layout ───────────────────────────────────────────────────────────────────

def _norm_sheet(s) -> str:
    s = str(s or "").strip().upper().replace(" ", "")
    m = re.match(r"^([A-Z]*-?)0*(\d+[A-Z]?)$", s)
    return (m.group(1) + m.group(2)) if m else s


def pick_layout(layouts: list, read: TitleBlockRead) -> tuple:
    """(layout, score, why) or (None, 0, reason)."""
    scored = []
    cands = {_norm_sheet(c) for c in read.sheet_no_candidates}
    text_up = (read.raw_text or "").upper()
    for lay in layouts or []:
        if not (lay.get("viewports") or []) and not (lay.get("texts") or lay.get("blocks")):
            continue
        score, why = 0.0, []
        for b in lay.get("blocks") or []:
            attrs = b.get("attributes") or {}
            sheet = attrs.get("#") or attrs.get("SHEET") or attrs.get("SHEET_NO") or attrs.get("SHT")
            if sheet and _norm_sheet(sheet) in cands:
                score += 1.0
                why.append(f"title block # {sheet}")
            title = attrs.get("SHEET_TITLE") or attrs.get("TITLE")
            if title and read.raw_text:
                r = difflib.SequenceMatcher(None, str(title).upper(), (read.sheet_title or "").upper()).ratio() \
                    if read.sheet_title else 0.0
                if r >= 0.6 or (len(str(title)) > 6 and str(title).upper() in text_up):
                    score += 0.5
                    why.append(f"sheet title '{title}'")
        name = str(lay.get("name") or "")
        if name and (_norm_sheet(name) in cands or (len(name) > 3 and name.upper() in text_up)):
            score += 0.3
            why.append(f"layout name '{name}'")
        scored.append((score, lay, ", ".join(why)))
    if not scored:
        return None, 0.0, "no_layouts"
    scored.sort(key=lambda s: -s[0])
    best = scored[0]
    if best[0] < 0.5:
        with_vp = [s for s in scored if s[1].get("viewports")]
        if len(with_vp) == 1:
            return with_vp[0][1], 0.4, "only layout with a viewport"
        return None, 0.0, "no_layout_match"
    if len(scored) > 1 and best[0] - scored[1][0] < AMBIGUITY:
        return None, 0.0, "ambiguous_layout"
    return best[1], best[0], best[2]


def viewport_refs(layout: dict) -> list:
    out = []
    for vp in layout.get("viewports") or []:
        try:
            if not vp.get("on", True):
                continue
            out.append(ViewportRef(handle=str(vp.get("handle") or ""), paper_center=list(vp["paper_center"]),
                                   paper_size=list(vp["paper_size"]), scale=float(vp["scale"]),
                                   twist_deg=float(vp.get("twist_deg") or 0.0), model_center=list(vp["model_center"][:2]),
                                   on=True, frozen_layers=list(vp.get("frozen_layers") or [])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def transforms_for(match: SheetMatch) -> tuple:
    """(PageToPaper, [PaperToModel]) rebuilt from a SheetMatch record."""
    p2p = PageToPaper.from_dict(match.page_to_paper)
    vps = [PaperToModel(tuple(v["paper_center"]), tuple(v["paper_size"]), v["scale"], v["twist_deg"],
                        tuple(v["model_center"]), v["handle"]) for v in match.viewports]
    return p2p, vps


# ── The match ────────────────────────────────────────────────────────────────

def match(page, read: TitleBlockRead, settings, open_drawing, border_rect_px=None) -> SheetMatch:
    """`open_drawing(path)` returns the drawing's snapshot dict (and opens it
    in Civil 3D on the way, through spatial_cache.get). Everything else is
    local."""
    out = SheetMatch(page_no=page.page_no, title_block=read.to_dict())
    open_paths = open_documents(settings.plugin_base)
    cands = find_candidates(read, open_paths, settings.project_roots)
    out.candidates_considered = cands[:8]
    chosen, reason = choose_candidate(cands)
    if chosen is None:
        out.reason = reason
        return out
    out.dwg_path, out.how, out.score = chosen["path"], chosen["how"], chosen["score"]
    out.opened_by_pipeline = os.path.normcase(chosen["path"]) not in {os.path.normcase(p) for p in open_paths}
    try:
        data = open_drawing(chosen["path"])
    except Exception as e:
        out.reason = f"could_not_open: {e}"
        return out
    layout, lscore, why = pick_layout(data.get("layouts") or [], read)
    if layout is None:
        out.reason = why
        return out
    out.layout_name, out.layout_handle = layout.get("name"), layout.get("handle")
    out.score = round(out.score + lscore, 3)
    p2p = PageToPaper.nominal(page.width_px, page.height_px, layout)
    if settings.border_rect and border_rect_px:
        refined = p2p.refined(border_rect_px, settings.border_rect)
        if refined.rmse_in <= 0.1:
            p2p = refined
    out.page_to_paper = p2p.to_dict()
    out.viewports = [v.to_dict() for v in viewport_refs(layout)]
    if not out.viewports:
        out.reason = "layout_has_no_viewport"
    return out
