"""Where a comment lands, and what is there.

A comment's anchor (the thing it points at when the OCR found one, else the
ink itself) goes page -> paper; the viewport under it goes paper -> model.
comment_context() then writes the digest the planner reads: the index's
sentence for the cell, the alignment frame, the nearest entity of each
useful tag with its handle, everything within a radius grouped by tag, the
closed shapes the point is inside, nearby labels and text. Handles are what
the modify blocks take, so the digest is what makes a comment actionable.

Entities on layers frozen in that viewport were not on the sheet the
reviewer saw, so they are left out.
"""
import math

from .paper_index import PaperIndex
from .transform import viewport_at
from .types import Located

PICK_TOLERANCE_FT = 5.0     # floor: how far from the arrow tip an object may be and still be "the one it points at"
PICK_TOLERANCE_IN = 0.15    # ... and on paper, scaled by the viewport (see pick_tolerance)
NEAREST_TAGS = ("hydrant", "appurtenance", "fitting", "water", "sanitary", "storm", "structure", "pipe", "curb",
                "lot_line", "pad", "driveway", "swale", "label", "text", "block", "alignment", "easement", "right_of_way")
FAMILY_PLAN_TYPE = {"water": "water", "sanitary": "sanitary", "storm": "storm", "grading": "grading",
                    "contour": "grading", "swale": "grading", "pad": "grading", "driveway": "grading"}


def layer_map(data: dict) -> dict:
    """handle -> layer for everything in the snapshot that carries both."""
    out = {}
    for key, items in (data or {}).items():
        if not isinstance(items, list):
            continue
        for it in items:
            if isinstance(it, dict) and it.get("handle") and it.get("layer"):
                out[str(it["handle"])] = str(it["layer"])
            if isinstance(it, dict):
                for sub in ("pipes", "structures", "fittings", "appurtenances"):
                    for s in it.get(sub) or []:
                        if isinstance(s, dict) and s.get("handle"):
                            out[str(s["handle"])] = str(s.get("layer") or "")
    return out


def _frozen(layer: str, frozen: set) -> bool:
    if not layer or not frozen:
        return False
    lay = layer.lower()
    return lay in frozen or lay.split("|")[-1] in frozen


def _points_near(items, key, xy, radius, limit, layers, frozen):
    out = []
    for it in items or []:
        p = it.get(key)
        if not p:
            continue
        try:
            d = math.hypot(float(p[0]) - xy[0], float(p[1]) - xy[1])
        except (TypeError, ValueError, IndexError):
            continue
        if d > radius or _frozen(layers.get(str(it.get("handle")), it.get("layer", "")), frozen):
            continue
        rec = {"handle": it.get("handle"), "distance": round(d, 1)}
        for k in ("text", "kind", "type", "target", "name", "layer"):
            if it.get(k):
                rec[k] = it[k] if k != "text" else str(it[k])[:80]
        out.append(rec)
    out.sort(key=lambda r: r["distance"])
    return out[:limit]


def pick_tolerance(feet_per_inch: float | None) -> float:
    """How far from the arrow tip the picked object may be: a hand-placed
    tip is good to about 0.15 in on paper, so the tolerance grows with the
    plot scale (6 ft at 1"=40', 15 ft at 1"=100'), never below the floor."""
    if not feet_per_inch:
        return PICK_TOLERANCE_FT
    return round(max(PICK_TOLERANCE_FT, PICK_TOLERANCE_IN * float(feet_per_inch)), 2)


def comment_context(index, data: dict, model_xy, radius: float = 100.0, frozen_layers=None,
                    paper_index: PaperIndex | None = None, paper_xy=None, layers: dict | None = None,
                    pick_tolerance_ft: float | None = None) -> dict:
    x, y = float(model_xy[0]), float(model_xy[1])
    layers = layers if layers is not None else layer_map(data)
    frozen = {str(f).lower() for f in (frozen_layers or [])}
    tol = pick_tolerance_ft or PICK_TOLERANCE_FT
    ctx = {"model_xy": [round(x, 2), round(y, 2)], "radius_ft": radius, "pick_tolerance_ft": tol}
    try:
        loc = index.locate(x, y)
        ctx["cell"] = index.describe(loc["key"])
        ctx["frame"] = loc.get("frame")
        ctx["region"] = loc.get("region")
        ctx["neighbours"] = {k: v for k, v in (loc.get("neighbours") or {}).items() if v}
    except Exception as e:      # an index without cells here is not an error
        ctx["cell"] = f"(no cell: {e})"
    nearest = []
    for tag in NEAREST_TAGS:
        try:
            hit = index.nearest(tag, (x, y), max_distance=radius * 2)
        except Exception:
            hit = None
        if not hit:
            continue
        handle, pt, dist = hit
        if _frozen(layers.get(str(handle), ""), frozen):
            continue
        nearest.append({"tag": index.entity_tag.get(handle, tag), "handle": handle, "distance": round(dist, 1),
                        "at": [round(pt[0], 1), round(pt[1], 1)], "layer": layers.get(str(handle))})
    ctx["nearest"] = nearest
    grouped = {}
    try:
        for h in index.entities(point=(x, y), radius=radius):
            if _frozen(layers.get(str(h), ""), frozen):
                continue
            tag = index.entity_tag.get(h, "other")
            if tag == "viewport":
                continue
            grouped.setdefault(tag, []).append(h)
    except Exception:
        pass
    ctx["within"] = {t: {"count": len(hs), "handles": sorted(hs)[:8]} for t, hs in sorted(grouped.items())}
    try:
        ctx["inside"] = [{"handle": h, "tag": t} for h, t in index.inside((x, y))]
    except Exception:
        ctx["inside"] = []
    ctx["labels"] = _points_near(data.get("labels"), "anchor", (x, y), radius, 6, layers, frozen)
    ctx["texts"] = _points_near(data.get("texts"), "position", (x, y), radius, 6, layers, frozen)
    # the objects themselves, with geometry: what the parameter filler works from
    try:
        import spatial_summary
        lookup = spatial_summary.by_handle(data)
        ctx["pick"] = spatial_summary.object_at(data, index, (x, y), tolerance=tol, frozen=frozen, lookup=lookup)
        ctx["objects"] = spatial_summary.nearby(data, index, (x, y), radius=radius, limit=8, frozen=frozen, lookup=lookup)["objects"]
    except Exception as e:      # the digest must never fail the comment
        ctx["pick"] = {"object": None, "labels": [], "candidates": [], "error": str(e)}
        ctx["objects"] = []
    if paper_index is not None and paper_xy is not None:
        ctx["paper_texts"] = paper_index.texts_near(paper_xy, radius_in=3.0)
        ctx["paper_decimated"] = paper_index.decimated
    ctx["plan_type_guess"] = guess_plan_type(ctx)
    return ctx


def paper_context(paper_index: PaperIndex, paper_xy, layout_name: str) -> dict:
    return {"paper_xy": [round(paper_xy[0], 2), round(paper_xy[1], 2)], "layout": layout_name,
            "paper_texts": paper_index.texts_near(paper_xy, radius_in=4.0, limit=10),
            "paper_blocks": paper_index.blocks_near(paper_xy, radius_in=4.0),
            "paper_tables": paper_index.tables_near(paper_xy),
            "paper_decimated": paper_index.decimated, "plan_type_guess": None}


def guess_plan_type(ctx: dict) -> str | None:
    votes = {}
    for tag, rec in (ctx.get("within") or {}).items():
        fam = tag.split("_")[0]
        pt = FAMILY_PLAN_TYPE.get(fam) or FAMILY_PLAN_TYPE.get(tag)
        if pt:
            votes[pt] = votes.get(pt, 0) + rec.get("count", 0)
    for n in ctx.get("nearest") or []:
        fam = str(n.get("tag", "")).split("_")[0]
        pt = FAMILY_PLAN_TYPE.get(fam)
        if pt and n.get("distance", 1e9) < 30:
            votes[pt] = votes.get(pt, 0) + 3
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: kv[1])[0]


def locate_point(comment, anchor_px, page_to_paper, viewports, index, data, paper_index,
                 frozen_by_handle: dict | None = None, radius: float = 100.0, layers: dict | None = None,
                 layout_name: str = "", target_index: int = 0, target_count: int = 1) -> Located:
    ax, ay = anchor_px
    px, py = page_to_paper.to_paper(ax, ay)
    vp = viewport_at(viewports, px, py)
    out = Located(comment_id=comment.id, space="none", paper_xy=[round(px, 3), round(py, 3)],
                  target_index=target_index, target_count=target_count, target_px=[round(ax, 1), round(ay, 1)])
    if vp is None:
        out.space = "paper"
        out.context = paper_context(paper_index, (px, py), layout_name) if paper_index else {}
        return out
    X, Y = vp.to_model(px, py)
    out.space = "model"
    out.viewport_handle = vp.handle
    out.model_xy = [round(X, 3), round(Y, 3)]
    frozen = (frozen_by_handle or {}).get(vp.handle) or []
    out.context = comment_context(index, data, (X, Y), radius=radius, frozen_layers=frozen,
                                  paper_index=paper_index, paper_xy=(px, py), layers=layers,
                                  pick_tolerance_ft=pick_tolerance(vp.feet_per_inch()))
    out.context["viewport"] = {"handle": vp.handle, "scale_text": f"1\"={1.0 / vp.scale:.0f}'",
                               "twist_deg": vp.twist_deg}
    return out


def locate(comment, page_to_paper, viewports, index, data, paper_index, frozen_by_handle: dict | None = None,
           radius: float = 100.0, layers: dict | None = None, layout_name: str = "") -> list:
    """One Located per spot the comment is about: one for an ordinary
    comment, one per arrow tip for a multi-target comment, none for a
    sheet-wide note (which is stored as a directive instead)."""
    anchors = comment.anchors_px()
    return [locate_point(comment, a, page_to_paper, viewports, index, data, paper_index, frozen_by_handle=frozen_by_handle,
                         radius=radius, layers=layers, layout_name=layout_name, target_index=i, target_count=len(anchors))
            for i, a in enumerate(anchors)]
