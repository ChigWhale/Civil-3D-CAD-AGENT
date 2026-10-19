"""Per-tool helpers that fill parameters from what code can see.

A helper is a plain function `fn(req, so_far) -> Filled | None` registered
for one or more tool names with @helper(...). It reads the Request (the
prompt text, the anchor and its digest, the spatial index and snapshot,
the lookups' results so far, the handles a parameter may name, the tool's
schema and what prefill already knows) and returns the parameters it is
sure of. Helpers run in registration order, "*" helpers first, each seeing
what the earlier ones filled (`so_far`).

Filled.grounds are handles the helper read from the index; the planner
admits them to the set a parameter may name. Filled.refuse is a plain
reason the step cannot be filled ("no hydrant found", "2 hydrants in the
drawing: say which"): the step stops there and the model is never asked.
A tool's parameters are `complete` (no model call) when SATISFIED says so.

Four modules do the reading: measures (quantities), words (a noun phrase ->
the kind of thing), subjects (the grammar: what the prompt is about, the
places it names), geometry (vectors and points over the index). The lookups
in the sequence (object_at, nearby, find_entities, describe_location) remain
the live reads; the helpers work from the index the dashboard already holds.

Pure code: nothing here imports cad_mcp_server or calls a tool.
"""
import re
from dataclasses import dataclass, field

from redline.fill import ACI, HANDLE_KEYS

from . import geometry, measures, subjects, words


@dataclass
class Request:
    tool: str
    schema: dict                                  # tools.schema(tool): dashboard removed
    text: str                                     # the prompt as typed
    anchor_xy: list | None = None                 # model feet, or None
    context: dict = field(default_factory=dict)   # locate.comment_context digest, or {}
    index: object = None                          # spatial index (SpatialIndex) or None
    data: dict | None = None                      # the model-space snapshot the index was built from
    sequence: list = field(default_factory=list)  # every tool name of the plan
    position: int = 0                             # this step's index in the sequence
    results: list = field(default_factory=list)   # [{"tool", "preview", "created"}] of the steps so far
    known: set = field(default_factory=set)       # handles a parameter may name
    prefilled: dict = field(default_factory=dict) # what fill.prefill knew
    cache: dict = field(default_factory=dict)     # per-step memo (the resolved subject, the references)

    @property
    def props(self) -> dict:
        return (self.schema or {}).get("properties") or {}

    @property
    def required(self) -> list:
        return list((self.schema or {}).get("required") or [])


@dataclass
class Filled:
    params: dict = field(default_factory=dict)
    complete: bool = False       # nothing left for a model to decide
    note: str = ""               # one line for the plan card ("offset from '20 ft north'")
    grounds: set = field(default_factory=set)   # handles read from the drawing, now nameable
    refuse: str = ""             # the step cannot be filled; the reason, for the card


_HELPERS: dict = {}


def helper(*tool_names):
    """Registers the decorated function for the given tools ("*" = every tool)."""
    def deco(fn):
        for name in tool_names:
            _HELPERS.setdefault(name, []).append(fn)
        return fn
    return deco


def helpers_for(tool: str) -> list:
    return list(_HELPERS.get("*", [])) + list(_HELPERS.get(tool, []))


def required_missing(schema: dict, params: dict) -> list:
    return [k for k in ((schema or {}).get("required") or []) if (params or {}).get(k) is None]


def _has(p: dict, *keys) -> bool:
    return all(p.get(k) is not None and p.get(k) != [] and p.get(k) != "" for k in keys)


def _any(p: dict, *keys) -> bool:
    return any(p.get(k) is not None for k in keys)


# When a tool's parameters need nothing more from a model. Independent of
# which helper filled what, so registering a new helper never changes it.
SATISFIED = {
    "move_entities": lambda p: _has(p, "handles") and (_has(p, "offset") or _has(p, "from_point", "to_point")),
    "copy_entities": lambda p: _has(p, "handles") and (_has(p, "offset") or _has(p, "from_point", "to_point")),
    "rotate_entities": lambda p: _has(p, "handles", "base_point", "angle_deg"),
    "scale_entities": lambda p: _has(p, "handles", "base_point") and _any(p, "factor", "new_length"),
    "offset_entities": lambda p: _has(p, "handles", "distance") and _any(p, "side_point", "through_point"),
    "draw_circle": lambda p: _has(p, "center") and _any(p, "radius", "diameter"),
    "draw_text": lambda p: _has(p, "text", "position"),
    "draw_mtext": lambda p: _has(p, "text", "position"),
    "draw_rectangle": lambda p: _has(p, "corner1") and (_has(p, "corner2") or _has(p, "width", "height")),
    "set_properties": lambda p: _has(p, "handles") and _any(p, "layer", "color", "linetype", "lineweight",
                                                             "linetype_scale", "transparency", "visible"),
    "move_to_layer": lambda p: _has(p, "handles", "layer"),
    "edit_text": lambda p: _has(p, "handle") and _any(p, "text", "height", "rotation_deg", "width", "position", "justification"),
    "delete_objects": lambda p: _has(p, "handles"),
    "find_entities": lambda p: _has(p, "x", "y"),
    "nearby": lambda p: _has(p, "x", "y"),
    "create_layer": lambda p: _has(p, "name"),
}


def run_helpers(req: Request) -> Filled | None:
    """Every helper for the tool, in order; their parameters merged (later
    wins), notes joined, grounds pooled. The first refusal stops the run.
    `complete` comes from SATISFIED when the tool is listed, else from the
    last helper's view with nothing required missing."""
    fns = helpers_for(req.tool)
    if not fns:
        return None
    so_far = dict(req.prefilled or {})
    params, notes, grounds, complete, refuse = {}, [], set(), False, ""
    for fn in fns:
        try:
            out = fn(req, dict(so_far))
        except Exception as e:          # a helper must never take the plan down
            notes.append(f"{fn.__name__} failed: {e.__class__.__name__}: {str(e)[:100]}")
            continue
        if not out or not isinstance(out, Filled):
            continue
        if out.refuse:
            refuse = out.refuse
            if out.note:
                notes.append(out.note)
            break
        if out.params:
            params.update(out.params)
            so_far.update(out.params)
        grounds |= set(out.grounds or ())
        if out.note:
            notes.append(out.note)
        complete = bool(out.complete)
    if refuse:
        return Filled(params=params, complete=False, note="; ".join(notes), grounds=grounds, refuse=refuse)
    if not params and not notes:
        return None
    sat = SATISFIED.get(req.tool)
    if sat is not None:
        complete = bool(sat(so_far))
    else:
        complete = complete and not required_missing(req.schema, so_far)
    return Filled(params=params, complete=complete, note="; ".join(notes), grounds=grounds)


# ── shared bits ──────────────────────────────────────────────────────────────

_HANDLE_KEY = re.compile(r"handle", re.I)
_POINT_KEYS = ("center", "position", "point", "insertion_point", "base_point", "start")
_DIRS = {"n": (0, 1), "north": (0, 1), "up": (0, 1),
         "s": (0, -1), "south": (0, -1), "down": (0, -1),
         "e": (1, 0), "east": (1, 0), "right": (1, 0),
         "w": (-1, 0), "west": (-1, 0), "left": (-1, 0)}
_OFFSET = re.compile(r"(\d+(?:\.\d+)?)\s*(?:'|ft\b|feet\b|foot\b)?\s*(?:to the\s+|towards?\s+)?"
                     r"(north|south|east|west|up|down|left|right|n|s|e|w)\b", re.I)
_QUOTED = re.compile(r"[\"“”']([^\"“”']{1,200})[\"“”']")
_TO_LAYER = re.compile(r"\bto\s+(?:the\s+)?layer\s+[\"']?([^\s\"',.;]+)", re.I)
_ANY_LAYER = re.compile(r"\blayer\s+[\"']?([^\s\"',.;]+)", re.I)
_THE_LAYER = re.compile(r"\b(?:on|to|onto|into)\s+the\s+(.+?)\s+layer\b|\bthe\s+(.+?)'s\s+layer\b", re.I)
_COMPASS_WORD = re.compile(r"\b(north|south|east|west|northeast|northwest|southeast|southwest)\b", re.I)
_DRAW_TOOLS = ("draw_text", "draw_mtext", "draw_circle", "draw_rectangle")
_PLACE_KEYS = ("position", "center", "corner1")


def _is_list(spec: dict) -> bool:
    if spec.get("type") == "array":
        return True
    return any(a.get("type") == "array" for a in spec.get("anyOf") or [])


def _handle_keys(req: Request) -> list:
    return [k for k in req.props if k in HANDLE_KEYS or _HANDLE_KEY.search(k)]


def _xy(p) -> list:
    return [round(float(p[0]), 3), round(float(p[1]), 3)]


def _masked(req: Request) -> str:
    """The prompt with quotes and every place phrase blanked, so '10 ft north
    of the hydrant' is not read as a size."""
    s = measures.strip_quoted(req.text).lower()
    try:
        _, quals = subjects.parse(req.text, req.index)
    except Exception:
        quals = []
    for q in quals:
        a, b = q.span
        s = s[:a] + " " * (b - a) + s[b:]
    return s


def _subject_reps(req: Request, so_far: dict) -> list:
    """Representative points of the step's subject: the handles already
    filled, else the resolved subject's."""
    if req.index is None:
        return []
    lookup = subjects._lookup(req)
    handles = []
    for k in _handle_keys(req):
        v = so_far.get(k)
        if isinstance(v, list):
            handles += [str(h) for h in v]
        elif v:
            handles.append(str(v))
    if not handles:
        subj = subjects.resolve(req)
        if subj and not subj.reason:
            handles = list(subj.handles)
    pts = [geometry.rep_point(req.index, req.data, h, lookup) for h in handles]
    return [p for p in pts if p is not None]


def _ref_refusal(ref) -> str:
    if not ref.handles and not ref.one:
        return f"no {ref.kind.word if ref.kind else ref.phrase} found to place against"
    return ref.reason or ""


# ── the seed helpers (text patterns and result plumbing) ─────────────────────

@helper("*")
def anchor_point(req: Request, so_far: dict) -> Filled | None:
    """The anchor as the one required point of a drawing tool (a circle's
    center, a text's position) when nothing else has given it."""
    if not req.anchor_xy:
        return None
    x, y = round(float(req.anchor_xy[0]), 3), round(float(req.anchor_xy[1]), 3)
    for k in _POINT_KEYS:
        if k == "base_point":                 # base_point() reads the subject's own point
            continue
        if k in req.props and k in req.required and so_far.get(k) is None:
            return Filled({k: [x, y]}, complete=False, note=f"{k} from the anchor")
    return None


@helper("*")
def handles_from_results(req: Request, so_far: dict) -> Filled | None:
    """The last lookup's handles into the tool's one required handle
    parameter (object_at's pick, find_entities' matches). Complete only when
    handles are all the tool takes (delete_objects, explode_entities ...)."""
    keys = [k for k in _handle_keys(req) if k in req.required and so_far.get(k) is None]
    if len(keys) != 1:                       # two handle parameters (line_a/line_b): not a guess to make
        return None
    found, source = [], ""
    for r in reversed(req.results or []):
        if r.get("created"):
            found, source = list(r["created"]), str(r.get("tool") or "")
            break
    if not found:
        pick = (req.context or {}).get("pick") or {}
        obj = pick.get("object") or {}
        if obj.get("handle"):
            found, source = [str(obj["handle"])], "the anchor's pick"
    if not found:
        return None
    k = keys[0]
    params = {k: list(found) if _is_list(req.props[k]) else found[0]}
    only_handles = set(req.props) <= set(_handle_keys(req))
    return Filled(params, complete=only_handles, note=f"{k} from {source}")


@helper("*")
def subject_handles(req: Request, so_far: dict) -> Filled | None:
    """What the prompt is about, read from the drawing: "all hydrants" is
    every hydrant, "the hydrant" the one the anchor picks or is nearest to.
    A lookup's handles stand when the subject is singular and they are of
    its kind; a set, or a pick of the wrong kind, is replaced. Nothing found,
    or several with nothing to choose by, is a refusal."""
    if req.index is None:
        return None
    keys = [k for k in _handle_keys(req) if k in req.required]
    if len(keys) != 1:
        return None
    key = keys[0]
    subject = subjects.resolve(req)
    if subject is None or subject.kind is None:
        return None
    if subject.reason:
        return Filled(refuse=subject.reason, note=f"{key}: {subject.phrase}")
    have = so_far.get(key)
    if have is not None:
        current = [str(h) for h in have] if isinstance(have, list) else [str(have)]
        lookup = subjects._lookup(req)
        if not subject.plural and current and all(words.matches(req.index, req.data, h, subject.kind, lookup) for h in current):
            return None
    handles = list(subject.handles)
    if not handles:
        return Filled(refuse=f"no {subject.kind.word} found")
    if req.tool == "edit_text" and len(handles) > 1:
        return Filled(refuse=f"edit_text changes one text: {len(handles)} texts matched; say which")
    params = {key: handles if _is_list(req.props[key]) else handles[0]}
    what = f"{len(handles)} object(s)" if subject.plural else handles[0]
    return Filled(params, grounds=set(handles), note=f"{key}: {subject.phrase} -> {what} ({subject.source})")


@helper("move_entities", "copy_entities")
def offset_from_text(req: Request, so_far: dict) -> Filled | None:
    """'20 ft north', "15' W", '10 to the east' -> offset [dx, dy]."""
    if "offset" not in req.props or so_far.get("offset") is not None:
        return None
    m = _OFFSET.search(measures.strip_quoted(req.text))
    if not m:
        return None
    d = float(m.group(1))
    dx, dy = _DIRS[m.group(2).lower()]
    params = {"offset": [round(dx * d, 3), round(dy * d, 3)]}
    merged = {**so_far, **params}
    return Filled(params, complete=not required_missing(req.schema, merged), note=f"offset from '{m.group(0).strip()}'")


@helper("draw_text", "draw_mtext", "edit_text")
def quoted_text(req: Request, so_far: dict) -> Filled | None:
    """The first quoted words are the text."""
    key = "new_text" if "new_text" in req.props else "text" if "text" in req.props else None
    if not key or so_far.get(key) is not None:
        return None
    m = _QUOTED.search(req.text or "")
    if not m:
        return None
    params = {key: m.group(1).strip()}
    merged = {**so_far, **params}
    return Filled(params, complete=not required_missing(req.schema, merged), note="text from the quoted words")


@helper("move_to_layer", "create_layer", "edit_layer", "set_current_layer")
def layer_from_text(req: Request, so_far: dict) -> Filled | None:
    """'to layer X' first, else the last 'layer X', else the quoted name."""
    key = "layer" if "layer" in req.props else "name" if "name" in req.props else None
    if not key or so_far.get(key) is not None:
        return None
    text = req.text or ""
    m = _TO_LAYER.search(text)
    name = m.group(1) if m else None
    if not name:
        all_ = _ANY_LAYER.findall(text)
        name = all_[-1] if all_ else None
    if not name:
        q = _QUOTED.search(text)
        name = q.group(1).strip() if q else None
    if not name:
        return None
    params = {key: name}
    merged = {**so_far, **params}
    return Filled(params, complete=not required_missing(req.schema, merged), note=f"{key} from the prompt")


# ── the drawing-reading helpers ──────────────────────────────────────────────

@helper(*_DRAW_TOOLS, "copy_entities", "move_entities")
def placement(req: Request, so_far: dict) -> Filled | None:
    """Where a drawn thing goes: "10 ft north of the hydrant", "next to the
    pad", "at the hydrant", "here" (the anchor). For move/copy only "to the
    X" counts, as to_point (from_point = the subject)."""
    if req.index is None:
        return None
    draw = req.tool in _DRAW_TOOLS
    key = next((k for k in _PLACE_KEYS if k in req.props), None) if draw else "to_point"
    if key is None or key not in req.props:
        return None
    # anchor_point may already have put the anchor here; a place the words name wins over it
    from_anchor = req.anchor_xy is not None and so_far.get(key) is not None and list(so_far[key]) == _xy(req.anchor_xy)
    if so_far.get(key) is not None and not from_anchor:
        return None
    refs = subjects.references(req)
    places = [q for q in refs if (draw or q.relation == "at")]
    q = next((q for q in places if q.relation != "here"), None)
    if q is None:
        if draw and req.anchor_xy and so_far.get(key) is None:
            return Filled({key: _xy(req.anchor_xy)}, note=f"{key} from the anchor")
        return None
    if not q.refs:
        return None
    ref = q.refs[0]
    if ref.reason or not ref.handles:
        return Filled(refuse=_ref_refusal(ref))
    lookup = subjects._lookup(req)
    pt = geometry.placement(req.index, req.data, ref.handles, q.relation, q.direction, q.distance_ft, req.anchor_xy, lookup)
    if pt is None:
        if q.relation in ("toward", "away"):
            return Filled(refuse=f"'{q.relation} the {ref.phrase}' needs an anchor to start from")
        return None
    params = {key: _xy(pt)}
    if not draw:
        reps = _subject_reps(req, so_far)
        c = geometry.centroid(reps)
        if c:
            params["from_point"] = _xy(c)
    where = {"compass": f"{q.distance_ft if q.distance_ft is not None else geometry.CLEARANCE_FT:g} ft {q.direction} of",
             "side": f"{q.direction} of", "near": "next to", "from": f"{q.distance_ft:g} ft from" if q.distance_ft else "near",
             "toward": "toward", "away": "away from", "along": "along", "at": "at", "inside": "in"}.get(q.relation, q.relation)
    return Filled(params, grounds=set(ref.handles), note=f"{key}: {where} the {ref.phrase} ({ref.one or len(ref.handles)})")


@helper("move_entities", "copy_entities")
def relational_offset(req: Request, so_far: dict) -> Filled | None:
    """"10 ft toward the road", "20 ft along the water main", "15 ft to the
    left" -> offset as a vector from the subject; a copy's count."""
    params, notes, grounds = {}, [], set()
    c = measures.count(req.text)
    if req.tool == "copy_entities" and "count" in req.props and so_far.get("count") is None and c:
        params["count"] = c
        notes.append(f"{c} copies")
    if req.index is not None:
        refs = subjects.references(req)
        q = next((q for q in refs if q.relation in ("toward", "away", "along", "side")), None)
        if q is not None and (so_far.get("offset") is None or q.relation == "side"):
            d = q.distance_ft
            if d is None:
                m = measures.first_distance(req.text, q.span)
                d = m.value if m else None
            if d is not None:
                reps = _subject_reps(req, so_far)
                origin = geometry.centroid(reps) or (tuple(req.anchor_xy) if req.anchor_xy else None)
                vec = None
                if origin is not None:
                    if q.relation == "side":
                        handles = [h for r in q.refs for h in r.handles]
                        road = not handles or any(req.index.entity_tag.get(h) in geometry.ROAD_TAGS for h in handles)
                        vec = geometry.side_vector(req.index, handles, origin, q.direction, road)
                    else:
                        if not q.refs or q.refs[0].reason or not q.refs[0].handles:
                            ref = q.refs[0] if q.refs else None
                            return Filled(refuse=(_ref_refusal(ref) if ref else f"'{q.relation}' needs something to aim at"))
                        handles = q.refs[0].handles
                        grounds |= set(handles)
                        vec = {"toward": geometry.toward, "away": geometry.away}.get(q.relation, lambda i, p, hs: geometry.along(i, hs, p))(req.index, origin, handles)
                if vec:
                    params["offset"] = _xy(geometry.point_at((0.0, 0.0), vec, d))
                    rel_word = {"toward": "toward", "away": "away from", "along": "along"}.get(q.relation, q.relation)
                    what = f"to the {q.direction} (road frame)" if q.relation == "side" else f"{rel_word} the {q.refs[0].phrase}"
                    notes.append(f"offset {d:g} ft {what}")
    if not params:
        return None
    return Filled(params, grounds=grounds, note="; ".join(notes))


@helper("offset_entities")
def offset_side(req: Request, so_far: dict) -> Filled | None:
    """The offset distance, and a point on the side the words say (toward
    the road, south, to the left)."""
    params, notes, grounds = {}, [], set()
    masked = _masked(req)
    if so_far.get("distance") is None:
        m = re.search(r"\boffset\b", masked)
        d = measures.first_distance(masked, (m.start(), m.end()) if m else None)
        if d is None:
            d = measures.first_distance(measures.strip_quoted(req.text))
        if d is not None:
            params["distance"] = d.value
            notes.append(f"distance {d.value:g} ft")
    if req.index is not None and so_far.get("side_point") is None and so_far.get("through_point") is None:
        reps = _subject_reps(req, so_far)
        origin = geometry.centroid(reps)
        vec = None
        refs = subjects.references(req)
        q = next((q for q in refs if q.relation in ("toward", "away", "along", "side", "compass")), None)
        if q is not None and q.relation == "compass":
            vec = geometry.compass_vector(q.direction)
        elif q is not None and q.relation == "side":
            handles = [h for r in q.refs for h in r.handles]
            if origin:
                vec = geometry.side_vector(req.index, handles, origin, q.direction, not handles or any(req.index.entity_tag.get(h) in geometry.ROAD_TAGS for h in handles))
        elif q is not None and q.relation in ("toward", "away"):
            if not q.refs or q.refs[0].reason or not q.refs[0].handles:
                ref = q.refs[0] if q.refs else None
                return Filled(refuse=(_ref_refusal(ref) if ref else f"'{q.relation}' needs something to aim at"))
            handles = q.refs[0].handles
            grounds |= set(handles)
            if origin:
                vec = geometry.toward(req.index, origin, handles) if q.relation == "toward" else geometry.away(req.index, origin, handles)
        else:
            w = _COMPASS_WORD.search(masked)
            if w:
                vec = geometry.compass_vector(w.group(1))
        if vec and origin:
            params["side_point"] = _xy(geometry.point_at(origin, vec, 1.0))
            notes.append("side from the words")
    if not params:
        return None
    return Filled(params, grounds=grounds, note="; ".join(notes))


@helper("draw_circle", "draw_text", "draw_mtext", "draw_rectangle", "rotate_entities", "scale_entities", "copy_entities")
def sizes(req: Request, so_far: dict) -> Filled | None:
    """Sizes and amounts from the words: a circle's radius, a text height, a
    rectangle's width and height, an angle, a scale factor, a count."""
    params, notes = {}, []
    masked = _masked(req)
    if req.tool == "draw_circle" and so_far.get("radius") is None and so_far.get("diameter") is None:
        r, note = measures.circle_size(masked)
        if r:
            params["radius"] = r
            notes.append(note)
    if req.tool in ("draw_text", "draw_mtext") and "height" in req.props and so_far.get("height") is None:
        h = measures.text_height(masked)
        if h:
            params["height"] = h
            notes.append(f"height {h:g} ft")
    if req.tool == "draw_rectangle" and so_far.get("corner2") is None and so_far.get("width") is None:
        wh = measures.rectangle_size(masked)
        if wh:
            params["width"], params["height"] = wh
            notes.append(f"{wh[0]:g} x {wh[1]:g} ft")
    if req.tool == "rotate_entities" and so_far.get("angle_deg") is None:
        a = measures.degrees(masked)
        if a is not None:
            params["angle_deg"] = a
            notes.append(f"{a:g} degrees")
    if req.tool == "scale_entities" and so_far.get("factor") is None and so_far.get("new_length") is None:
        f = measures.scale_factor(masked)
        if f is not None:
            params["factor"] = f
            notes.append(f"factor {f:g}")
    if req.tool == "copy_entities" and "count" in req.props and so_far.get("count") is None:
        c = measures.count(masked)
        if c:
            params["count"] = c
            notes.append(f"{c} copies")
    if not params:
        return None
    return Filled(params, note="; ".join(notes))


@helper("find_entities", "nearby")
def lookup_scope(req: Request, so_far: dict) -> Filled | None:
    """What a lookup should look for and how far: the tag the subject's kind
    carries in this drawing, the whole site for "all" or with no anchor,
    "within N ft" as the radius."""
    if req.index is None:
        return None
    kind = subjects.target_kind(req)
    if kind is None:
        return None
    lookup = subjects._lookup(req)
    handles = words.find(req.index, req.data, kind, lookup)
    if not handles:
        return Filled(refuse=f"no {words.plural_of(kind.word) if kind.plural else kind.word} found")
    params, notes = {}, [f"{kind.word}: {len(handles)} in the drawing"]
    if req.tool == "find_entities" and not so_far.get("tag"):
        tags = {str(req.index.entity_tag.get(h) or "") for h in handles}
        tags.discard("")
        tag = ""
        if len(tags) == 1:
            tag = tags.pop()
        else:
            fams = {str(words.spatial.TAGS.family_of(t)) for t in tags}
            if len(fams) == 1:
                f = fams.pop()
                if req.index.tags_of(f):
                    tag = f
        if tag:
            params["tag"] = tag
    if req.tool == "nearby" and kind.types and not so_far.get("types"):
        params["types"] = ",".join(sorted(kind.types))
    subject, quals = subjects.parse(req.text, req.index)
    plural = subject.plural if (subject is not None and subject.kind is kind) else kind.plural
    if plural or req.anchor_xy is None:
        cx, cy, r = geometry.site_extent(req.index)
        params.update(x=cx, y=cy, radius=r)
        notes.append("whole drawing")
    within = next((q for q in quals if q.relation == "within" and q.distance_ft), None)
    if within:
        params["radius"] = float(within.distance_ft)
    if "state" in req.props and not so_far.get("state"):
        m = re.search(r"\b(existing|proposed|future)\b", measures.strip_quoted(req.text).lower())
        if m:
            params["state"] = m.group(1)
    return Filled(params, note="; ".join(notes))


@helper("move_to_layer", "set_properties", "edit_layer", "set_current_layer")
def layer_target(req: Request, so_far: dict) -> Filled | None:
    """The typed layer words -> a layer the drawing really has ("the water
    main layer" -> c-watr-main). A name the drawing lacks is kept as typed."""
    key = "layer" if "layer" in req.props else "name" if "name" in req.props else None
    if key is None:
        return None
    text = measures.strip_quoted(req.text)
    if req.tool == "set_properties" and "layer" not in text.lower():
        return None
    typed = so_far.get(key)
    if typed is None:
        m = _THE_LAYER.search(text)
        typed = (m.group(1) or m.group(2)).strip() if m else None
    if not typed or req.index is None:
        return None
    real = words.resolve_layer(req.index, req.data, str(typed))
    if real and real != typed:
        return Filled({key: real}, note=f"layer '{typed}' -> {real}")
    if real:
        return Filled({key: real}) if so_far.get(key) is None else None
    return Filled({key: str(typed)} if so_far.get(key) is None else {}, note=f"layer '{typed}' is not in the drawing (kept as typed)")


@helper("set_properties", "draw_text", "draw_mtext")
def colour(req: Request, so_far: dict) -> Filled | None:
    """A colour word (red, cyan ...) or "color N" -> the ACI index."""
    if "color" not in req.props or so_far.get("color") is not None:
        return None
    s = measures.strip_quoted(req.text).lower()
    m = re.search(r"\bcolou?r\s+(\d{1,3})\b", s)
    if m:
        return Filled({"color": int(m.group(1))}, note=f"color {m.group(1)}")
    for word in sorted(ACI, key=len, reverse=True):
        if re.search(rf"\b{re.escape(word)}\b", s):
            return Filled({"color": ACI[word]}, note=f"colour {word} -> ACI {ACI[word]}")
    return None


@helper("rotate_entities", "scale_entities")
def base_point(req: Request, so_far: dict) -> Filled | None:
    """The point to rotate or scale about: the anchor when asked ("about
    here"), else the subject's own point (the centroid of several)."""
    if "base_point" not in req.props or so_far.get("base_point") is not None:
        return None
    s = measures.strip_quoted(req.text).lower()
    if req.anchor_xy and re.search(r"\b(?:about|around|at)\s+(?:here|the anchor|this point)\b", s):
        return Filled({"base_point": _xy(req.anchor_xy)}, note="base point from the anchor")
    reps = _subject_reps(req, so_far)
    c = geometry.centroid(reps)
    if c is None:
        return None
    return Filled({"base_point": _xy(c)}, note="base point: the subject" + (f" (centroid of {len(reps)})" if len(reps) > 1 else ""))
