"""Acceptance checks: small declarative rules evaluated against a spatial index.

A playbook's checks.json is a list of these. Each names a kind, a tag (the
subjects) and a few numbers, and reads in the index's own words: "every
hydrant within 2 ft of a water main", "no water crossing sanitary", "mains 8
to 20 ft off the alignment". A dashboard run evaluates them after it draws,
scoped to what it drew; calibrate_checks runs them over the exemplars a
playbook was written from, so a check the exemplars themselves fail is caught
before it ever blocks a run.

Nothing here draws or decides; results name offenders by handle.
"""

import math
import fnmatch
from collections import Counter, defaultdict

import layer_words
import spatial
from spatial import _signed_offset, point_at_station

# ── Schema ────────────────────────────────────────────────────────────────────

KINDS = {
    "count":        {"required": [],             "optional": {"min": 1, "max": None, "region": None},
                     "doc": "min <= number of subjects <= max; region = corridor | block limits where they count"},
    "within":       {"required": ["of"],         "optional": {"distance": 5.0},
                     "doc": "every subject has a piece of tag `of` within `distance` ft"},
    "no_crossing":  {"required": ["other"],      "optional": {},
                     "doc": "no subject linework crosses tag `other` (touching at an end is not a crossing)"},
    "inside":       {"required": ["ring"],       "optional": {"min_fraction": 1.0},
                     "doc": "share of subjects lying inside a closed shape of tag `ring` >= min_fraction"},
    "exists_along": {"required": [],             "optional": {"alignment": "*", "half_width": 40.0, "step": 50.0,
                                                              "min_fraction": 0.8},
                     "doc": "walking the alignment(s) every `step` ft, the share of stations with a subject "
                            "within `half_width` >= min_fraction"},
    "label_per":    {"required": [],             "optional": {"label": "label", "distance": 10.0, "by": "auto"},
                     "doc": "every subject has a `label`: by=target -> a label whose labelled object IS the "
                            "subject (Civil 3D FeatureId, any distance); by=distance -> any `label` within "
                            "`distance` ft; by=auto (default) -> target when the drawing reports label targets, "
                            "else distance"},
    "spacing":      {"required": ["max"],        "optional": {"along": "*", "min": 0.0},
                     "doc": "subjects projected to station along the alignment(s); consecutive gaps within [min, max]"},
    "offset":       {"required": ["min", "max"], "optional": {"alignment": "*", "min_fraction": 0.9},
                     "doc": "share of subject points whose offset from the alignment is within [min, max] "
                            ">= min_fraction"},
    "covered":      {"required": [],             "optional": {"min_fraction": 1.0, "layout": "*"},
                     "doc": "share of subjects with every probe point inside an ON viewport window "
                            "(of layouts matching `layout`) >= min_fraction: nothing falls off the sheets"},
    "sheet_has":    {"required": ["name"],       "optional": {"layout": "*", "what": "block", "min": 1},
                     "no_subjects": True,
                     "doc": "every layout matching `layout` holds >= min `what` (block | table | text) whose "
                            "name / title / text matches `name` (fnmatch, case-insensitive); needs the snapshot"},
}

COMMON_OPTIONAL = {"name": None, "layer": None, "scope": "delta", "severity": "error", "why": "",
                   "state": None, "other_state": None}
SCOPES = ("delta", "all")
SEVERITIES = ("error", "warn")
NUMERIC = {"min", "max", "distance", "min_fraction", "half_width", "step"}
MAX_OFFENDERS = 20
PROBES_PER_HANDLE = 8


def describe_kinds() -> str:
    lines = ["  tag / of / other / ring / label may be a full tag (water_hydrant) or a family (water = every",
             "  water_* tag). state filters the subjects and other_state the things they are measured against:",
             "  proposed | existing | future | demolition | abandoned | temporary | civil3d."]
    for kind, spec in KINDS.items():
        req = ", ".join(spec["required"]) or "-"
        opt = ", ".join(f"{k}={v}" for k, v in spec["optional"].items()) or "-"
        lines.append(f"  {kind:13s} requires: {req:12s} optional: {opt}\n{'':17s}{spec['doc']}")
    return "\n".join(lines)


def validate(checks) -> list:
    """Errors that make a check list unusable; [] when it is fine."""
    errors = []
    if not isinstance(checks, list):
        return ["checks must be a list"]
    seen = set()
    for i, c in enumerate(checks):
        where = f"check {i + 1}"
        if not isinstance(c, dict):
            errors.append(f"{where}: not an object")
            continue
        cid = str(c.get("id") or "").strip()
        if not cid:
            errors.append(f"{where}: missing id")
        elif cid in seen:
            errors.append(f"{where}: duplicate id {cid!r}")
        seen.add(cid)
        where = f"check {cid or i + 1}"
        kind = str(c.get("kind") or "")
        if kind not in KINDS:
            errors.append(f"{where}: unknown kind {kind!r}; known: {', '.join(KINDS)}")
            continue
        if not str(c.get("tag") or "").strip() and not KINDS[kind].get("no_subjects"):
            errors.append(f"{where}: missing tag")
        for field in KINDS[kind]["required"]:
            if c.get(field) in (None, ""):
                errors.append(f"{where}: {kind} needs {field}")
        for field in NUMERIC:
            if field in c and c[field] is not None:
                try:
                    float(c[field])
                except (TypeError, ValueError):
                    errors.append(f"{where}: {field} must be a number")
        if c.get("scope") not in (None, *SCOPES):
            errors.append(f"{where}: scope must be one of {SCOPES}")
        for field in ("state", "other_state"):
            if c.get(field) not in (None, "", *layer_words.STATES):
                errors.append(f"{where}: {field} must be one of {layer_words.STATES}")
        if c.get("severity") not in (None, *SEVERITIES):
            errors.append(f"{where}: severity must be one of {SEVERITIES}")
        if "min" in c and "max" in c and c.get("min") is not None and c.get("max") is not None:
            try:
                if float(c["min"]) > float(c["max"]):
                    errors.append(f"{where}: min is greater than max")
            except (TypeError, ValueError):
                pass
    return errors


def warnings(checks) -> list:
    """Soft problems: tags no vocabulary rule produces (they can still appear
    as entity types, so this is a warning, not an error)."""
    known = set(spatial.TAGS.known_words())
    out = []
    for c in checks if isinstance(checks, list) else []:
        if not isinstance(c, dict):
            continue
        for field in ("tag", "of", "other", "ring", "label"):
            v = c.get(field)
            if KINDS.get(c.get("kind"), {}).get("no_subjects") and field == "tag":
                continue
            if isinstance(v, str) and v and v.lower() not in known:
                out.append(f"check {c.get('id')}: {field} {v!r} is not a tag the vocabulary produces")
    return out


def _norm(c: dict) -> dict:
    out = dict(COMMON_OPTIONAL)
    out.update(KINDS[c["kind"]]["optional"])
    out.update({k: v for k, v in c.items() if v is not None})
    out["tag"] = str(out.get("tag") or "").lower()
    for field in NUMERIC:
        if out.get(field) is not None:
            out[field] = float(out[field])
    return out


# ── Subjects and probes ───────────────────────────────────────────────────────


def _subjects(index, c: dict, scope) -> dict:
    """handle -> pieces for the check's subjects: the tag's entities, limited to
    `scope` (a set of handles) when the check is scoped to the delta, then by
    name / layer substrings."""
    handles = set(index.handles_for(c["tag"], state=c.get("state") or None))
    if c.get("scope", "delta") == "delta" and scope is not None:
        handles &= set(str(h) for h in scope)
    name = str(c.get("name") or "").lower()
    layer = str(c.get("layer") or "").lower()
    out = {}
    for h in handles:
        pieces = index.pieces_for(h)
        if not pieces and h in index.rings:
            pieces = []
        if name and not any(name in (p.name or "").lower() for p in pieces):
            continue
        if layer and not any(layer in (p.layer or "").lower() for p in pieces):
            continue
        out[h] = pieces
    return out


def _probes(index, handle, pieces) -> list:
    pts = []
    for p in pieces:
        if p.kind == "pt":
            pts.append(p.geom)
        else:
            a, b = p.endpoints()
            chords = p.chords()
            mid = chords[len(chords) // 2] if chords else (a, b)
            pts.extend([a, b, ((mid[0][0] + mid[1][0]) / 2, (mid[0][1] + mid[1][1]) / 2)])
        if len(pts) >= PROBES_PER_HANDLE:
            break
    if not pts and handle in index.rings:
        ring = index.rings[handle][0]
        pts = [(sum(q[0] for q in ring) / len(ring), sum(q[1] for q in ring) / len(ring))]
    return pts[:PROBES_PER_HANDLE]


def _representative(index, handle, pieces):
    pts = _probes(index, handle, pieces)
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _alignments(index, name) -> list:
    name = str(name or "*")
    if name == "*":
        return list(index.alignments)
    return [a for a in index.alignments if a["name"].lower() == name.lower()]


def _nearest_alignment(index, p, candidates):
    best = None
    for a in candidates:
        d, sta, side = _signed_offset(a["points"], p)
        if best is None or d < best[1]:
            best = (a, d, sta, side)
    return best


# ── Evaluators ────────────────────────────────────────────────────────────────


def _result(c, ok, expected, actual, detail=None, offenders=None, subjects=0):
    return {"id": c["id"], "kind": c["kind"], "tag": c["tag"], "severity": c["severity"], "ok": bool(ok),
            "scope": c["scope"], "state": c.get("state"), "other_state": c.get("other_state"),
            "subjects": subjects, "expected": expected, "actual": actual,
            "detail": detail or {}, "offenders": list(offenders or [])[:MAX_OFFENDERS], "why": c.get("why", "")}


def _eval_count(index, c, subs):
    handles = list(subs)
    if c.get("region"):
        want = str(c["region"]).lower()
        handles = [h for h in handles
                   if (index.regions.get(index.region_of(handle=h) or "", {}).get("kind") or "") == want]
    n = len(handles)
    lo = c["min"] if c.get("min") is not None else 0
    hi = c["max"]
    ok = n >= lo and (hi is None or n <= hi)
    expected = f"at least {lo:g}" if hi is None else f"between {lo:g} and {hi:g}"
    return _result(c, ok, f"{expected} {c['tag']}" + (f" in a {c['region']}" if c.get("region") else ""),
                   str(n), {"count": n}, [] if ok else handles, n)


def _eval_within(index, c, subs, of=None, distance=None):
    of = (of or c["of"]).lower()
    distance = float(distance if distance is not None else c["distance"])
    offenders, worst = [], {}
    for h, pieces in subs.items():
        best = None
        for p in _probes(index, h, pieces):
            hit = index.nearest(of, p, max_distance=max(distance * 4, 50.0), state=c.get("other_state") or None)
            if hit and hit[0] != h and (best is None or hit[2] < best):
                best = hit[2]
        if best is None or best > distance:
            offenders.append(h)
            worst[h] = None if best is None else round(best, 2)
    ok = not offenders
    return _result(c, ok, f"every {c['tag']} within {distance:g} ft of {of}",
                   f"{len(subs) - len(offenders)} of {len(subs)}", {"farthest": dict(list(worst.items())[:MAX_OFFENDERS])},
                   offenders, len(subs))


def _eval_label_per(index, c, subs):
    by = str(c.get("by") or "auto").lower()
    targets = getattr(index, "label_targets", {}) or {}
    if by == "auto":
        by = "target" if targets else "distance"
    if by != "target":
        r = _eval_within(index, c, subs, of=c.get("label") or "label", distance=c.get("distance"))
        r["detail"]["by"] = "distance"
        return r
    wanted = index.tags_of(str(c.get("label") or "label"))
    offenders = []
    for h in subs:
        mine = [l for l in index.labels_of(h) if index.entity_tag.get(l) in wanted]
        if not mine:
            offenders.append(h)
    ok = not offenders
    return _result(c, ok, f"every {c['tag']} labelled (a {c.get('label') or 'label'} whose target is the subject)",
                   f"{len(subs) - len(offenders)} of {len(subs)}", {"by": "target", "targets_known": len(targets)},
                   offenders, len(subs))


def _eval_no_crossing(index, c, subs):
    other = str(c["other"]).lower()
    offenders, where, crossings = [], {}, []
    for h, pieces in subs.items():
        hits = []
        for piece in pieces:
            if piece.kind == "pt":
                continue
            hits.extend(x for x in index._crossings_of(piece, other, state=c.get("other_state") or None) if x[0] != h)
        if hits:
            offenders.append(h)
            where[h] = [round(hits[0][2][0], 2), round(hits[0][2][1], 2)]
            # every crossing point, so a caller can ask "which of these are on this sheet"
            crossings.extend({"handle": h, "hits": x[0], "at": [round(x[2][0], 2), round(x[2][1], 2)]}
                             for x in hits[:MAX_OFFENDERS])
    ok = not offenders
    return _result(c, ok, f"no {c['tag']} crossing {other}", f"{len(offenders)} crossing",
                   {"at": dict(list(where.items())[:MAX_OFFENDERS]), "crossings": crossings[:MAX_OFFENDERS * 4]},
                   offenders, len(subs))


def _eval_inside(index, c, subs):
    ring = str(c["ring"]).lower()
    inside_n, offenders = 0, []
    for h, pieces in subs.items():
        pts = _probes(index, h, pieces)
        if pts and all(any(r[0] != h for r in index.inside(p, [ring], state=c.get("other_state") or None)) for p in pts):
            inside_n += 1
        else:
            offenders.append(h)
    frac = inside_n / len(subs) if subs else 1.0
    ok = frac >= float(c["min_fraction"])
    return _result(c, ok, f"{float(c['min_fraction']):.0%} of {c['tag']} inside {ring}",
                   f"{frac:.0%}", {"inside": inside_n, "total": len(subs)}, [] if ok else offenders, len(subs))


def _eval_exists_along(index, c, subs):
    aligns = _alignments(index, c.get("alignment"))
    if not aligns:
        return _result(c, False, f"{c['tag']} along {c.get('alignment') or 'every alignment'}",
                       "no such alignment", {}, [], len(subs))
    step, half = float(c["half_width"]) and float(c["step"]), float(c["half_width"])
    step = float(c["step"])
    want = set(subs)
    covered = total = 0
    gaps = []
    for a in aligns:
        s = 0.0
        while s <= a["length"] + 1e-6:
            p = point_at_station(a["points"], s)
            total += 1
            near = set(index.entities(c["tag"], point=p, radius=half)) & want
            if near:
                covered += 1
            else:
                gaps.append({"alignment": a["name"], "station": round(s, 1)})
            s += step
    frac = covered / total if total else 0.0
    ok = frac >= float(c["min_fraction"])
    return _result(c, ok, f"{c['tag']} within {half:g} ft of the alignment at {float(c['min_fraction']):.0%} of stations",
                   f"{frac:.0%} ({covered} of {total} stations)", {"gaps": gaps[:MAX_OFFENDERS]}, [], len(subs))


def _eval_spacing(index, c, subs):
    aligns = _alignments(index, c.get("along"))
    if not aligns:
        return _result(c, False, f"{c['tag']} spacing along {c.get('along') or 'every alignment'}",
                       "no such alignment", {}, [], len(subs))
    lo, hi = float(c.get("min") or 0.0), float(c["max"])
    by_align = defaultdict(list)
    for h, pieces in subs.items():
        rp = _representative(index, h, pieces)
        if rp is None:
            continue
        best = _nearest_alignment(index, rp, aligns)
        if best:
            by_align[best[0]["name"]].append((best[2], h))
    offenders, gaps = [], []
    for name, items in by_align.items():
        items.sort()
        for (s0, h0), (s1, h1) in zip(items, items[1:]):
            gap = s1 - s0
            if gap > hi or gap < lo:
                offenders.extend([h0, h1])
                gaps.append({"alignment": name, "from_station": round(s0, 1), "to_station": round(s1, 1),
                             "gap": round(gap, 1)})
    ok = not gaps
    return _result(c, ok, f"{c['tag']} every {lo:g}-{hi:g} ft along the alignment",
                   f"{len(gaps)} gap(s) out of range", {"gaps": gaps[:MAX_OFFENDERS]}, offenders, len(subs))


def _eval_offset(index, c, subs):
    aligns = _alignments(index, c.get("alignment"))
    if not aligns:
        return _result(c, False, f"{c['tag']} offset from {c.get('alignment') or 'every alignment'}",
                       "no such alignment", {}, [], len(subs))
    lo, hi = float(c["min"]), float(c["max"])
    good = total = 0
    offenders, sample = [], {}
    for h, pieces in subs.items():
        bad = 0
        for p in _probes(index, h, pieces):
            best = _nearest_alignment(index, p, aligns)
            total += 1
            if best and lo <= best[1] <= hi:
                good += 1
            else:
                bad += 1
                sample.setdefault(h, round(best[1], 2) if best else None)
        if bad:
            offenders.append(h)
    frac = good / total if total else 1.0
    ok = frac >= float(c["min_fraction"])
    return _result(c, ok, f"{float(c['min_fraction']):.0%} of {c['tag']} points {lo:g}-{hi:g} ft off the alignment",
                   f"{frac:.0%}", {"out_of_range_offsets": dict(list(sample.items())[:MAX_OFFENDERS])},
                   [] if ok else offenders, len(subs))


def _windows(index, layout_pattern) -> list:
    pat = str(layout_pattern or "*").lower()
    return [w["ring"] for w in getattr(index, "windows", {}).values()
            if w.get("on", True) and fnmatch.fnmatchcase(str(w.get("layout") or "").lower(), pat)]


def _eval_covered(index, c, subs):
    rings = _windows(index, c.get("layout"))
    if not rings:
        return _result(c, False, f"{c['tag']} inside a viewport of layouts {c.get('layout') or '*'}",
                       "no viewport windows in the drawing", {}, [], len(subs))
    offenders, on, total = [], 0, 0
    for h, pieces in subs.items():
        pts = _probes(index, h, pieces)
        if not pts:
            continue
        total += 1
        if all(any(spatial._point_in_polygon(p, ring) for ring in rings) for p in pts):
            on += 1
        else:
            offenders.append(h)
    frac = on / total if total else 1.0
    ok = frac >= float(c["min_fraction"])
    return _result(c, ok, f"{float(c['min_fraction']):.0%} of {c['tag']} on a sheet",
                   f"{frac:.0%} ({on} of {total})", {"windows": len(rings)}, [] if ok else offenders, total)


def _eval_sheet_has(index, c, subs, data=None):
    layouts = (data or {}).get("layouts") or []
    pat = str(c.get("layout") or "*").lower()
    name = str(c.get("name") or "*").lower()
    what = str(c.get("what") or "block").lower()
    need = int(float(c.get("min") or 1))
    matched = [l for l in layouts if fnmatch.fnmatchcase(str(l.get("name") or "").lower(), pat)]
    if not matched:
        return _result(c, False, f"layouts {c.get('layout') or '*'} with a {what} {c.get('name')}",
                       "no such layout in the snapshot" if layouts else "no layouts in the snapshot", {}, [], 0)
    missing = []
    for l in matched:
        if what == "block":
            items = [str(b.get("name") or "") for b in l.get("blocks") or []]
        elif what == "table":
            items = [str(t.get("title") or "") for t in l.get("tables") or []]
        else:
            items = [str(t.get("text") or "") for t in l.get("texts") or []]
        hits = sum(1 for it in items if fnmatch.fnmatchcase(it.lower(), name) or (what == "text" and name.strip("*") in it.lower()))
        if hits < need:
            missing.append(l.get("name"))
    ok = not missing
    return _result(c, ok, f"{need} {what}(s) named {c.get('name')} on every layout {c.get('layout') or '*'}",
                   f"{len(matched) - len(missing)} of {len(matched)} layouts", {"missing_on": missing[:MAX_OFFENDERS]},
                   [], len(matched))


_EVAL = {"count": _eval_count, "within": _eval_within, "no_crossing": _eval_no_crossing, "inside": _eval_inside,
         "exists_along": _eval_exists_along, "label_per": _eval_label_per, "spacing": _eval_spacing,
         "offset": _eval_offset, "covered": _eval_covered, "sheet_has": _eval_sheet_has}


def evaluate(index, checks: list, scope=None, data=None) -> list:
    """Every check against the index. `scope` is the set of handles a run
    created (or None for the whole drawing); checks with scope "all" ignore it.
    An empty subject set passes every kind but count and exists_along, with a
    note, so a check about hydrants does not fail a run that drew only mains.
    `data` is the snapshot, for kinds that read the paper (sheet_has)."""
    results = []
    for raw in checks or []:
        if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
            continue
        c = _norm(raw)
        try:
            if KINDS[c["kind"]].get("no_subjects"):
                results.append(_EVAL[c["kind"]](index, c, {}, data))
                continue
            subs = _subjects(index, c, scope)
            if not subs and c["kind"] not in ("count", "exists_along"):
                r = _result(c, True, f"{c['kind']} over {c['tag']}", "no subjects in scope",
                            {"note": "no subjects in scope"}, [], 0)
            else:
                r = _EVAL[c["kind"]](index, c, subs)
        except Exception as e:                       # a broken check must not break the run
            r = _result(c, False, c["kind"], f"check failed to run: {e}", {"error": str(e)}, [], 0)
        results.append(r)
    return results


def summarize(results: list) -> dict:
    failed = [r["id"] for r in results if not r["ok"] and r["severity"] == "error"]
    warned = [r["id"] for r in results if not r["ok"] and r["severity"] == "warn"]
    passed = [r["id"] for r in results if r["ok"]]
    if not results:
        message = "no checks"
    elif not failed and not warned:
        message = f"all {len(results)} check(s) passed"
    else:
        message = f"{len(passed)} passed, {len(failed)} failed, {len(warned)} warned"
        if failed:
            message += ": " + ", ".join(failed[:6])
    return {"ok": not failed, "passed": len(passed), "failed": len(failed), "warned": len(warned),
            "failed_ids": failed, "warned_ids": warned, "message": message}
