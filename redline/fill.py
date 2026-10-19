"""Parameters for one tool call at a time.

Given the comment, where it landed, what is around it, the tool sequence and
the real results of the steps already run, the language model fills the
parameters of the next tool from that tool's own schema (the one the MCP
registry carries). Code fills what it can first (the located point for
x/y), and checks the answer before anything runs:

- every required parameter present, simple types coerced;
- every handle named must come from the digest or an earlier step's result
  (a model never types a handle);
- every coordinate must sit inside the viewport's model window, with slack.

One retry with the error stated; then the step is "not_filled" and the
comment goes to a person. The backends are the planner's (PlannerBackend /
OpenAICompatPlanner); FakeFiller answers by rule for the tests.
"""
import json
import re
import time

from . import openai_compat, tools
from .openai_compat import ModelError
from .planner import OpenAICompatPlanner, PlannerBackend, known_handles  # noqa: F401
from .types import ToolStep

SYSTEM_PROMPT = """You are a Civil 3D drafter carrying out a reviewer's comment, one tool call at a time.
You are given the comment, where it lands in the drawing (model feet), what is drawn around that spot
(with object handles), the planned sequence of tools, the results of the steps already run, and the ONE tool
to fill parameters for now, with its description and parameter schema.
Rules:
1. Answer with a JSON object holding that tool's parameters only. No prose.
2. Handles (object ids such as "A1E2") must be copied from CONTEXT or RESULTS. Never invent one.
3. Coordinates are model feet near LOCATION. Offsets are [dx, dy] in feet; north is +y, east is +x.
4. Text content comes from the comment. Layers and styles come from CONTEXT or RESULTS when given.
5. Leave optional parameters out unless the comment needs them. Keep PREFILLED values unless the comment
   says otherwise.
6. Colours are ACI numbers (1 red, 2 yellow, 3 green, 4 cyan, 5 blue, 6 magenta, 7 white) or [r, g, b].
7. If you are asked again because a tool reported an error, change the parameters so the error cannot
   recur; do not repeat the same values. Read the PICK's geometry: a polygon needs a radius, an open
   polyline is closed with edit_polyline, a segment is removed with break_entity or trim_entities.
"""

ACI = {"red": 1, "yellow": 2, "green": 3, "cyan": 4, "blue": 5, "magenta": 6, "white": 7, "black": 7,
       "gray": 8, "grey": 8, "dark gray": 8, "light gray": 9, "orange": 30, "purple": 200, "brown": 34}

NEAR_MISS_REACH = 3.0   # a candidate farther than this many pick tolerances from the arrow tip is not the comment's object


def usable_handles(context: dict) -> set:
    """The handles a filled parameter may name: everything the digest knows,
    except that when nothing was under the arrow tip, the far candidates
    object_at listed (to show a near miss) are not usable. Deleting or moving
    something 100 ft from where the reviewer pointed is a guess; the comment
    goes to a person instead, with those candidates in the report."""
    ctx = context or {}
    out = set(known_handles(ctx))
    pick = ctx.get("pick") or {}
    if pick.get("object") is None and pick.get("candidates"):
        reach = NEAR_MISS_REACH * float(ctx.get("pick_tolerance_ft") or 5.0)
        near_ids = set()
        for o in ctx.get("objects") or []:
            if o.get("handle") and (o.get("distance") is None or float(o["distance"]) <= reach):
                near_ids.add(str(o["handle"]))
        for c in pick["candidates"]:
            h = str(c.get("handle"))
            if c.get("distance") is not None and float(c["distance"]) > reach and h not in near_ids:
                out.discard(h)
                for l in c.get("labels") or []:
                    if isinstance(l, dict) and l.get("handle"):
                        out.discard(str(l["handle"]))
        for o in ctx.get("objects") or []:
            if o.get("handle") and o.get("distance") is not None and float(o["distance"]) > reach:
                out.discard(str(o["handle"]))
    return out


_POINT_KEY = re.compile(r"(point|points|center|centre|vertices|vertex|corner|position|axis_|base|anchor|window|path|polygon)", re.I)
_HANDLE_KEY = re.compile(r"handle", re.I)
HANDLE_KEYS = frozenset(("handles", "handle", "target", "tools", "objects", "source", "sources", "entities", "boundary",
                         "curve", "reference", "line_a", "line_b", "arc_handle", "primary", "targets"))
_HEX = re.compile(r"^[0-9A-Fa-f]{1,16}$")
WINDOW_SLACK = 0.5     # the viewport window grown by half its size on every side


def _j(obj, limit: int = 1500) -> str:
    s = json.dumps(obj, default=str)
    return s if len(s) <= limit else s[:limit] + " ...(truncated)"


def schema_lines(schema: dict) -> str:
    props = schema.get("properties") or {}
    req = set(schema.get("required") or [])
    out = []
    for k, v in props.items():
        t = v.get("type")
        if not t and v.get("anyOf"):
            t = "|".join(str(a.get("type", "?")) for a in v["anyOf"] if a.get("type") != "null") or "any"
        if t == "array":
            items = v.get("items") or {}
            inner = items.get("type") or ("array" if items.get("items") else "any")
            t = f"array of {inner}"
        tag = "required" if k in req else f"default {json.dumps(v.get('default'))}" if "default" in v else "optional"
        extra = f" one of {v['enum']}" if v.get("enum") else ""
        out.append(f"- {k}: {t} ({tag}){extra}")
    return "\n".join(out) if out else "(no parameters)"


def prefill(tool: str, schema: dict, located) -> dict:
    """What code knows without asking: the located point for x/y, point or
    center parameters when the tool has exactly that shape."""
    props = schema.get("properties") or {}
    out = {}
    xy = getattr(located, "model_xy", None) if not isinstance(located, dict) else located.get("model_xy")
    if xy and len(xy) >= 2:
        x, y = float(xy[0]), float(xy[1])
        if "x" in props and "y" in props:
            out["x"], out["y"] = round(x, 3), round(y, 3)
        ctx = getattr(located, "context", None) or {}
        if tool == "object_at" and "tolerance" in props and ctx.get("pick_tolerance_ft"):
            out["tolerance"] = float(ctx["pick_tolerance_ft"])
        if tool == "select_entities" and "points" in props:
            out["points"] = [[round(x, 3), round(y, 3)]]
            out["mode"] = "point_radius"
            if "radius" in props:
                out["radius"] = 5.0
        if tool == "trace_boundary" and "point" in props:
            out["point"] = [round(x, 3), round(y, 3)]
    return out


def answer_schema(schema: dict) -> dict:
    """The tool's schema as the model's answer schema: `title` so Ollama
    names the grammar, nothing else added."""
    return {"title": "parameters", "type": "object", "properties": dict(schema.get("properties") or {}),
            "required": list(schema.get("required") or [])}


def build_user_prompt(comment, located, context: dict, sequence: list, index: int, tool: str, schema: dict,
                      prefilled: dict, results: list, window=None, retry_error: str | None = None) -> str:
    parts = [f'COMMENT: text="{str(comment.text).replace(chr(34), chr(39))}" kind={comment.kind}']
    xy = getattr(located, "model_xy", None)
    pxy = getattr(located, "paper_xy", None)
    if xy:
        parts.append(f"LOCATION: model ({xy[0]:.2f}, {xy[1]:.2f}) ft" + (f"; viewport model window {window}" if window else ""))
    elif pxy:
        parts.append(f"LOCATION: paper ({pxy[0]:.2f}, {pxy[1]:.2f}) in (a layout, not model space)")
    ctx = dict(context or {})
    pick = ctx.get("pick") or {}
    if pick.get("object"):
        parts.append("PICK (the object under the comment's arrow): " + _j(pick.get("object"), 900)
                     + (f"\n  its labels: {_j(pick.get('labels'), 300)}" if pick.get("labels") else "")
                     + (f"\n  also close: {_j([{k: c.get(k) for k in ('handle', 'type', 'layer', 'name', 'text', 'distance')} for c in pick.get('candidates') or []], 500)}"
                        if pick.get("candidates") else ""))
    elif "pick" in ctx:
        parts.append("PICK: nothing within reach of the arrow tip"
                     + (f"; nearest: {_j([{k: c.get(k) for k in ('handle', 'type', 'layer', 'name', 'text', 'distance')} for c in pick.get('candidates') or []], 500)}"
                        if pick.get("candidates") else ""))
    if pick.get("enclosing"):
        parts.append("INSIDE (closed shapes the point is within, smallest first): "
                     + _j([{k: e.get(k) for k in ("handle", "type", "layer", "name", "geometry", "area")} for e in pick["enclosing"][:3]], 700))
    if ctx.get("objects"):
        parts.append("OBJECTS nearby (nearest first): " + _j(ctx["objects"], 2200))
    brief = {k: ctx.get(k) for k in ("cell", "frame", "within", "inside", "labels", "texts", "paper_texts", "viewport") if ctx.get(k)}
    parts.append("CONTEXT: " + _j(brief, 1800))
    seq = ", ".join(f"[{t}]" if i == index else t for i, t in enumerate(sequence))
    parts.append(f"SEQUENCE: {seq}   (step {index + 1} of {len(sequence)} is in brackets)")
    if results:
        parts.append("RESULTS so far:")
        for r in results:
            parts.append(f"- {r['tool']}: {r['preview']}")
    else:
        parts.append("RESULTS so far: none")
    parts.append(f"TOOL: {tool}")
    parts.append(f"DESCRIPTION: {tools.description(tool)}")
    parts.append("PARAMETERS:\n" + schema_lines(schema))
    if prefilled:
        parts.append("PREFILLED: " + _j(prefilled))
    if retry_error:
        parts.append(f"Your previous answer was rejected: {retry_error}. Answer again.")
    parts.append("ANSWER: a JSON object with the parameters.")
    return "\n".join(parts)


def _coerce(value, spec: dict):
    t = spec.get("type")
    if not t and spec.get("anyOf"):
        alts = [a for a in spec["anyOf"] if a.get("type") != "null"]
        if value is None and len(alts) < len(spec["anyOf"]):
            return None
        # the alternative that already fits the value goes first, so a colour 1 stays a number
        # when a string is also allowed, and [r, g, b] stays a list
        native = ("integer", "number") if isinstance(value, (int, float)) and not isinstance(value, bool) else \
                 ("array",) if isinstance(value, list) else ("boolean",) if isinstance(value, bool) else \
                 ("object",) if isinstance(value, dict) else ("string",)
        alts.sort(key=lambda a: 0 if a.get("type") in native else 1)
        for alt in alts:
            try:
                return _coerce(value, alt)
            except (ValueError, TypeError):
                continue
        raise ValueError("no matching type")
    if t == "number" or t == "integer":
        if isinstance(value, bool):
            raise ValueError("boolean given for a number")
        if isinstance(value, str):
            value = value.strip().replace("'", "").replace("ft", "")
        v = float(value)
        return int(v) if t == "integer" else v
    if t == "string":
        return value if isinstance(value, str) else json.dumps(value) if isinstance(value, (dict, list)) else str(value)
    if t == "boolean":
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "1")
        return bool(value)
    if t == "array":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = [p.strip() for p in value.split(",") if p.strip()]
        if not isinstance(value, list):
            raise ValueError("not a list")
        items = spec.get("items") or {}
        return [_coerce(v, items) if items else v for v in value]
    if t == "object":
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    return value


def _pairs(value) -> list:
    """Every [x, y] numeric pair inside a value, however nested."""
    out = []
    if isinstance(value, (list, tuple)):
        if len(value) in (2, 3) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            out.append((float(value[0]), float(value[1])))
        else:
            for v in value:
                out.extend(_pairs(v))
    return out


def _handles_named(key: str, value) -> list:
    if not (key in HANDLE_KEYS or _HANDLE_KEY.search(key)):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def validate(params: dict, schema: dict, known: set, window=None) -> tuple:
    """(clean params, None) or (None, error)."""
    if not isinstance(params, dict):
        return None, "the answer was not a JSON object"
    props = schema.get("properties") or {}
    clean = {}
    for k, spec in props.items():
        if k not in params or params[k] is None:
            continue
        value = params[k]
        if k in ("color", "colour") and isinstance(value, str):
            word = value.strip().lower()
            if word in ACI:                             # the plugin takes ACI numbers, [r,g,b], bylayer or byblock
                value = ACI[word]
            elif word.isdigit():
                value = int(word)
        try:
            clean[k] = _coerce(value, spec)
        except (ValueError, TypeError) as e:
            return None, f"parameter {k}: {e}"
    missing = [k for k in (schema.get("required") or []) if k not in clean]
    if missing:
        return None, "missing required parameter(s): " + ", ".join(missing)
    known_up = {str(h).upper() for h in (known or set())}
    for k, v in clean.items():
        for h in _handles_named(k, v):
            if h.startswith("@"):
                continue
            if h.upper() not in known_up:
                return None, f"handle {h!r} in {k} is not in CONTEXT or RESULTS"
        if k in HANDLE_KEYS and isinstance(v, list) and not v:
            return None, f"{k} is empty"
    if window:
        (x0, y0), (x1, y1) = window
        sx, sy = (x1 - x0) * WINDOW_SLACK, (y1 - y0) * WINDOW_SLACK
        for k, v in clean.items():
            if k in ("offset", "scale", "angle_deg", "distance") or not _POINT_KEY.search(k):
                continue
            for px, py in _pairs(v):
                if not (x0 - sx <= px <= x1 + sx and y0 - sy <= py <= y1 + sy):
                    return None, f"{k} point ({px:.1f}, {py:.1f}) is outside the sheet's model window {window}"
    return clean, None


def explain_rejection(error: str | None, context: dict) -> str | None:
    """A handle the guard refused that object_at listed as a far candidate
    gets the plain reason: the arrow tip is that far from it."""
    if not error:
        return error
    m = re.search(r"handle '([^']+)' in \w+ is not in CONTEXT or RESULTS", error)
    if not m:
        return error
    h = m.group(1)
    pick = (context or {}).get("pick") or {}
    tol = (context or {}).get("pick_tolerance_ft")
    for c in list(pick.get("candidates") or []) + list((context or {}).get("objects") or []):
        if str(c.get("handle", "")).upper() == h.upper() and c.get("distance") is not None:
            return (f"{h} is {float(c['distance']):.0f} ft from the comment's arrow tip (pick tolerance {tol} ft): "
                    f"the tip is not on it, so it is not treated as the object the comment is about. "
                    f"Move the callout's arrow onto the object, or a person applies this one")
    return error


def fill(comment, located, context: dict, sequence: list, index: int, backend, known: set, results: list,
         window=None, log=None, retry_error: str | None = None, prefilled: dict | None = None,
         skip_model: bool = False) -> ToolStep:
    """`retry_error` is a tool's own error message from a run that failed:
    the first prompt then already says what went wrong.

    `prefilled` is what code outside this module already knows (the prompt
    block's helpers): merged over prefill() and shown to the model as
    PREFILLED. With `skip_model` the merged values are validated first and
    answer the step on their own (attempt 0); the model is consulted only
    when they do not validate, with that reason in its first prompt."""
    tool = sequence[index]
    step = ToolStep(tool=tool)
    schema = tools.schema(tool)
    if not schema and tool not in tools.registry():
        step.status, step.error = "not_filled", f"unknown tool {tool}"
        return step
    pre = {**prefill(tool, schema, located), **(prefilled or {})}
    if not (schema.get("properties") or {}):
        step.params, step.status = {}, "planned"
        return step
    error = f"the tool reported: {retry_error}" if retry_error else None
    if skip_model and not retry_error:
        clean, err = validate(pre, schema, known, window)
        if clean is not None:
            step.params, step.status, step.attempt = clean, "planned", 0
            return step
        error = err
        if log:
            log(f"    {tool}: prefilled values rejected ({err}); asking the model")
    for attempt in (1, 2):
        step.attempt = attempt
        prompt = build_user_prompt(comment, located, context, sequence, index, tool, schema, pre, results,
                                   window=window, retry_error=error)
        t0 = time.time()
        try:
            reply = backend.complete(SYSTEM_PROMPT, prompt, answer_schema(schema))
        except ModelError as e:
            step.latency_s += time.time() - t0
            step.status, step.error = "not_filled", f"model_error: {str(e)[:300]}"
            return step
        step.latency_s += time.time() - t0
        step.raw_reply = str(reply)[:4000]
        try:
            obj = openai_compat.parse_json(reply)
        except ValueError as e:
            error = f"reply was not JSON ({e})"
            continue
        if isinstance(obj, dict) and isinstance(obj.get("parameters"), dict):
            obj = obj["parameters"]
        merged = dict(pre)
        if isinstance(obj, dict):
            merged.update(obj)
        clean, err = validate(merged, schema, known, window)
        if clean is not None:
            step.params, step.status = clean, "planned"
            return step
        error = err
        if log:
            log(f"    {tool}: attempt {attempt} rejected: {err}")
    step.status, step.error = "not_filled", f"did_not_validate: {explain_rejection(error, context)}"
    return step


class FakeFiller:
    """Answers by rule: {"tool": name, "params": dict | callable(info)}; info
    holds comment, located, known handles, results so far and the prefill.
    Without a rule the prefilled values alone are answered (enough for a
    lookup, not for a write, which then fails validation as it should)."""
    name = "fake"

    def __init__(self, rules: list | None = None):
        self.rules = list(rules or [])
        self.calls: list = []
        self.info: dict = {}

    def complete(self, system: str, user: str, schema: dict | None) -> str:
        self.calls.append(user)
        m = re.search(r"^TOOL: (\S+)", user, re.M)
        tool = m.group(1) if m else ""
        pre = {}
        mp = re.search(r"^PREFILLED: (.*)$", user, re.M)
        if mp:
            try:
                pre = json.loads(mp.group(1))
            except ValueError:
                pre = {}
        results = re.findall(r"^- (\S+): (.*)$", user.split("RESULTS so far", 1)[-1].split("TOOL:", 1)[0], re.M)
        info = {"tool": tool, "prefill": pre, "results": results, "prompt": user}
        for rule in self.rules:
            if rule.get("tool") == tool:
                p = rule.get("params")
                p = p(info) if callable(p) else p
                if isinstance(p, str):          # a raw reply, to exercise parsing and the retry
                    return p
                return json.dumps({**pre, **dict(p or {})})
        return json.dumps(pre)
