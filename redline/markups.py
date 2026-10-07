"""Bluebeam text boxes, read from the PDF's annotations.

A Text Box or a Callout (a text box with a leader) made in Bluebeam is a
FreeText annotation: its text, rectangle, author, subject, date and the
leader's points are stored in the file, so nothing needs detecting or
reading by a model. Only FreeText is used here; sticky notes, clouds,
arrows, pen strokes and the rest are counted and ignored. A flattened PDF
has no annotations at all and yields nothing.

Coordinates: PDF points, origin at the page's bottom-left, converted to the
page-image pixels pdf.py renders (top-left origin, /Rotate applied) so the
boxes line up with the renders.
"""
import re

from .types import Comment

ROTATIONS = (0, 90, 180, 270)


def _rotate(page) -> int:
    try:
        r = int(page.get("/Rotate", 0)) % 360
    except (TypeError, ValueError):
        r = 0
    return r if r in ROTATIONS else 0


def _mediabox(page) -> tuple:
    mb = page.mediabox
    return float(mb.left), float(mb.bottom), float(mb.right), float(mb.top)


def to_px(x_pt: float, y_pt: float, mediabox: tuple, rotate: int, dpi: int) -> tuple:
    """A point in default user space -> pixels on the rendered page image."""
    x0, y0, x1, y1 = mediabox
    w, h = x1 - x0, y1 - y0
    u, v = x_pt - x0, y1 - y_pt            # unrotated: right, down (points)
    if rotate == 90:
        u, v = h - v, u                    # page turned clockwise for display
    elif rotate == 180:
        u, v = w - u, h - v
    elif rotate == 270:
        u, v = v, w - u
    s = dpi / 72.0
    return (u * s, v * s)


def _text(annot) -> str:
    contents = annot.get("/Contents")
    text = str(contents) if contents is not None else ""
    if not text.strip():
        rc = annot.get("/RC")
        if rc is not None:
            text = re.sub(r"<[^>]+>", " ", str(rc))
            text = re.sub(r"&nbsp;|&#160;", " ", text)
            text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return " ".join(text.replace("\r", "\n").split())


def _floats(arr) -> list:
    out = []
    for v in arr or []:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            return []
    return out


def _str(annot, key) -> str | None:
    v = annot.get(key)
    return str(v) if v is not None and str(v).strip() else None


def read(pdf_path: str, dpi: int = 150, pages: list | None = None) -> dict:
    """{page_no: {"comments": [Comment], "size_pt": (w, h), "rotate": int,
    "annots_seen": int, "annots_other": {subtype: count}}} for every page
    (or the 1-based pages asked for)."""
    from pypdf import PdfReader

    reader = PdfReader(pdf_path)
    wanted = {int(p) for p in pages} if pages else None
    out = {}
    for idx, page in enumerate(reader.pages):
        page_no = idx + 1
        if wanted is not None and page_no not in wanted:
            continue
        mediabox = _mediabox(page)
        rotate = _rotate(page)
        rec = {"comments": [], "size_pt": (mediabox[2] - mediabox[0], mediabox[3] - mediabox[1]), "rotate": rotate,
               "annots_seen": 0, "annots_other": {}}
        annots = page.get("/Annots") or []
        k = 0
        for ref in annots:
            try:
                annot = ref.get_object()
            except Exception:
                continue
            if annot is None:
                continue
            rec["annots_seen"] += 1
            subtype = str(annot.get("/Subtype") or "").lstrip("/")
            if subtype != "FreeText":
                rec["annots_other"][subtype or "?"] = rec["annots_other"].get(subtype or "?", 0) + 1
                continue
            rect = _floats(annot.get("/Rect"))
            if len(rect) != 4:
                continue
            corners = [to_px(rect[0], rect[1], mediabox, rotate, dpi), to_px(rect[2], rect[3], mediabox, rotate, dpi)]
            xs, ys = sorted(c[0] for c in corners), sorted(c[1] for c in corners)
            bbox_px = [int(round(xs[0])), int(round(ys[0])), int(round(xs[1])), int(round(ys[1]))]
            cl = _floats(annot.get("/CL"))
            targets = []
            if len(cl) in (4, 6):
                tip = to_px(cl[0], cl[1], mediabox, rotate, dpi)   # the first pair is the arrowhead end
                targets = [[round(tip[0], 1), round(tip[1], 1)]]
            k += 1
            text = _text(annot)
            rec["comments"].append(Comment(
                id=f"p{page_no}-m{k}", page_no=page_no, bbox_px=bbox_px, text=text,
                kind="arrow" if targets else "note", targets_geometry=bool(targets), targets_px=targets,
                confidence=1.0, scope="point",
                author=_str(annot, "/T"), subject=_str(annot, "/Subj"), created=_str(annot, "/M")))
        out[page_no] = rec
    return out


def summary(rec: dict) -> str:
    n = len(rec.get("comments") or [])
    other = rec.get("annots_other") or {}
    s = f"{n} text box{'es' if n != 1 else ''} read"
    if other:
        s += "; other markups ignored: " + ", ".join(f"{k} {v}" for k, v in sorted(other.items()))
    return s
