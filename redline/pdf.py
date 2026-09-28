"""PDF pages as images.

pypdfium2 (BSD / Apache, redistributable without conditions) renders; when it
is missing, PyMuPDF (AGPL) is used instead for development. Both are imported
lazily so the rest of the package and every offline test work without either.
"""
import os

from PIL import Image

from .types import Page


class _Backend:
    name = ""

    def page_count(self, path) -> int: ...

    def render_page(self, path, index, dpi) -> tuple: ...       # (PIL image, width_pt, height_pt)

    def render_clip(self, path, index, dpi, clip_pt) -> Image.Image: ...


class _Pdfium(_Backend):
    name = "pypdfium2"

    def __init__(self):
        import pypdfium2
        self.m = pypdfium2

    def page_count(self, path):
        doc = self.m.PdfDocument(path)
        try:
            return len(doc)
        finally:
            doc.close()

    def render_page(self, path, index, dpi):
        doc = self.m.PdfDocument(path)
        try:
            page = doc[index]
            w, h = page.get_size()
            img = page.render(scale=dpi / 72.0).to_pil().convert("RGB")
            return img, w, h
        finally:
            doc.close()

    def render_clip(self, path, index, dpi, clip_pt):
        doc = self.m.PdfDocument(path)
        try:
            page = doc[index]
            w, h = page.get_size()
            x0, y0, x1, y1 = clip_pt                      # top-left origin, points
            x0, y0 = max(0.0, x0), max(0.0, y0)
            x1, y1 = min(w, x1), min(h, y1)
            # crop = (left, bottom, right, top) amounts to cut, bottom-left origin
            crop = (x0, h - y1, w - x1, y0)
            return page.render(scale=dpi / 72.0, crop=crop).to_pil().convert("RGB")
        finally:
            doc.close()


class _MuPdf(_Backend):
    name = "pymupdf"

    def __init__(self):
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf
        self.m = pymupdf

    def page_count(self, path):
        with self.m.open(path) as doc:
            return doc.page_count

    def render_page(self, path, index, dpi):
        with self.m.open(path) as doc:
            pg = doc[index]
            pix = pg.get_pixmap(dpi=dpi, colorspace=self.m.csRGB, alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples), pg.rect.width, pg.rect.height

    def render_clip(self, path, index, dpi, clip_pt):
        with self.m.open(path) as doc:
            pg = doc[index]
            pix = pg.get_pixmap(dpi=dpi, clip=self.m.Rect(*clip_pt), colorspace=self.m.csRGB, alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


_backend = None


def backend() -> _Backend:
    global _backend
    if _backend is None:
        errors = []
        for cls in (_Pdfium, _MuPdf):
            try:
                _backend = cls()
                break
            except ImportError as e:
                errors.append(str(e))
        if _backend is None:
            raise RuntimeError("no PDF renderer installed: pip install pypdfium2 (" + "; ".join(errors) + ")")
    return _backend


def page_count(pdf_path: str) -> int:
    return backend().page_count(pdf_path)


def rasterise(pdf_path: str, dpi: int = 150, out_dir: str | None = None, pages: list | None = None) -> list:
    """Every page (or the 1-based `pages`) of a PDF as a Page with a PNG on
    disk under out_dir (default: beside the PDF, in <stem>_pages/)."""
    out_dir = out_dir or os.path.join(os.path.dirname(os.path.abspath(pdf_path)),
                                      os.path.splitext(os.path.basename(pdf_path))[0] + "_pages")
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    b = backend()
    out = []
    for i in range(1, b.page_count(pdf_path) + 1):
        if pages and i not in pages:
            continue
        img, w_pt, h_pt = b.render_page(pdf_path, i - 1, dpi)
        path = os.path.join(out_dir, f"{stem}_p{i}.png")
        img.save(path)
        out.append(Page(pdf=os.path.abspath(pdf_path), page_no=i, dpi=dpi, width_px=img.width, height_px=img.height,
                        image_path=path, width_in=round(w_pt / 72.0, 4), height_in=round(h_pt / 72.0, 4), _image=img))
    return out


def crop(page: Page, bbox_px: list, dpi: int | None = None) -> Image.Image:
    """A region of the page. From a PDF it is re-rendered at `dpi` (sharper
    than upscaling the page image); from an in-memory page it is cut and
    resized."""
    x0, y0, x1, y1 = [int(round(v)) for v in bbox_px]
    target_dpi = dpi or page.dpi
    if page.pdf and os.path.exists(page.pdf):
        try:
            k = 72.0 / page.dpi
            return backend().render_clip(page.pdf, page.page_no - 1, target_dpi, (x0 * k, y0 * k, x1 * k, y1 * k))
        except Exception:
            pass
    img = page.image().crop((x0, y0, x1, y1))
    if target_dpi != page.dpi and img.width and img.height:
        f = target_dpi / float(page.dpi)
        img = img.resize((max(1, int(img.width * f)), max(1, int(img.height * f))), Image.LANCZOS)
    return img


def page_from_image(img: Image.Image, dpi: int, page_no: int = 1, pdf: str = "") -> Page:
    """A Page wrapping an image already in memory (tests, or a scanned PNG)."""
    return Page(pdf=pdf, page_no=page_no, dpi=dpi, width_px=img.width, height_px=img.height,
                width_in=img.width / float(dpi), height_in=img.height / float(dpi), _image=img.convert("RGB"))
