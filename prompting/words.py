"""The words of a prompt -> the things in the drawing they name.

A noun phrase ("hydrant", "water main", "lot line", "text") becomes a
KindQuery: the tags, families, type words, names and groups an entity may
carry to count as that kind. There is no reverse map from English to tags in
the vocabulary, so this holds an alias table, falls back to composing a
family word and a part word through the layer-word dictionary (water +
hydrant -> water_hydrant, the way derived tags are built), and finally
accepts a literal tag the index knows.

matches() and find() test entities against a KindQuery; resolve_layer() turns
typed words into a layer name the drawing really has.

Pure: nothing here imports cad_mcp_server or calls a tool.
"""
import re
from dataclasses import dataclass, field

import layer_words
import spatial
import spatial_summary


@dataclass
class KindQuery:
    word: str                              # the phrase as understood, singular
    plural: bool = False
    tags: set = field(default_factory=set)
    families: set = field(default_factory=set)
    tag_suffixes: tuple = ()
    types: set = field(default_factory=set)      # spatial_summary type words
    names: re.Pattern | None = None              # on names / part types / text
    groups: set = field(default_factory=set)     # Piece.group (pipe|structure|fitting|appurtenance)
    layer: str | None = None
    any: bool = False                            # everything (viewports excepted)

    def describe(self) -> str:
        return self.word


# key -> fields. Keys are singular and lower-case; multi-word keys are tried first.
ALIASES = {
    "hydrant": {"suffixes": ("_hydrant", "_fire_hydrant", "_fhyd"), "names": r"hydrant|\bfh\b|fhyd"},
    "fire hydrant": "hydrant", "fh": "hydrant", "fhyd": "hydrant", "hyd": "hydrant",
    "valve": {"suffixes": ("_valve", "_valv"), "names": r"valve"},
    "fitting": {"tags": {"fitting"}, "types": {"fitting"}, "groups": {"fitting"}},
    "appurtenance": {"tags": {"appurtenance"}, "types": {"appurtenance"}, "groups": {"appurtenance"}},
    "manhole": {"suffixes": ("_manhole", "_mhol"), "names": r"manhole|\bmh\b"},
    "mh": "manhole",
    "inlet": {"suffixes": ("_inlet", "_inlt"), "names": r"inlet"},
    "structure": {"tags": {"structure"}, "types": {"structure"}, "groups": {"structure"}},
    "pipe": {"tags": {"pipe"}, "families": {"pipe"}, "suffixes": ("_pipe",), "types": {"pipe", "pressure_pipe"}, "groups": {"pipe"}},
    "water main": {"tags": {"water_main", "water_line"}, "suffixes": ("_main",), "prefix": "water_"},
    "main": "water main", "water line": "water main", "waterline": "water main", "wm": "water main",
    "water": {"families": {"water"}},
    "sewer": {"families": {"sanitary", "storm"}},
    "sanitary": {"families": {"sanitary"}}, "sanitary sewer": "sanitary", "sanitary line": "sanitary", "ss": "sanitary",
    "storm": {"families": {"storm"}}, "storm sewer": "storm", "storm drain": "storm", "sd": "storm",
    "gas": {"families": {"gas"}}, "electric": {"families": {"electric"}}, "telephone": {"families": {"telephone"}},
    "cable": {"families": {"cable"}},
    "road": {"tags": {"curb", "centreline", "right_of_way", "alignment"}, "families": {"road"}, "types": {"alignment"}},
    "street": "road", "roadway": "road",
    "curb": {"tags": {"curb"}}, "back of curb": "curb", "edge of pavement": "curb",
    "centreline": {"tags": {"centreline", "alignment"}, "types": {"alignment"}}, "centerline": "centreline", "cl": "centreline",
    "alignment": {"tags": {"alignment"}, "types": {"alignment"}},
    "lot line": {"tags": {"lot_line"}}, "property line": "lot line",
    "lot": {"tags": {"lot_line", "parcel"}, "families": {"lots"}, "types": {"parcel"}}, "parcel": "lot",
    "pad": {"tags": {"pad"}, "families": {"building"}}, "building pad": "pad", "building": "pad",
    "easement": {"tags": {"easement"}, "families": {"easement"}},
    "right of way": {"tags": {"right_of_way"}}, "row": "right of way", "r/w": "right of way",
    "setback": {"tags": {"setback"}},
    "boundary": {"tags": {"boundary"}, "families": {"boundary"}},
    "driveway": {"tags": {"driveway"}}, "drive": "driveway",
    "swale": {"tags": {"swale"}},
    "contour": {"tags": {"contour"}, "families": {"contour"}},
    "sidewalk": {"families": {"sidewalk"}, "suffixes": ("_sidewalk", "_swlk", "_walk")}, "walk": "sidewalk",
    "text": {"tags": {"text"}, "types": {"text"}}, "note": "text", "mtext": "text", "callout": "text", "annotation": "text",
    "label": {"tags": {"label"}, "types": {"label"}},
    "block": {"tags": {"block"}, "types": {"block"}}, "symbol": "block",
    "line": {"types": {"line"}},
    "polyline": {"types": {"polyline"}}, "pline": "polyline",
    "circle": {"types": {"circle"}},
    "arc": {"types": {"arc"}},
    "point": {"types": {"point", "cogo_point"}}, "cogo point": "point",
    "dimension": {"types": {"dimension"}}, "dim": "dimension",
    "object": {"any": True}, "entity": "object", "thing": "object", "everything": "object", "anything": "object",
}

_NO_SINGULAR = {"gas", "ss", "sd", "cl", "mh", "fh", "row", "this", "its", "bus"}
_LAYER_TAIL = re.compile(r"^(?:the\s+)?(.*?)(?:'s)?\s*(?:layer)?$", re.I)


def singular(word: str) -> str:
    w = str(word or "").strip().lower()
    if not w or w in _NO_SINGULAR or len(w) < 4 and not w.endswith("s"):
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    for tail in ("sses", "ches", "shes", "xes"):
        if w.endswith(tail):
            return w[:-2]
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def plural_of(word: str) -> str:
    w = str(word or "")
    if not w:
        return w
    if w.endswith("y") and w[-2:-1] not in "aeiou":
        return w[:-1] + "ies"
    if w.endswith(("s", "x", "ch", "sh")):
        return w + "es"
    return w + "s"


def _alias(key: str) -> dict | None:
    v = ALIASES.get(key)
    seen = set()
    while isinstance(v, str) and v not in seen:
        seen.add(v)
        v = ALIASES.get(v)
    return v if isinstance(v, dict) else None


_REVERSE: dict = {"version": None, "majors": {}, "minors": {}}


def _reverse_words():
    """word -> tag key (majors) and word -> slug (minors), rebuilt when the
    dictionary changes. "sanitary sewer" also maps from "sanitary"."""
    words = spatial.TAGS.words
    if _REVERSE["version"] == (words.version, len(words.majors), len(words.minors)):
        return _REVERSE["majors"], _REVERSE["minors"]
    majors, minors = {}, {}
    for abbr, v in words.majors.items():
        word, tag = str(v.get("word") or abbr).lower(), str(v.get("tag") or abbr)
        majors.setdefault(word, tag)
        majors.setdefault(singular(word), tag)
        first = word.split()[0] if word.split() else word
        majors.setdefault(first, tag)
        majors.setdefault(abbr, tag)
    for abbr, word in words.minors.items():
        w = str(word).lower()
        minors.setdefault(w, layer_words._slug(w))
        minors.setdefault(singular(w), layer_words._slug(w))
        minors.setdefault(abbr, layer_words._slug(w))
    _REVERSE.update(version=(words.version, len(words.majors), len(words.minors)), majors=majors, minors=minors)
    return majors, minors


def kind_for(phrase: str, index=None) -> KindQuery | None:
    """The kind a noun phrase names, or None when it is not one."""
    raw = re.sub(r"\s+", " ", str(phrase or "").strip().lower().strip(",.;:"))
    if not raw:
        return None
    toks = raw.split(" ")
    sing_last = " ".join(toks[:-1] + [singular(toks[-1])])
    plural = sing_last != raw
    for key in (raw, sing_last):
        spec = _alias(key)
        if spec is not None:
            k = KindQuery(word=key, plural=plural)
            k.tags = set(spec.get("tags") or ())
            k.families = set(spec.get("families") or ())
            k.tag_suffixes = tuple(spec.get("suffixes") or ())
            k.types = set(spec.get("types") or ())
            k.groups = set(spec.get("groups") or ())
            k.any = bool(spec.get("any"))
            if spec.get("names"):
                k.names = re.compile(spec["names"], re.I)
            if spec.get("prefix"):
                # "water main": the drawing's tags that start with water_ and end
                # in _main / _line, else the usual derived names
                prefix = spec["prefix"]
                if index is not None:
                    k.tags |= {t for t in index.tag_handles if t.startswith(prefix) and any(t.endswith(s) for s in k.tag_suffixes)}
                k.tag_suffixes = ()
            return k
    majors, minors = _reverse_words()
    stoks = [singular(t) for t in toks]
    if len(stoks) == 2 and stoks[0] in majors and stoks[1] in minors:
        return KindQuery(word=sing_last, plural=plural, tags={f"{majors[stoks[0]]}_{minors[stoks[1]]}"})
    if len(stoks) == 1 and stoks[0] in majors:
        return KindQuery(word=sing_last, plural=plural, families={majors[stoks[0]]})
    if len(stoks) == 1 and stoks[0] in minors and stoks[0] not in ("line", "back", "center"):
        # a lone part word ("service", "valve") is a kind only when the drawing has such a tag
        suffix = f"_{minors[stoks[0]]}"
        if index is not None and any(t.endswith(suffix) for t in index.tag_handles):
            return KindQuery(word=sing_last, plural=plural, tag_suffixes=(suffix,))
        return None
    if index is not None:
        literal = sing_last.replace(" ", "_")
        if literal in index.tag_handles:
            return KindQuery(word=sing_last, plural=plural, tags={literal})
        literal = raw.replace(" ", "_")
        if literal in index.tag_handles:
            return KindQuery(word=raw, plural=False, tags={literal})
    return None


def _family_hit(index, tag: str, families: set) -> bool:
    if not families or not tag:
        return False
    fam = spatial.TAGS.family_of(tag)
    if fam in families:
        return True
    for f in families:
        if tag in index.tags_of(f) or tag in (index.family_tags.get(f) or ()) or tag.startswith(f + "_"):
            return True
    return False


def matches(index, data, handle: str, kind: KindQuery, lookup: dict | None = None) -> bool:
    h = str(handle)
    tag = str(index.entity_tag.get(h) or "")
    if tag == "viewport":
        return False
    if kind.any:
        return True
    if tag in kind.tags or _family_hit(index, tag, kind.families):
        return True
    if any(tag.endswith(s) for s in kind.tag_suffixes):
        return True
    lookup = lookup if lookup is not None else spatial_summary.by_handle(data or {})
    hit = lookup.get(h)
    type_word, raw = (hit if hit else ("", {}))
    if type_word and type_word in kind.types:
        return True
    if kind.names is not None:
        for v in (raw.get("name"), raw.get("part_type"), raw.get("description"), raw.get("text")):
            if v and kind.names.search(str(v)):
                return True
        for p in index.pieces_for(h):
            if p.name and kind.names.search(str(p.name)):
                return True
    if kind.groups:
        for p in index.pieces_for(h):
            if p.group in kind.groups:
                return True
    return False


def find(index, data, kind: KindQuery, lookup: dict | None = None, exclude_nested: bool = False) -> list:
    """Sorted handles of the drawing's entities of this kind; viewports never,
    xref (nested) material only when wanted (it can be a reference, not a
    thing to move or delete)."""
    lookup = lookup if lookup is not None else spatial_summary.by_handle(data or {})
    out = []
    for h in index.entity_tag:
        if exclude_nested:
            hit = lookup.get(h)
            if hit and hit[1].get("source") == "nested":
                continue
        if matches(index, data, h, kind, lookup):
            out.append(h)
    return sorted(out)


def layer_names(index, data) -> list:
    names = []
    for l in (data or {}).get("layers") or []:
        n = l.get("name") if isinstance(l, dict) else l
        if n:
            names.append(str(n))
    for n in (getattr(index, "layer_info", None) or {}):
        if n and n not in names:
            names.append(str(n))
    return names


def resolve_layer(index, data, words: str) -> str | None:
    """The real layer name typed words mean: exact, after the xref '|',
    suffix / contained (shortest native name first), or the layer of a kind
    ("the water main layer"). None when nothing matches."""
    w = str(words or "").strip().strip("\"'").strip()
    m = _LAYER_TAIL.match(w)
    w = (m.group(1) if m else w).strip().lower()
    if not w:
        return None
    names = layer_names(index, data)
    low = {n.lower(): n for n in names}
    if w in low:
        return low[w]
    for n in names:
        if n.lower().rsplit("|", 1)[-1] == w:
            return n
    hits = [n for n in names if n.lower().endswith(w) or w in n.lower()]
    if hits:
        hits.sort(key=lambda n: ("|" in n, len(n)))
        return hits[0]
    kind = kind_for(w, index)
    if kind is not None:
        info = getattr(index, "layer_info", None) or {}
        best, score = None, -1.0
        for name, rec in info.items():
            tag = str(rec.get("tag") or "")
            ok = tag in kind.tags or any(tag.endswith(s) for s in kind.tag_suffixes) or \
                _family_hit(index, tag, kind.families) or str(rec.get("family") or "") in kind.families
            if not ok:
                continue
            s = float(rec.get("feet") or 0) + float(rec.get("handles") or 0)
            if s > score:
                best, score = name, s
        if best is not None:
            return low.get(str(best).lower(), str(best))
    return None
