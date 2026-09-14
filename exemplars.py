"""Exemplar bundles: a finished drawing written out as what an LLM can learn from.

An exemplar is one finished plan of one type (water, sanitary, street ...). The
export splits it into the BASE a designer starts from -- decided per plan type by
people, in playbooks/<type>/base.json -- and the DELTA the designer added, then
writes both as snapshots, as spatial-index cells in words, as a cell-by-cell diff,
and as relations (how far each delta tag sits from what was already there, where
along the alignments, how often). Bundles hold client geometry, so they live
outside the repo under CAD_EXEMPLAR_DIR.

The brief is the digest of every bundle of one type, bounded in size and free of
absolute coordinates, that Claude Desktop reads before writing the playbook.
"""

import fnmatch
import hashlib
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict

import playbooks
import sheets
import spatial
import spatial_cache
from spatial import _signed_offset

MAX_CELL_ROWS = 60_000
MAX_DIFF_ROWS = 8_000
SAMPLE_HANDLES = 300
PROBES_PER_HANDLE = 4
NEAREST_MAX = 500.0
BRIEF_EXEMPLARS = 12

META_CATEGORIES = ("layers", "point_groups", "profile_views", "section_views", "standard_entities")
DROP_CATEGORIES = ("adjacency",)
NESTED = {
    "pipe_networks": ("pipes", "structures"),
    "pressure_networks": ("pipes", "fittings", "appurtenances"),
    "layouts": ("viewports",),
}
# category (or "category.leaf") -> (name field, entity type) the vocabulary sees,
# the same choices spatial._snapshot_pieces makes.
_TAG_ARGS = {
    "blocks": ("name", "block"), "texts": (None, "text"), "points": (None, "point"),
    "dimensions": (None, "dimension"), "labels": ("kind", "label"), "circles": (None, "circle"),
    "feature_lines": ("name", "feature_line"), "alignments": ("name", "alignment"),
    "parcels": ("name", "parcel"), "surfaces": ("name", "surface"), "corridor_lines": ("code", "corridor_line"),
    "cogo_points": ("description", "cogo_point"), "sample_lines": ("name", "sample_line"),
    "catchments": ("name", "catchment"), "gradings": ("name", "grading"),
    "intersections": ("name", "intersection"), "survey_figures": ("name", "survey_figure"),
    "view_frames": ("name", "view_frame"),
    "pipe_networks.pipes": (None, "pipe"), "pipe_networks.structures": (None, "structure"),
    "pressure_networks.pipes": ("part_size", "pressure_pipe"),
    "pressure_networks.fittings": ("part_type", "fitting"),
    "pressure_networks.appurtenances": ("part_type", "appurtenance"),
    "layouts.viewports": (None, "viewport"),
}


class ExemplarError(RuntimeError):
    pass


# ── Where bundles live ────────────────────────────────────────────────────────


def exemplar_root() -> str:
    root = os.environ.get("CAD_EXEMPLAR_DIR", "").strip()
    if not root:
        raise ExemplarError("CAD_EXEMPLAR_DIR is not set: point it at a folder outside the repo "
                            "(bundles hold client drawing data)")
    os.makedirs(root, exist_ok=True)
    return root


def safe_name(text: str) -> str:
    stem = os.path.splitext(os.path.basename(str(text or "")))[0] or "exemplar"
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)[:80]


def bundle_dir(plan_type: str, name: str, root=None) -> str:
    return os.path.join(root or exemplar_root(), safe_name(plan_type), safe_name(name))


# ── The split ─────────────────────────────────────────────────────────────────


def xref_names(data: dict) -> set:
    names = set()
    for lyr in data.get("layers") or []:
        n = str(lyr.get("name") or "")
        if "|" in n:
            names.add(n.split("|", 1)[0].lower())
    for cat in ("lines", "arcs", "polylines"):
        for e in data.get(cat) or []:
            if e.get("xref"):
                names.add(str(e["xref"]).lower())
    return names


def is_xref_sourced(category: str, entity: dict, xrefs: set) -> tuple:
    """(is it from an xref, which one). Nested geometry is stamped by
    fetch_snapshot; an xref-dependent layer reads 'xref|layer'; and the xref's
    own block reference is a block whose name is an xref name."""
    layer = str(entity.get("layer") or "")
    if entity.get("source") == "nested" or "|" in layer:
        return True, (str(entity.get("xref") or layer.split("|", 1)[0])).lower()
    if category == "blocks":
        name = str(entity.get("name") or "").lower()
        if name in xrefs:
            return True, name
    return False, ""


def _tag_of(index, key: str, entity: dict) -> str:
    name_field, etype = _TAG_ARGS.get(key, (None, None))
    if entity.get("source") == "nested":          # nested handles can collide with native ones
        return spatial.tag_for(entity.get("layer"))
    handle = str(entity.get("handle") or "")
    if index is not None and handle in index.entity_tag:
        return index.entity_tag[handle]
    return spatial.tag_for(entity.get("layer"), name=entity.get(name_field) if name_field else None, type=etype)


def _side(spec: dict, key: str, category: str, entity: dict, index, xrefs: set) -> tuple:
    keep, delta = spec["keep"], spec["delta"]
    if category in keep["categories"]:
        return "base", "category"
    from_xref, xref = is_xref_sourced(category, entity, xrefs)
    tag = _tag_of(index, key, entity)
    # tags in the spec may be families: "water" keeps water_main and water_hydrant together
    if from_xref and any(fnmatch.fnmatch(xref, str(p).lower()) for p in keep["xrefs"]):
        if spatial.TAGS.matches(tag, delta["tags"]) and entity.get("source") != "nested" and category != "blocks":
            return "delta", "delta_tag"
        return "base", "xref"
    if spatial.TAGS.matches(tag, delta["tags"]):
        return "delta", "delta_tag"
    if spatial.TAGS.matches(tag, keep["tags"]):
        return "base", "keep_tag"
    etype = _TAG_ARGS.get(key, (None, None))[1]
    if etype and etype in keep["kinds"]:
        return "base", "kind"
    return "delta", "default"


def apply_base_spec(data: dict, spec: dict, index=None) -> tuple:
    """(base, delta, report): the snapshot split entity by entity. Nested
    containers (networks, layouts) are split at their leaves and kept on a
    side only when they still hold something there."""
    xrefs = xref_names(data)
    base, delta = {}, {}
    reasons = Counter()
    counts = {"base": Counter(), "delta": Counter()}

    def put(side, cat, entity):
        side_dict = base if side == "base" else delta
        side_dict.setdefault(cat, []).append(entity)
        counts[side][cat] += 1

    for category, value in data.items():
        if category in DROP_CATEGORIES:
            continue
        if category in META_CATEGORIES or not isinstance(value, list):
            base[category] = value
            delta[category] = value
            continue
        if category in NESTED:
            for container in value:
                if not isinstance(container, dict):
                    continue
                sides = {"base": dict(container), "delta": dict(container)}
                has = {"base": False, "delta": False}
                for leaf in NESTED[category]:
                    for s in sides.values():
                        s[leaf] = []
                    for entity in container.get(leaf) or []:
                        if not isinstance(entity, dict):
                            continue
                        side, why = _side(spec, f"{category}.{leaf}", category, entity, index, xrefs)
                        reasons[why] += 1
                        sides[side][leaf].append(entity)
                        counts[side][f"{category}.{leaf}"] += 1
                        has[side] = True
                for side in ("base", "delta"):
                    if has[side] or category in spec["keep"]["categories"] and side == "base":
                        (base if side == "base" else delta).setdefault(category, []).append(sides[side])
            continue
        for entity in value:
            if not isinstance(entity, dict):
                continue
            side, why = _side(spec, category, category, entity, index, xrefs)
            reasons[why] += 1
            put(side, category, entity)

    for cat in data:
        if cat not in DROP_CATEGORIES:
            base.setdefault(cat, [] if isinstance(data[cat], list) else data[cat])
            delta.setdefault(cat, [] if isinstance(data[cat], list) else data[cat])
    report = {"base": dict(counts["base"]), "delta": dict(counts["delta"]),
              "reasons": dict(reasons), "xrefs": sorted(xrefs)}
    return base, delta, report


def leaf_handles(data: dict) -> set:
    out = set()
    for category, value in data.items():
        if category in META_CATEGORIES or not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            if category in NESTED:
                for leaf in NESTED[category]:
                    for e in item.get(leaf) or []:
                        if isinstance(e, dict) and e.get("handle"):
                            out.add(str(e["handle"]))
            elif item.get("handle"):
                out.add(str(item["handle"]))
    return out


# ── Index views written to disk ───────────────────────────────────────────────


def cell_records(index, level: int = 0) -> list:
    out = []
    for key in sorted(k for k in index.cells if k[0] == level):
        c = index.cells[key]
        if not c.tags() and not c.covered:
            continue
        angle, aniso = c.orientation()
        out.append({
            "key": list(key), "bounds": list(spatial.bounds(key)),
            "region": index._region_of_key(key) if level == 0 else index._region_of_key(key),
            "frame": c.frame, "tags": c.tags(),
            "length": {t: round(v, 1) for t, v in c.length.items() if v > 0},
            "points": {t: n for t, n in c.points.items() if n},
            "covered": {t: round(v, 3) for t, v in c.covered.items() if v > 0},
            "handles": {t: len(hs) for t, hs in c.handles.items() if hs},
            "orientation": [None if angle is None else round(angle, 1), round(aniso, 3)],
            "sentence": index.describe(key),
        })
    return out


def write_index_dir(index, path: str) -> dict:
    os.makedirs(path, exist_ok=True)
    level = 0 if len([k for k in index.cells if k[0] == 0]) <= MAX_CELL_ROWS else 1
    sizes = {}
    with open(os.path.join(path, "site_map.txt"), "w", encoding="utf-8") as f:
        f.write(index.site_map() + "\n")
    with open(os.path.join(path, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(index.summary() + "\n")
    n = 0
    with open(os.path.join(path, "cells.jsonl"), "w", encoding="utf-8") as f:
        for rec in cell_records(index, level):
            f.write(json.dumps(rec) + "\n")
            n += 1
    for name in ("site_map.txt", "summary.txt", "cells.jsonl"):
        sizes[name] = os.path.getsize(os.path.join(path, name))
    return {"level": level, "cells": n, "sizes": sizes}


def diff_records(index_base, index_fin, limit: int = MAX_DIFF_ROWS) -> dict:
    d = index_base.diff(index_fin)
    rows, by_tag = [], Counter()
    keys = [(k, "added") for k in d["added"]] + [(k, "changed") for k in d["changed"]] + \
           [(k, "removed") for k in d["removed"]]
    for key, kind in keys:
        cb, cf = index_base.cells.get(key), index_fin.cells.get(key)
        tags_b = set(cb.tags()) if cb else set()
        tags_f = set(cf.tags()) if cf else set()
        if not tags_b and not tags_f:
            continue
        for t in tags_f - tags_b:
            by_tag[t] += 1
        rows.append({
            "cell": list(key), "kind": kind,
            "before": index_base.describe(key) if cb else "empty",
            "after": index_fin.describe(key) if cf else "empty",
            "tags_added": sorted(tags_f - tags_b), "tags_removed": sorted(tags_b - tags_f),
            "length_delta": {t: round((cf.length.get(t, 0) if cf else 0) - (cb.length.get(t, 0) if cb else 0), 1)
                             for t in tags_b | tags_f
                             if abs((cf.length.get(t, 0) if cf else 0) - (cb.length.get(t, 0) if cb else 0)) > 0.05},
            "points_delta": {t: (cf.points.get(t, 0) if cf else 0) - (cb.points.get(t, 0) if cb else 0)
                             for t in tags_b | tags_f
                             if (cf.points.get(t, 0) if cf else 0) != (cb.points.get(t, 0) if cb else 0)},
        })
    return {"added": len(d["added"]), "changed": len(d["changed"]), "removed": len(d["removed"]),
            "by_tag_added": dict(by_tag.most_common()), "rows": rows[:limit],
            "truncated": max(0, len(rows) - limit)}


# ── Relations: the numbers a rule of thumb is made of ────────────────────────


def _probes(pieces):
    pts = []
    for p in pieces:
        if p.kind == "pt":
            pts.append(p.geom)
        else:
            a, b = p.endpoints()
            pts.extend([a, b])
        if len(pts) >= PROBES_PER_HANDLE:
            break
    return pts[:PROBES_PER_HANDLE]


def _stats(values: list) -> dict:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"n": 0}
    q = lambda f: vals[min(len(vals) - 1, int(round(f * (len(vals) - 1))))]
    return {"n": len(vals), "p10": round(q(0.1), 2), "median": round(statistics.median(vals), 2),
            "p90": round(q(0.9), 2)}


def relations(index, delta_handles: set, sample: int = SAMPLE_HANDLES) -> dict:
    by_tag = defaultdict(list)
    for h in delta_handles:
        t = index.entity_tag.get(str(h))
        if t:
            by_tag[t].append(str(h))
    delta_tags = set(by_tag)
    base_tags = sorted(t for t, hs in index.tag_handles.items() if hs - delta_handles)
    out = {}
    for tag, handles in by_tag.items():
        handles = sorted(handles)
        chosen = handles[:: max(1, len(handles) // sample)][:sample]
        pieces = {h: index.pieces_for(h) for h in chosen}
        length = sum(p.length() for hs in handles for p in index.pieces_for(hs)) if len(handles) <= 5000 else \
            sum(p.length() for h in chosen for p in pieces[h]) * len(handles) / max(1, len(chosen))
        points = sum(1 for h in handles for p in index.pieces_for(h) if p.kind == "pt") if len(handles) <= 5000 else 0
        names = Counter((p.name or "").strip().lower() for h in chosen for p in pieces[h] if p.name)
        regions = Counter()
        for h in chosen:
            rid = index.region_of(handle=h)
            regions[(index.regions.get(rid) or {}).get("kind") or "none"] += 1
        frames = defaultdict(lambda: {"station": [], "offset": [], "side": Counter()})
        probes = {h: _probes(pieces[h]) for h in chosen}
        for h in chosen:
            for p in probes[h]:
                best = None
                for a in index.alignments:
                    d, sta, side = _signed_offset(a["points"], p)
                    if best is None or d < best[1]:
                        best = (a["name"], d, sta, side)
                if best:
                    fr = frames[best[0]]
                    fr["station"].append(best[2]); fr["offset"].append(best[1])
                    fr["side"]["left" if best[3] > 0 else "right"] += 1
        frames_out = {}
        for name, fr in frames.items():
            n = sum(fr["side"].values()) or 1
            frames_out[name] = {"station": [round(min(fr["station"]), 1), round(max(fr["station"]), 1)],
                                "offset": _stats(fr["offset"]),
                                "side": {k: round(v / n, 2) for k, v in fr["side"].items()}}
        nearest_base, nearest_delta = {}, {}
        for other in sorted(index.tag_handles):
            if other == tag or other in ("other",):
                continue
            dists, misses = [], 0
            for h in chosen:
                best = None
                for p in probes[h]:
                    hit = index.nearest(other, p, max_distance=NEAREST_MAX)
                    if hit and hit[0] != h and (best is None or hit[2] < best):
                        best = hit[2]
                if best is None:
                    misses += 1
                else:
                    dists.append(best)
            if not dists:
                continue
            rec = {**_stats(dists), "n_none": misses}
            (nearest_delta if other in delta_tags else nearest_base)[other] = rec
        spacing = {}
        pt_handles = [h for h in chosen if any(p.kind == "pt" for p in pieces[h])]
        if len(pt_handles) >= 3 and index.alignments:
            stations = defaultdict(list)
            for h in pt_handles:
                p = probes[h][0]
                best = min(((a["name"],) + _signed_offset(a["points"], p) for a in index.alignments), key=lambda x: x[1])
                stations[best[0]].append(best[2])
            gaps = []
            for name, sts in stations.items():
                sts.sort()
                gaps.extend(b - a for a, b in zip(sts, sts[1:]))
            if gaps:
                spacing = _stats(gaps)
        out[tag] = {"handles": len(handles), "sampled": len(chosen), "length_ft": round(length, 1),
                    "points": points, "names": dict(names.most_common(8)),
                    "regions": {k: round(v / max(1, len(chosen)), 2) for k, v in regions.items()},
                    "frames": frames_out, "nearest_base": nearest_base, "nearest_delta": nearest_delta,
                    "spacing": spacing}
    return {"tags": out, "base_tags": base_tags, "delta_tags": sorted(delta_tags),
            "delta_families": sorted({spatial.TAGS.family_of(t) for t in delta_tags}),
            "families": {t: spatial.TAGS.family_of(t) for t in delta_tags}}


def unmatched_report(index, delta_data: dict) -> dict:
    """Delta layers whose names are unverified: nothing recognisable in the
    name (tagged by type or 'other'), or an auto name with no known word."""
    delta_layers = Counter()
    for h in leaf_handles(delta_data):
        t = index.entity_tag.get(h)
        pieces = index.pieces_for(h)
        layer = pieces[0].layer if pieces else None
        info = index.layer_info.get(layer) if layer else None
        wordless = t in ("other", "block", "text", "point", "dimension")
        unverified = bool(info and info["source"] == "derived" and info["confidence"] == "none")
        if layer and (wordless or unverified):
            delta_layers[layer] += 1
    rows = []
    for l, n in delta_layers.most_common(40):
        info = index.layer_info.get(l) or {}
        rows.append({"layer": l, "count": n, "tag": info.get("tag"), "confidence": info.get("confidence"),
                     "source": info.get("source")})
    return {"linework": [{"layer": l, "feet": round(ft, 1)} for l, ft in index.unmatched.most_common(40)],
            "points": [{"layer": l, "count": n} for l, n in index.unmatched_points.most_common(40)],
            "delta_layers": rows, "auto_named": index.derivation_stats()}


# ── Export ────────────────────────────────────────────────────────────────────


def _dump(path: str, obj) -> int:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return os.path.getsize(path)


def export_bundle(data: dict, plan_type: str, name: str, drawing: str, revision, notes: str = "",
                  spec: dict = None, root: str = None, overwrite: bool = False) -> dict:
    spec = spec or playbooks.load_base_spec(plan_type)
    path = bundle_dir(plan_type, name, root)
    if os.path.exists(path) and not overwrite:
        raise ExemplarError(f"bundle already exists: {path} (pass overwrite=True to replace it)")
    os.makedirs(path, exist_ok=True)

    data = {k: v for k, v in data.items() if k not in DROP_CATEGORIES}
    index_fin = spatial.build(data)
    base, delta, report = apply_base_spec(data, spec, index_fin)
    index_base = spatial.build(base)
    delta_h = leaf_handles(delta)

    sizes = {}
    sizes["snapshot.json"] = _dump(os.path.join(path, "snapshot.json"), data)
    sizes["base.json"] = _dump(os.path.join(path, "base.json"), base)
    sizes["delta.json"] = _dump(os.path.join(path, "delta.json"), delta)
    fin_info = write_index_dir(index_fin, os.path.join(path, "index_finished"))
    base_info = write_index_dir(index_base, os.path.join(path, "index_base"))
    diff = diff_records(index_base, index_fin)
    sizes["diff.json"] = _dump(os.path.join(path, "diff.json"), diff)
    rel = relations(index_fin, delta_h)
    sizes["relations.json"] = _dump(os.path.join(path, "relations.json"), rel)
    unmatched = unmatched_report(index_fin, delta)
    sizes["unmatched.json"] = _dump(os.path.join(path, "unmatched.json"), unmatched)
    sheet_info = sheets.sheet_stats(data, delta_h, index_fin)
    sizes["sheets.json"] = _dump(os.path.join(path, "sheets.json"), sheet_info)

    manifest = {
        "schema": 1, "plan_type": plan_type, "name": safe_name(name), "drawing": drawing,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "revision": revision,
        "vocabulary_version": spatial.TAGS.version,
        "spatial": {"base_size": spatial.BASE_SIZE, "origin": list(spatial.ORIGIN), "levels": spatial.LEVELS},
        "base_spec": spec, "base_spec_sha1": hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12],
        "notes": notes, "counts": {"finished": {k: len(v) for k, v in data.items() if isinstance(v, list)},
                                   "base": report["base"], "delta": report["delta"]},
        "reasons": report["reasons"], "xrefs": report["xrefs"],
        "alignments": [a["name"] for a in index_fin.alignments],
        "cells": {"finished": fin_info["cells"], "base": base_info["cells"],
                  "diff": diff["added"] + diff["changed"] + diff["removed"]},
        "index_level": fin_info["level"], "delta_handles": len(delta_h),
        "delta_tags": rel["delta_tags"], "delta_families": rel["delta_families"],
        "words_version": spatial.TAGS.words.version,
        "sizes": {**sizes, **{f"index_finished/{k}": v for k, v in fin_info["sizes"].items()},
                                                   **{f"index_base/{k}": v for k, v in base_info["sizes"].items()}},
        "dropped": list(DROP_CATEGORIES),
        "unmatched_delta_layers": unmatched["delta_layers"][:10],
        "sheets": {"layouts": sheet_info["layouts"], "viewports": sheet_info["viewports"],
                   "scales": sheet_info["scales"]},
        "path": path,
    }
    _dump(os.path.join(path, "manifest.json"), manifest)
    return manifest


def export_drawing(file: str, plan_type: str, name: str = "", notes: str = "", overwrite: bool = False,
                   base_url: str = None) -> dict:
    """Read the drawing through the plugin and export it. Uses fetch_snapshot
    directly (with the plan type's extra xref layers) so the dashboards' cached
    indexes, keyed on the standard merge list, are left alone."""
    spec = playbooks.load_base_spec(plan_type)
    layers = list(spatial.merge_layers())
    for s in spec["nested_layers"]:
        if s not in layers:
            layers.append(s)
    try:
        data, rev = spatial_cache.fetch_snapshot(file, layers, base_url)
    except spatial_cache.PluginError as e:
        raise ExemplarError(str(e))
    if rev is None:
        rev = spatial_cache.revision(file, base_url)
    name = name or f"{safe_name(file)}-{time.strftime('%Y%m%d-%H%M')}"
    return export_bundle(data, plan_type, name, os.path.abspath(file), rev, notes, spec, overwrite=overwrite)


def export_snapshot_file(path: str, plan_type: str, name: str = "", notes: str = "", overwrite: bool = False) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    name = name or f"{safe_name(path)}-{time.strftime('%Y%m%d-%H%M')}"
    return export_bundle(data, plan_type, name, os.path.abspath(path), None, notes, overwrite=overwrite)


# ── Reading bundles ───────────────────────────────────────────────────────────


def list_bundles(plan_type: str = "", root: str = None) -> list:
    root = root or exemplar_root()
    out = []
    types = [safe_name(plan_type)] if plan_type else sorted(
        d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    for t in types:
        tdir = os.path.join(root, t)
        if not os.path.isdir(tdir):
            continue
        for name in sorted(os.listdir(tdir)):
            mpath = os.path.join(tdir, name, "manifest.json")
            try:
                with open(mpath, "r", encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                continue
            out.append({k: m.get(k) for k in ("plan_type", "name", "drawing", "exported_at", "revision",
                                                "vocabulary_version", "words_version", "alignments", "cells",
                                                "delta_tags", "delta_families", "delta_handles", "notes", "sheets")})
    out.sort(key=lambda m: m.get("exported_at") or "")
    return out


PARTS = {
    "manifest": "manifest.json", "summary": "index_finished/summary.txt", "site_map": "index_finished/site_map.txt",
    "base_summary": "index_base/summary.txt", "base_site_map": "index_base/site_map.txt",
    "relations": "relations.json", "unmatched": "unmatched.json", "diff": "diff.json",
    "cells": "index_finished/cells.jsonl", "base_cells": "index_base/cells.jsonl",
    "sheets": "sheets.json",
}


def read_part(plan_type: str, name: str, part: str = "summary", max_chars: int = 30_000, root: str = None) -> str:
    if part not in PARTS:
        raise ExemplarError(f"unknown part {part!r}; one of {', '.join(PARTS)}")
    path = os.path.join(bundle_dir(plan_type, name, root), PARTS[part])
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read(max_chars + 1)
    except OSError:
        raise ExemplarError(f"no such bundle part: {path}")
    if len(text) > max_chars:
        cut = text.rfind("\n", 0, max_chars)
        text = text[:cut if cut > 0 else max_chars] + f"\n... (truncated at {max_chars} chars)"
    return text


# ── The brief ─────────────────────────────────────────────────────────────────


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _delta_lines(rel: dict) -> list:
    lines = []
    for tag, r in sorted(rel["tags"].items(), key=lambda kv: -kv[1]["length_ft"] - kv[1]["points"] * 10):
        parts = [f"{r['handles']} pieces"]
        if r["length_ft"]:
            parts.append(f"{r['length_ft']:,.0f} ft")
        if r["points"]:
            parts.append(f"{r['points']} points")
        if r["names"]:
            parts.append("names: " + ", ".join(f"{n} x{c}" for n, c in list(r["names"].items())[:5]))
        reg = ", ".join(f"{k} {v:.0%}" for k, v in sorted(r["regions"].items(), key=lambda kv: -kv[1])[:2])
        if reg:
            parts.append(reg)
        for name, fr in list(r["frames"].items())[:3]:
            off = fr["offset"]
            side = max(fr["side"].items(), key=lambda kv: kv[1])[0] if fr["side"] else "?"
            sta = fr["station"]
            parts.append(f"{name} sta {sta[0] / 100:.0f}+{sta[0] % 100:02.0f} to {sta[1] / 100:.0f}+{sta[1] % 100:02.0f}, "
                         f"{off.get('p10', 0):.0f}-{off.get('p90', 0):.0f} ft off (median {off.get('median', 0):.0f}), "
                         f"{side} side {fr['side'].get(side, 0):.0%}")
        near = sorted(r["nearest_base"].items(), key=lambda kv: kv[1]["median"])[:4]
        if near:
            parts.append("nearest base: " + ", ".join(f"{t} median {s['median']:.0f} ft (p10 {s['p10']:.0f}, p90 {s['p90']:.0f})"
                                                     for t, s in near))
        near_d = sorted(r["nearest_delta"].items(), key=lambda kv: kv[1]["median"])[:3]
        if near_d:
            parts.append("nearest delta: " + ", ".join(f"{t} median {s['median']:.0f} ft" for t, s in near_d))
        if r.get("spacing", {}).get("n"):
            s = r["spacing"]
            parts.append(f"spacing along alignment {s['p10']:.0f}-{s['p90']:.0f} ft (median {s['median']:.0f})")
        lines.append(f"- {tag}: " + "; ".join(parts))
    return lines


_COORD_RE = re.compile(r"\b(x|y)\s*=\s*-?[\d,]{4,}")


def _strip_coords(line: str) -> str:
    return _COORD_RE.sub(lambda m: f"{m.group(1)}=…", line)


def _fit(lines: list, budget: int) -> list:
    out, used = [], 0
    for ln in lines:
        if used + len(ln) + 1 > budget:
            out.append("  … (more omitted for size)")
            break
        out.append(ln)
        used += len(ln) + 1
    return out


def build_brief(plan_type: str, max_chars: int = 60_000, palette=None, root: str = None) -> str:
    spec = playbooks.load_base_spec(plan_type)
    bundles = list_bundles(plan_type, root)[-BRIEF_EXEMPLARS:]
    if not bundles:
        raise ExemplarError(f"no exemplar bundles for plan type {plan_type!r}; export some first")
    contract = playbooks.playbook_output_contract()
    reserve_contract, reserve_cross, reserve_palette = min(len(contract) + 50, 8000), 6000, 4000
    per_ex = max(1500, (max_chars - reserve_contract - reserve_cross - reserve_palette) // len(bundles))

    head = [f"# Playbook brief: {plan_type}  ({len(bundles)} exemplar(s), vocabulary v{spatial.TAGS.version}, "
            f"words v{spatial.TAGS.words.version})",
            "Tags read family_part: water_main, water_hydrant, sanitary_pipe. A family word (water) names all its parts.",
            "## Base for this plan type",
            spec["description"],
            "kept as base: categories " + ", ".join(spec["keep"]["categories"]) + "; xrefs " +
            ", ".join(spec["keep"]["xrefs"]) + "; tags " + ", ".join(spec["keep"]["tags"]),
            "always the designer's work (delta): " + (", ".join(spec["delta"]["tags"]) or "(none listed)")]

    sections, all_rel = [], []
    for m in bundles:
        bdir = bundle_dir(plan_type, m["name"], root)
        try:
            rel = _load(os.path.join(bdir, "relations.json"))
            diff = _load(os.path.join(bdir, "diff.json"))
            unmatched = _load(os.path.join(bdir, "unmatched.json"))
        except (OSError, ValueError) as e:
            sections.append([f"## Exemplar: {m['name']}", f"(unreadable: {e})"])
            continue
        all_rel.append(rel)
        cells = m.get("cells") or {}
        lines = [f"## Exemplar: {m['name']}  (drawing {os.path.basename(str(m.get('drawing') or ''))}, "
                 f"{len(m.get('alignments') or [])} alignment(s), {cells.get('base', 0)} base cells, "
                 f"{cells.get('diff', 0)} cells changed)"]
        if m.get("vocabulary_version") != spatial.TAGS.version:
            lines.append(f"  (exported with vocabulary v{m.get('vocabulary_version')}; re-export to use the current words)")
        if m.get("notes"):
            lines.append(f"  notes: {m['notes']}")
        lines.append("Delta by tag:")
        lines.extend(_delta_lines(rel))
        rows = [r for r in diff.get("rows", []) if r.get("tags_added")][:8]
        if rows:
            lines.append(f"Where it changed (before -> after), {len(rows)} of {diff.get('changed', 0) + diff.get('added', 0)} cells:")
            for r in rows:
                lines.append(_strip_coords(f"- {r['before']} -> {r['after']}"))
        missing = unmatched.get("delta_layers") or []
        if missing:
            lines.append("Unverified auto-names (edit_learned / rename_word): " +
                         ", ".join(f"{u['layer']} -> {u.get('tag') or '?'} x{u['count']}" for u in missing[:8]))
        sections.append(_fit(lines, per_ex))

    cross = ["## Across exemplars"]
    if all_rel:
        presence = Counter(t for rel in all_rel for t in rel.get("delta_families") or rel["delta_tags"])
        always = [t for t, n in presence.items() if n == len(all_rel)]
        usually = [f"{t} ({n}/{len(all_rel)})" for t, n in presence.items() if 0 < n < len(all_rel)]
        cross.append("Always present: " + (", ".join(sorted(always)) or "(none)"))
        if usually:
            cross.append("Sometimes: " + ", ".join(sorted(usually)))
        typical = defaultdict(list)
        for rel in all_rel:
            for tag, r in rel["tags"].items():
                for other, s in r["nearest_base"].items():
                    typical[(tag, other)].append(s["median"])
                for name, fr in r["frames"].items():
                    if fr["offset"].get("median") is not None:
                        typical[(tag, "offset from alignment")].append(fr["offset"]["median"])
                if r.get("spacing", {}).get("median") is not None:
                    typical[(tag, "spacing")].append(r["spacing"]["median"])
        for (tag, other), meds in sorted(typical.items()):
            if len(meds) >= max(1, len(all_rel) // 2):
                cross.append(f"- {tag} to {other}: median of medians {statistics.median(meds):.0f} ft "
                             f"(range {min(meds):.0f}-{max(meds):.0f}) across {len(meds)} exemplar(s)")
    cross = _fit(cross, reserve_cross)

    # How the finished drawings were cut into sheets, for the playbook's Sheets
    # section and the sheets.json proposal: paper units and ratios only.
    stats = []
    for m in bundles:
        try:
            stats.append(_load(os.path.join(bundle_dir(plan_type, m["name"], root), "sheets.json")))
        except (OSError, ValueError):
            pass
    sheet_lines = ["## Sheets in the exemplars"]
    with_sheets = [st for st in stats if st.get("layouts")]
    if with_sheets:
        scales = Counter()
        furniture = Counter()
        frozen = Counter()
        overlaps = [st["overlap"] for st in with_sheets if st.get("overlap") is not None]
        for st in with_sheets:
            for k, n in (st.get("scales") or {}).items():
                scales[k] += n
            for k, n in (st.get("furniture") or {}).items():
                furniture[k] += n
            for l in st.get("frozen_common") or []:
                frozen[l] += 1
        n_layouts = sum(st["layouts"] for st in with_sheets)
        sheet_lines.append(f"{n_layouts} layout(s) across {len(with_sheets)} exemplar(s); scales: " +
                           ", ".join(f"{k} x{n}" for k, n in scales.most_common(6)))
        rects = Counter()
        for st in with_sheets:
            for r in st.get("viewport_rects") or []:
                rects[(tuple(r["center"]), tuple(r["size"]))] += r["count"]
        if rects:
            (c, sz), n = rects.most_common(1)[0]
            sheet_lines.append(f"viewport on the paper: centre {list(c)}, size {list(sz)} (in {n} viewport(s))")
        if overlaps:
            overlaps.sort()
            sheet_lines.append(f"overlap between windows: median {overlaps[len(overlaps) // 2]:.0%}")
        margins = [st["margin"] for st in with_sheets if st.get("margin")]
        if margins:
            sheet_lines.append("margin around the work (ft): " + "; ".join(
                ", ".join(f"{k} {v:g}" for k, v in m.items()) for m in margins[:3]))
        everywhere = [k for k, n in furniture.most_common() if n >= 0.9 * n_layouts]
        sometimes = [f"{k} ({n}/{n_layouts})" for k, n in furniture.most_common(12) if k not in everywhere]
        sheet_lines.append("furniture on every sheet: " + (", ".join(everywhere) or "(none)"))
        if sometimes:
            sheet_lines.append("furniture on some sheets: " + ", ".join(sometimes))
        if frozen:
            sheet_lines.append("layers usually frozen in the viewports: " +
                               ", ".join(l for l, _ in frozen.most_common(10)))
        tables = Counter()
        for st in with_sheets:
            for k, n in (st.get("tables") or {}).items():
                tables[k] += n
        if tables:
            sheet_lines.append("tables: " + ", ".join(f"{k} x{n}" for k, n in tables.most_common(6)))
        sheet_lines.append("Propose a sheets.json for this type (get_sheet_spec shows the template and a "
                           "suggestion from these numbers) and save it with save_sheet_spec.")
    else:
        sheet_lines.append("(no layouts in these exemplars; the sheet spec must be written from the standard)")
    cross = cross + [""] + _fit(sheet_lines, 3000)

    pal = ["## Blocks that produce these tags"]
    if palette:
        words = set()
        for rel in all_rel:
            words.update(rel["delta_tags"])
        wanted = {"draw_line", "draw_polyline", "draw_arc", "insert_block", "draw_text", "draw_mtext",
                  "label_grading", "space_labels", "draw_driveways", "draw_rear_swales", "offset_entities"}
        for b in palette:
            t = str(b.get("type") or "")
            desc = str(b.get("description") or "")
            if t in wanted or any(w.replace("_", " ") in (t + " " + desc).lower() for w in words):
                fields = ", ".join(f["name"] for f in (b.get("fields") or [])[:8])
                pal.append(f"- {t}({fields}): {desc[:90]}")
    else:
        pal.append("(dashboard server not reachable; call list_blocks for the palette)")
    pal = _fit(pal, reserve_palette)

    text = "\n".join(head + [""] + ["\n".join(s) for s in sections] + [""] + cross + [""] + pal + ["", contract])
    if len(text) > max_chars:
        cut = text.rfind("\n", 0, max_chars - 60)
        text = text[:cut] + "\n… (brief truncated; raise max_chars or read_exemplar for detail)"
    return text


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python exemplars.py export <dwg> <type> [--name N] [--notes T] [--overwrite]\n"
              "       python exemplars.py export-json <snapshot.json> <type> [--name N] [--notes T] [--overwrite]\n"
              "       python exemplars.py brief <type> [--max N]\n"
              "       python exemplars.py list [type]")
        return 0

    def opt(flag, default=None):
        if flag in argv:
            i = argv.index(flag)
            return argv[i + 1] if i + 1 < len(argv) else default
        return default

    try:
        cmd = argv[0]
        if cmd in ("export", "export-json"):
            fn = export_drawing if cmd == "export" else export_snapshot_file
            m = fn(argv[1], argv[2], name=opt("--name", "") or "", notes=opt("--notes", "") or "",
                   overwrite="--overwrite" in argv)
            print(json.dumps({k: m[k] for k in ("plan_type", "name", "path", "counts", "delta_tags",
                                                 "unmatched_delta_layers")}, indent=2))
        elif cmd == "brief":
            print(build_brief(argv[1], int(opt("--max", "60000"))))
        elif cmd == "list":
            print(json.dumps(list_bundles(argv[1] if len(argv) > 1 else ""), indent=2))
        else:
            print(f"unknown command {cmd!r}")
            return 2
    except (ExemplarError, playbooks.PlaybookError, IndexError, OSError) as e:
        print(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
