"""Page pixels -> paper inches -> model feet, and back.

Two transforms, composed:

PageToPaper   The scan is the plotted sheet. Nominally one page inch is one
              paper inch, with the plot rotation applied and the y axis
              flipped (pixels count down, paper counts up). A scan that was
              shrunk to fit a smaller page, or is slightly off, is refined by
              fitting the border rectangle found in the image to the border's
              known paper rectangle (per-axis scale and translation).

PaperToModel  One per viewport. The same arithmetic as CadCommands.ViewportWindow
              and spatial.viewport_ring: paper offset from the viewport centre,
              over the scale (paper units per model unit), rotated by -twist,
              added to the model centre.

    model = model_center + R(-twist) . ((paper - paper_center) / scale)
"""
import math
from dataclasses import dataclass

PLOT_ROTATION = {"Degrees000": 0, "Degrees090": 90, "Degrees180": 180, "Degrees270": 270,
                 "0": 0, "90": 90, "180": 180, "270": 270}


def paper_size_in(layout: dict) -> tuple:
    """(width, height) of the layout's plot paper in inches. The AutoCAD API
    reports PlotPaperSize in millimetres whatever the plot units say; a value
    too small to be millimetres is taken as inches already."""
    size = layout.get("plot_paper_size") or [914.4, 609.6]
    w, h = float(size[0]), float(size[1])
    if max(w, h) > 100.0:
        w, h = w / 25.4, h / 25.4
    return w, h


def plot_rotation_deg(layout: dict) -> int:
    return PLOT_ROTATION.get(str(layout.get("plot_rotation") or "Degrees000"), 0)


@dataclass
class PageToPaper:
    """paper_x = a*px + b*py + tx ; paper_y = c*px + d*py + ty (inches)."""
    a: float
    b: float
    c: float
    d: float
    tx: float
    ty: float
    method: str = "nominal"
    rmse_in: float = 0.0

    def to_paper(self, px, py) -> tuple:
        return (self.a * px + self.b * py + self.tx, self.c * px + self.d * py + self.ty)

    def to_page(self, x, y) -> tuple:
        det = self.a * self.d - self.b * self.c
        if abs(det) < 1e-12:
            raise ValueError("degenerate page transform")
        u, v = x - self.tx, y - self.ty
        return ((self.d * u - self.b * v) / det, (-self.c * u + self.a * v) / det)

    def inches_per_pixel(self) -> float:
        return max(math.hypot(self.a, self.c), math.hypot(self.b, self.d))

    def to_dict(self) -> dict:
        return {"A": [[self.a, self.b], [self.c, self.d]], "t": [self.tx, self.ty],
                "method": self.method, "rmse_in": round(self.rmse_in, 4)}

    @classmethod
    def from_dict(cls, d: dict) -> "PageToPaper":
        (a, b), (c, dd) = d["A"]
        return cls(a, b, c, dd, d["t"][0], d["t"][1], d.get("method", "nominal"), d.get("rmse_in", 0.0))

    @classmethod
    def nominal(cls, width_px: int, height_px: int, layout: dict | None = None,
                page_in: tuple | None = None) -> "PageToPaper":
        """The page is the plotted sheet. With a layout, the paper size and
        plot rotation come from its page setup; otherwise the page's own size
        in inches (page_in) is the paper, unrotated."""
        rot = plot_rotation_deg(layout) if layout else 0
        if layout:
            pw, ph = paper_size_in(layout)
        elif page_in:
            pw, ph = float(page_in[0]), float(page_in[1])
        else:
            raise ValueError("nominal() needs a layout or the page size in inches")
        # Image u (right) and v (down) as fractions of the page, then onto the
        # paper axes for each rotation. Rotation 0: paper x = u, paper y = 1 - v.
        sx, sy = 1.0 / width_px, 1.0 / height_px
        if rot == 0:
            return cls(pw * sx, 0.0, 0.0, -ph * sy, 0.0, ph, "nominal")
        if rot == 180:
            return cls(-pw * sx, 0.0, 0.0, ph * sy, pw, 0.0, "nominal")
        if rot == 90:
            # paper turned a quarter counter-clockwise on the page: the paper's
            # x axis runs up the page, its y axis runs left.
            # paper x = (1 - v) * pw ; paper y = (1 - u) * ph
            return cls(0.0, -pw * sy, -ph * sx, 0.0, pw, ph, "nominal")
        # 270: paper x = v * pw ; paper y = u * ph
        return cls(0.0, pw * sy, ph * sx, 0.0, 0.0, 0.0, "nominal")

    def refined(self, page_rect_px: tuple, paper_rect_in: tuple) -> "PageToPaper":
        """A copy whose scale and offset make the border rectangle found in
        the image land on the border's paper rectangle. Axis orientation is
        kept from this transform; only per-axis scale and translation move."""
        px0, py0, px1, py1 = page_rect_px
        corners_px = [(px0, py0), (px1, py0), (px1, py1), (px0, py1)]
        got = [self.to_paper(*p) for p in corners_px]
        gx = sorted(p[0] for p in got)
        gy = sorted(p[1] for p in got)
        want_x0, want_y0, want_x1, want_y1 = paper_rect_in
        gx0, gx1, gy0, gy1 = gx[0], gx[-1], gy[0], gy[-1]
        if gx1 - gx0 < 1e-9 or gy1 - gy0 < 1e-9:
            return self
        kx = (want_x1 - want_x0) / (gx1 - gx0)
        ky = (want_y1 - want_y0) / (gy1 - gy0)
        ox = want_x0 - kx * gx0
        oy = want_y0 - ky * gy0
        out = PageToPaper(self.a * kx, self.b * kx, self.c * ky, self.d * ky,
                          self.tx * kx + ox, self.ty * ky + oy, "border_fit")
        fitted = [out.to_paper(*p) for p in corners_px]
        fx = sorted(p[0] for p in fitted)
        fy = sorted(p[1] for p in fitted)
        errs = [fx[0] - want_x0, fx[-1] - want_x1, fy[0] - want_y0, fy[-1] - want_y1]
        out.rmse_in = math.sqrt(sum(e * e for e in errs) / 4.0)
        return out


@dataclass
class PaperToModel:
    paper_center: tuple
    paper_size: tuple
    scale: float                 # paper units per model unit (CustomScale)
    twist_deg: float
    model_center: tuple
    handle: str = ""

    @classmethod
    def from_viewport(cls, vp: dict) -> "PaperToModel":
        return cls(tuple(float(v) for v in vp["paper_center"]),
                   tuple(float(v) for v in vp["paper_size"]),
                   float(vp["scale"]), float(vp.get("twist_deg") or 0.0),
                   tuple(float(v) for v in vp["model_center"][:2]),
                   str(vp.get("handle") or ""))

    def contains(self, x, y, slack_in: float = 0.0) -> bool:
        return (abs(x - self.paper_center[0]) <= self.paper_size[0] / 2.0 + slack_in
                and abs(y - self.paper_center[1]) <= self.paper_size[1] / 2.0 + slack_in)

    def to_model(self, x, y) -> tuple:
        dx = (x - self.paper_center[0]) / self.scale
        dy = (y - self.paper_center[1]) / self.scale
        a = -math.radians(self.twist_deg)
        cs, sn = math.cos(a), math.sin(a)
        return (self.model_center[0] + dx * cs - dy * sn,
                self.model_center[1] + dx * sn + dy * cs)

    def to_paper(self, X, Y) -> tuple:
        a = -math.radians(self.twist_deg)
        cs, sn = math.cos(a), math.sin(a)
        ux, uy = X - self.model_center[0], Y - self.model_center[1]
        dx = ux * cs + uy * sn
        dy = -ux * sn + uy * cs
        return (self.paper_center[0] + dx * self.scale, self.paper_center[1] + dy * self.scale)

    def model_ring(self) -> list:
        hw, hh = self.paper_size[0] / 2.0, self.paper_size[1] / 2.0
        cx, cy = self.paper_center
        return [list(self.to_model(cx + ox, cy + oy))
                for ox, oy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]

    def feet_per_inch(self) -> float:
        return 1.0 / self.scale


def viewport_at(viewports: list, x, y, slack_in: float = 0.0):
    """The PaperToModel whose paper rectangle holds (x, y); the smallest when
    several overlap (a detail window over a plan). None when none does."""
    hits = [v for v in viewports if v.contains(x, y, slack_in)]
    if not hits:
        return None
    return min(hits, key=lambda v: v.paper_size[0] * v.paper_size[1])
