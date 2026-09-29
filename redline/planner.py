"""One comment -> a block chain the dashboard will accept, or a reason to skip.

The prompt is plain sections with headers, which 7B-class models follow
better than nested JSON: rules, the palette, the plan type's playbook steps
and recipe, a few worked examples, then the comment, where it landed
and what is there. The reply is JSON: {"blocks": [...]} or
{"skip": true, "reason": ...}. A chain is validated by the dashboard's own
compile_blocks and by a handle guard (every handle in the chain must appear
in the context, so the model cannot invent one). A failing chain is sent
back once with the error; a second failure is a skip.
"""
import json
import re
import time
from typing import Protocol

from . import openai_compat
from .types import ChainProposal

MAX_BLOCKS = 6

SYSTEM_PROMPT = """You turn one reviewer's handwritten redline comment on a civil engineering plan sheet into a JSON chain of dashboard blocks that a CAD plugin will run, or you decline.

Rules:
1. Use only block types listed under PALETTE, with exactly the listed field names. A "handles" field takes a list of handle strings; "point" takes [x, y]; "points" takes [[x, y], ...].
2. Handles and coordinates come only from CONTEXT. Never invent a handle. Model coordinates are feet in world space; north is +y, east is +x.
3. Distances in the comment are feet unless marked otherwise (' = feet, " = inches).
4. If the comment needs anything CONTEXT does not hold (surface or profile elevations, hydraulics, a person's judgement, text you cannot read, an entity that is not listed), answer {"skip": true, "reason": "..."} with a specific reason.
5. Prefer one to four blocks. Read-only Query blocks may come first. Do not add labels or layers the comment did not ask for.
6. EXAMPLES are other reviewers' comments on other drawings. They show the format only. Never reuse an example's reason, handles or coordinates; every answer must come from THIS comment and THIS context.
7. Answer with JSON only, in the ANSWER SCHEMA. Start with "rationale": one sentence saying what this comment asks for and which listed handle(s), if any, it concerns. Then either "blocks" or "skip" with a specific "reason"."""

ANSWER_SCHEMA = {
    "title": "chain",
    "type": "object",
    "properties": {
        "rationale": {"type": "string"},
        "blocks": {"type": "array", "items": {"type": "object", "properties": {
            "type": {"type": "string"}, "params": {"type": "object"}}, "required": ["type", "params"]}},
        "skip": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["rationale"],
}


def _example_handles(examples: list) -> set:
    """Every handle-looking string in the examples' contexts and answers."""
    out = set()
    for ex in examples or []:
        for m in re.finditer(r'"handle[s]?":\s*(\[[^\]]*\]|"[^"]*")', json.dumps(ex, default=str)):
            out.update(re.findall(r'"([0-9A-Za-z]{1,8})"', m.group(1)))
    return out


def relevant_examples(examples: list, comment, limit: int = 4) -> list:
    """The examples most like this comment (word overlap, same kind), least
    relevant first so the closest one sits nearest the comment in the prompt.
    Fewer, closer examples give a small model less to copy blindly."""
    words = set(re.findall(r"[a-z0-9']+", str(comment.text).lower()))
    scored = []
    for ex in examples or []:
        ew = set(re.findall(r"[a-z0-9']+", str(ex.get("comment", "")).lower()))
        score = len(words & ew) / float(len(words | ew) or 1)
        if ex.get("kind") == getattr(comment, "kind", None):
            score += 0.15
        scored.append((score, ex))
    scored.sort(key=lambda s: s[0])
    return [ex for _, ex in scored[-limit:]]


class PlannerBackend(Protocol):
    name: str

    def complete(self, system: str, user: str, schema: dict | None) -> str: ...


class OpenAICompatPlanner:
    def __init__(self, base: str, model: str, timeout: float = 600.0, temperature: float = 0.1):
        self.base, self.model, self.timeout, self.temperature = base, model, timeout, temperature
        self.name = model

    def complete(self, system: str, user: str, schema: dict | None) -> str:
        return openai_compat.chat(self.base, self.model,
                                  [{"role": "system", "content": system}, {"role": "user", "content": user}],
                                  schema=schema, temperature=self.temperature, timeout=self.timeout)


class FakePlanner:
    """Replies by rule: the first rule whose `match` (lower-case substring)
    is in the comment text answers; a rule's reply may be a list, consumed
    one per attempt (to exercise the retry). Unmatched comments are skipped."""
    name = "fake"

    def __init__(self, rules: list | None = None):
        self.rules = list(rules or [])
        self.calls: list = []
        self._served: dict = {}

    def complete(self, system: str, user: str, schema: dict | None) -> str:
        self.calls.append(user)
        m = re.search(r'^COMMENT:.*?text="(.*?)"', user, re.M | re.S)
        text = (m.group(1) if m else "").lower()
        for i, rule in enumerate(self.rules):
            if str(rule.get("match", "")).lower() in text:
                reply = rule["reply"]
                if isinstance(reply, list):
                    n = self._served.get(i, 0)
                    self._served[i] = n + 1
                    reply = reply[min(n, len(reply) - 1)]
                return json.dumps(reply)
        return json.dumps({"skip": True, "reason": "fake planner: no rule matched"})


# ── Prompt ───────────────────────────────────────────────────────────────────

def _j(obj, limit: int = 4000) -> str:
    s = json.dumps(obj, default=str)
    return s if len(s) <= limit else s[:limit] + " ...(truncated)"


def build_user_prompt(comment, located, match, palette_text: str, steps: str = "", recipe: list | None = None,
                      examples: list | None = None, retry_error: str | None = None) -> str:
    parts = ["PALETTE (block types and their fields):", palette_text, ""]
    plan_type = (located.context or {}).get("plan_type_guess")
    if steps:
        parts += [f"PLAYBOOK STEPS ({plan_type}):", steps, ""]
    if recipe:
        parts += [f"RECIPE ({plan_type}), reference values only:", _j(recipe, 1500), ""]
    if examples:
        parts.append("EXAMPLES: other comments on other drawings, shown for the format only; do not copy them")
        chosen = relevant_examples(examples, comment)
        for ex in chosen:
            parts.append(f'comment: text="{ex["comment"]}" kind={ex.get("kind", "note")}')
            if ex.get("context"):
                parts.append("context: " + _j(ex["context"], 800))
            parts.append("answer: " + json.dumps(ex["answer"]))
            parts.append("")
        # A small model will lift a handle straight out of an example. Say
        # outright which of those handles this drawing does not have.
        foreign = sorted(_example_handles(chosen) - known_handles(located.context))
        if foreign:
            parts.append("The example handles " + ", ".join(foreign) + " do not exist on this drawing. "
                         "Use only handles that appear under CONTEXT below.")
            parts.append("")
    sheet = f"sheet={match.layout_name}" if match and match.layout_name else "sheet=?"
    vp = (located.context or {}).get("viewport") or {}
    if vp:
        sheet += f" ({vp.get('scale_text')}, twist {vp.get('twist_deg')})"
    parts.append(f'COMMENT: text="{comment.text}" kind={comment.kind} targets_geometry={comment.targets_geometry} '
                 f'confidence={comment.confidence:.2f} {sheet}')
    if located.space == "model":
        ctx = located.context or {}
        parts.append(f"LOCATION: model ({located.model_xy[0]:.1f}, {located.model_xy[1]:.1f}) ft; {ctx.get('cell', '')}")
        nearest = ctx.get("nearest") or []
        if nearest and comment.kind in ("arrow", "circle", "cloud", "strike"):
            n = min(nearest, key=lambda n: n.get("distance", 1e9))
            parts.append(f"MARK TARGET: the {comment.kind} sits closest to {n.get('tag')} handle {n.get('handle')} "
                         f"({n.get('distance')} ft away); a mark that rings, points at or crosses out something "
                         f"is about that thing unless the words say otherwise")
        parts.append("CONTEXT:")
        parts.append("nearest: " + _j(ctx.get("nearest", []), 1500))
        parts.append("within radius, by tag: " + _j(ctx.get("within", {}), 1500))
        if ctx.get("inside"):
            parts.append("inside: " + _j(ctx["inside"], 400))
        if ctx.get("labels"):
            parts.append("labels: " + _j(ctx["labels"], 800))
        if ctx.get("texts"):
            parts.append("model texts: " + _j(ctx["texts"], 800))
        if ctx.get("paper_texts"):
            parts.append("paper texts near the comment (handles are paper-space text): " + _j(ctx["paper_texts"], 800))
    elif located.space == "paper":
        ctx = located.context or {}
        parts.append(f"LOCATION: paper ({located.paper_xy[0]:.2f}, {located.paper_xy[1]:.2f}) in on layout "
                     f"{ctx.get('layout')}; outside every viewport, so only paper-space text and blocks are here")
        parts.append("CONTEXT:")
        parts.append("paper texts: " + _j(ctx.get("paper_texts", []), 1500))
        if ctx.get("paper_blocks"):
            parts.append("paper blocks: " + _j(ctx["paper_blocks"], 800))
    else:
        parts.append("LOCATION: unknown (the comment could not be placed)")
    parts.append("")
    if retry_error:
        parts.append(f"Your previous chain failed validation: {retry_error}\nReturn a corrected chain, or skip with a reason.")
        parts.append("")
    parts.append('ANSWER SCHEMA: {"rationale":"...","blocks":[{"type":"<block type>","params":{...}}, ...]}  or  '
                 '{"rationale":"...","skip":true,"reason":"..."}')
    return "\n".join(parts)


# ── Validation ───────────────────────────────────────────────────────────────

def known_handles(context: dict) -> set:
    out = set()
    ctx = context or {}
    for n in ctx.get("nearest") or []:
        out.add(str(n.get("handle")))
    for rec in (ctx.get("within") or {}).values():
        out.update(str(h) for h in rec.get("handles") or [])
    for key in ("inside", "labels", "texts", "paper_texts", "paper_blocks", "paper_tables"):
        for rec in ctx.get(key) or []:
            if rec.get("handle"):
                out.add(str(rec["handle"]))
    out.discard("None")
    return out


def _handles_in(block: dict, ftypes: dict) -> list:
    out = []
    types = ftypes.get(block.get("type"), {})
    for name, val in (block.get("params") or {}).items():
        t = types.get(name)
        if t == "handles":
            if isinstance(val, str):
                out += [v.strip() for v in val.split(",") if v.strip()]
            elif isinstance(val, (list, tuple)):
                out += [str(v) for v in val]
        elif name == "handle" or (t == "text" and name.endswith("_handle")):
            if val not in (None, ""):
                out.append(str(val))
    return out


def validate(blocks, handles: set, field_types: dict) -> tuple:
    """(normalised blocks, error). Normalises ids and repeat, runs the
    dashboard's compile_blocks and the handle guard."""
    from dashboard import blocks as dashboard_blocks
    if not isinstance(blocks, list) or not blocks:
        return None, "blocks must be a non-empty list"
    if len(blocks) > MAX_BLOCKS:
        return None, f"at most {MAX_BLOCKS} blocks per comment"
    norm = []
    for i, b in enumerate(blocks, start=1):
        if not isinstance(b, dict) or not b.get("type"):
            return None, f"block {i}: needs a type"
        params = dict(b.get("params") or {})
        # the prompt promises "point" takes [x, y]; the dashboard's coercer wants [[x, y]]
        for name, t in field_types.get(str(b["type"]), {}).items():
            v = params.get(name)
            if t == "point" and isinstance(v, (list, tuple)) and len(v) == 2 \
                    and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in v):
                params[name] = [list(v)]
        norm.append({"id": str(i), "type": str(b["type"]), "params": params, "repeat": 1})
    try:
        dashboard_blocks.compile_blocks(norm)
    except ValueError as e:
        return None, str(e)
    for i, b in enumerate(norm, start=1):
        for h in _handles_in(b, field_types):
            if h not in handles:
                return None, (f"block {i} ({b['type']}): handle {h!r} is not in CONTEXT; use only handles listed there")
    return norm, None


# ── The call ─────────────────────────────────────────────────────────────────

def plan(comment, located, match, backend, palette_text: str, field_types: dict, steps: str = "",
         recipe: list | None = None, examples: list | None = None, log=None) -> ChainProposal:
    handles = known_handles(located.context)
    error = None
    proposal = ChainProposal(comment_id=comment.id, model=getattr(backend, "name", "?"))
    for attempt in (1, 2):
        user = build_user_prompt(comment, located, match, palette_text, steps, recipe, examples, retry_error=error)
        proposal.attempt, proposal.prompt_chars = attempt, len(user) + len(SYSTEM_PROMPT)
        t0 = time.time()
        try:
            raw = backend.complete(SYSTEM_PROMPT, user, ANSWER_SCHEMA)
        except openai_compat.ModelError as e:
            proposal.skip, proposal.reason = True, f"model_error: {e}"
            proposal.latency_s += time.time() - t0
            return proposal
        proposal.latency_s += time.time() - t0
        proposal.raw_reply = str(raw)[:4000]
        try:
            reply = openai_compat.parse_json(raw)
        except ValueError as e:
            error = f"reply was not JSON ({e})"
            proposal.compile_error = error
            if log:
                log(f"  {comment.id}: attempt {attempt} {error}")
            continue
        if reply.get("skip"):
            proposal.skip, proposal.reason = True, str(reply.get("reason") or "model declined")[:400]
            return proposal
        norm, err = validate(reply.get("blocks"), handles, field_types)
        if err is None:
            proposal.blocks, proposal.compile_error = norm, None
            return proposal
        error = err
        proposal.compile_error = err
        if log:
            log(f"  {comment.id}: attempt {attempt} did not validate: {err}")
    proposal.skip, proposal.reason = True, f"did_not_validate: {error}"
    return proposal
