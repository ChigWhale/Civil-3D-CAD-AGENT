"""Text in, tool names out.

One transcribed reviewer comment goes to the language model with the tool
vocabulary (redline/tools.py) and a handful of worked examples; the answer
is the ordered list of tool names the model would call to address it.
Nothing else: no parameters, no handles, no coordinates, no run. Every
comment gets an answer row; an empty or unusable answer is recorded as
empty with the raw reply, never as a skip.

The model backends are the planner's (PlannerBackend protocol and
OpenAICompatPlanner); FakeSequencer answers by rule for the tests.
"""
import json
import os
import re
import time

from . import openai_compat, tools
from .openai_compat import ModelError
from .planner import OpenAICompatPlanner, PlannerBackend  # noqa: F401  (re-exported for callers)
from .types import ToolSequence

MAX_TOOLS = 8
EXAMPLES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples", "sequences.json")

SYSTEM_PROMPT = """You are a Civil 3D drafter addressing a reviewer's handwritten comment on a plan sheet.
You are given ONE comment and the list of TOOLS you may call. Answer with the ordered list of tool
names you would call to address the comment: the names only, no parameters, no explanation.
Rules:
1. Use names from TOOLS only, spelled exactly.
2. 1 to 8 tools, in the order you would call them.
3. When the comment is about an existing object (move it, change it, delete it, label it), start with
   object_at (the object under the comment's arrow), then the tools that change the drawing. Use
   nearby to see everything around a spot, find_entities for every object of one kind, and
   describe_location to understand a place before drawing something new there.
4. Always answer with at least one tool, even when the comment is unclear; pick the closest fit.
Answer as JSON: {"tools": ["first_tool", "second_tool"]}"""


def answer_schema(names: list) -> dict:
    return {"title": "sequence", "type": "object",
            "properties": {"tools": {"type": "array", "items": {"type": "string", "enum": list(names)},
                                     "minItems": 1, "maxItems": MAX_TOOLS}},
            "required": ["tools"]}


def load_examples(path: str | None = None) -> list:
    path = path or EXAMPLES_FILE
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [e for e in data if isinstance(e, dict) and e.get("comment") and e.get("tools")]


def build_user_prompt(comment, tools_text: str, examples: list | None = None, retry_error: str | None = None,
                      mode: str = "full") -> str:
    """The user message. mode "full" carries the tool list and the few-shot
    examples; "short" carries the comment line only, the prompt a model
    fine-tuned on this task (finetune/) is trained and served on."""
    parts = []
    if mode != "short":
        parts += ["TOOLS:", tools_text, ""]
        if examples:
            parts.append("EXAMPLES:")
            for e in examples:
                kind = f" kind={e.get('kind')}" if e.get("kind") else ""
                parts.append(f'comment: text="{e["comment"]}"{kind} -> tools: {", ".join(e["tools"])}')
            parts.append("")
    text = str(comment.text).replace('"', "'")
    parts.append(f'COMMENT: text="{text}" kind={comment.kind} confidence={float(comment.confidence):.2f}')
    if retry_error:
        parts.append(f"Your previous answer could not be used: {retry_error}. Answer again with tool names from TOOLS only.")
    parts.append('ANSWER as JSON: {"tools": [...]}')
    return "\n".join(parts)


def _candidates(reply: str) -> list:
    """The names a reply offers: {"tools": [...]}, a bare JSON list, or a
    line of names separated by commas, arrows or newlines."""
    try:
        obj = openai_compat.parse_json(reply)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        for key in ("tools", "sequence", "calls", "steps"):
            if isinstance(obj.get(key), list):
                return [x if isinstance(x, str) else (x.get("name") or x.get("tool") or x.get("type")) if isinstance(x, dict) else None
                        for x in obj[key]]
        return []
    if isinstance(obj, list):
        return [x if isinstance(x, str) else (x.get("name") or x.get("tool") or x.get("type")) if isinstance(x, dict) else None
                for x in obj]
    text = str(reply or "")
    if not text.strip():
        return []
    return [p for p in re.split(r"[,\n→>]+|->", text) if p.strip()]


def clean(reply: str, names: list) -> tuple:
    """(tools, unknown): normalised known names in order, consecutive repeats
    folded, capped at MAX_TOOLS; and what was dropped."""
    known, unknown = [], []
    for raw in _candidates(reply):
        n = tools.normalise(raw, names)
        if n is None:
            if isinstance(raw, str) and raw.strip():
                unknown.append(raw.strip()[:60])
            continue
        if known and known[-1] == n:
            continue
        known.append(n)
    return known[:MAX_TOOLS], unknown


def sequence(comment, backend, tools_text: str, names: list, examples: list | None = None, log=None,
             system_prompt: str | None = None, mode: str = "full") -> ToolSequence:
    """`system_prompt` lets another caller (the dashboard's prompt block, where
    the text is an instruction typed by the user, not a reviewer's comment)
    keep this parsing and retry while framing the task its own way. `mode`
    is the prompt shape (see build_user_prompt)."""
    out = ToolSequence(comment_id=comment.id, model=getattr(backend, "name", ""))
    schema = answer_schema(names)
    error = None
    for attempt in (1, 2):
        out.attempt = attempt
        prompt = build_user_prompt(comment, tools_text, examples, retry_error=error, mode=mode)
        out.prompt_chars = len(prompt)
        t0 = time.time()
        try:
            reply = backend.complete(system_prompt or SYSTEM_PROMPT, prompt, schema)
        except ModelError as e:
            out.latency_s += time.time() - t0
            out.error = f"model_error: {str(e)[:300]}"
            return out
        out.latency_s += time.time() - t0
        out.raw_reply = str(reply)[:4000]
        found, unknown = clean(reply, names)
        out.unknown_tools = unknown
        if found:
            out.tools = found
            out.error = None
            return out
        error = ("no tool names recognised" + (f" (got: {', '.join(unknown[:5])})" if unknown else " (empty answer)"))
        if log:
            log(f"  {comment.id}: attempt {attempt} {error}")
    out.error = "no_valid_tools: " + error
    return out


class FakeSequencer:
    """Answers by rule: the first rule whose `match` (lower-case substring)
    is in the comment text answers with its `tools`. The default rules cover
    the stub and smoke-test comments; `fallback` answers everything else,
    because this stage never skips."""
    name = "fake"
    DEFAULT_RULES = [
        {"match": "move hyd", "tools": ["object_at", "move_entities"]},
        {"match": "dr14", "tools": ["object_at", "edit_text"]},
        {"match": "change to", "tools": ["object_at", "edit_text"]},
        {"match": "add inlet", "tools": ["describe_location", "draw_shape", "draw_leader"]},
        {"match": "label", "tools": ["object_at", "draw_leader"]},
        {"match": "connect", "tools": ["object_at", "draw_polyline"]},
        {"match": "delete", "tools": ["object_at", "delete_objects"]},
        {"match": "fix", "tools": ["space_labels"]},
    ]

    def __init__(self, rules: list | None = None, fallback: list | None = None):
        self.rules = list(rules) if rules is not None else list(self.DEFAULT_RULES)
        self.fallback = list(fallback) if fallback is not None else ["describe_location"]
        self.calls: list = []
        self._served: dict = {}

    def complete(self, system: str, user: str, schema: dict | None) -> str:
        self.calls.append(user)
        m = re.search(r'^COMMENT:.*?text="(.*?)"', user, re.M | re.S)
        text = (m.group(1) if m else "").lower()
        for i, rule in enumerate(self.rules):
            if str(rule.get("match", "")).lower() in text:
                reply = rule.get("reply", {"tools": rule.get("tools", [])})
                if isinstance(reply, list):          # one per attempt, to exercise the retry
                    n = self._served.get(i, 0)
                    self._served[i] = n + 1
                    reply = reply[min(n, len(reply) - 1)]
                return reply if isinstance(reply, str) else json.dumps(reply)
        return json.dumps({"tools": self.fallback})
