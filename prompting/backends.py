"""The models behind the prompt block, and the fakes the tests use.

One OpenAI-compatible endpoint (Ollama / llama-server on model_base) serves
both stages; the model name is `prompt_llm` from cad-agent.json or
CAD_PROMPT_LLM, else the redline `llm`. That single setting is where a
fine-tuned model drops in later.
"""
import os

import requests

from redline import config  # noqa: F401  (puts the repo root on sys.path)
from redline.fill import FakeFiller
from redline.planner import OpenAICompatPlanner
from redline.sequence import FakeSequencer

import cadagent_config

# The fake sequencer's rules: enough for the tests and the stub plugin, which
# draws text but cannot move anything.
FAKE_SEQUENCE_RULES = [
    # grounded-by-grammar prompts (the drawing-reading helpers): a lookup over the whole drawing first
    {"match": "all hydrants", "tools": ["find_entities", "move_entities"]},
    {"match": "hydrants red", "tools": ["find_entities", "set_properties"]},
    {"match": "of the hydrant", "tools": ["find_entities", "draw_text"]},
    {"match": "next to the hydrant", "tools": ["find_entities", "draw_text"]},
    {"match": "offset the water main", "tools": ["object_at", "offset_entities"]},
    {"match": "text on layer", "tools": ["find_entities", "move_to_layer"]},
    {"match": "delete the easement", "tools": ["find_entities", "delete_objects"]},
    {"match": "draw a circle", "tools": ["draw_circle"]},
    {"match": "move hyd", "tools": ["object_at", "move_entities"]},
    {"match": "move", "tools": ["object_at", "move_entities"]},
    {"match": "delete", "tools": ["object_at", "delete_objects"]},
    {"match": "leader", "tools": ["describe_location", "draw_shape", "draw_leader"]},
    {"match": "circle", "tools": ["describe_location", "draw_circle"]},
    {"match": "note", "tools": ["describe_location", "draw_text"]},
    {"match": "text", "tools": ["describe_location", "draw_text"]},
]
FAKE_FALLBACK = ["describe_location"]


def is_fake(settings) -> bool:
    return str(getattr(settings, "planner_backend", "openai") or "").lower() == "fake"


def model_name(settings) -> str:
    if is_fake(settings):
        return "fake"
    return str(cadagent_config.get("prompt_llm") or settings.llm)


def backends(settings, fake_filler_rules=None):
    """(sequencer, filler): one real model for both, or the two fakes."""
    if is_fake(settings):
        return FakeSequencer(FAKE_SEQUENCE_RULES, FAKE_FALLBACK), FakeFiller(fake_filler_rules)
    one = OpenAICompatPlanner(settings.model_base, model_name(settings), timeout=float(settings.model_timeout))
    return one, one


def preflight(settings) -> str | None:
    """None when the block may run, else the reason it must not.

    The fake backend answers by rule, so it is only allowed against a stub
    plugin (CAD_PLUGIN_BASE set): a stray variable on the live server must
    not have rules drawing in a production drawing. The real backend is
    probed so an unreachable model server fails the run with a plain reason
    instead of a timeout per step."""
    if is_fake(settings):
        if not os.environ.get("CAD_PLUGIN_BASE"):
            return ("the fake planner backend (CAD_REDLINE_PLANNER=fake) only runs against a stub plugin "
                    "(CAD_PLUGIN_BASE set)")
        return None
    base = str(settings.model_base).rstrip("/")
    try:
        r = requests.get(f"{base}/models", timeout=3)
    except requests.exceptions.RequestException as e:
        return f"model server not reachable at {base} (CAD_REDLINE_MODEL_BASE): {e.__class__.__name__}"
    if r.status_code >= 500:
        return f"model server at {base} answered HTTP {r.status_code}"
    return None
