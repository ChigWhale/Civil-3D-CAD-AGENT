"""A spatial index over a model space snapshot, built for reading.

The C# SpatialHash is a broad phase: cells are bags of ids, consumed once to
find adjacency edges and thrown away. This module keeps the same floor-division
grid but promotes the cell to something with content and position, so an agent
-- or a planner -- can ask "what is here", "where is this relative to the
street", and "does what I am about to draw cross anything", without reading
raw coordinates.

Structure, bottom up:

  grid      fixed origin, three levels (50 / 200 / 800 ft). Keys are a pure
            function of coordinates, so the same cell has the same key in
            every snapshot and snapshots diff cell by cell.
  cell      a record: distinct entities per tag, CLIPPED length per tag, area
            fraction covered by closed shapes, a length-weighted structure
            tensor (orientation that ADDS, so parents are exact sums of their
            children), a content hash, and a station/offset frame against the
            nearest alignment.
  region    corridors straight from the alignments (station range x offset
            band) and blocks as connected runs of what is left between them.
  render    fixed templates that turn a record into a sentence, and the coarse
            level into a character map. No model writes these: they have to be
            identical every run for the same drawing.

Geometry is inserted only at level 0, by exact grid traversal (a segment marks
the cells it crosses, not its bounding box). Everything above is aggregation.
Linework (lines, arcs, polylines, circles, pipes, feature lines) contributes
length and direction; point entities (blocks, texts, labels, structures,
points, dimensions) contribute a count and sit in exactly one cell.

  words     the vocabulary is layer_tags.json (see Vocabulary): layer suffix,
            block name or entity type -> tag, glyph, xref merge list. Rules can
            be added at runtime and saved; the version bumps so cached indexes
            rebuild.
  gate      conflicts(payloads): proposed geometry (draw_* payloads, sequences,
            a handler report's "proposed" block) checked for crossings, for
            landing inside a pad, and for proximity.
  check     verify(after, payloads): what a write changed, in these words, and
            whether any changed cell lies outside the area the write should
            have touched.

The index is cached per drawing by spatial_cache.py, keyed on the plugin's
change counter and this vocabulary's version.

Nothing here decides what an operation acts on. Queries return handles; the
operations take handles.
"""

import hashlib
import json
import math
import os
import re
from collections import defaultdict, Counter
from dataclasses import asdict, dataclass, field

import layer_words
from driveways import (
    PAD_LAYER_SUFFIXES,
    ROAD_LAYER,
    _add,
    _alignment_points,
    _bulge_arc,
    _chords,
    _dot,
    _mid,
    _norm,
    _point_seg_distance,
    _polyline_pieces,
    _pt,
    _ray_circle,
    _ray_segments,
    _scale,
    _v,
    is_pad_layer,
)


def point_at_station(pts, station: float):
    """The point `station` ft along a flattened alignment, clamped to its ends."""
    if not pts:
        return None
    if station <= 0:
        return pts[0]
    run = 0.0
    for i in range(len(pts) - 1):
        seg = math.dist(pts[i], pts[i + 1])
        if seg <= 0:
            continue
        if run + seg >= station:
            t = (station - run) / seg
            return (pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t,
                    pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t)
        run += seg
    return pts[-1]


def _centreline_length(pts) -> float:
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def _signed_offset(pts, p):
    """(distance, station, side) of p against a flattened alignment.

    side is +1 left of increasing station and -1 right of it. Station clamps at
    the ends, which the frame reports as beyond_end rather than hiding."""
    best = (float("inf"), 0.0, 1.0)
    run = 0.0
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        seg = _v(a, b)
        len2 = _dot(seg, seg)
        if len2 == 0:
            continue
        length = math.sqrt(len2)
        t = max(0.0, min(1.0, _dot(_v(a, p), seg) / len2))
        foot = _add(a, _scale(seg, t))
        d = math.dist(p, foot)
        if d < best[0]:
            u = _norm(seg)
            cross = u[0] * (p[1] - foot[1]) - u[1] * (p[0] - foot[0])
            best = (d, run + t * length, 1.0 if cross >= 0 else -1.0)
        run += length
    return best

# ── Grid constants ────────────────────────────────────────────────────────────
#
# The origin is a CONSTANT, not the drawing's extents. Derive it from extents
# and every key shifts the moment someone draws a north arrow off in a corner,
# and no two snapshots of the same site can be compared.
ORIGIN = (0.0, 0.0)
BASE_SIZE = 50.0          # ft, level 0 -- about one lot width
FACTOR = 4                # each level is 4x4 children of the one below
LEVELS = 3                # 50, 200, 800 ft
SUBSAMPLE = 4             # covered-fraction sampling grid per cell (4x4)
CORRIDOR_HALF_WIDTH = 40.0  # ft off an alignment that still counts as road

# ── Tags ──────────────────────────────────────────────────────────────────────
#
# The vocabulary an agent sees, read from layer_tags.json beside this file. A
# layer name (or a block name, or an entity type) maps to a tag by the same
# suffix habits the planners use, so "x-plat (ph1a)|c-lots-line" is a lot line
# here exactly as it is there. First rule to match wins. Pads are also checked
# through driveways.is_pad_layer so the planners and the index can never
# disagree about what a pad is. Rules can be added at runtime (define_tag) and
# saved back to the file; every change bumps the vocabulary version, which the
# index cache keys on, so a renamed layer family shows on the next query.

TAGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "layer_tags.json")

_FALLBACK_RULES = [   # used only when layer_tags.json is missing or unreadable
    {"tag": "curb",         "on": "layer", "match": "equals",   "needle": ROAD_LAYER,      "glyph": "R"},
    {"tag": "lot_line",     "on": "layer", "match": "endswith", "needle": "c-lots-line",   "glyph": "L"},
    {"tag": "setback",      "on": "layer", "match": "endswith", "needle": "c-lots-stbk"},
    {"tag": "centreline",   "on": "layer", "match": "endswith", "needle": "c-rway-center"},
    {"tag": "right_of_way", "on": "layer", "match": "endswith", "needle": "c-rway-line"},
    {"tag": "boundary",     "on": "layer", "match": "contains", "needle": "c-bndy"},
    {"tag": "easement",     "on": "layer", "match": "contains", "needle": "c-esmt"},
    {"tag": "driveway",     "on": "layer", "match": "contains", "needle": "drive",         "glyph": "d"},
    {"tag": "swale",        "on": "layer", "match": "contains", "needle": "swale",         "glyph": "d"},
]

_MATCHERS = {
    "equals":     lambda value, needle: value == needle,
    "endswith":   lambda value, needle: value.endswith(needle),
    "startswith": lambda value, needle: value.startswith(needle),
    "contains":   lambda value, needle: needle in value,
    "regex":      lambda value, needle: re.search(needle, value) is not None,
}


@dataclass(frozen=True)
class Classification:
    """What the vocabulary says about one entity: its tag, the family the tag
    belongs to (water_hydrant -> water), its state (proposed / existing / future
    ...), where the answer came from and, for derived names, how sure it is."""
    tag: str
    family: str
    state: str = "proposed"
    source: str = "other"      # pad | rule | learned | derived | type | other
    confidence: str = ""       # full | partial | none (derived only)


class Vocabulary:
    """The tag rules, glyphs and merge list, plus the word dictionary and the
    learned families, with a version that changes on every edit so cached
    indexes built with the old words are rebuilt.

    classify() precedence: pad layers -> hand rules on layer / block name ->
    learned entries a person edited -> derivation from the layer's words ->
    hand rules on entity type -> the type itself -> "other"."""

    PRECEDENCE = ("pad -> layer/name rules -> learned (edited) -> derived from words "
                  "-> type rules -> type -> other")

    def __init__(self, path=TAGS_FILE, words_path=None, learned_path=None):
        self.path = path
        self.version = 0
        self.rules: list = []
        self._layer_rules: list = []
        self._type_rules: list = []
        self.combos: list = []
        self.glyph_priority: list = []
        self.merge: list = []
        self.conflicts: dict = {}
        self._families: dict = {}
        self._cache: dict = {}
        self._known = None
        self.words = layer_words.Words(words_path or layer_words.WORDS_FILE)
        self.learned = layer_words.Learned(learned_path or layer_words.LEARNED_FILE)
        self._mtimes: dict = {}
        self.load()

    # -- loading / saving ---------------------------------------------------

    def load(self):
        raw = None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = None
        if not isinstance(raw, dict):
            raw = {"rules": list(_FALLBACK_RULES)}
        self.rules = [self._normalise(r) for r in (raw.get("rules") or []) if isinstance(r, dict)]
        self.rules = [r for r in self.rules if r]
        self.combos = [c for c in (raw.get("combos") or []) if isinstance(c, dict) and c.get("glyph")]
        self.glyph_priority = [str(t) for t in (raw.get("glyph_priority") or [])]
        self.merge = [str(s).lower() for s in (raw.get("merge") or []) if str(s).strip()]
        self.conflicts = raw.get("conflicts") if isinstance(raw.get("conflicts"), dict) else {}
        self._families = {}
        for r in self.rules:
            if r.get("family"):
                self._families[r["tag"]] = r["family"]
        for key, e in self.learned.entries.items():
            if e.get("edited") and e.get("tag") and e.get("family"):
                self._families[e["tag"]] = e["family"]
        self._sort()
        self._remember_mtimes()
        self._bump()

    def _bump(self):
        self.version += 1
        self._cache.clear()
        self._known = None

    def _remember_mtimes(self):
        for p in (self.path, self.words.path):
            try:
                self._mtimes[p] = os.path.getmtime(p)
            except OSError:
                self._mtimes[p] = None

    def refresh_if_changed(self) -> bool:
        """Reloads when the rules or the dictionary changed on disk: the MCP
        server and the dashboard server each hold a Vocabulary, and an edit
        made through one must reach the other without a restart. The learned
        file is not part of the check because this process may be writing it."""
        changed = False
        for p in (self.path, self.words.path):
            try:
                m = os.path.getmtime(p)
            except OSError:
                m = None
            if m != self._mtimes.get(p):
                changed = True
        if changed:
            self.words.load()
            self.learned.load()
            self.load()
        return changed

    def save(self):
        data = {
            "rules": [dict(r) for r in self.rules],
            "combos": self.combos,
            "glyph_priority": self.glyph_priority,
            "merge": self.merge,
            "conflicts": self.conflicts,
        }
        try:                                    # keep the file's own notes
            with open(self.path, "r", encoding="utf-8") as f:
                about = json.load(f).get("_about")
            if about:
                data = {"_about": about, **data}
        except (OSError, ValueError, AttributeError):
            pass
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)
        self._remember_mtimes()

    @staticmethod
    def _normalise(rule: dict):
        tag = str(rule.get("tag") or "").strip().lower().replace(" ", "_")
        needle = str(rule.get("needle") or "").strip().lower()
        match = str(rule.get("match") or "contains").strip().lower()
        on = str(rule.get("on") or "layer").strip().lower()
        if not tag or not needle or match not in _MATCHERS or on not in ("layer", "name", "type"):
            return None
        out = {"tag": tag, "on": on, "match": match, "needle": needle}
        glyph = str(rule.get("glyph") or "").strip()
        if glyph:
            out["glyph"] = glyph[0]
        family = str(rule.get("family") or "").strip().lower().replace(" ", "_")
        if family:
            out["family"] = family
        try:
            out["priority"] = int(rule.get("priority", 100))
        except (TypeError, ValueError):
            out["priority"] = 100
        return out

    def _sort(self):
        self.rules.sort(key=lambda r: r.get("priority", 100))   # stable: file order within a priority
        self._layer_rules = [r for r in self.rules if r["on"] in ("layer", "name")]
        self._type_rules = [r for r in self.rules if r["on"] == "type"]

    # -- the one question ---------------------------------------------------

    def classify(self, layer=None, name=None, type=None, record=True) -> Classification:
        layer_l = str(layer or "").lower()
        name_l = str(name or "").lower()
        type_l = str(type or "").lower()
        key = (layer_l, name_l, type_l)
        hit = self._cache.get(key)
        if hit is not None:
            if record and hit.source == "derived":
                self._note(layer_l)
            return hit
        c = self._classify(layer_l, name_l, type_l, record)
        if len(self._cache) > 200_000:
            self._cache.clear()
        self._cache[key] = c
        return c

    def _note(self, layer_l: str):
        d = self.words.derive(layer_l)
        if d.derivable and d.family_key and self.learned.get(d.family_key) is None:
            self.learned.note(d.family_key, d)

    def _classify(self, layer_l, name_l, type_l, record) -> Classification:
        d = self.words.derive(layer_l) if layer_l else None
        state = d.state if d else "proposed"
        if layer_l and is_pad_layer(layer_l):
            return Classification("pad", "pad", state, "pad")
        for r in self._layer_rules:
            value = layer_l if r["on"] == "layer" else name_l
            if not value:
                continue
            try:
                if _MATCHERS[r["match"]](value, r["needle"]):
                    fam = r.get("family") or self._families.get(r["tag"], r["tag"])
                    return Classification(r["tag"], fam, state, "rule")
            except re.error:
                continue
        if d is not None:
            learned = self.learned.get(d.family_key) if d.family_key else None
            if learned and learned.get("edited"):
                tag = learned.get("tag") or d.tag
                fam = learned.get("family") or (d.family if d.derivable else tag)
                st = learned.get("state") or state
                if tag:
                    self._families[tag] = fam
                    return Classification(tag, fam, st, "learned", d.confidence)
            if d.derivable:
                self._families[d.tag] = d.family
                if record:
                    self._note(layer_l)
                return Classification(d.tag, d.family, state, "derived", d.confidence)
        for r in self._type_rules:
            if type_l and _MATCHERS[r["match"]](type_l, r["needle"]):
                fam = r.get("family") or self._families.get(r["tag"], r["tag"])
                return Classification(r["tag"], fam, state, "type")
        if type_l:
            return Classification(type_l, type_l, state, "type")
        return Classification("other", "other", state, "other")

    def tag_for(self, layer=None, name=None, type=None) -> str:
        return self.classify(layer, name, type).tag

    def explain(self, layer) -> dict:
        """The whole story for one layer name: classification, derivation, the
        learned entry, and the hand rule that won (if one did)."""
        c = self.classify(layer, record=False)
        d = self.words.derive(layer)
        rule = None
        layer_l = str(layer or "").lower()
        for r in self._layer_rules:
            if r["on"] == "layer":
                try:
                    if _MATCHERS[r["match"]](layer_l, r["needle"]):
                        rule = dict(r)
                        break
                except re.error:
                    continue
        return {"layer": layer, "classification": asdict(c), "derivation": d.as_dict(),
                "learned": self.learned.get(d.family_key), "rule": rule,
                "pad_shortcut": bool(layer_l and is_pad_layer(layer_l))}

    # -- families --------------------------------------------------------------

    def family_of(self, tag) -> str:
        tag = str(tag or "").lower()
        return self._families.get(tag, tag)

    def matches(self, tag, words) -> bool:
        """Whether a tag is named by any of `words`: itself or its family."""
        tag = str(tag or "").lower()
        wanted = {str(w).lower() for w in (words or ())}
        return tag in wanted or self.family_of(tag) in wanted

    def state_word(self, state) -> str:
        return self.words.state_word(state)

    # -- editing ------------------------------------------------------------

    def define(self, tag, needle, match="contains", on="layer", glyph="", priority=None, persist=True, family=""):
        rule = self._normalise({"tag": tag, "needle": needle, "match": match, "on": on,
                                "glyph": glyph, "priority": 80 if priority is None else priority,
                                "family": family})
        if rule is None:
            raise ValueError(f"bad rule: tag={tag!r} needle={needle!r} match={match!r} on={on!r}; "
                             f"match must be one of {sorted(_MATCHERS)} and on one of layer, name, type")
        # Replace a rule with the same needle/on/match rather than shadowing it.
        self.rules = [r for r in self.rules
                      if not (r["on"] == rule["on"] and r["match"] == rule["match"] and r["needle"] == rule["needle"])]
        self.rules.append(rule)
        if rule.get("family"):
            self._families[rule["tag"]] = rule["family"]
        if rule["on"] == "layer" and rule["match"] in ("endswith", "equals") and rule["needle"] not in self.merge:
            self.merge.append(rule["needle"])
        self._sort()
        self._bump()
        if persist:
            self.save()
        return rule

    def forget(self, tag, persist=True) -> int:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r["tag"] != tag]
        self._sort()
        self._bump()
        if persist:
            self.save()
        return before - len(self.rules)

    def rename_word(self, abbr, word, kind="major", tag_key="", glyph="") -> dict:
        out = self.words.set_word(abbr, word, kind=kind, tag_key=tag_key, glyph=glyph)
        self._remember_mtimes()
        self._bump()
        return out

    def edit_learned(self, family_key, tag=None, family=None, state=None, note=None) -> dict:
        entry = self.learned.edit(family_key, tag=tag, family=family, state=state, note=note)
        if entry.get("tag") and entry.get("family"):
            self._families[entry["tag"]] = entry["family"]
        self._bump()
        return entry

    def flush_learned(self) -> int:
        return self.learned.flush()

    # -- what the agent may ask ----------------------------------------------

    def tags(self) -> list:
        seen, out = set(), []
        for r in self.rules:
            if r["tag"] not in seen:
                seen.add(r["tag"])
                out.append(r["tag"])
        if "pad" not in seen:
            out.insert(0, "pad")
        return out

    def known_words(self) -> frozenset:
        """Every word a check or query may name: hand tags, families, every
        family_minor the dictionary can produce, learned tags, type tags."""
        if self._known is None:
            words = set(self.tags()) | set(self.words.all_tag_words()) | self.learned.tags()
            words |= set(self._families.values())
            words |= {"other", "block", "text", "label", "point", "dimension", "pad"}
            self._known = frozenset(w for w in words if w)
        return self._known

    def glyph_for(self, tag) -> str:
        for r in self.rules:
            if r["tag"] == tag and r.get("glyph"):
                return r["glyph"]
        return self.words.family_glyph(self.family_of(tag))

    def describe(self) -> dict:
        return {
            "version": self.version,
            "file": self.path,
            "precedence": self.PRECEDENCE,
            "tags": self.tags(),
            "families": sorted(set(self._families.values()) | set(self.words.families())),
            "rules": [dict(r) for r in self.rules],
            "combos": self.combos,
            "glyph_priority": self.glyph_priority,
            "merge_layers": list(self.merge),
            "conflicts": dict(self.conflicts),
            "words": self.words.describe(),
            "learned": self.learned.describe(),
            "fallback": "a layer no rule names is read from its words (C-WATR-HYDR -> water_hydrant); "
                        "an entity with no readable layer is tagged by its type (block, text, pipe ...) "
                        "or 'other' for plain linework",
        }


TAGS = Vocabulary()


def tag_for(layer=None, name=None, type=None) -> str:
    return TAGS.tag_for(layer, name, type)


def classify(layer=None, name=None, type=None, record=True) -> Classification:
    return TAGS.classify(layer, name, type, record)


def merge_layers() -> list:
    """Layer suffixes to pull out of xrefs into a snapshot before indexing."""
    return [*TAGS.merge, *[s for s in PAD_LAYER_SUFFIXES if s not in TAGS.merge]]


def cell_glyph(cell) -> str:
    """The site-map character for a cell: combos first, then tags (or their
    families) in the configured priority, then any tag with a glyph, then 'o'."""
    present = set(cell.tags())
    families = {TAGS.family_of(t) for t in present}
    for combo in TAGS.combos:
        wanted = [str(t) for t in (combo.get("all") or [])]
        if wanted and all(t in present or t in families for t in wanted):
            return str(combo["glyph"])[0]
    for word in TAGS.glyph_priority:
        for t in present:
            if (t == word or TAGS.family_of(t) == word) and TAGS.glyph_for(t):
                return TAGS.glyph_for(t)
    for tag in sorted(present):
        g = TAGS.glyph_for(tag)
        if g:
            return g
    return "o" if (cell.total_length() > 0 or cell.total_points() > 0) else "."


def map_legend() -> str:
    parts = []
    for combo in TAGS.combos:
        parts.append(f"{combo['glyph']} {'+'.join(combo.get('all') or [])}")
    by_glyph, order = {}, []
    names = [*TAGS.glyph_priority, *TAGS.tags()]
    names += [f for f in TAGS.words.families() if TAGS.words.family_glyph(f)]
    for tag in names:
        g = TAGS.glyph_for(tag)
        if not g:
            continue
        if g not in by_glyph:
            by_glyph[g] = []
            order.append(g)
        if tag not in by_glyph[g]:
            by_glyph[g].append(tag)
    for g in order:
        parts.append(f"{g} {'/'.join(t.replace('_', ' ') for t in by_glyph[g])}")
    parts.append("o other linework")
    parts.append(". empty")
    return "legend: " + "  ".join(parts)


# Intents that read the drawing without changing it. Posting one of these never
# needs the gate and never invalidates a cached index.
READ_ONLY_INTENTS = frozenset({
    "get_drawing_settings", "list_styles", "list_layers", "list_block_definitions",
    "get_properties", "select_entities", "entity_key_points", "closest_point_on",
    "intersections", "tangent_points", "measure_entities", "angle_between",
    "point_along", "bounding_box", "find_duplicates", "zoom_view",
    "list_surface_label_styles", "export_block", "save_drawing",
})


# ── Grid maths ────────────────────────────────────────────────────────────────


def cell_size(level: int) -> float:
    return BASE_SIZE * (FACTOR ** level)


def key_at(x: float, y: float, level: int = 0) -> tuple:
    s = cell_size(level)
    return (level,
            int(math.floor((x - ORIGIN[0]) / s)),
            int(math.floor((y - ORIGIN[1]) / s)))


def parent_key(key: tuple) -> tuple:
    level, cx, cy = key
    return (level + 1, cx // FACTOR, cy // FACTOR)   # floor division: exact for negatives too


def bounds(key: tuple) -> tuple:
    level, cx, cy = key
    s = cell_size(level)
    x0, y0 = ORIGIN[0] + cx * s, ORIGIN[1] + cy * s
    return (x0, y0, x0 + s, y0 + s)


def centre(key: tuple) -> tuple:
    x0, y0, x1, y1 = bounds(key)
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _traverse(a, b, level: int = 0):
    """Cells a segment crosses, with the length inside each: Amanatides & Woo.

    This is the whole reason a cell can be trusted: a diagonal road marks the
    cells it passes through, not the rectangle its bounding box covers."""
    total = math.dist(a, b)
    if total < 1e-12:
        return [(key_at(a[0], a[1], level), 0.0)]

    s = cell_size(level)
    dx, dy = (b[0] - a[0]) / total, (b[1] - a[1]) / total
    _, cx, cy = key_at(a[0], a[1], level)

    def first_boundary(pos, origin, c, d):
        if d > 0:
            return (origin + (c + 1) * s - pos) / d
        if d < 0:
            return (origin + c * s - pos) / d
        return math.inf

    t_max_x = first_boundary(a[0], ORIGIN[0], cx, dx)
    t_max_y = first_boundary(a[1], ORIGIN[1], cy, dy)
    t_delta_x = s / abs(dx) if dx else math.inf
    t_delta_y = s / abs(dy) if dy else math.inf
    step_x = 1 if dx > 0 else -1
    step_y = 1 if dy > 0 else -1

    out, t = [], 0.0
    for _ in range(100000):                     # guard, never reached in practice
        t_exit = min(t_max_x, t_max_y, total)
        out.append(((level, cx, cy), max(0.0, t_exit - t)))
        if t_exit >= total - 1e-9:
            break
        if t_max_x < t_max_y:
            cx += step_x
            t_max_x += t_delta_x
        elif t_max_y < t_max_x:
            cy += step_y
            t_max_y += t_delta_y
        else:                                   # exactly through a corner
            cx += step_x
            cy += step_y
            t_max_x += t_delta_x
            t_max_y += t_delta_y
        t = t_exit
    return out


def _clip(a, b, box):
    """Segment ab clipped to an axis-aligned box (Liang-Barsky), or None."""
    x0, y0, x1, y1 = box
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if abs(p) < 1e-12:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return None
            t0 = max(t0, r)
        else:
            if r < t0:
                return None
            t1 = min(t1, r)
    if t1 - t0 < 1e-9:
        return None
    return ((a[0] + t0 * dx, a[1] + t0 * dy), (a[0] + t1 * dx, a[1] + t1 * dy))


def _point_in_polygon(p, verts) -> bool:
    inside = False
    n = len(verts)
    for i in range(n):
        a, b = verts[i], verts[(i + 1) % n]
        if (a[1] > p[1]) != (b[1] > p[1]):
            x = a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if p[0] < x:
                inside = not inside
    return inside


# ── Pieces ────────────────────────────────────────────────────────────────────
#
# Everything is reduced to segments, true arcs and points, carrying tag and
# handle. A point piece ("pt") is anything that sits at one place -- a block,
# a text, a label anchor, a pipe structure -- and lands in exactly one cell.


@dataclass(frozen=True)
class Piece:
    kind: str          # "seg" | "arc" | "pt"
    geom: tuple        # seg: (a, b)   arc: (centre, r, a0, a1)   pt: (x, y)
    tag: str
    handle: str
    layer: str
    name: str = ""     # block name, label kind, text -- whatever names the thing

    def chords(self, per_arc=None):
        if self.kind == "seg":
            return [self.geom]
        if self.kind == "pt":
            return []
        c, r, a0, a1 = self.geom
        n = per_arc or max(8, int(math.ceil(r * (a1 - a0) / (BASE_SIZE / 2.0))))
        return _chords(c, r, a0, a1, n)

    def endpoints(self):
        if self.kind == "seg":
            return self.geom
        if self.kind == "pt":
            return (self.geom, self.geom)
        c, r, a0, a1 = self.geom
        return ((c[0] + r * math.cos(a0), c[1] + r * math.sin(a0)),
                (c[0] + r * math.cos(a1), c[1] + r * math.sin(a1)))

    def length(self):
        if self.kind == "seg":
            return math.dist(*self.geom)
        if self.kind == "pt":
            return 0.0
        c, r, a0, a1 = self.geom
        return r * (a1 - a0)

    def descriptor(self):
        """Geometry only, rounded -- what the content hash is over."""
        if self.kind == "seg":
            (ax, ay), (bx, by) = self.geom
            return f"{self.tag}|seg|{ax:.3f},{ay:.3f}|{bx:.3f},{by:.3f}"
        if self.kind == "pt":
            return f"{self.tag}|pt|{self.geom[0]:.3f},{self.geom[1]:.3f}"
        (cx, cy), r, a0, a1 = self.geom
        return f"{self.tag}|arc|{cx:.3f},{cy:.3f}|{r:.3f}|{a0:.5f}|{a1:.5f}"


def _xy(raw):
    """A point from any of the shapes the snapshot uses, or None."""
    try:
        if isinstance(raw, dict):
            raw = raw.get("position") or raw.get("point")
        return (float(raw[0]), float(raw[1]))
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def _snapshot_pieces(data: dict):
    """Every entity of the snapshot as pieces. Lines, arcs and polylines are the
    linework; blocks, texts, points, dimensions, labels and pipe structures are
    point pieces; circles are full arcs; pipes and feature lines are segment
    chains. Tags come from the layer first, then the block name, then the
    type, exactly as layer_tags.json says."""
    for ln in data.get("lines", []) or []:
        yield Piece("seg", (_pt(ln["start_point"]), _pt(ln["end_point"])),
                    tag_for(ln.get("layer")), str(ln.get("handle")), str(ln.get("layer")))
    for arc in data.get("arcs", []) or []:
        a0, a1 = float(arc["start_angle"]), float(arc["end_angle"])
        if a1 < a0:
            a1 += 2 * math.pi
        yield Piece("arc", (_pt(arc["center"]), float(arc["radius"]), a0, a1),
                    tag_for(arc.get("layer")), str(arc.get("handle")), str(arc.get("layer")))
    for pl in data.get("polylines", []) or []:
        tag, handle, layer = tag_for(pl.get("layer")), str(pl.get("handle")), str(pl.get("layer"))
        for kind, geom in _polyline_pieces(pl):
            yield Piece("seg" if kind == "segment" else "arc", geom, tag, handle, layer)

    for ci in data.get("circles", []) or []:
        c, r = _xy(ci.get("center")), ci.get("radius")
        if c is None or not r:
            continue
        yield Piece("arc", (c, float(r), 0.0, 2 * math.pi),
                    tag_for(ci.get("layer"), type="circle"), str(ci.get("handle")), str(ci.get("layer")))

    for br in data.get("blocks", []) or []:
        p = _xy(br.get("position"))
        if p is None:
            continue
        yield Piece("pt", p, tag_for(br.get("layer"), name=br.get("name"), type="block"),
                    str(br.get("handle")), str(br.get("layer")), str(br.get("name") or ""))

    for tx in data.get("texts", []) or []:
        p = _xy(tx.get("position"))
        if p is None:
            continue
        yield Piece("pt", p, tag_for(tx.get("layer"), type="text"),
                    str(tx.get("handle")), str(tx.get("layer")), str(tx.get("text") or "")[:40])

    for pt in data.get("points", []) or []:
        p = _xy(pt.get("position"))
        if p is None:
            continue
        yield Piece("pt", p, tag_for(pt.get("layer"), type="point"),
                    str(pt.get("handle")), str(pt.get("layer")))

    for dim in data.get("dimensions", []) or []:
        p = _xy(dim.get("text_position"))
        if p is None:
            continue
        yield Piece("pt", p, tag_for(dim.get("layer"), type="dimension"),
                    str(dim.get("handle")), str(dim.get("layer")))

    for lbl in data.get("labels", []) or []:
        p = _xy(lbl.get("anchor"))
        if p is None:
            continue
        yield Piece("pt", p, tag_for(lbl.get("layer"), name=lbl.get("kind"), type="label"),
                    str(lbl.get("handle")), str(lbl.get("layer")),
                    str(lbl.get("kind") or lbl.get("type") or ""))

    for net in data.get("pipe_networks", []) or []:
        net_name = str(net.get("name") or "")
        for pipe in net.get("pipes", []) or []:
            a, b = _xy(pipe.get("start_point")), _xy(pipe.get("end_point"))
            if a is None or b is None:
                continue
            yield Piece("seg", (a, b), tag_for(pipe.get("layer"), name=net_name, type="pipe"),
                        str(pipe.get("handle")), str(pipe.get("layer") or net_name),
                        str(pipe.get("name") or ""))
        for s in net.get("structures", []) or []:
            p = _xy(s.get("insertion_point"))
            if p is None:
                continue
            yield Piece("pt", p, tag_for(s.get("layer"), name=net_name, type="structure"),
                        str(s.get("handle")), str(s.get("layer") or net_name),
                        str(s.get("name") or ""))

    for fl in data.get("feature_lines", []) or []:
        pts = [q for q in (_xy(p) for p in fl.get("points") or []) if q is not None]
        tag = tag_for(fl.get("layer"), name=fl.get("name"), type="feature_line")
        for i in range(len(pts) - 1):
            yield Piece("seg", (pts[i], pts[i + 1]), tag, str(fl.get("handle")),
                        str(fl.get("layer") or ""), str(fl.get("name") or ""))

    # Alignments as linework in their own right (they already serve as the
    # station/offset frames): every tangent and arc.
    for al in data.get("alignments", []) or []:
        tag = tag_for(al.get("layer"), name=al.get("name"), type="alignment")
        handle, name = str(al.get("handle") or al.get("name")), str(al.get("name") or "")
        for ent in al.get("entities") or []:
            kind = ent.get("type")
            if kind == "Tangent":
                a, b = _xy(ent.get("start")), _xy(ent.get("end"))
                if a and b:
                    yield Piece("seg", (a, b), tag, handle, str(al.get("layer") or ""), name)
            elif kind == "Arc":
                c, r = _xy(ent.get("center")), ent.get("radius")
                a, b = _xy(ent.get("start")), _xy(ent.get("end"))
                if c and r and a and b:
                    a0 = math.atan2(a[1] - c[1], a[0] - c[0])
                    a1 = math.atan2(b[1] - c[1], b[0] - c[0])
                    if ent.get("clockwise"):
                        a0, a1 = a1, a0          # store counter-clockwise from the end
                    while a1 < a0:
                        a1 += 2 * math.pi
                    yield Piece("arc", (c, float(r), a0, a1), tag, handle, str(al.get("layer") or ""), name)

    # Pressure networks: pipes as segments, fittings and appurtenances as points.
    for net in data.get("pressure_networks", []) or []:
        net_name = str(net.get("name") or "")
        for pipe in net.get("pipes", []) or []:
            a, b = _xy(pipe.get("start_point")), _xy(pipe.get("end_point"))
            if a is None or b is None:
                continue
            yield Piece("seg", (a, b), tag_for(pipe.get("layer"), name=net_name, type="pressure_pipe"),
                        str(pipe.get("handle")), str(pipe.get("layer") or net_name),
                        str(pipe.get("part_size") or pipe.get("name") or ""))
        for kind_key, kind in (("fittings", "fitting"), ("appurtenances", "appurtenance")):
            for part in net.get(kind_key, []) or []:
                p = _xy(part.get("position"))
                if p is None:
                    continue
                yield Piece("pt", p, tag_for(part.get("layer"), name=part.get("part_type") or part.get("name"), type=kind),
                            str(part.get("handle")), str(part.get("layer") or net_name),
                            str(part.get("part_type") or part.get("description") or part.get("name") or ""))

    # Parcels: every loop segment, tagged by the parcel's layer or "parcel".
    for pc in data.get("parcels", []) or []:
        tag = tag_for(pc.get("layer"), name=pc.get("name"), type="parcel")
        handle, layer, name = str(pc.get("handle")), str(pc.get("layer") or ""), str(pc.get("name") or "")
        for loop in pc.get("loops") or []:
            for seg in loop or []:
                a, b = _xy(seg.get("start")), _xy(seg.get("end"))
                if a is None or b is None:
                    continue
                if seg.get("type") == "arc" and seg.get("center") and seg.get("radius"):
                    c = _xy(seg["center"])
                    a0 = math.atan2(a[1] - c[1], a[0] - c[0])
                    a1 = math.atan2(b[1] - c[1], b[0] - c[0])
                    if seg.get("clockwise"):
                        a0, a1 = a1, a0
                    while a1 < a0:
                        a1 += 2 * math.pi
                    yield Piece("arc", (c, float(seg["radius"]), a0, a1), tag, handle, layer, name)
                else:
                    yield Piece("seg", (a, b), tag, handle, layer, name)

    # Surface borders, corridor feature lines, sample lines, survey figures,
    # catchment / grading / view-frame boundaries: point chains.
    def chains(items, type_name, key, closed):
        for it in items or []:
            pts = [q for q in (_xy(p) for p in it.get(key) or []) if q is not None]
            if len(pts) < 2:
                continue
            tag = tag_for(it.get("layer"), name=it.get("name") or it.get("code"), type=type_name)
            handle = str(it.get("handle"))
            layer = str(it.get("layer") or "")
            name = str(it.get("name") or it.get("code") or "")
            n = len(pts)
            for i in range(n if (closed and n > 2) else n - 1):
                yield Piece("seg", (pts[i], pts[(i + 1) % n]), tag, handle, layer, name)

    for sf in data.get("surfaces", []) or []:
        for i, loop in enumerate(sf.get("boundary") or []):
            yield from chains([{"points": loop.get("points"), "layer": sf.get("layer"), "name": sf.get("name"),
                                "handle": f"{sf.get('handle')}/b{i}"}], "surface", "points", True)
    yield from chains(data.get("corridor_lines"), "corridor_line", "points", False)
    yield from chains(data.get("sample_lines"), "sample_line", "points", False)
    yield from chains(data.get("survey_figures"), "survey_figure", "points", False)
    yield from chains(data.get("catchments"), "catchment", "boundary", True)
    yield from chains(data.get("gradings"), "grading", "boundary", True)
    yield from chains(data.get("view_frames"), "view_frame", "boundary", True)

    for cp in data.get("cogo_points", []) or []:
        p = _xy(cp.get("position"))
        if p is None:
            continue
        yield Piece("pt", p, tag_for(cp.get("layer"), name=cp.get("description"), type="cogo_point"),
                    str(cp.get("handle")), str(cp.get("layer") or ""), str(cp.get("description") or cp.get("number") or ""))

    for ix in data.get("intersections", []) or []:
        p = _xy(ix.get("position"))
        if p is None:
            continue
        yield Piece("pt", p, tag_for(ix.get("layer"), name=ix.get("name"), type="intersection"),
                    str(ix.get("handle")), str(ix.get("layer") or ""), str(ix.get("name") or ""))

    # Paper-space viewports as the model-space windows they show: closed rings
    # tagged "viewport", named by layout, so a cell can say which sheet sees it.
    for lay in data.get("layouts", []) or []:
        for vp in lay.get("viewports") or []:
            verts = _viewport_verts(vp)
            if not verts or not vp.get("on", True):
                continue
            yield from _ring_pieces(verts, True, tag_for(vp.get("layer"), type="viewport"),
                                    str(vp.get("handle")), str(vp.get("layer") or ""), None)


def _viewport_verts(vp: dict) -> list:
    """A viewport's model window as four corners: the plugin's twisted
    model_ring when it reports one, else the axis-aligned model_window."""
    ring = vp.get("model_ring")
    if ring:
        verts = [q for q in (_xy(v) for v in ring) if q is not None]
        if len(verts) >= 3:
            return verts
    win = vp.get("model_window")
    if not win:
        return []
    lo, hi = _xy(win[0]), _xy(win[1])
    if lo is None or hi is None:
        return []
    return [(lo[0], lo[1]), (hi[0], lo[1]), (hi[0], hi[1]), (lo[0], hi[1])]


def _snapshot_polygons(data: dict):
    """Closed polylines as chorded vertex rings, for covered-fraction."""
    for pl in data.get("polylines", []) or []:
        if not pl.get("closed"):
            continue
        ring = []
        for kind, geom in _polyline_pieces(pl):
            if kind == "segment":
                ring.append(geom[0])
            else:
                for a, _ in _chords(*geom, 12):
                    ring.append(a)
        if len(ring) >= 3:
            yield ring, tag_for(pl.get("layer")), str(pl.get("handle"))


def _ring_pieces(verts, closed, tag, handle, layer, bulges=None):
    n = len(verts)
    if n < 2:
        return
    count = n if (closed and n > 2) else n - 1
    for i in range(count):
        a, b = verts[i], verts[(i + 1) % n]
        bulge = float(bulges[i]) if bulges and i < len(bulges) else 0.0
        if abs(bulge) < 1e-9 or math.dist(a, b) < 1e-9:
            yield Piece("seg", (a, b), tag, handle, layer)
        else:
            yield Piece("arc", _bulge_arc(a, b, bulge), tag, handle, layer)


def viewport_ring(view_center, paper_scale, width, height, twist_deg=0.0):
    """The model-space corners a paper viewport shows: paper size over the
    scale (paper units per model unit), twisted about the view centre. The
    same arithmetic as CadCommands.ViewportWindow, so a planned window and
    the one the plugin reports back agree."""
    c = _xy(view_center)
    try:
        ps = float(paper_scale)
        w, h = float(width), float(height)
    except (TypeError, ValueError):
        return []
    if c is None or ps <= 0 or w <= 0 or h <= 0:
        return []
    half_w, half_h = w / ps / 2.0, h / ps / 2.0
    a = -math.radians(float(twist_deg or 0.0))
    cs, sn = math.cos(a), math.sin(a)
    return [(c[0] + ox * cs - oy * sn, c[1] + ox * sn + oy * cs)
            for ox, oy in ((-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h))]


def _rect_verts(p):
    c1 = _xy(p.get("corner1"))
    if c1 is None:
        return []
    c2 = _xy(p.get("corner2"))
    if c2 is None:
        w, h = p.get("width"), p.get("height")
        if w is None or h is None:
            return []
        c2 = (c1[0] + float(w), c1[1] + float(h))
    x0, y0, x1, y1 = min(c1[0], c2[0]), min(c1[1], c2[1]), max(c1[0], c2[0]), max(c1[1], c2[1])
    verts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    rot = math.radians(float(p.get("rotation_deg") or 0.0))
    if abs(rot) > 1e-12:
        cs, sn = math.cos(rot), math.sin(rot)
        verts = [(c1[0] + (x - c1[0]) * cs - (y - c1[1]) * sn,
                  c1[1] + (x - c1[0]) * sn + (y - c1[1]) * cs) for x, y in verts]
    return verts


def _payload_pieces(payloads: list):
    """Proposed payloads reduced to pieces, for the dry-run gate and the
    post-write check. Covers the drawing intents whose geometry is fully given
    in the payload; a sequence yields its steps under the sequence's index, and
    a handler report's "proposed" block (points / segments / boxes) is read the
    same way. Anything else -- an offset, a trim, a move by handle -- carries
    no geometry of its own and yields nothing."""
    for index, p in enumerate(payloads):
        if not isinstance(p, dict):
            continue
        yield from ((index, piece) for piece in _one_payload_pieces(p, f"payload:{index}"))


def _one_payload_pieces(p: dict, handle: str):
    intent = str(p.get("intent") or "")
    layer = str(p.get("layer") or "")
    tag = tag_for(layer) if layer else "other"

    if intent == "sequence":
        for i, step in enumerate(p.get("steps") or []):
            if isinstance(step, dict):
                yield from _one_payload_pieces(step, f"{handle}/{i}")
        return
    if "proposed" in p and isinstance(p["proposed"], dict):
        yield from proposed_pieces(p["proposed"], handle, layer)
        return

    if intent == "draw_line":
        a, b = _xy(p.get("start_point") or p.get("start")), _xy(p.get("end_point") or p.get("end"))
        if a and b:
            yield Piece("seg", (a, b), tag, handle, layer)
    elif intent == "draw_arc":
        c = _xy(p.get("center"))
        if c and p.get("radius"):
            a0, a1 = float(p.get("start_angle") or 0.0), float(p.get("end_angle") or 0.0)
            if a1 < a0:
                a1 += 2 * math.pi
            yield Piece("arc", (c, float(p["radius"]), a0, a1), tag, handle, layer)
    elif intent == "draw_arc_by":
        c, r = _xy(p.get("center")), p.get("radius")
        a, b = _xy(p.get("start")), _xy(p.get("end"))
        if c and a and b:
            r = r or math.dist(c, a)
            a0 = math.atan2(a[1] - c[1], a[0] - c[0])
            a1 = math.atan2(b[1] - c[1], b[0] - c[0])
            if a1 < a0:
                a1 += 2 * math.pi
            yield Piece("arc", (c, float(r), a0, a1), tag, handle, layer)
        elif a and b:                              # chord at least
            yield Piece("seg", (a, b), tag, handle, layer)
    elif intent in ("draw_shape", "draw_polyline", "draw_revision_cloud", "draw_wipeout"):
        verts = [q for q in (_xy(v) for v in p.get("vertices") or []) if q is not None]
        closed = bool(p.get("closed")) or intent in ("draw_revision_cloud", "draw_wipeout")
        yield from _ring_pieces(verts, closed, tag, handle, layer, p.get("bulges"))
    elif intent == "draw_rectangle":
        yield from _ring_pieces(_rect_verts(p), True, tag, handle, layer)
    elif intent == "create_viewport":
        # The window a new sheet will show, as the ring the index will hold once
        # the plugin reports it back: planned cells, never a conflict (see
        # conflicts()) -- a viewport frame crosses everything it frames.
        verts = viewport_ring(p.get("view_center"), p.get("scale"), p.get("width"), p.get("height"),
                              p.get("twist_deg"))
        yield from _ring_pieces(verts, True, "viewport", handle, layer)
    elif intent == "draw_polygon":
        c, r, sides = _xy(p.get("center")), p.get("radius"), p.get("sides")
        if c and r and sides:
            rot = math.radians(float(p.get("rotation_deg") or 0.0))
            n = max(3, int(sides))
            verts = [(c[0] + float(r) * math.cos(rot + 2 * math.pi * i / n),
                      c[1] + float(r) * math.sin(rot + 2 * math.pi * i / n)) for i in range(n)]
            yield from _ring_pieces(verts, True, tag, handle, layer)
    elif intent in ("draw_circle", "draw_donut"):
        c = _xy(p.get("center"))
        r = p.get("radius") or (float(p["diameter"]) / 2 if p.get("diameter") else None)
        if intent == "draw_donut" and p.get("outer_diameter"):
            r = float(p["outer_diameter"]) / 2
        if c and r:
            yield Piece("arc", (c, float(r), 0.0, 2 * math.pi), tag, handle, layer)
    elif intent == "draw_ellipse":
        c = _xy(p.get("center"))
        r = p.get("major_radius")
        end = _xy(p.get("major_axis_end"))
        if c and (r or end):
            r = float(r) if r else math.dist(c, end)
            yield Piece("arc", (c, r, 0.0, 2 * math.pi), tag, handle, layer)   # generous: the bounding circle
    elif intent == "draw_spline":
        pts = [q for q in (_xy(v) for v in p.get("points") or []) if q is not None]
        yield from _ring_pieces(pts, bool(p.get("closed")), tag, handle, layer)
    elif intent == "draw_point":
        for q in (_xy(v) for v in p.get("points") or []):
            if q is not None:
                yield Piece("pt", q, tag_for(layer, type="point"), handle, layer)
    elif intent in ("draw_text", "draw_mtext"):
        q = _xy(p.get("position") or p.get("insertion_point") or p.get("point"))
        if q is not None:
            yield Piece("pt", q, tag_for(layer, type="text"), handle, layer, str(p.get("text") or "")[:40])
    elif intent == "insert_block":
        q = _xy(p.get("position") or p.get("insertion_point") or p.get("point"))
        if q is not None:
            yield Piece("pt", q, tag_for(layer, name=p.get("name"), type="block"), handle, layer,
                        str(p.get("name") or ""))
    elif intent == "draw_leader":
        pts = [q for q in (_xy(v) for v in p.get("points") or p.get("vertices") or []) if q is not None]
        yield from _ring_pieces(pts, False, tag_for(layer, type="text"), handle, layer)


def proposed_pieces(proposed: dict, handle: str = "proposed", layer: str = ""):
    """A handler report's "proposed" block -- points, segments and boxes -- as
    pieces, so a compute-stage report is checked exactly like a draw payload."""
    tag = tag_for(layer) if layer else "label"
    for i, q in enumerate(proposed.get("points") or []):
        xy = _xy(q)
        if xy is not None:
            yield Piece("pt", xy, tag, f"{handle}/p{i}", layer)
    for i, seg in enumerate(proposed.get("segments") or []):
        try:
            a, b = _xy(seg[0]), _xy(seg[1])
        except (TypeError, IndexError):
            continue
        if a and b:
            yield Piece("seg", (a, b), tag, f"{handle}/s{i}", layer)
    for i, box in enumerate(proposed.get("boxes") or []):
        try:
            lo, hi = _xy(box["min"]), _xy(box["max"])
        except (TypeError, KeyError):
            continue
        if lo and hi:
            verts = [(lo[0], lo[1]), (hi[0], lo[1]), (hi[0], hi[1]), (lo[0], hi[1])]
            for piece in _ring_pieces(verts, True, tag, f"{handle}/b{i}", layer):
                yield piece


# ── Cells ─────────────────────────────────────────────────────────────────────


@dataclass
class Cell:
    key: tuple
    handles: dict = field(default_factory=lambda: defaultdict(set))   # tag -> {handle}
    length: dict = field(default_factory=lambda: defaultdict(float))  # tag -> clipped ft
    covered: dict = field(default_factory=lambda: defaultdict(float)) # tag -> area fraction
    points: dict = field(default_factory=lambda: defaultdict(int))    # tag -> point pieces inside
    tensor: list = field(default_factory=lambda: [0.0, 0.0, 0.0])     # sxx, sxy, syy
    vertices: set = field(default_factory=set)                        # rounded endpoints inside
    pieces: list = field(default_factory=list)                        # level 0 only
    clipped: list = field(default_factory=list)                       # level 0: (tag, a, b) inside the cell
    marks: list = field(default_factory=list)                         # level 0: (tag, p) point pieces inside
    states: dict = field(default_factory=lambda: defaultdict(Counter)) # tag -> Counter(state -> piece touches)
    frame: dict = None
    children: list = field(default_factory=list)
    hash: str = ""

    # -- derived, never stored ------------------------------------------------

    def count(self, tag) -> int:
        return len(self.handles.get(tag, ()))

    def tags(self) -> list:
        return sorted(t for t in set(self.handles) | set(self.length) | set(self.covered) | set(self.points)
                      if self.count(t) or self.length.get(t, 0) > 0 or self.covered.get(t, 0) > 0
                      or self.points.get(t, 0) > 0)

    def total_length(self) -> float:
        return sum(self.length.values())

    def total_points(self) -> int:
        return sum(self.points.values())

    def orientation(self):
        """(bearing degrees 0-180, anisotropy 0-1) from the structure tensor."""
        sxx, sxy, syy = self.tensor
        trace = sxx + syy
        if trace <= 1e-9:
            return None, 0.0
        angle = 0.5 * math.degrees(math.atan2(2 * sxy, sxx - syy)) % 180.0
        half = math.sqrt(((sxx - syy) / 2) ** 2 + sxy ** 2)
        lam1, lam2 = trace / 2 + half, trace / 2 - half
        return angle, (lam1 - lam2) / (lam1 + lam2) if (lam1 + lam2) > 0 else 0.0

    def all_handles(self) -> set:
        out = set()
        for s in self.handles.values():
            out |= s
        return out


# ── Index ─────────────────────────────────────────────────────────────────────


class SpatialIndex:
    def __init__(self, data: dict):
        self.cells: dict = {}
        self.entity_cells: dict = defaultdict(set)     # handle -> {level-0 key}
        self.entity_tag: dict = {}                     # handle -> tag
        self.tag_handles: dict = defaultdict(set)      # tag -> {handle}
        self.rings: dict = {}                          # handle -> (ring, tag) of closed shapes
        self.entity_state: dict = {}                   # handle -> proposed | existing | future ...
        self.family_tags: dict = defaultdict(set)      # family -> {tags present}
        self.layer_info: dict = {}                     # layer -> how it was named, feet, points
        self.windows: dict = {}                        # viewport handle -> {ring, layout, on}
        self.unmatched: Counter = Counter()            # layer -> ft of linework nothing could name
        self.unmatched_points: Counter = Counter()     # layer -> point pieces tagged by type only
        self.tags_version = TAGS.version
        self.alignments = self._prepare_alignments(data)
        self._insert(data)
        self._frames()
        self._aggregate()
        self._hash()
        self.regions = self._regions()
        self.site = self._site_frame()

    # -- build ----------------------------------------------------------------

    @staticmethod
    def _prepare_alignments(data):
        out = []
        for a in data.get("alignments", []) or []:
            pts = _alignment_points(a)
            if len(pts) >= 2:
                out.append({"name": str(a.get("name")), "points": pts,
                            "length": _centreline_length(pts)})
        return out

    def _cell(self, key) -> Cell:
        c = self.cells.get(key)
        if c is None:
            c = self.cells[key] = Cell(key)
        return c

    def _verdict(self, piece) -> tuple:
        """(state, source, confidence) for a piece, from its layer's words: what
        the classifier would say, without carrying it on every Piece."""
        d = TAGS.words.derive(piece.layer) if piece.layer else None
        state = d.state if d else "proposed"
        if piece.tag == "other":
            return state, "other", ""
        if d is not None and d.derivable and d.tag == piece.tag:
            return state, "derived", d.confidence
        if piece.tag in ("block", "text", "point", "dimension", "circle"):
            return state, "type", ""
        return state, "rule", ""

    def _record(self, piece, state, source, confidence, feet=0.0, points=0):
        self.entity_tag[piece.handle] = piece.tag
        self.tag_handles[piece.tag].add(piece.handle)
        self.entity_state[piece.handle] = state
        self.family_tags[TAGS.family_of(piece.tag)].add(piece.tag)
        info = self.layer_info.get(piece.layer)
        if info is None:
            info = self.layer_info[piece.layer] = {"tag": piece.tag, "family": TAGS.family_of(piece.tag),
                                                   "state": state, "source": source, "confidence": confidence,
                                                   "feet": 0.0, "points": 0, "handles": 0}
        info["feet"] += feet
        info["points"] += points
        info["handles"] += 1

    def _insert(self, data):
        for lay in data.get("layouts", []) or []:
            for vp in lay.get("viewports") or []:
                verts = _viewport_verts(vp)
                if verts:
                    self.windows[str(vp.get("handle"))] = {"ring": verts, "layout": str(lay.get("name") or ""),
                                                           "on": bool(vp.get("on", True))}
        for piece in _snapshot_pieces(data):
            state, source, confidence = self._verdict(piece)
            unverified = source == "other" or (source == "derived" and confidence == "none")
            if piece.kind == "pt":
                self._record(piece, state, source, confidence, points=1)
                key = key_at(*piece.geom)
                c = self._cell(key)
                c.handles[piece.tag].add(piece.handle)
                c.points[piece.tag] += 1
                c.states[piece.tag][state] += 1
                c.pieces.append(piece)
                c.marks.append((piece.tag, piece.geom))
                self.entity_cells[piece.handle].add(key)
                if unverified or (piece.layer and source == "type"):
                    self.unmatched_points[piece.layer] += 1
                continue
            self._record(piece, state, source, confidence, feet=piece.length())
            if unverified:
                self.unmatched[piece.layer] += piece.length()
            for a, b in piece.chords():
                seg = _v(a, b)
                length = math.hypot(seg[0], seg[1])
                if length < 1e-12:
                    continue
                ux, uy = seg[0] / length, seg[1] / length
                for key, clipped in _traverse(a, b):
                    if clipped <= 0:
                        continue
                    c = self._cell(key)
                    c.handles[piece.tag].add(piece.handle)
                    c.length[piece.tag] += clipped
                    c.tensor[0] += clipped * ux * ux
                    c.tensor[1] += clipped * ux * uy
                    c.tensor[2] += clipped * uy * uy
                    if not c.pieces or c.pieces[-1] is not piece:
                        c.pieces.append(piece)
                        c.states[piece.tag][state] += 1
                    span = _clip(a, b, bounds(key))
                    if span:
                        c.clipped.append((piece.tag, span[0], span[1]))
                    self.entity_cells[piece.handle].add(key)
            for p in piece.endpoints():
                self._cell(key_at(*p)).vertices.add((round(p[0], 3), round(p[1], 3)))

        for ring, tag, handle in _snapshot_polygons(data):
            self.rings[handle] = (ring, tag)
            self.tag_handles[tag].add(handle)
            self.entity_tag.setdefault(handle, tag)
            self.entity_state.setdefault(handle, "proposed")
            self.family_tags[TAGS.family_of(tag)].add(tag)
            xs, ys = [p[0] for p in ring], [p[1] for p in ring]
            _, cx0, cy0 = key_at(min(xs), min(ys))
            _, cx1, cy1 = key_at(max(xs), max(ys))
            s = BASE_SIZE
            for cx in range(cx0, cx1 + 1):
                for cy in range(cy0, cy1 + 1):
                    key = (0, cx, cy)
                    x0, y0 = ORIGIN[0] + cx * s, ORIGIN[1] + cy * s
                    inside = 0
                    for i in range(SUBSAMPLE):
                        for j in range(SUBSAMPLE):
                            p = (x0 + (i + 0.5) * s / SUBSAMPLE, y0 + (j + 0.5) * s / SUBSAMPLE)
                            if _point_in_polygon(p, ring):
                                inside += 1
                    if inside:
                        c = self._cell(key)
                        c.covered[tag] = min(1.0, c.covered[tag] + inside / (SUBSAMPLE ** 2))
                        c.handles[tag].add(handle)
                        self.entity_cells[handle].add(key)

        # First sight of a layer family -> layer_tags.learned.json. Never fatal:
        # a read-only disk or a race with another process must not cost a build.
        try:
            TAGS.flush_learned()
        except OSError:
            pass

    def _frames(self):
        if not self.alignments:
            return
        for key, cell in self.cells.items():
            if key[0] != 0:
                continue
            x0, y0, x1, y1 = bounds(key)
            probes = ((x0, y0), (x1, y0), (x1, y1), (x0, y1), ((x0 + x1) / 2, (y0 + y1) / 2))
            per = []
            for align in self.alignments:
                hits = [_signed_offset(align["points"], p) for p in probes]
                dist = [h[0] for h in hits]
                sta = [h[1] for h in hits]
                sides = {h[2] for h in hits}
                per.append({
                    "alignment": align["name"],
                    "offset": [round(min(dist), 2), round(max(dist), 2)],
                    "station": [round(min(sta), 2), round(max(sta), 2)],
                    "side": "mixed" if len(sides) > 1 else ("left" if 1.0 in sides else "right"),
                    "beyond_end": min(sta) <= 1e-6 or max(sta) >= align["length"] - 1e-6,
                })
            per.sort(key=lambda f: f["offset"][0])
            cell.frame = {"nearest": per[0], "second": per[1] if len(per) > 1 else None}

    def _aggregate(self):
        for level in range(1, LEVELS):
            groups = defaultdict(list)
            for key, cell in list(self.cells.items()):
                if key[0] == level - 1:
                    groups[parent_key(key)].append(cell)
            for pkey, kids in groups.items():
                parent = self._cell(pkey)
                parent.children = [k.key for k in kids]
                child_area = cell_size(level - 1) ** 2
                parent_area = cell_size(level) ** 2
                for k in kids:
                    for tag, hs in k.handles.items():
                        parent.handles[tag] |= hs
                    for tag, ln in k.length.items():
                        parent.length[tag] += ln
                    for tag, n in k.points.items():
                        parent.points[tag] += n
                    for tag, st in k.states.items():
                        parent.states[tag].update(st)
                    for tag, cov in k.covered.items():
                        parent.covered[tag] += cov * child_area / parent_area
                    for i in range(3):
                        parent.tensor[i] += k.tensor[i]
                    parent.vertices |= k.vertices
                parent.frame = self._envelope([k.frame for k in kids if k.frame])

    @staticmethod
    def _envelope(frames):
        """A parent's frame: the alignment most children name, its ranges unioned.
        Side is 'mixed' when children disagree -- the honest answer for a cell
        a centreline passes through."""
        if not frames:
            return None
        by_name = defaultdict(list)
        for f in frames:
            by_name[f["nearest"]["alignment"]].append(f["nearest"])
            if f.get("second"):
                by_name[f["second"]["alignment"]].append(f["second"])
        ranked = sorted(by_name.items(), key=lambda kv: -len(kv[1]))

        def fold(name, parts):
            sides = {p["side"] for p in parts}
            return {"alignment": name,
                    "offset": [min(p["offset"][0] for p in parts), max(p["offset"][1] for p in parts)],
                    "station": [min(p["station"][0] for p in parts), max(p["station"][1] for p in parts)],
                    "side": sides.pop() if len(sides) == 1 else "mixed",
                    "beyond_end": any(p["beyond_end"] for p in parts)}

        nearest = fold(*ranked[0])
        second = fold(*ranked[1]) if len(ranked) > 1 else None
        return {"nearest": nearest, "second": second}

    def _hash(self):
        for level in range(LEVELS):
            for key, cell in self.cells.items():
                if key[0] != level:
                    continue
                if level == 0:
                    # Over the geometry INSIDE the cell, not the whole entity:
                    # shortening a pad's far end must not change the hash of
                    # the cells its near end sits in.
                    text = "\n".join(sorted(
                        f"{t}|{a[0]:.3f},{a[1]:.3f}|{b[0]:.3f},{b[1]:.3f}"
                        for t, a, b in cell.clipped))
                    text += "\n" + "\n".join(sorted(f"{t}|pt|{p[0]:.3f},{p[1]:.3f}" for t, p in cell.marks))
                    text += "\n" + "\n".join(f"{t}:{v:.3f}" for t, v in sorted(cell.covered.items()))
                else:
                    text = "\n".join(sorted(self.cells[k].hash for k in cell.children))
                cell.hash = hashlib.sha1(text.encode()).hexdigest()[:16]

    # -- regions --------------------------------------------------------------

    def _regions(self):
        # Regions are assigned at LEVEL 0. A 200ft cell that touches an
        # alignment would count as corridor wholesale and swallow the lots
        # behind the curb; 50ft cells keep road and lot apart.
        level = 0
        cells = {k: c for k, c in self.cells.items() if k[0] == level}
        regions, member = {}, {}

        # Corridors come straight from the alignments: station range x offset band.
        for align in self.alignments:
            keys = [k for k, c in cells.items()
                    if c.frame and c.frame["nearest"]["alignment"] == align["name"]
                    and c.frame["nearest"]["offset"][0] <= CORRIDOR_HALF_WIDTH]
            if not keys:
                continue
            rid = f"{align['name']} corridor"
            regions[rid] = self._region(rid, "corridor", align["name"], keys)
            for k in keys:
                member[k] = rid

        # Blocks: connected runs of whatever occupied cells are left.
        remaining = [k for k in cells if k not in member]
        seen, n = set(), 0
        for start in sorted(remaining, key=lambda k: (-k[2], k[1])):    # north-west first
            if start in seen:
                continue
            comp, stack = [], [start]
            seen.add(start)
            while stack:
                k = stack.pop()
                comp.append(k)
                for nk in self._neighbours(k, diagonal=False):
                    if nk in cells and nk not in member and nk not in seen:
                        seen.add(nk)
                        stack.append(nk)
            n += 1
            rid = f"Block {n}"
            regions[rid] = self._region(rid, "block", None, comp)
            for k in comp:
                member[k] = rid

        # Relations: what each region touches.
        for rid, region in regions.items():
            touches = set()
            for k in region["cells"]:
                for nk in self._neighbours(k):
                    other = member.get(nk)
                    if other and other != rid:
                        touches.add(other)
            region["touches"] = sorted(touches)
        self._member = member
        return regions

    def _region(self, rid, kind, source, keys):
        handles = set()
        for k in keys:
            handles |= self.cells[k].all_handles()
        xs0 = [bounds(k)[0] for k in keys]; ys0 = [bounds(k)[1] for k in keys]
        xs1 = [bounds(k)[2] for k in keys]; ys1 = [bounds(k)[3] for k in keys]
        by_tag, length = defaultdict(set), Counter()
        for k in keys:
            c = self.cells[k]
            for t, hs in c.handles.items():
                by_tag[t] |= hs
            for t, ln in c.length.items():
                length[t] += ln
        counts = {t: len(hs) for t, hs in by_tag.items() if hs}
        return {"id": rid, "kind": kind, "source": source, "cells": sorted(keys),
                "handles": sorted(handles),
                "bounds": (min(xs0), min(ys0), max(xs1), max(ys1)),
                "count_by_tag": dict(counts), "length_by_tag": {t: round(v, 1) for t, v in length.items()},
                "touches": []}

    @staticmethod
    def _neighbours(key, diagonal=True):
        level, cx, cy = key
        out = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                if not diagonal and dx and dy:
                    continue
                out.append((level, cx + dx, cy + dy))
        return out

    def _site_frame(self):
        """Row/column presentation over the OCCUPIED extents. This is a view
        for rendering; keys never move with it."""
        level = 1 if LEVELS > 1 else 0
        keys = [k for k in self.cells if k[0] == level]
        if not keys:
            return {"level": level, "cols": 0, "rows": 0}
        cxs, cys = [k[1] for k in keys], [k[2] for k in keys]
        return {"level": level, "cx0": min(cxs), "cx1": max(cxs), "cy0": min(cys), "cy1": max(cys),
                "cols": max(cxs) - min(cxs) + 1, "rows": max(cys) - min(cys) + 1}

    # -- queries --------------------------------------------------------------

    def cell(self, key):
        return self.cells.get(key)

    def tags_of(self, word) -> set:
        """Every tag a word names: itself, or every member of the family it
        names (water -> water_main, water_hydrant ...)."""
        word = str(word or "").lower()
        out = set(self.family_tags.get(word, ()))
        if word in self.tag_handles:
            out.add(word)
        return out

    def handles_for(self, word, state=None) -> set:
        """Handles of a tag or a family, optionally only those in one state."""
        out = set()
        for t in self.tags_of(word):
            out |= self.tag_handles.get(t, set())
        if state:
            out = {h for h in out if self.entity_state.get(h, "proposed") == state}
        return out

    def _wanted(self, tag):
        """None (anything) or the set of tags a query word stands for; a word
        the index has never seen yields an empty set, so it matches nothing."""
        if tag is None or tag == "":
            return None
        return self.tags_of(tag)

    def pieces_for(self, handle) -> list:
        """Every piece of one entity, once each (a piece sits in every cell it crosses)."""
        seen, out = set(), []
        for key in self.entity_cells.get(str(handle), ()):
            cell = self.cells.get(key)
            if not cell:
                continue
            for piece in cell.pieces:
                if piece.handle == str(handle) and id(piece) not in seen:
                    seen.add(id(piece))
                    out.append(piece)
        return out

    def _region_of_key(self, key):
        """A cell's region; for a parent, the region most of its base cells are in."""
        if key[0] == 0:
            return self._member.get(key)
        hits, stack = Counter(), [key]
        while stack:
            k = stack.pop()
            c = self.cells.get(k)
            if not c:
                continue
            if k[0] == 0:
                r = self._member.get(k)
                if r:
                    hits[r] += 1
            else:
                stack.extend(c.children)
        return hits.most_common(1)[0][0] if hits else None

    def region_of(self, handle=None, point=None):
        if handle is not None:
            keys = self.entity_cells.get(str(handle), ())
            hits = Counter(self._member.get(k) for k in keys)
            hits.pop(None, None)
            return hits.most_common(1)[0][0] if hits else None
        if point is not None:
            return self._member.get(key_at(point[0], point[1]))
        return None

    def locate(self, x, y):
        key = key_at(x, y)
        cell = self.cells.get(key)
        neighbours = {}
        for name, (dx, dy) in (("N", (0, 1)), ("NE", (1, 1)), ("E", (1, 0)), ("SE", (1, -1)),
                               ("S", (0, -1)), ("SW", (-1, -1)), ("W", (-1, 0)), ("NW", (-1, 1))):
            nc = self.cells.get((0, key[1] + dx, key[2] + dy))
            neighbours[name] = nc.tags() if nc else []
        return {"key": key, "bounds": bounds(key), "empty": cell is None,
                "tags": cell.tags() if cell else [],
                "frame": cell.frame if cell else None,
                "region": self.region_of(point=(x, y)),
                "neighbours": neighbours}

    def _keys_in(self, bbox, level=0):
        x0, y0, x1, y1 = bbox
        _, cx0, cy0 = key_at(min(x0, x1), min(y0, y1), level)
        _, cx1, cy1 = key_at(max(x0, x1), max(y0, y1), level)
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                if (level, cx, cy) in self.cells:
                    yield (level, cx, cy)

    def entities(self, tag=None, bbox=None, point=None, radius=None, state=None):
        """Handles of entities of `tag` (a tag or a family) touching a bbox, or
        within `radius` of a point; `state` keeps only existing / proposed / ..."""
        wanted = self._wanted(tag)
        if point is not None and radius is not None:
            bbox = (point[0] - radius, point[1] - radius, point[0] + radius, point[1] + radius)
        if bbox is None:
            raise ValueError("entities() needs a bbox or a point and radius")
        out = set()
        box = (min(bbox[0], bbox[2]), min(bbox[1], bbox[3]), max(bbox[0], bbox[2]), max(bbox[1], bbox[3]))
        for key in self._keys_in(box):
            cell = self.cells[key]
            for piece in cell.pieces:
                if wanted is not None and piece.tag not in wanted:
                    continue
                if state and self.entity_state.get(piece.handle, "proposed") != state:
                    continue
                if piece.kind == "pt":
                    px, py = piece.geom
                    if radius is not None:
                        if math.dist(point, piece.geom) > radius:
                            continue
                    elif not (box[0] <= px <= box[2] and box[1] <= py <= box[3]):
                        continue
                    out.add(piece.handle)
                    continue
                chords = piece.chords()
                if radius is not None:
                    if min(_point_seg_distance(point, a, b)[0] for a, b in chords) > radius:
                        continue
                elif not any(_clip(a, b, box) for a, b in chords):
                    continue                     # in a touched cell, but outside the box
                out.add(piece.handle)
            if radius is None:
                # Closed shapes that COVER the cell without any edge in it (a pad
                # around a whole cell) have no pieces here; count them when the
                # cell's centre is inside the box.
                cx, cy = centre(key)
                if box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
                    for t, hs in cell.handles.items():
                        if wanted is None or t in wanted:
                            out |= {h for h in hs if not state or self.entity_state.get(h, "proposed") == state}
        return sorted(out)

    def nearest(self, tag, point, direction=None, max_distance=2000.0, state=None):
        """The nearest piece of `tag` (a tag or a family), optionally only looking
        along `direction` and only in one `state`. Returns (handle, hit point,
        distance) or None."""
        wanted = self._wanted(tag)
        if direction is not None:
            d = _norm(direction)
            far = _add(point, _scale(d, max_distance))
            best = None
            for key, _ in _traverse(point, far):
                cell = self.cells.get(key)
                if not cell:
                    continue
                for piece in cell.pieces:
                    if (wanted is not None and piece.tag not in wanted) or piece.kind == "pt":
                        continue
                    if state and self.entity_state.get(piece.handle, "proposed") != state:
                        continue
                    if piece.kind == "seg":
                        hit = _ray_segments(point, d, [piece.geom])
                        if hit and (best is None or hit[0] < best[2]):
                            best = (piece.handle, hit[1], hit[0])
                    else:
                        for t, p in _ray_circle(point, d, *piece.geom):
                            if best is None or t < best[2]:
                                best = (piece.handle, p, t)
                if best and best[2] <= math.dist(point, centre(key)) + cell_size(0):
                    break                       # nothing further along can be nearer
            return best

        ring, best = 0, None
        _, cx, cy = key_at(*point)
        while ring * BASE_SIZE <= max_distance:
            for dx in range(-ring, ring + 1):
                for dy in range(-ring, ring + 1):
                    if max(abs(dx), abs(dy)) != ring:
                        continue
                    cell = self.cells.get((0, cx + dx, cy + dy))
                    if not cell:
                        continue
                    for piece in cell.pieces:
                        if wanted is not None and piece.tag not in wanted:
                            continue
                        if state and self.entity_state.get(piece.handle, "proposed") != state:
                            continue
                        if piece.kind == "pt":
                            dist = math.dist(point, piece.geom)
                            if best is None or dist < best[2]:
                                best = (piece.handle, piece.geom, dist)
                            continue
                        for a, b in piece.chords():
                            dist, foot = _point_seg_distance(point, a, b)
                            if best is None or dist < best[2]:
                                best = (piece.handle, foot, dist)
            if best and best[2] <= ring * BASE_SIZE:
                return best
            ring += 1
        return best

    def between(self, a, b, tag=None, state=None):
        """Pieces the segment ab crosses in its interior: [(handle, tag, point)]."""
        return self._crossings_of(Piece("seg", (a, b), "query", "query", ""), tag, state=state)

    def _crossings_of(self, probe: Piece, tag=None, eps=1e-3, state=None):
        wanted = self._wanted(tag)
        out, seen = [], set()
        for a, b in probe.chords():
            seg = _v(a, b)
            length = math.hypot(seg[0], seg[1])
            if length < 1e-12:
                continue
            d = (seg[0] / length, seg[1] / length)
            for key, _ in _traverse(a, b):
                cell = self.cells.get(key)
                if not cell:
                    continue
                for piece in cell.pieces:
                    if (wanted is not None and piece.tag not in wanted) or piece.kind == "pt":
                        continue
                    if state and self.entity_state.get(piece.handle, "proposed") != state:
                        continue
                    if piece.kind == "seg":
                        hit = _ray_segments(a, d, [piece.geom])
                        hits = [(hit[0], hit[1])] if hit else []
                    else:
                        hits = _ray_circle(a, d, *piece.geom)
                    for t, p in hits:
                        if not (eps < t < length - eps):
                            continue                 # touches at an end are not crossings
                        if piece.kind == "seg":
                            pa, pb = piece.geom
                            u = _dot(_v(pa, p), _v(pa, pb)) / max(1e-12, _dot(_v(pa, pb), _v(pa, pb)))
                            if not (eps / max(1e-9, math.dist(pa, pb)) < u < 1 - eps / max(1e-9, math.dist(pa, pb))):
                                continue
                        sig = (piece.handle, round(p[0], 3), round(p[1], 3))
                        if sig in seen:
                            continue
                        seen.add(sig)
                        out.append((piece.handle, piece.tag, p))
        return out

    def crossings(self, payloads: list, tags=None):
        """The dry-run gate. Which proposed payloads cross existing geometry of
        the given tags (all tags if None), and where. Touching at an endpoint
        -- a fillet landing on the curb, a leg landing on a lot line -- is not
        a crossing."""
        out = []
        for index, piece in _payload_pieces(payloads):
            for t in (tags or [None]):
                for handle, tag, p in self._crossings_of(piece, t):
                    if tags is None or tag in tags:
                        out.append({"payload": index, "intent": payloads[index].get("intent"),
                                    "crosses": handle, "tag": tag,
                                    "at": [round(p[0], 3), round(p[1], 3)]})
        return out

    # -- the gate ---------------------------------------------------------------

    def planned_cells(self, payloads: list, handles=None) -> set:
        """Level-0 cells a set of payloads is expected to change: every cell the
        proposed geometry passes through, every cell the named handles occupy,
        and their immediate neighbours."""
        keys = set()
        for _, piece in _payload_pieces(payloads):
            if piece.kind == "pt":
                keys.add(key_at(*piece.geom))
                continue
            for a, b in piece.chords():
                for key, _ in _traverse(a, b):
                    keys.add(key)
        for h in handles or ():
            keys |= self.entity_cells.get(str(h), set())
        out = set(keys)
        for k in keys:
            out.update(self._neighbours(k))
        return out

    def inside(self, point, tags=None, state=None):
        """Closed shapes of `tags` (tags or families; default the vocabulary's
        'inside' conflict tags) that contain the point: [(handle, tag)]."""
        words = tags if tags is not None else (TAGS.conflicts.get("inside") or ["pad"])
        expanded = set()
        for w in words:
            expanded |= self.tags_of(w)
        cell = self.cells.get(key_at(*point))
        if cell is None:
            return []
        out = []
        for tag in expanded:
            for h in cell.handles.get(tag, ()):
                if state and self.entity_state.get(h, "proposed") != state:
                    continue
                ring = self.rings.get(h)
                if ring and _point_in_polygon(point, ring[0]):
                    out.append((h, tag))
        return out

    def conflicts(self, payloads: list, tags=None) -> list:
        """The dry-run gate over a list of payloads (draw_* intents, sequences,
        or {"proposed": ...} blocks from a handler report). Three kinds:
          crossing   a proposed line passes through existing linework of `tags`
                     (all tags when None); touching at an end is not a crossing;
          inside     a proposed point, or the middle of a proposed line, lands
                     inside a closed shape of an 'inside' tag (pads by default)
                     -- unless what is being drawn is that tag itself;
          proximity  a proposed point sits within the vocabulary's
                     proximity_clearance of linework (off when 0).
        Each conflict names the payload index, what it hits and where."""
        out = []
        clearance = float(TAGS.conflicts.get("proximity_clearance") or 0.0)
        inside_words = list(TAGS.conflicts.get("inside") or ["pad"])
        wanted_words = None if tags is None else set(t for t in tags)
        seen = set()
        for index, piece in _payload_pieces(payloads):
            if piece.tag == "viewport":
                continue                         # a sheet's window, not linework in the model
            intent = payloads[index].get("intent") if isinstance(payloads[index], dict) else None
            proposed_state = TAGS.words.derive(piece.layer).state if piece.layer else "proposed"

            def record(kind, handle, tag, at, **extra):
                out.append({"kind": kind, "payload": index, "intent": intent, "piece": piece.handle,
                            "hits": handle, "tag": tag, "family": TAGS.family_of(tag),
                            "state": self.entity_state.get(handle, "proposed"),
                            "proposed_state": proposed_state, "at": at, **extra})

            if piece.kind in ("seg", "arc"):
                for t in (tags or [None]):
                    for handle, tag, p in self._crossings_of(piece, t):
                        if wanted_words is not None and not TAGS.matches(tag, wanted_words):
                            continue
                        sig = ("crossing", index, handle, round(p[0], 2), round(p[1], 2))
                        if sig in seen:
                            continue
                        seen.add(sig)
                        record("crossing", handle, tag, [round(p[0], 3), round(p[1], 3)])
                probes = [_mid(ch) for ch in piece.chords()[:: max(1, len(piece.chords()) // 4)]]
            else:
                probes = [piece.geom]
            if TAGS.matches(piece.tag, inside_words):
                continue
            for q in probes:
                for handle, tag in self.inside(q, inside_words):
                    sig = ("inside", index, handle)
                    if sig in seen:
                        continue
                    seen.add(sig)
                    record("inside", handle, tag, [round(q[0], 3), round(q[1], 3)])
            if piece.kind == "pt" and clearance > 0:
                for h in self.entities(bbox=None, point=piece.geom, radius=clearance):
                    if h == piece.handle:
                        continue
                    tag = self.entity_tag.get(h, "other")
                    if wanted_words is not None and not TAGS.matches(tag, wanted_words):
                        continue
                    sig = ("proximity", index, h)
                    if sig in seen:
                        continue
                    seen.add(sig)
                    record("proximity", h, tag, [round(piece.geom[0], 3), round(piece.geom[1], 3)],
                           within=clearance)
        return out

    def verify(self, after: "SpatialIndex", payloads: list = None, handles=None, limit: int = 12) -> dict:
        """What a write did, in the words it was planned in. Compares this
        (pre-write) index with `after`: the cells that changed, described; the
        handles that appeared and vanished; and any changed cell OUTSIDE the
        cells the payloads were expected to touch -- a side effect nobody
        planned. `ok` is False only when such a cell exists."""
        d = self.diff(after)

        def has_content(index, k):
            c = index.cells.get(k)
            return bool(c and (c.tags() or c.covered))

        # A cell that only ever held a vertex (a line ending exactly on a cell
        # boundary) is not a change anyone can see.
        changed = sorted(k for k in set(d["added"]) | set(d["removed"]) | set(d["changed"])
                         if has_content(self, k) or has_content(after, k))
        expected = self.planned_cells(payloads or [], handles) if (payloads or handles) else None
        unexpected = [k for k in changed if expected is not None and k not in expected]
        before_h, after_h = set(self.entity_cells), set(after.entity_cells)
        new = sorted(h for h in after_h - before_h if not h.startswith("payload:"))
        gone = sorted(before_h - after_h)

        def words(keys):
            return [{"cell": list(k), "bounds": list(bounds(k)),
                     "before": self.describe(k), "after": after.describe(k)} for k in keys[:limit]]

        return {
            "ok": not unexpected,
            "verified": expected is not None,
            "changed_cells": len(changed),
            "changed": words(changed),
            "unexpected": words(unexpected),
            "new_handles": new[:200],
            "removed_handles": gone[:200],
            "note": (None if expected is not None else
                     "no geometry or handles in the request to check the change against; "
                     "changed cells are listed but not judged"),
        }

    def diff(self, other: "SpatialIndex"):
        """Level-0 cells added, removed or changed, found coarse-to-fine so an
        unchanged parent's children are never visited."""
        added, removed, changed = [], [], []

        def walk(level, keys_a, keys_b):
            for k in sorted(set(keys_a) | set(keys_b)):
                ca, cb = self.cells.get(k), other.cells.get(k)
                if ca is None:
                    (removed if level == 0 else None) is not None and removed.append(k)
                    if level > 0:
                        walk(level - 1, [], cb.children)
                elif cb is None:
                    if level == 0:
                        added.append(k)
                    else:
                        walk(level - 1, ca.children, [])
                elif ca.hash != cb.hash:
                    if level == 0:
                        changed.append(k)
                    else:
                        walk(level - 1, ca.children, cb.children)

        top = LEVELS - 1
        walk(top, [k for k in self.cells if k[0] == top], [k for k in other.cells if k[0] == top])
        return {"added": added, "removed": removed, "changed": changed}

    # -- rendering ------------------------------------------------------------

    @staticmethod
    def _station(value):
        return f"{int(value // 100)}+{value % 100:05.2f}"

    @staticmethod
    def _bearing_words(angle, anisotropy):
        if angle is None:
            return "no linework"
        if anisotropy < 0.4:
            return "no dominant direction"
        for name, centre_deg in (("E-W", 0), ("NE-SW", 45), ("N-S", 90), ("NW-SE", 135), ("E-W", 180)):
            if abs(angle - centre_deg) <= 22.5:
                return f"running {name}"
        return "running E-W"

    def _edge_words(self, cell, tag):
        """Where a tag's linework sits within the cell, as a compass edge."""
        pts = [_mid((a, b)) for t, a, b in cell.clipped if t == tag]
        if not pts:
            return ""
        cx, cy = centre(cell.key)
        mx = sum(p[0] for p in pts) / len(pts) - cx
        my = sum(p[1] for p in pts) / len(pts) - cy
        s = cell_size(cell.key[0])
        if max(abs(mx), abs(my)) < 0.25 * s:
            return "through the middle"
        ns = "north" if my > 0.25 * s else ("south" if my < -0.25 * s else "")
        ew = "east" if mx > 0.25 * s else ("west" if mx < -0.25 * s else "")
        return f"along the {(ns + ' ' + ew).strip()} edge"

    def _frame_words(self, frame):
        if not frame:
            return "no alignment nearby"
        n = frame["nearest"]
        sta = f"sta {self._station(n['station'][0])} to {self._station(n['station'][1])}"
        if n["beyond_end"]:
            sta += " (at the end)"
        side = {"left": "left side", "right": "right side", "mixed": "passes through"}[n["side"]]
        off = f"{n['offset'][0]:.0f} to {n['offset'][1]:.0f} ft off"
        return f"{n['alignment']}, {sta}, {side}, {off}"

    def describe(self, key) -> str:
        cell = self.cells.get(key)
        if cell is None:
            return f"cell {key}: empty"
        region = self._region_of_key(key)
        head = f"{region + ', ' if region else ''}{self._frame_words(cell.frame)}."
        parts = []
        pads = cell.count("pad")
        if pads:
            cov = cell.covered.get("pad", 0.0)
            parts.append(f"{pads} pad{'s' if pads != 1 else ''}" + (f" ({cov:.0%} covered)" if cov else ""))
        angle, aniso = cell.orientation()
        for tag in self._tag_order(cell):
            ln = cell.length.get(tag, 0.0)
            if ln <= 0:
                continue
            n = cell.count(tag)
            words = f"{self._state_prefix(cell, tag)}{tag.replace('_', ' ')} {ln:.0f} ft"
            if n > 1:
                words += f" ({n} pieces)"
            if key[0] == 0 and tag in ("curb", "lot_line", "driveway", "swale"):
                edge = self._edge_words(cell, tag)
                if edge:
                    words += f" {edge}"
            parts.append(words)
        # Things that sit at a point: labels, blocks, structures, text ...
        for tag in self._tag_order(cell):
            n = cell.points.get(tag, 0)
            if n <= 0:
                continue
            word = tag.replace("_", " ")
            parts.append(f"{n} {self._state_prefix(cell, tag)}{word}{'s' if n != 1 and not word.endswith('s') else ''}")
        body = ", ".join(parts) if parts else "nothing drawn"
        orient = self._bearing_words(angle, aniso)
        cont = ""
        if key[0] == 0 and cell.length.get("curb", 0) > 0:
            dirs = [d for d, (dx, dy) in (("E", (1, 0)), ("W", (-1, 0)), ("N", (0, 1)), ("S", (0, -1)))
                    if (0, key[1] + dx, key[2] + dy) in self.cells
                    and self.cells[(0, key[1] + dx, key[2] + dy)].length.get("curb", 0) > 0]
            if dirs:
                cont = f" Curb continues {' and '.join(dirs)}."
        tail = f"; linework {orient}." if angle is not None else "."
        return f"{head} {body[0].upper() + body[1:]}{tail}{cont}"

    @staticmethod
    def _state_prefix(cell, tag) -> str:
        """'existing ' / 'future ' when every piece of the tag in this cell
        shares one non-proposed state; '(existing/proposed) ' when mixed."""
        st = cell.states.get(tag)
        if not st:
            return ""
        kinds = sorted(k for k, n in st.items() if n)
        if len(kinds) == 1:
            return "" if kinds[0] == "proposed" else f"{TAGS.state_word(kinds[0])} "
        return "(" + "/".join(TAGS.state_word(k) for k in kinds) + ") "

    @staticmethod
    def _tag_order(cell) -> list:
        """Tags in the vocabulary's order, pads first, then families in the
        glyph priority, then the rest alphabetically, 'other' last, so a
        sentence reads the same way for every cell."""
        known = [t for t in TAGS.tags() if t != "pad"]
        present = [t for t in cell.tags() if t != "pad"]
        ordered = [t for t in known if t in present]
        rest = [t for t in present if t not in known and t != "other"]
        prio = {f: i for i, f in enumerate(TAGS.glyph_priority)}
        rest.sort(key=lambda t: (prio.get(TAGS.family_of(t), 99), TAGS.family_of(t), t))
        ordered += rest
        if "other" in present:
            ordered.append("other")
        return ordered

    def describe_region(self, rid) -> str:
        r = self.regions.get(rid)
        if not r:
            return f"{rid}: no such region"
        x0, y0, x1, y1 = r["bounds"]
        counts = ", ".join(f"{n} {t.replace('_', ' ')}{'s' if n != 1 and t == 'pad' else ''}"
                           for t, n in sorted(r["count_by_tag"].items()) if n)
        lengths = ", ".join(f"{t.replace('_', ' ')} {v:.0f} ft" for t, v in sorted(r["length_by_tag"].items()) if v)
        touch = f" Touches {', '.join(r['touches'])}." if r["touches"] else ""
        return (f"{rid} ({r['kind']}): {len(r['cells'])} cells, {x1 - x0:.0f} x {y1 - y0:.0f} ft. "
                f"{counts}. {lengths}.{touch}")

    # The map is bounded: a drawing nearly always has something far from the
    # site -- an xref inserted at the origin, a north arrow, a title block --
    # and a grid spanning the gap is millions of cells. The map shows the
    # main cluster of occupied cells and lists the rest as outliers.
    MAP_MAX_COLS = 120
    MAP_MAX_ROWS = 60
    MAP_GAP = 8          # cells of empty space that still count as "the same site"

    def _main_cluster(self, keys: list) -> tuple:
        """(cluster keys, outlier keys): the cluster grown from the fullest
        cell by repeatedly widening its bounding box by MAP_GAP cells and
        taking every occupied cell that falls inside."""
        if not keys:
            return [], []
        weight = {k: (self.cells[k].total_length() + 10 * self.cells[k].total_points()
                      + 100 * sum(self.cells[k].covered.values())) for k in keys}
        seed = max(keys, key=lambda k: weight[k])
        inside = {seed}
        x0 = x1 = seed[1]
        y0 = y1 = seed[2]
        while True:
            gx0, gx1, gy0, gy1 = x0 - self.MAP_GAP, x1 + self.MAP_GAP, y0 - self.MAP_GAP, y1 + self.MAP_GAP
            added = [k for k in keys if k not in inside and gx0 <= k[1] <= gx1 and gy0 <= k[2] <= gy1]
            if not added:
                break
            inside.update(added)
            x0, x1 = min(x0, min(k[1] for k in added)), max(x1, max(k[1] for k in added))
            y0, y1 = min(y0, min(k[2] for k in added)), max(y1, max(k[2] for k in added))
        outliers = [k for k in keys if k not in inside]
        return sorted(inside), sorted(outliers)

    def _outlier_words(self, keys: list, site_box: tuple, limit: int = 8) -> list:
        """One line per far-away cell: what it holds and where, relative to the site."""
        sx0, sy0, sx1, sy1 = site_box
        out = []
        for k in keys[:limit]:
            cell = self.cells[k]
            x0, y0, x1, y1 = bounds(k)
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            dx = sx0 - cx if cx < sx0 else (cx - sx1 if cx > sx1 else 0.0)
            dy = sy0 - cy if cy < sy0 else (cy - sy1 if cy > sy1 else 0.0)
            where = []
            if dx > 0:
                where.append(f"{dx:,.0f} ft {'west' if cx < sx0 else 'east'}")
            if dy > 0:
                where.append(f"{dy:,.0f} ft {'south' if cy < sy0 else 'north'}")
            what = []
            for tag in self._tag_order(cell) + (["pad"] if cell.count("pad") else []):
                n = cell.count(tag)
                names = sorted({p.name for p in self.cells.get(k, cell).pieces if p.tag == tag and p.name})[:2]
                what.append(f"{n} {tag.replace('_', ' ')}" + (f" ({', '.join(names)})" if names else ""))
            out.append(f"  {', '.join(what) or 'something'} at x={cx:,.0f}, y={cy:,.0f}"
                       f" ({' and '.join(where) + ' of' if where else 'beside'} the site)")
        if len(keys) > limit:
            out.append(f"  and {len(keys) - limit} more far-away cell(s)")
        return out

    def site_map(self, level=None) -> str:
        """The coarse grid as characters, north up, with a legend -- the main
        cluster of the drawing, never wider than MAP_MAX_COLS x MAP_MAX_ROWS.
        Occupied cells far from it are listed under the map, not drawn."""
        level = self.site["level"] if level is None else level
        keys = [k for k in self.cells if k[0] == level]
        if not keys:
            return "(empty)"
        cluster, outliers = self._main_cluster(keys)

        # Too big even for the cluster: go coarser, and if the top level is
        # still too wide, crop around the fullest cells and say so.
        cropped = False
        while True:
            cxs, cys = [k[1] for k in cluster], [k[2] for k in cluster]
            cx0, cx1, cy0, cy1 = min(cxs), max(cxs), min(cys), max(cys)
            cols, rows = cx1 - cx0 + 1, cy1 - cy0 + 1
            if cols <= self.MAP_MAX_COLS and rows <= self.MAP_MAX_ROWS:
                break
            if level + 1 < LEVELS:
                level += 1
                keys = [k for k in self.cells if k[0] == level]
                cluster, outliers = self._main_cluster(keys)
                continue
            # crop: keep the MAP_MAX window holding the most occupied cells
            mid_x = sorted(cxs)[len(cxs) // 2]
            mid_y = sorted(cys)[len(cys) // 2]
            cx0 = max(cx0, mid_x - self.MAP_MAX_COLS // 2)
            cy0 = max(cy0, mid_y - self.MAP_MAX_ROWS // 2)
            cx1, cy1 = cx0 + self.MAP_MAX_COLS - 1, cy0 + self.MAP_MAX_ROWS - 1
            outliers = outliers + [k for k in cluster if not (cx0 <= k[1] <= cx1 and cy0 <= k[2] <= cy1)]
            cluster = [k for k in cluster if cx0 <= k[1] <= cx1 and cy0 <= k[2] <= cy1]
            cropped = True
            break

        lines = []
        for cy in range(cy1, cy0 - 1, -1):
            row = []
            for cx in range(cx0, cx1 + 1):
                cell = self.cells.get((level, cx, cy))
                row.append(cell_glyph(cell) if cell else ".")
            lines.append("".join(row))
        s = cell_size(level)
        site_box = (ORIGIN[0] + cx0 * s, ORIGIN[1] + cy0 * s, ORIGIN[0] + (cx1 + 1) * s, ORIGIN[1] + (cy1 + 1) * s)
        head = (f"{len(lines)} rows x {cx1 - cx0 + 1} cols of {s:.0f} ft cells, north up; "
                f"column 0 is x={site_box[0]:,.0f}, top row is y={ORIGIN[1] + cy1 * s:,.0f}"
                + ("; cropped to the densest part of the site" if cropped else ""))
        tail = []
        if outliers:
            tail = ["", f"Not drawn, far from the site ({len(outliers)} cell(s)):"] + \
                   self._outlier_words(outliers, site_box)
        return "\n".join([head, map_legend(), ""] + lines + tail)

    def summary(self) -> str:
        lines = [f"{len([k for k in self.cells if k[0] == 0])} occupied cells at {BASE_SIZE:.0f} ft, "
                 f"{len(self.alignments)} alignments, {len(self.regions)} regions."]
        for rid in self.regions:
            lines.append("  " + self.describe_region(rid))
        lines.extend(self.unmatched_words())
        return "\n".join(lines)

    def unmatched_words(self, limit: int = 8) -> list:
        """The busiest layers no rule names, so 'other' is a prompt to name
        something (define_tag) rather than a place data disappears."""
        out = []
        top = self.unmatched.most_common(limit)
        if top:
            words = ", ".join(f"{layer or '(no layer)'} {ft:,.0f} ft" for layer, ft in top)
            more = len(self.unmatched) - len(top)
            out.append(f"Unnamed linework ('other'): {words}"
                       + (f", and {more} more layer{'s' if more != 1 else ''}" if more > 0 else "")
                       + ". Name a layer family with define_tag to see it on the map.")
        top_p = self.unmatched_points.most_common(limit)
        if top_p:
            words = ", ".join(f"layer '{layer}' x{n}" for layer, n in top_p)
            out.append(f"Point entities tagged by type only (block/text/point/dimension): {words}.")
        low = [(layer, info) for layer, info in self.layer_info.items()
               if info["source"] == "derived" and info["confidence"] == "none"]
        low.sort(key=lambda kv: -(kv[1]["feet"] + 10 * kv[1]["points"]))
        if low:
            words = ", ".join(f"{layer} -> {info['tag']}" + (f" {info['feet']:,.0f} ft" if info["feet"] else f" x{info['points']}")
                              for layer, info in low[:limit])
            out.append(f"Named from their words (unverified): {words}. Correct one with edit_learned, "
                       f"or teach an abbreviation with rename_word.")
        return out

    def derivation_stats(self, limit: int = 25) -> dict:
        """How the drawing's layers were named: counts by source and confidence,
        the unverified ones with their auto names, and the states seen."""
        by_source, by_conf, states = Counter(), Counter(), Counter()
        low = []
        for layer, info in self.layer_info.items():
            by_source[info["source"]] += 1
            if info["source"] == "derived":
                by_conf[info["confidence"]] += 1
            states[info["state"]] += 1
            if info["source"] == "derived" and info["confidence"] != "full":
                low.append({"layer": layer, "tag": info["tag"], "family": info["family"],
                            "confidence": info["confidence"], "feet": round(info["feet"], 1),
                            "points": info["points"]})
        low.sort(key=lambda r: -(r["feet"] + 10 * r["points"]))
        return {"layers": len(self.layer_info), "by_source": dict(by_source), "auto_by_confidence": dict(by_conf),
                "states": dict(states), "unverified": low[:limit]}


def build(data: dict) -> SpatialIndex:
    return SpatialIndex(data)
