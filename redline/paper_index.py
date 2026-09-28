"""What sits on the paper of one layout: text, blocks with attributes, tables.

The snapshot's layouts[] already carries these (CivilCoverage.Layouts); the
spatial index does not hold paper space, so a comment that lands outside
every viewport ("revise note 4") is answered from here. Positions are paper
inches. `decimated` says the plugin thinned the text list (more than 400
entries), so a miss is not proof of absence.
"""
import math
import re


def _xy(p):
    try:
        return float(p[0]), float(p[1])
    except (TypeError, ValueError, IndexError):
        return None


def _clean_text(s: str) -> str:
    # MText carries formatting codes: \pxqc; {\fArial|b1;...} \P newlines
    s = re.sub(r"\\[A-Za-z][^;]*;", "", str(s or ""))
    s = s.replace("\\P", " ").replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", s).strip()


class PaperIndex:
    def __init__(self, layout: dict):
        self.layout = layout or {}
        self.name = self.layout.get("name")
        self.texts = [{"handle": t.get("handle"), "text": _clean_text(t.get("text")), "layer": t.get("layer"),
                       "position": _xy(t.get("position"))}
                      for t in (self.layout.get("texts") or []) if _xy(t.get("position"))]
        self.blocks = [{"handle": b.get("handle"), "name": b.get("name"), "layer": b.get("layer"),
                        "position": _xy(b.get("position")), "attributes": dict(b.get("attributes") or {})}
                       for b in (self.layout.get("blocks") or []) if _xy(b.get("position"))]
        self.tables = [{"handle": t.get("handle"), "title": t.get("title"), "position": _xy(t.get("position")),
                        "rows": t.get("rows"), "columns": t.get("columns")}
                       for t in (self.layout.get("tables") or []) if _xy(t.get("position"))]
        listed = len(self.texts) + len(self.blocks) + len(self.tables) + len(self.layout.get("viewports") or [])
        self.decimated = int(self.layout.get("entities") or 0) > max(listed, 400)

    # ── queries ────────────────────────────────────────────────────────────

    def texts_near(self, xy, radius_in: float = 3.0, limit: int = 8) -> list:
        return self._near(self.texts, xy, radius_in, limit)

    def blocks_near(self, xy, radius_in: float = 3.0, limit: int = 6) -> list:
        return self._near(self.blocks, xy, radius_in, limit)

    def tables_near(self, xy, radius_in: float = 6.0, limit: int = 3) -> list:
        return self._near(self.tables, xy, radius_in, limit)

    def find_text(self, pattern: str, flags=re.I) -> list:
        rx = re.compile(pattern, flags)
        return [t for t in self.texts if rx.search(t["text"] or "")]

    def title_block(self) -> dict | None:
        """The title block reference: a block whose attributes carry a sheet
        number ('#') or whose name says title block."""
        for b in self.blocks:
            attrs = b.get("attributes") or {}
            if "#" in attrs or any(k.upper().startswith("SHEET") for k in attrs):
                return b
        for b in self.blocks:
            if re.search(r"tblk|title", str(b.get("name") or ""), re.I):
                return b
        return None

    def all_text(self) -> str:
        parts = [t["text"] for t in self.texts if t["text"]]
        for b in self.blocks:
            parts.extend(str(v) for v in (b.get("attributes") or {}).values() if v)
        return "\n".join(parts)

    @staticmethod
    def _near(items, xy, radius_in, limit):
        x, y = float(xy[0]), float(xy[1])
        out = []
        for it in items:
            p = it.get("position")
            if not p:
                continue
            d = math.hypot(p[0] - x, p[1] - y)
            if d <= radius_in:
                out.append({**it, "distance_in": round(d, 2)})
        out.sort(key=lambda r: r["distance_in"])
        return out[:limit]
