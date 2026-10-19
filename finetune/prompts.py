"""The prompt the tuned model is trained and served on, rendered by the
pipeline's own code (redline.sequence in "short" mode) so there is one
source of truth. Never imports torch.

    render(text, kind, confidence)  -> (system, user)
    messages(text, kind, confidence) -> [system message, user message]
    completion(tools)                -> '{"tools": ["a", "b"]}'
"""
import json

from . import paths  # noqa: F401  (puts the repo root on sys.path)
from redline import sequence
from redline.types import Comment

MODE = "short"
DEFAULT_KIND = "note"          # what `redline review --text` gives a typed comment
DEFAULT_CONFIDENCE = 1.0


def comment_for(text: str, kind: str = DEFAULT_KIND, confidence: float = DEFAULT_CONFIDENCE, cid: str = "ft") -> Comment:
    return Comment(id=cid, page_no=0, bbox_px=[0, 0, 0, 0], text=str(text), kind=str(kind or DEFAULT_KIND),
                   confidence=float(confidence if confidence is not None else DEFAULT_CONFIDENCE))


def render(text: str, kind: str = DEFAULT_KIND, confidence: float = DEFAULT_CONFIDENCE) -> tuple:
    user = sequence.build_user_prompt(comment_for(text, kind, confidence), "", None, mode=MODE)
    return sequence.SYSTEM_PROMPT, user


def messages(text: str, kind: str = DEFAULT_KIND, confidence: float = DEFAULT_CONFIDENCE) -> list:
    system, user = render(text, kind, confidence)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def completion(tools: list) -> str:
    """The assistant turn: the literal JSON shape the system prompt shows."""
    return json.dumps({"tools": [str(t) for t in tools]})


def completion_messages(tools: list) -> list:
    return [{"role": "assistant", "content": completion(tools)}]


def system_sha1() -> str:
    return paths.sha1_text(sequence.SYSTEM_PROMPT)


def parse_completion(text: str, names: list) -> tuple:
    """(tools, unknown) the pipeline's own parser reads from a reply."""
    return sequence.clean(text, names)
