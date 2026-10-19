"""Every plan lands in the redline label set (source "prompt"): the prompt
text and the tool names the model chose, for a person to confirm or fix in
the label-set window and for the fine-tune to read later. The same text
planned again updates its row instead of adding a twin. Never raises.
"""
from redline import labelset

LABELLER = "prompt-block"


def _find(data: dict, text: str) -> dict | None:
    key = str(text or "").strip()
    for e in data.get("examples") or []:
        if e.get("source") == "prompt" and str(e.get("text") or "").strip() == key:
            return e
    return None


def record_plan(settings, text: str, tool_names: list, note: str = "", log=None) -> str | None:
    """The example's id, or None when nothing could be recorded (no tools,
    empty text, folder not writable)."""
    try:
        fold = labelset.folder(settings)
        existing = _find(labelset.load(fold), text)
        if existing:
            out = labelset.update(fold, existing["id"], tool_names=list(tool_names), note=note, source="prompt")
            return out["id"] if out else None
        return labelset.add(fold, str(text or "").strip(), list(tool_names), labeller=LABELLER, note=note,
                            source="prompt")["id"]
    except (ValueError, OSError, KeyError, TypeError) as e:
        if log:
            log(f"  (label set not updated: {e})")
        return None


def record_outcome(settings, ex_id: str | None, note: str, log=None) -> None:
    if not ex_id:
        return
    try:
        labelset.update(labelset.folder(settings), ex_id, note=note)
    except (ValueError, OSError, KeyError, TypeError) as e:
        if log:
            log(f"  (label set note not updated: {e})")
