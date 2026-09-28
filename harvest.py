"""Harvest the layers, blocks and drafting conventions a drawing actually uses
into one global standards file that a new one-task script is written against.

    python harvest.py "C:\\Jobs\\1234\\Water Plan.dwg"           # via the running plugin
    python harvest.py snapshot.json                              # offline, from a saved snapshot
    python harvest.py a.dwg b.dwg c.json                         # several at once
    python harvest.py --list                                     # what has been harvested
    python harvest.py --forget "Water Plan.dwg"                  # drop one source and rebuild
    python harvest.py --rebuild                                  # regenerate global.json + global.md
    HARVEST  (inside Civil 3D)                                   # the open drawing, via the plugin's command

Every drawing becomes one observation file (standards/observations/<source>.json).
global.json and global.md are REBUILT from all observations on every run, so
harvesting the same drawing twice changes nothing, harvesting a changed drawing
replaces its old numbers, and nothing is ever double counted. An entry that is
already in global.json keeps what it has: its descriptive fields (colour,
linetype, block definition ...) are set the first time it is seen and never
overwritten by a later harvest; only counts, sources and value lists
accumulate. Fields a person adds to an entry (role, note, anything not in
HARVESTED) are carried over on rebuild by key.

What is recorded per layer (the drawing's own layers; xref-dependent layers
collapse to a name list per xref): tag/family/state from the vocabulary,
colour, linetype, lineweight, plottable, description, which entity types sit
on it and how many, text heights seen on it. Per block (xref and anonymous
blocks skipped): the layers it is placed on, scales, a rotation summary,
attribute tags and the values seen, and the definition (entity count,
attribute tags, description) when the plugin is reachable. Per Civil 3D part
(pipe network pipes / structures, pressure pipes / fittings / appurtenances):
the descriptions, sizes and name patterns in use per network. Conventions:
text height / style by layer, hatch pattern / scale by layer, dimension
styles, label types by layer, per-entity colour / linetype overrides by layer,
drawing units.

Needs nothing but the standard library. Reading a .dwg needs Civil 3D running
with the CADAgent plugin (GET /modelspace opens the drawing if it is not open);
a .json snapshot needs nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import cadagent_config
# the catalog is edited by people, so a packaged install keeps it in the data dir
STANDARDS_DIR = cadagent_config.folder("standards_dir", "standards", os.path.join(ROOT, "standards"), seed=True)
PLUGIN_BASE = cadagent_config.get("plugin_base")
TIMEOUT = float(os.environ.get("CAD_HARVEST_TIMEOUT", "300"))

# The fields a rebuild writes on a layer / block entry. Anything else on the
# entry in global.json belongs to a person and is preserved by key.
# FIRST_SEEN: harvested fields that are set once, when the entry first appears
# in global.json, and never overwritten by a later harvest (delete the entry
# and --rebuild to reset them). Everything else in HARVESTED is recomputed.
FIRST_SEEN = {
    "layers": ("color", "linetype", "lineweight", "plottable", "description"),
    "blocks": ("definition",),
    "parts": (),
}
HARVESTED = {
    "layers": {"name", "tag", "family", "state", "tag_source", "color", "linetype", "lineweight",
               "plottable", "description", "count", "entities", "text_heights", "sources"},
    "blocks": {"name", "count", "layers", "scales", "rotation", "rotations", "attribute_tags",
               "attribute_values", "definition", "space", "sources"},
    "parts": {"network", "kind", "description", "count", "sizes", "name_patterns", "layers", "sources"},
}
PART_KINDS = {"pipe": "pipe_network", "structure": "pipe_network", "pressure_pipe": "pressure_network",
              "fitting": "pressure_network", "appurtenance": "pressure_network"}
PERSON_FIELDS = ("role", "note")     # created empty so the file shows where to write

# categories whose items carry their own nested entity lists
NESTED = {"pipe_networks": ("pipes", "structures"),
          "pressure_networks": ("pipes", "fittings", "appurtenances"),
          "layouts": ("viewports", "texts", "blocks", "tables")}
SKIP_CATEGORIES = {"layers", "standard_entities", "adjacency", "file", "revision"}
SINGULAR = {"lines": "line", "arcs": "arc", "polylines": "polyline", "circles": "circle", "ellipses": "ellipse",
            "splines": "spline", "points": "point", "texts": "text", "dimensions": "dimension", "hatches": "hatch",
            "blocks": "block", "labels": "label", "alignments": "alignment", "surfaces": "surface",
            "corridors": "corridor", "feature_lines": "feature_line", "parcels": "parcel",
            "profile_views": "profile_view", "section_views": "section_view", "cogo_points": "cogo_point",
            "sample_lines": "sample_line", "catchments": "catchment", "gradings": "grading",
            "intersections": "intersection", "survey_figures": "survey_figure", "view_frames": "view_frame",
            "pipe_networks.pipes": "pipe", "pipe_networks.structures": "structure",
            "pressure_networks.pipes": "pressure_pipe", "pressure_networks.fittings": "fitting",
            "pressure_networks.appurtenances": "appurtenance", "layouts.viewports": "viewport",
            "layouts.texts": "sheet_text", "layouts.blocks": "sheet_block", "layouts.tables": "sheet_table"}
TOP_VALUES = 12          # how many distinct attribute values / heights an entry keeps
TOP_ROTATIONS = 6


class HarvestError(Exception):
    pass


# ── files ─────────────────────────────────────────────────────────────────────


def paths(root: str = None) -> dict:
    root = root or STANDARDS_DIR
    return {"root": root, "global": os.path.join(root, "global.json"), "brief": os.path.join(root, "global.md"),
            "observations": os.path.join(root, "observations")}


def _write_atomic(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if isinstance(obj, str):
                f.write(obj)
            else:
                json.dump(obj, f, indent=2, ensure_ascii=False)
                f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def safe_name(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._ -]+", "_", str(text)).strip()
    return text or "drawing"


# ── the plugin ────────────────────────────────────────────────────────────────


def _http(method: str, path: str, base: str, params: dict = None, body: dict = None):
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.URLError as e:
        raise HarvestError(f"CADAgent plugin not reachable at {base} ({e.reason}); start Civil 3D with the "
                           f"plugin loaded, or pass a saved snapshot .json instead") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise HarvestError(f"{path}: the plugin did not return JSON: {raw[:200]}") from None


def fetch_from_plugin(dwg: str, base: str = PLUGIN_BASE) -> tuple:
    """(snapshot, layer definitions, block definitions, settings) for a drawing,
    opened by the plugin if it is not already open."""
    dwg = os.path.abspath(dwg)
    snap = _http("GET", "/modelspace", base, params={"file": dwg})
    if isinstance(snap, dict) and snap.get("error"):
        raise HarvestError(f"/modelspace: {snap['error']}")
    layers, blocks, settings = [], [], {}
    for intent, key in (("list_layers", "layers"), ("list_block_definitions", "blocks"),
                        ("get_drawing_settings", None)):
        try:
            res = _http("POST", "/execute", base, body={"intent": intent, "file": dwg,
                                                        **({"count_entities": False} if intent == "list_layers" else {})})
        except HarvestError:
            res = {}
        if not isinstance(res, dict) or not res.get("ok", True) or res.get("error"):
            continue
        if key == "layers":
            layers = res.get("layers") or []
        elif key == "blocks":
            blocks = res.get("blocks") or []
        else:
            settings = {k: v for k, v in res.items() if k not in ("ok", "message", "file")}
    return snap, layers, blocks, settings


# ── one drawing -> one observation ────────────────────────────────────────────


def _iter_entities(data: dict):
    """(category, entity) for every entity dict in a snapshot, nested lists included."""
    for cat, items in data.items():
        if cat in SKIP_CATEGORIES or not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            yield cat, item
            for sub in NESTED.get(cat, ()):
                for leaf in item.get(sub) or []:
                    if isinstance(leaf, dict):
                        yield f"{cat}.{sub}", leaf


def _xref_names(data: dict) -> set:
    names = set()
    for lyr in data.get("layers") or []:
        n = str(lyr.get("name") or "")
        if "|" in n:
            names.add(n.split("|", 1)[0].lower())
    for cat in ("lines", "arcs", "polylines"):
        for e in data.get(cat) or []:
            if e.get("xref"):
                names.add(str(e["xref"]).lower())
    for b in data.get("blocks") or []:
        if b.get("is_xref"):
            names.add(str(b.get("name") or "").lower())
    return names


def _num(v, nd=3):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _scale_of(b: dict):
    s = b.get("scale")
    if isinstance(s, list):
        s = s[0] if s else None
    return _num(s, 3)


def _rotation_summary(counter: Counter) -> tuple:
    """('fixed:0' | 'cardinal' | 'varied', top rotations) from degree counts."""
    if not counter:
        return "", []
    top = [{"deg": d, "count": n} for d, n in counter.most_common(TOP_ROTATIONS)]
    if len(counter) == 1:
        return f"fixed:{next(iter(counter))}", top
    if all(abs(d % 90) < 0.5 or abs(d % 90 - 90) < 0.5 for d in counter):
        return "cardinal", top
    return "varied", top


def _top(counter: Counter, n=TOP_VALUES) -> dict:
    return {str(k): v for k, v in counter.most_common(n)}


def observe(data: dict, source: str, path: str = "", layer_defs: list = None,
            block_defs: list = None, settings: dict = None, classify=None) -> dict:
    """Everything worth keeping from one drawing. Pure: no files, no servers."""
    classify = classify or _classifier()
    xrefs = _xref_names(data)

    layer_meta = {}
    for l in (layer_defs or []):
        layer_meta[str(l.get("name") or "").lower()] = l
    for l in data.get("layers") or []:
        key = str(l.get("name") or "").lower()
        layer_meta.setdefault(key, {}).update({k: v for k, v in l.items() if k in ("name", "color")})

    ent_by_layer = defaultdict(Counter)
    heights_by_layer = defaultdict(Counter)
    text_styles_by_layer = defaultdict(Counter)
    hatch_by_layer = defaultdict(Counter)
    label_types_by_layer = defaultdict(Counter)
    dim_styles = Counter()
    overrides_by_layer = defaultdict(lambda: {"colors": Counter(), "linetypes": Counter()})
    blocks = {}
    parts = {}
    part_networks = _part_networks(data)
    total = 0

    for cat, e in _iter_entities(data):
        total += 1
        kind = SINGULAR.get(cat, cat)
        if kind in PART_KINDS:
            _observe_part(parts, kind, e, part_networks)
        layer = str(e.get("layer") or "")
        if not layer:
            continue
        lkey = layer.lower()
        ent_by_layer[lkey][kind] += 1
        if kind in ("text", "sheet_text"):
            h = _num(e.get("height"), 3)
            if h:
                heights_by_layer[lkey][h] += 1
            if e.get("style"):
                text_styles_by_layer[lkey][str(e["style"])] += 1
        elif kind == "hatch":
            pat = str(e.get("pattern") or "")
            if pat:
                hatch_by_layer[lkey][f"{pat} @ {_num(e.get('scale'), 3)}"] += 1
        elif kind == "dimension" and e.get("style"):
            dim_styles[str(e["style"])] += 1
        elif kind == "label":
            label_types_by_layer[lkey][str(e.get("type") or "Label")] += 1
        c = e.get("color")
        if c not in (None, "", "BYLAYER", "ByLayer", "256"):
            overrides_by_layer[lkey]["colors"][str(c)] += 1
        lt = e.get("linetype")
        if lt and str(lt).upper() != "BYLAYER":
            overrides_by_layer[lkey]["linetypes"][str(lt)] += 1

        if kind in ("block", "sheet_block"):
            name = str(e.get("name") or "")
            nkey = name.lower()
            if not name or name.startswith("*") or nkey in xrefs or e.get("is_xref"):
                continue
            b = blocks.setdefault(nkey, {"name": name, "count": 0, "layers": Counter(), "scales": Counter(),
                                         "rotations": Counter(), "attribute_tags": set(),
                                         "attribute_values": defaultdict(Counter), "space": Counter()})
            b["count"] += 1
            b["layers"][layer] += 1
            b["space"]["sheet" if kind == "sheet_block" else "model"] += 1
            s = _scale_of(e)
            if s is not None:
                b["scales"][s] += 1
            r = _num(e.get("rotation", e.get("rotation_deg")), 0)
            if r is not None:
                b["rotations"][r % 360] += 1
            for tag, val in (e.get("attributes") or {}).items():
                b["attribute_tags"].add(str(tag))
                if val not in (None, ""):
                    b["attribute_values"][str(tag)][str(val)] += 1

    defs = {str(d.get("name") or "").lower(): d for d in (block_defs or [])}

    layers_out, xref_layers = {}, defaultdict(list)
    for lkey in sorted(set(layer_meta) | set(ent_by_layer)):
        meta = layer_meta.get(lkey) or {}
        name = str(meta.get("name") or lkey)
        if "|" in name:
            x, suffix = name.split("|", 1)
            xref_layers[x].append(suffix)
            continue
        cls = classify(name)
        ents = ent_by_layer.get(lkey) or Counter()
        entry = {"name": name, "tag": cls["tag"], "family": cls["family"], "state": cls["state"],
                 "tag_source": cls["source"], "count": sum(ents.values()), "entities": dict(ents.most_common())}
        for k in ("color", "linetype", "lineweight", "plottable", "description"):
            if meta.get(k) not in (None, ""):
                entry[k] = meta[k]
        if heights_by_layer.get(lkey):
            entry["text_heights"] = _top(heights_by_layer[lkey])
        layers_out[lkey] = entry

    blocks_out = {}
    for nkey, b in sorted(blocks.items()):
        mode, top = _rotation_summary(b["rotations"])
        entry = {"name": b["name"], "count": b["count"], "layers": _top(b["layers"]),
                 "scales": _top(b["scales"]), "rotation": mode, "rotations": top,
                 "attribute_tags": sorted(b["attribute_tags"]),
                 "attribute_values": {t: _top(c) for t, c in sorted(b["attribute_values"].items())}}
        if b["space"].get("sheet"):
            entry["space"] = _top(b["space"])
        d = defs.get(nkey)
        if d:
            entry["definition"] = {k: d.get(k) for k in ("entities", "attribute_tags", "description", "origin")
                                   if d.get(k) not in (None, "", [])}
        blocks_out[nkey] = entry

    parts_out = {}
    for pkey, p in sorted(parts.items()):
        parts_out[pkey] = {"network": p["network"], "kind": p["kind"], "description": p["description"],
                           "count": p["count"], "sizes": _top(p["sizes"]), "name_patterns": _top(p["patterns"], 4),
                           "layers": _top(p["layers"])}

    text_layers = {lk for lk, c in heights_by_layer.items() if c} | {lk for lk, c in text_styles_by_layer.items() if c}
    conventions = {
        "text": {lk: {"heights": _top(heights_by_layer.get(lk) or Counter()),
                      "styles": _top(text_styles_by_layer.get(lk) or Counter())} for lk in sorted(text_layers)},
        "hatches": {lk: _top(c) for lk, c in sorted(hatch_by_layer.items()) if c},
        "dimension_styles": _top(dim_styles),
        "labels": {lk: _top(c) for lk, c in sorted(label_types_by_layer.items()) if c},
        "overrides": {lk: {"colors": _top(v["colors"]), "linetypes": _top(v["linetypes"])}
                      for lk, v in sorted(overrides_by_layer.items()) if v["colors"] or v["linetypes"]},
        "settings": settings or {},
    }
    return {"source": source, "path": path, "entities": total,
            "layers": layers_out, "xrefs": {x: sorted(set(v)) for x, v in sorted(xref_layers.items())},
            "blocks": blocks_out, "parts": parts_out, "conventions": conventions}


def _clean_network(name) -> str:
    """'Sanitary Sewer (Ph2)' -> 'Sanitary Sewer': phase / parenthetical suffixes dropped."""
    return re.sub(r"\s*\([^)]*\)", "", str(name or "")).strip() or "(unnamed network)"


def _part_networks(data: dict) -> dict:
    """part handle -> cleaned network name, for structures and pressure parts
    (pipe network pipes carry `network` themselves)."""
    m = {}
    for cat in ("pipe_networks", "pressure_networks"):
        for net in data.get(cat) or []:
            if not isinstance(net, dict):
                continue
            for sub in NESTED.get(cat, ()):
                for leaf in net.get(sub) or []:
                    if isinstance(leaf, dict) and leaf.get("handle"):
                        m[leaf["handle"]] = _clean_network(net.get("name"))
    return m


def _observe_part(parts: dict, kind: str, e: dict, networks: dict) -> None:
    network = _clean_network(e["network"]) if e.get("network") else networks.get(e.get("handle"), "(unnamed network)")
    desc = str(e.get("description") or e.get("part_type") or e.get("part_family") or "").strip() or "(no description)"
    key = f"{network}/{kind}/{desc}".lower()
    p = parts.setdefault(key, {"network": network, "kind": kind, "description": desc, "count": 0,
                               "sizes": Counter(), "patterns": Counter(), "layers": Counter()})
    p["count"] += 1
    size = e.get("part_size") or e.get("inner_diameter")
    if size not in (None, "", 0, 0.0):
        p["sizes"][size if isinstance(size, str) else _num(size, 3)] += 1
    name = str(e.get("name") or "")
    if name:
        p["patterns"][re.sub(r"\d+", "#", name)] += 1
    if e.get("layer"):
        p["layers"][str(e["layer"])] += 1


def _classifier():
    """layer -> {tag, family, state, source} through the full vocabulary
    (rules, edited learned entries, word derivation) WITHOUT noting anything in
    layer_tags.learned.json. Falls back to the word dictionary alone if the
    spatial module cannot import."""
    try:
        import spatial
        def f(layer):
            c = spatial.TAGS.classify(layer=layer, record=False)
            return {"tag": c.tag, "family": c.family, "state": c.state, "source": c.source}
        return f
    except Exception:
        import layer_words
        words = layer_words.Words()
        def f(layer):
            d = words.derive(layer)
            return {"tag": d.tag, "family": d.family, "state": d.state, "source": "derived"}
        return f


# ── observations on disk ──────────────────────────────────────────────────────


def observation_path(source: str, root: str = None) -> str:
    return os.path.join(paths(root)["observations"], safe_name(source) + ".json")


def save_observation(obs: dict, root: str = None) -> str:
    p = observation_path(obs["source"], root)
    _write_atomic(p, obs)
    return p


def load_observations(root: str = None) -> list:
    d = paths(root)["observations"]
    out = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith(".json"):
                obs = _load(os.path.join(d, fn))
                if isinstance(obs, dict) and obs.get("source"):
                    out.append(obs)
    return out


# ── rebuild global.json from every observation ────────────────────────────────


def _add_counts(dst: dict, src: dict) -> None:
    for k, v in (src or {}).items():
        dst[k] = dst.get(k, 0) + (v if isinstance(v, (int, float)) else 0)


def _sorted_counts(d: dict, n: int = None) -> dict:
    items = sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))
    return dict(items[:n] if n else items)


def _seed(prev_entry, kind: str, fresh: dict) -> dict:
    """A new merge entry, pre-filled with the FIRST_SEEN fields global.json
    already holds for that key so a later harvest cannot overwrite them."""
    for f in FIRST_SEEN[kind]:
        if prev_entry and prev_entry.get(f) not in (None, ""):
            fresh[f] = prev_entry[f]
    return fresh


def merge(observations: list, previous: dict = None) -> dict:
    """Aggregate every observation; carry person fields over from `previous`,
    keep FIRST_SEEN fields from `previous`."""
    prev_layers = (previous or {}).get("layers") or {}
    prev_blocks = (previous or {}).get("blocks") or {}
    prev_parts = (previous or {}).get("parts") or {}
    layers, blocks, parts, xrefs = {}, {}, {}, {}
    text, hatches, dims, labels, overrides, settings = {}, {}, {}, {}, {}, {}
    sources = {}

    for obs in observations:
        src = obs["source"]
        sources[src] = {"path": obs.get("path", ""), "entities": obs.get("entities", 0),
                        "layers": len(obs.get("layers") or {}), "blocks": len(obs.get("blocks") or {}),
                        "parts": len(obs.get("parts") or {})}
        for x, names in (obs.get("xrefs") or {}).items():
            m = xrefs.setdefault(x.lower(), {"name": x, "layers": set(), "sources": []})
            m["layers"].update(names)
            m["sources"].append(src)
        for pkey, e in (obs.get("parts") or {}).items():
            m = parts.setdefault(pkey, {"network": e["network"], "kind": e["kind"], "description": e["description"],
                                        "count": 0, "sizes": {}, "name_patterns": {}, "layers": {}, "sources": []})
            m["count"] += e.get("count", 0)
            _add_counts(m["sizes"], e.get("sizes"))
            _add_counts(m["name_patterns"], e.get("name_patterns"))
            _add_counts(m["layers"], e.get("layers"))
            m["sources"].append(src)
        for lkey, e in (obs.get("layers") or {}).items():
            m = layers.setdefault(lkey, _seed(prev_layers.get(lkey), "layers",
                                              {"name": e["name"], "count": 0, "entities": {}, "_heights": {}, "sources": []}))
            for k in ("tag", "family", "state", "tag_source", "color", "linetype", "lineweight",
                      "plottable", "description"):
                if k in e and e[k] not in (None, "") and k not in m:
                    m[k] = e[k]
            m["count"] += e.get("count", 0)
            _add_counts(m["entities"], e.get("entities"))
            _add_counts(m["_heights"], e.get("text_heights"))
            m["sources"].append(src)
        for nkey, e in (obs.get("blocks") or {}).items():
            m = blocks.setdefault(nkey, _seed(prev_blocks.get(nkey), "blocks",
                                              {"name": e["name"], "count": 0, "layers": {}, "scales": {}, "_rot": {},
                                               "attribute_tags": set(), "attribute_values": {}, "sources": []}))
            m["count"] += e.get("count", 0)
            _add_counts(m["layers"], e.get("layers"))
            _add_counts(m["scales"], e.get("scales"))
            for r in e.get("rotations") or []:
                m["_rot"][str(r["deg"])] = m["_rot"].get(str(r["deg"]), 0) + r["count"]
            m["attribute_tags"].update(e.get("attribute_tags") or [])
            for tag, vals in (e.get("attribute_values") or {}).items():
                _add_counts(m["attribute_values"].setdefault(tag, {}), vals)
            if e.get("definition") and "definition" not in m:
                m["definition"] = e["definition"]
            if e.get("space"):
                _add_counts(m.setdefault("space", {}), e["space"])
            m["sources"].append(src)
        conv = obs.get("conventions") or {}
        for lk, v in (conv.get("text") or {}).items():
            t = text.setdefault(lk, {"heights": {}, "styles": {}})
            _add_counts(t["heights"], v.get("heights"))
            _add_counts(t["styles"], v.get("styles"))
        for lk, v in (conv.get("hatches") or {}).items():
            _add_counts(hatches.setdefault(lk, {}), v)
        _add_counts(dims, conv.get("dimension_styles"))
        for lk, v in (conv.get("labels") or {}).items():
            _add_counts(labels.setdefault(lk, {}), v)
        for lk, v in (conv.get("overrides") or {}).items():
            o = overrides.setdefault(lk, {"colors": {}, "linetypes": {}})
            _add_counts(o["colors"], v.get("colors"))
            _add_counts(o["linetypes"], v.get("linetypes"))
        for k, v in (conv.get("settings") or {}).items():
            settings.setdefault(k, {})
            settings[k][str(v)] = settings[k].get(str(v), 0) + 1

    def finish(entries: dict, prev: dict, kind: str) -> dict:
        out = {}
        for key in sorted(entries):
            m = entries[key]
            if kind == "layers":
                heights = m.pop("_heights")
                if heights:
                    m["text_heights"] = _sorted_counts(heights, TOP_VALUES)
                m["entities"] = _sorted_counts(m["entities"])
            elif kind == "blocks":
                rot = m.pop("_rot")
                c = Counter({float(k): v for k, v in rot.items()})
                m["rotation"], m["rotations"] = _rotation_summary(c)
                m["layers"] = _sorted_counts(m["layers"], TOP_VALUES)
                m["scales"] = _sorted_counts(m["scales"], TOP_VALUES)
                m["attribute_tags"] = sorted(m["attribute_tags"])
                m["attribute_values"] = {t: _sorted_counts(v, TOP_VALUES) for t, v in sorted(m["attribute_values"].items())}
            else:
                m["sizes"] = _sorted_counts(m["sizes"], TOP_VALUES)
                m["name_patterns"] = _sorted_counts(m["name_patterns"], 4)
                m["layers"] = _sorted_counts(m["layers"], TOP_VALUES)
            m["sources"] = sorted(set(m["sources"]))
            entry = {}
            for f in PERSON_FIELDS:
                entry[f] = ""
            old = prev.get(key) or {}
            for f, v in old.items():
                if f not in HARVESTED[kind]:
                    entry[f] = v          # a person's field: kept exactly
            entry.update(m)
            out[key] = entry
        return out

    return {
        "_about": [
            "Layers, blocks and drafting conventions harvested from real drawings (python harvest.py <dwg|json>).",
            "Rebuilt from standards/observations/*.json on every run; edit role / note (or add your own fields)",
            "on any layer or block and they survive a rebuild. Counts / sources / value lists are recomputed each",
            "time; colour, linetype, lineweight, plottable, description and a block's definition are set the first",
            "time an entry appears and never overwritten (delete the entry and --rebuild to reset).",
            "Keys are lower-cased names. counts are entities seen across every source. tag/family/state come",
            "from layer_tags.json + layer_words.json; fix a wrong tag there, not here. layers are the drawings'",
            "own (count 0 = defined but unused); xref-dependent layers are listed by name under xrefs. parts",
            "are Civil 3D pipe / pressure network parts by network and description (these are not blocks).",
        ],
        "sources": dict(sorted(sources.items())),
        "layers": finish(layers, prev_layers, "layers"),
        "xrefs": {k: {"name": v["name"], "layers": sorted(v["layers"]), "sources": sorted(set(v["sources"]))}
                  for k, v in sorted(xrefs.items())},
        "blocks": finish(blocks, prev_blocks, "blocks"),
        "parts": finish(parts, prev_parts, "parts"),
        "conventions": {
            "text": {lk: {"heights": _sorted_counts(v["heights"], TOP_VALUES), "styles": _sorted_counts(v["styles"], TOP_VALUES)}
                     for lk, v in sorted(text.items())},
            "hatches": {lk: _sorted_counts(v, TOP_VALUES) for lk, v in sorted(hatches.items())},
            "dimension_styles": _sorted_counts(dims),
            "labels": {lk: _sorted_counts(v) for lk, v in sorted(labels.items())},
            "overrides": {lk: {"colors": _sorted_counts(v["colors"], TOP_VALUES), "linetypes": _sorted_counts(v["linetypes"], TOP_VALUES)}
                          for lk, v in sorted(overrides.items())},
            "settings": {k: _sorted_counts(v) for k, v in sorted(settings.items())},
        },
    }


# ── the markdown brief ────────────────────────────────────────────────────────


def _fmt_counts(d: dict, n: int = 4) -> str:
    items = list(d.items())[:n]
    return ", ".join(f"{k} ({v})" for k, v in items) + (" …" if len(d) > n else "")


def brief(g: dict) -> str:
    lines = ["# Drafting standards (harvested)", "",
             f"{len(g['sources'])} drawing(s): " + ", ".join(sorted(g["sources"])) + ".",
             "Counts are entities seen. `role` / `note` are hand-written in global.json when present.",
             "Text heights, hatch patterns, dimension styles and label types by layer are in global.json "
             "under `conventions`.", ""]

    lines.append("## Layers in use, by family")
    fams, unused = defaultdict(list), defaultdict(list)
    for key, e in g["layers"].items():
        (fams if e.get("count") else unused)[e.get("family") or "other"].append(e)
    for fam in sorted(fams):
        lines.append(f"### {fam}")
        for e in sorted(fams[fam], key=lambda x: -x["count"]):
            props = [f"tag {e.get('tag')}"]
            if e.get("state") and e["state"] != "proposed":
                props.append(e["state"])
            for k in ("color", "linetype", "lineweight"):
                if e.get(k) not in (None, ""):
                    props.append(f"{k} {e[k]}")
            if e.get("plottable") is False:
                props.append("no-plot")
            desc = f' — "{e["description"]}"' if e.get("description") else ""
            role = f" — role: {e['role']}" if e.get("role") else ""
            note = f" — {e['note']}" if e.get("note") else ""
            heights = f"; text heights {_fmt_counts(e['text_heights'], 3)}" if e.get("text_heights") else ""
            lines.append(f"- **{e['name']}** ({', '.join(props)}): {_fmt_counts(e['entities'])}{heights}{desc}{role}{note}")
        lines.append("")

    if unused:
        lines.append("## Layers defined but unused (name (colour))")
        for fam in sorted(unused):
            lines.append(f"- {fam}: " + ", ".join(e["name"] + (f" ({e['color']})" if e.get("color") is not None else "")
                                                  for e in sorted(unused[fam], key=lambda x: x["name"])))
        lines.append("")

    if g.get("xrefs"):
        lines.append("## Xrefs seen (their layers)")
        for x in g["xrefs"].values():
            names = x["layers"]
            lines.append(f"- **{x['name']}** ({len(names)} layers): " + ", ".join(names[:25]) + (" …" if len(names) > 25 else ""))
        lines.append("")

    lines.append("## Blocks")
    if not g["blocks"]:
        lines.append("(none)")
    for key, e in sorted(g["blocks"].items(), key=lambda kv: -kv[1]["count"]):
        bits = [f"{e['count']}×", f"on {_fmt_counts(e['layers'], 3)}"]
        if e.get("scales"):
            bits.append(f"scale {_fmt_counts(e['scales'], 3)}")
        if e.get("rotation"):
            rot = e["rotation"]
            if rot == "varied":
                rot += " (" + ", ".join(f"{r['deg']:g}°" for r in e["rotations"][:4]) + ")"
            bits.append(f"rotation {rot}")
        if e.get("space", {}).get("sheet"):
            bits.append("on sheets")
        if e.get("attribute_tags"):
            vals = []
            for t in e["attribute_tags"]:
                seen = e.get("attribute_values", {}).get(t)
                vals.append(f"{t}={_fmt_counts(seen, 3)}" if seen else t)
            bits.append("attributes " + "; ".join(vals))
        d = e.get("definition") or {}
        if d.get("description"):
            bits.append(f'"{d["description"]}"')
        role = f" — role: {e['role']}" if e.get("role") else ""
        note = f" — {e['note']}" if e.get("note") else ""
        lines.append(f"- **{e['name']}**: " + "; ".join(bits) + role + note)
    lines.append("")

    if g.get("parts"):
        lines.append("## Civil 3D parts in use (by network)")
        by_net = defaultdict(list)
        for e in g["parts"].values():
            by_net[e["network"]].append(e)
        for net in sorted(by_net):
            lines.append(f"### {net}")
            for e in sorted(by_net[net], key=lambda x: (x["kind"], -x["count"])):
                bits = [f"{e['count']}×"]
                if e.get("sizes"):
                    bits.append("sizes " + _fmt_counts(e["sizes"], 4))
                if e.get("name_patterns"):
                    bits.append("names " + _fmt_counts(e["name_patterns"], 2))
                if e.get("layers"):
                    bits.append("on " + _fmt_counts(e["layers"], 2))
                role = f" — role: {e['role']}" if e.get("role") else ""
                note = f" — {e['note']}" if e.get("note") else ""
                lines.append(f"- {e['kind']} **{e['description']}**: " + "; ".join(bits) + role + note)
        lines.append("")

    lines.append("")
    return "\n".join(lines)


# ── top-level operations ──────────────────────────────────────────────────────


def rebuild(root: str = None) -> dict:
    p = paths(root)
    previous = _load(p["global"], {}) or {}
    g = merge(load_observations(root), previous)
    _write_atomic(p["global"], g)
    _write_atomic(p["brief"], brief(g))
    return g


def harvest_file(target: str, root: str = None, base: str = PLUGIN_BASE, name: str = "") -> dict:
    """One drawing (.dwg via the plugin) or snapshot (.json) -> observation on disk."""
    if not os.path.exists(target):
        raise HarvestError(f"not found: {target}")
    source = name or os.path.basename(target)
    if target.lower().endswith(".json"):
        data = _load(target)
        if isinstance(data, dict) and data.get("harvest") and isinstance(data.get("snapshot"), dict):
            # the envelope the plugin's HARVEST command writes for the open drawing
            source = name or str(data.get("source") or source)
            obs = observe(data["snapshot"], source, str(data.get("path") or ""),
                          data.get("layer_defs") or [], data.get("block_defs") or [], data.get("settings") or {})
            save_observation(obs, root)
            return obs
        if not isinstance(data, dict) or not any(isinstance(v, list) for v in data.values()):
            raise HarvestError(f"{target} is not a model-space snapshot")
        # an exemplar bundle's snapshot.json: name the source after the bundle
        if not name and os.path.basename(target).lower() == "snapshot.json":
            source = os.path.basename(os.path.dirname(os.path.abspath(target))) or source
        obs = observe(data, source, os.path.abspath(target))
    else:
        snap, layer_defs, block_defs, settings = fetch_from_plugin(target, base)
        obs = observe(snap, source, os.path.abspath(target), layer_defs, block_defs, settings)
    save_observation(obs, root)
    return obs


def forget(source: str, root: str = None) -> bool:
    p = observation_path(source, root)
    if not os.path.exists(p):
        return False
    os.unlink(p)
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("targets", nargs="*", help=".dwg (needs the plugin) or snapshot .json files")
    ap.add_argument("--out", default=None, help=f"standards folder (default {STANDARDS_DIR} or $CAD_STANDARDS_DIR)")
    ap.add_argument("--base", default=PLUGIN_BASE, help=f"plugin URL (default {PLUGIN_BASE} or $CAD_PLUGIN_BASE)")
    ap.add_argument("--name", default="", help="source label for a single target (default: file name)")
    ap.add_argument("--list", action="store_true", help="show what has been harvested")
    ap.add_argument("--forget", metavar="SOURCE", action="append", default=[], help="drop a source and rebuild")
    ap.add_argument("--rebuild", action="store_true", help="regenerate global.json / global.md only")
    a = ap.parse_args(argv)
    root = a.out or STANDARDS_DIR
    p = paths(root)

    if a.list:
        g = _load(p["global"], {}) or {}
        srcs = g.get("sources") or {}
        if not srcs:
            print(f"nothing harvested yet in {root}")
        for s, info in srcs.items():
            print(f"{s}: {info.get('entities', 0)} entities, {info.get('layers', 0)} layers, "
                  f"{info.get('blocks', 0)} blocks, {info.get('parts', 0)} parts")
        print(f"{len(g.get('layers') or {})} layers, {len(g.get('blocks') or {})} blocks, "
              f"{len(g.get('parts') or {})} parts -> {p['global']}")
        return 0

    changed = False
    for s in a.forget:
        if forget(s, root):
            print(f"forgot {s}")
            changed = True
        else:
            print(f"no observation for {s!r} (see --list)")

    if a.name and len(a.targets) > 1:
        print("--name applies to a single target", file=sys.stderr)
        return 2
    failures = 0
    for t in a.targets:
        try:
            obs = harvest_file(t, root, a.base, a.name)
            print(f"{obs['source']}: {obs['entities']} entities, {len(obs['layers'])} layers, "
                  f"{len(obs['blocks'])} blocks, {len(obs['parts'])} parts")
            changed = True
        except HarvestError as e:
            print(f"{t}: {e}", file=sys.stderr)
            failures += 1

    if changed or a.rebuild or not os.path.exists(p["global"]):
        g = rebuild(root)
        print(f"{len(g['layers'])} layers, {len(g['blocks'])} blocks, {len(g['parts'])} parts, "
              f"{len(g['sources'])} source(s)")
        print(f"-> {p['global']}\n-> {p['brief']}")
    elif not a.targets and not a.forget:
        ap.print_usage()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
