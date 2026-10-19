"""The grammar: what a prompt is about, and the places it names.

    move all hydrants 20 ft north
    put a note 'SEE DETAIL' 10 ft north of the hydrant
    delete the labels inside the pad
    offset the water main 6 ft toward the road

A SUBJECT is the first kind phrase (words.kind_for) that no relation word
governs: "all hydrants", "the water main", "the labels". Its quantifier says
one or many: all/every/each/both (or a bare plural) means the set; the/this/
it (or a singular) means one. RELATION phrases bind the kind phrase after
them: "north of the hydrant", "inside the pad", "toward the road", "on layer
X". After the subject they are QUALIFIERS (they narrow the set); for a draw
tool, or with a distance in front ("10 ft north of"), they are PLACES.

Resolution is against the index, through words.find. The singular rule:
the anchor's pick when it is of that kind; else the nearest of the kind to
the anchor; else the only one in the drawing; else a reason ("2 hydrants in
the drawing: say which"). A kind with no match is a reason too ("no easement
found"). Reasons stop the step; nothing is guessed.
"""
import re
from dataclasses import dataclass, field

import spatial
import spatial_summary
from redline import tools

from . import geometry, measures, words

QUANT_ALL = ("all", "all the", "all of the", "every", "each", "both", "any", "everything", "the whole")
QUANT_ONE = ("the", "this", "that", "it", "its", "one", "a", "an", "one of the")
_DET = r"(?:all(?:\s+of)?(?:\s+the)?|every|each|both|any|the|this|that|these|those|its?|an?|one of the)"
_WORD = re.compile(r"[a-z][a-z\-/']*")
_LEN = rf"(?:(?:\d+(?:\.\d+)?|\.\d+)\s*(?:'(?!')|ft\b|feet\b|foot\b|\"|''|in\b|inch(?:es)?\b)?|\d+\s*'\s*-?\s*\d+(?:\.\d+)?\s*\")"
_DIRS = r"(?:north|south|east|west|northeast|northwest|southeast|southwest|ne|nw|se|sw|above|below|up|down)"

# relation -> regex over the lower-cased text; each match binds the kind phrase that follows it
_RELATIONS = [
    ("between", re.compile(r"\bbetween\s+(?:the\s+)?")),
    ("compass", re.compile(rf"(?:(?P<d>{_LEN})\s+)?(?P<dir>{_DIRS})\s+(?:of|from)\s+(?:the\s+)?")),
    ("side", re.compile(rf"(?:(?P<d>{_LEN})\s+)?(?:to\s+the\s+|on\s+the\s+)?(?P<dir>left|right)(?:\s+side)?\s+of\s+(?:the\s+)?")),
    ("side_bare", re.compile(rf"(?:(?P<d>{_LEN})\s+)?(?:to\s+the\s+|on\s+the\s+)(?P<dir>left|right)(?:\s+side)?\b(?!\s+of)")),
    ("toward", re.compile(rf"(?:(?P<d>{_LEN})\s+)?towards?\s+(?:the\s+)?")),
    ("away", re.compile(rf"(?:(?P<d>{_LEN})\s+)?away\s+from\s+(?:the\s+)?")),
    ("along", re.compile(rf"(?:(?P<d>{_LEN})\s+)?along\s+(?:the\s+)?")),
    ("within", re.compile(rf"within\s+(?P<d>{_LEN})\s+(?:of|from)\s+(?:the\s+)?")),
    ("from", re.compile(rf"(?P<d>{_LEN})\s+(?:from|off(?:\s+of)?)\s+(?:the\s+)?")),
    ("near", re.compile(r"(?:near|next\s+to|beside|adjacent\s+to|close\s+to|around|by)\s+(?:the\s+)?")),
    ("layer", re.compile(r"(?P<prep>on|to|onto|into)\s+(?:the\s+)?layer\s+[\"']?(?P<l>[^\s\"',.;]+)")),
    ("layer_the", re.compile(r"(?P<prep>on|to|onto|into)\s+the\s+(?P<l>[a-z0-9\-_|$. ]+?)\s+layer\b")),
    ("inside", re.compile(r"(?:inside|in|within)\s+(?!\d)(?:the\s+)?")),
    ("at", re.compile(r"(?:at|on|onto|over|to)\s+(?!layer\b)(?!the\s+(?:left|right)\b)(?:the\s+)?")),
    ("here", re.compile(r"\b(?:here|at\s+this\s+point|at\s+the\s+anchor|at\s+the\s+point)\b")),
]
_MODIFIERS = {"move_entities", "copy_entities", "rotate_entities", "scale_entities", "mirror_entities", "offset_entities",
              "delete_objects", "set_properties", "move_to_layer", "edit_text", "explode_entities", "join_entities",
              "trim_entities", "extend_entities", "break_entity", "lengthen_entity", "stretch_entities", "align_entities",
              "array_entities", "fillet_entities", "chamfer_entities", "edit_polyline", "set_draw_order", "group_entities"}
_PLACING = ("compass", "side", "side_bare", "toward", "away", "along", "from", "near", "at", "here", "inside")
_FILTERING = ("compass", "side", "side_bare", "within", "from", "near", "along", "inside", "between", "layer", "layer_the")


@dataclass
class Ref:
    phrase: str
    kind: words.KindQuery | None
    handles: list = field(default_factory=list)
    one: str | None = None
    source: str = "none"
    reason: str | None = None


@dataclass
class Qualifier:
    relation: str
    span: tuple
    refs: list = field(default_factory=list)      # [Ref]
    direction: str | None = None
    distance_ft: float | None = None
    layer: str | None = None
    ref_phrases: list = field(default_factory=list)
    destination: bool = False                      # "to the X layer": where things go, not a filter


# verbs and function words that the part-word dictionary could mistake for kinds
_VERBS = {"move", "copy", "rotate", "scale", "offset", "delete", "draw", "put", "add", "make", "change", "set", "place",
          "erase", "remove", "shift", "mirror", "extend", "trim", "label", "show", "hide", "find", "select", "fix",
          "note", "text"}
_NOUN_VERBS = {"label", "note", "text"}          # these are kinds too; only skipped as the first word


@dataclass
class Subject:
    phrase: str
    kind: words.KindQuery | None
    plural: bool
    handles: list = field(default_factory=list)
    one: str | None = None
    source: str = "none"               # pick | nearest | only | all | none
    qualifiers: list = field(default_factory=list)
    reason: str | None = None
    span: tuple = (0, 0)


# ── parsing ──────────────────────────────────────────────────────────────────

def _tokens(s: str) -> list:
    return [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(s)]


def noun_phrases(text: str, index=None) -> list:
    """[(phrase, start, end, determiner, KindQuery)] over the quote-stripped text."""
    s = measures.strip_quoted(text).lower()
    toks = _tokens(s)
    out, i = [], 0
    while i < len(toks):
        hit = None
        if toks[i][0] in _VERBS and (i == 0 or toks[i][0] not in _NOUN_VERBS):
            i += 1
            continue
        for n in (3, 2, 1):
            if i + n > len(toks):
                continue
            phrase = " ".join(t[0] for t in toks[i:i + n])
            kind = words.kind_for(phrase, index)
            if kind is not None:
                det = ""
                before = s[:toks[i][1]]
                m = re.search(rf"({_DET})\s*$", before)
                if m:
                    det = m.group(1)
                hit = (phrase, toks[i][1], toks[i + n - 1][2], det, kind, n)
                break
        if hit:
            out.append(hit[:5])
            i += hit[5]
        else:
            i += 1
    return out


def _distance(d: str | None) -> float | None:
    if not d:
        return None
    v = measures.to_feet(d.strip())
    if v is None:
        try:
            v = float(d.strip())
        except ValueError:
            v = None
    return v


def parse(text: str, index=None) -> tuple:
    """(Subject | None, [Qualifier]): the grammar only, nothing resolved."""
    s = measures.strip_quoted(text).lower()
    phrases = noun_phrases(text, index)
    quals, bound = [], set()
    for rel, rx in _RELATIONS:
        for m in rx.finditer(s):
            gd = m.groupdict()
            q = Qualifier(relation=rel, span=(m.start(), m.end()), direction=gd.get("dir"),
                          distance_ft=_distance(gd.get("d")), layer=gd.get("l"))
            if rel in ("layer", "layer_the"):
                q.relation = "layer"
                q.destination = str(gd.get("prep") or "on") != "on"
                quals.append(q)
                continue
            if rel == "here":
                quals.append(q)
                continue
            # the kind phrase that follows, within a few characters
            following = [p for p in phrases if p[1] >= m.end() and p[1] - m.end() <= 12]
            if rel == "between":
                if following:
                    first = following[0]
                    second = next((p for p in phrases if p[1] >= first[2]
                                   and re.fullmatch(r"\s*and\s+(?:the\s+)?", s[first[2]:p[1]])), None)
                    if second is not None:
                        q.ref_phrases = [first, second]
                        bound.update((first[1], second[1]))
                        quals.append(q)
                continue
            if not following:
                if rel == "side_bare":
                    q.relation = "side"
                    quals.append(q)
                continue
            if any(abs(q2.span[0] - m.start()) < 1 and q2.ref_phrases and q2.ref_phrases[0][1] == following[0][1] for q2 in quals):
                continue
            # a longer relation already claimed this phrase (compass beats at; within beats inside)
            if following[0][1] in bound:
                continue
            q.ref_phrases = [following[0]]
            bound.add(following[0][1])
            quals.append(q)
    quals.sort(key=lambda q: q.span[0])
    subject = None
    for phrase, start, end, det, kind in phrases:
        if start in bound:
            continue
        plural = kind.plural or kind.any
        if det.startswith("all") or det in ("every", "each", "both", "any"):
            plural = True
        elif det in ("the", "this", "that", "it", "its", "a", "an", "one", "one of the") and not kind.plural and not kind.any:
            plural = False
        subject = Subject(phrase=phrase, kind=kind, plural=bool(plural), span=(start, end))
        break
    if subject is not None:
        subject.qualifiers = [q for q in quals if q.span[0] > subject.span[1] and q.relation in _FILTERING
                              and not q.destination
                              and not (q.relation in ("from", "compass", "along", "side") and q.distance_ft is not None)]
    return subject, quals


# ── resolution ───────────────────────────────────────────────────────────────

def _lookup(req):
    if "lookup" not in req.cache:
        req.cache["lookup"] = spatial_summary.by_handle(req.data or {})
    return req.cache["lookup"]


def _noun(kind, plural: bool) -> str:
    if kind.any:
        return "objects" if plural else "object"
    return words.plural_of(kind.word) if plural else kind.word


def pick_one(index, data, kind, handles: list, anchor_xy, context: dict, lookup=None, qual_text: str = "") -> tuple:
    """(handle, source, reason): the singular rule."""
    pick = ((context or {}).get("pick") or {}).get("object") or {}
    ph = str(pick.get("handle") or "")
    if ph and ph in handles:
        return ph, "pick", None
    if not handles:
        return None, "none", f"no {_noun(kind, False)}{qual_text} found"
    if len(handles) == 1:
        return handles[0], "only", None
    if anchor_xy is not None:
        def dist(h):
            d = spatial_summary.distance_to(index, h, anchor_xy)
            return float("inf") if d is None else d
        return min(handles, key=dist), "nearest", None
    return None, "none", f"{len(handles)} {_noun(kind, True)} in the drawing: say which (give an anchor or a place)"


def _pointlike(index, handles) -> bool:
    return bool(handles) and all(all(p.kind == "pt" for p in index.pieces_for(h)) for h in handles)


def _resolve_ref(req, phrase_tuple, exclude_nested: bool = False, allow_many: bool = False) -> Ref:
    """A reference ("the road", "the hydrant"). Several matches stand
    together when the caller allows it (a filter measures to the nearest of
    them anyway) or when the kind is linework; several point objects with
    nothing to choose by stay ambiguous."""
    phrase, start, end, det, kind = phrase_tuple
    ref = Ref(phrase=phrase, kind=kind)
    if req.index is None:
        ref.reason = "no drawing index"
        return ref
    ref.handles = words.find(req.index, req.data, kind, _lookup(req), exclude_nested=exclude_nested)
    plural = kind.plural or kind.any or det.startswith("all") or det in ("every", "each", "both", "any")
    if plural:
        ref.one = None
        ref.source = "all" if ref.handles else "none"
        if not ref.handles:
            ref.reason = f"no {_noun(kind, True)} found"
        return ref
    one, source, reason = pick_one(req.index, req.data, kind, ref.handles, req.anchor_xy, req.context, _lookup(req))
    if one is None and len(ref.handles) > 1 and (allow_many or not _pointlike(req.index, ref.handles)):
        ref.source = "all"
        return ref
    ref.one, ref.source, ref.reason = one, source, reason
    if one:
        ref.handles = [one]
    return ref


def _qual_text(q: Qualifier) -> str:
    refs = " and ".join(r.phrase for r in q.refs) if q.refs else ""
    if q.relation == "compass":
        return f" {q.direction} of the {refs}"
    if q.relation == "side":
        return f" {q.direction} of the {refs}" if refs else f" on the {q.direction}"
    if q.relation == "within":
        return f" within {q.distance_ft:g} ft of the {refs}"
    if q.relation == "layer":
        return f" on layer {q.layer}"
    return f" {q.relation} the {refs}"


def apply_qualifier(index, data, handles: list, q: Qualifier, anchor_xy, lookup=None) -> tuple:
    """(handles kept, reason|None)."""
    rep = {h: geometry.rep_point(index, data, h, lookup) for h in handles}
    refs = [h for r in q.refs for h in r.handles]
    exclude = set(refs)
    keep = []
    if q.relation == "layer":
        want = str(q.layer or "").lower()
        lookup = lookup if lookup is not None else spatial_summary.by_handle(data or {})
        for h in handles:
            raw = (lookup.get(h) or ("", {}))[1]
            lay = str(raw.get("layer") or "").lower()
            if lay == want or lay.rsplit("|", 1)[-1] == want:
                keep.append(h)
        return keep, None
    if q.relation == "side" and not refs:
        for h in handles:
            p = rep.get(h)
            hit = geometry.nearest_alignment(index, p) if p else None
            if hit and ((hit[3] > 0) == str(q.direction).startswith("l")):
                keep.append(h)
        return keep, None
    if not refs:
        return handles, None
    for h in handles:
        if h in exclude:
            continue
        p = rep.get(h)
        if p is None:
            continue
        ok = False
        if q.relation == "compass":
            vec = geometry.compass_vector(q.direction)
            nf = geometry._nearest_foot(index, p, refs)
            if vec and nf:
                ok = spatial._dot(spatial._v(nf[1], p), vec) > 1e-6
        elif q.relation == "side":
            road = any(index.entity_tag.get(r) in geometry.ROAD_TAGS for r in refs)
            if road and geometry.nearest_alignment(index, p):
                ok = (geometry.nearest_alignment(index, p)[3] > 0) == str(q.direction).startswith("l")
            else:
                nf = geometry._nearest_foot(index, p, refs)
                u = geometry.direction_at(index, nf[2], p) if nf else None
                if u:
                    cross = u[0] * (p[1] - nf[1][1]) - u[1] * (p[0] - nf[1][0])
                    ok = (cross > 0) == str(q.direction).startswith("l")
                elif nf:
                    ok = (p[0] < nf[1][0]) == str(q.direction).startswith("l")
        elif q.relation in ("within", "from", "near", "along"):
            limit = q.distance_ft if q.distance_ft is not None else (geometry.ALONG_FT if q.relation == "along" else geometry.NEAR_FT)
            ds = []
            for r in refs:
                # entity to reference, measured both ways so a long line counts by
                # its nearest point, not its midpoint
                ds.append(spatial_summary.distance_to(index, r, p))
                rp = geometry.rep_point(index, data, r, lookup)
                if rp is not None:
                    ds.append(spatial_summary.distance_to(index, h, rp))
            ds = [d for d in ds if d is not None]
            ok = bool(ds) and min(ds) <= limit + 1e-6
        elif q.relation == "inside":
            for r in refs:
                ring = index.rings.get(r)
                if ring:
                    ok = ok or spatial._point_in_polygon(p, ring[0])
                else:
                    s = spatial_summary.summarize(data or {}, index, r, lookup) or {}
                    g = s.get("geometry") or {}
                    if s.get("type") == "circle" and g.get("center") and g.get("radius"):
                        ok = ok or (spatial._dot(spatial._v(g["center"], p), spatial._v(g["center"], p)) <= float(g["radius"]) ** 2)
                    else:
                        return [], f"the {q.refs[0].phrase if q.refs else 'reference'} is not a closed shape"
        elif q.relation == "between" and len(q.refs) >= 2:
            a = geometry._nearest_foot(index, p, q.refs[0].handles)
            b = geometry._nearest_foot(index, p, q.refs[1].handles)
            if a and b:
                ok = spatial._dot(spatial._v(a[1], p), spatial._v(b[1], p)) <= 1e-6
        if ok:
            keep.append(h)
    return keep, None


def resolve(req) -> Subject | None:
    """The subject of req.text against the drawing, memoised per step."""
    if "subject" in req.cache:
        return req.cache["subject"]
    subject, quals = parse(req.text, req.index)
    if subject is None or req.index is None:
        req.cache["subject"] = subject
        return subject
    exclude_nested = req.tool in _MODIFIERS
    handles = words.find(req.index, req.data, subject.kind, _lookup(req), exclude_nested=exclude_nested)
    qual_text = ""
    for q in subject.qualifiers:
        q.refs = [_resolve_ref(req, pt, allow_many=True) for pt in q.ref_phrases]
        bad = [r for r in q.refs if r.reason]
        if bad:
            subject.reason = bad[0].reason
            subject.handles = []
            req.cache["subject"] = subject
            return subject
        handles, reason = apply_qualifier(req.index, req.data, handles, q, req.anchor_xy, _lookup(req))
        if reason:
            subject.reason = reason
            req.cache["subject"] = subject
            return subject
        qual_text += _qual_text(q)
    if subject.plural:
        subject.handles = handles
        subject.source = "all" if handles else "none"
        if not handles:
            subject.reason = f"no {_noun(subject.kind, True)}{qual_text} found"
    else:
        one, source, reason = pick_one(req.index, req.data, subject.kind, handles, req.anchor_xy, req.context, _lookup(req), qual_text)
        subject.one, subject.source, subject.reason = one, source, reason
        subject.handles = [one] if one else []
    req.cache["subject"] = subject
    return subject


def references(req) -> list:
    """Every place-naming relation phrase with its reference(s) resolved
    (xref material allowed), memoised per step."""
    if "refs" in req.cache:
        return req.cache["refs"]
    subject, quals = parse(req.text, req.index)
    out = []
    modifies = req.tool in _MODIFIERS
    for q in quals:
        if q.relation not in _PLACING and q.relation != "between":
            continue
        # for a tool that changes existing things, a relation after the subject
        # narrows the subject; for a tool that draws, it says where
        if modifies and subject is not None and q in subject.qualifiers:
            continue
        if q.ref_phrases and req.index is not None:
            q.refs = [_resolve_ref(req, pt) for pt in q.ref_phrases]
        out.append(q)
    req.cache["refs"] = out
    return out


def target_kind(req) -> words.KindQuery | None:
    """What a lookup step should look for: the subject when the sequence's
    write takes handles, else the first place reference's kind (a draw tool
    creates its subject; the lookup is for where)."""
    subject, quals = parse(req.text, req.index)
    write = next((t for t in req.sequence if tools.is_write(t)), None)
    takes_handles = write is None or write in _MODIFIERS
    if takes_handles and subject is not None:
        return subject.kind
    for q in quals:
        if q.ref_phrases and q.relation in _PLACING:
            return q.ref_phrases[0][4]
    return subject.kind if subject is not None else None
