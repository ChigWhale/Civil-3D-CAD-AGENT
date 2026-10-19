"""A person's verdict on each comment of a review, and the chain they would
have used: tool NAMES only, never a parameter.

Stored beside the run in <review folder>/labels.json, keyed by comment id
and then by labeller, so two people can label the same comments and their
agreement can be measured. Each saved label is mirrored into the review's
training.jsonl row (human_tools / human_verdict / human_labeller) so the
training data carries the labels too. Discarding removes the file and
clears those slots.
"""
import getpass
import json
import os
import time

FILE = "labels.json"
TRAINING = "training.jsonl"
VERDICTS = ("good", "bad", "unsure")
VERSION = 1


def whoami() -> str:
    try:
        return getpass.getuser() or "me"
    except Exception:
        return "me"


def path(folder: str) -> str:
    return os.path.join(folder, FILE)


def empty(review_id: str | None = None, tools_version: str | None = None) -> dict:
    return {"version": VERSION, "review_id": review_id, "tools_version": tools_version, "updated": None, "comments": {}}


def load(folder: str) -> dict:
    """The labels document; the empty one when missing or unreadable."""
    p = path(folder)
    if not os.path.isfile(p):
        return empty()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return empty()
    if not isinstance(data, dict) or not isinstance(data.get("comments"), dict):
        return empty()
    return data


def save(folder: str, data: dict) -> None:
    os.makedirs(folder, exist_ok=True)
    p = path(folder)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, p)


def validate(verdict, tools, known: list | None = None) -> str | None:
    """None when acceptable, else the reason. `known` is the run's
    vocabulary; a chain may only name tools from it."""
    if verdict is not None and verdict not in VERDICTS:
        return f"verdict must be one of {', '.join(VERDICTS)} or null"
    if tools is not None:
        if not isinstance(tools, list):
            return "tools must be a list of tool names"
        for t in tools:
            if not isinstance(t, str) or not t.strip():
                return "tools must be non-empty tool names"
        if known is not None:
            unknown = [t for t in tools if t not in known]
            if unknown:
                return "unknown tools: " + ", ".join(unknown)
    if verdict is None and tools is None:
        return "a label needs a verdict or a chain"
    return None


def set_label(folder: str, review_id: str, comment_id: str, labeller: str, verdict=None, tools=None, note: str = "",
              tools_version: str | None = None, known: list | None = None) -> dict:
    """Writes one labeller's record for one comment and mirrors it into the
    training rows. Raises ValueError on a bad verdict or an unknown tool."""
    err = validate(verdict, tools, known)
    if err:
        raise ValueError(err)
    labeller = str(labeller or "").strip() or whoami()
    data = load(folder)
    data.setdefault("version", VERSION)
    data["review_id"] = data.get("review_id") or review_id
    if tools_version:
        data["tools_version"] = tools_version
    rec = {"verdict": verdict, "tools": list(tools) if tools is not None else None, "note": str(note or "")[:500],
           "updated": time.time()}
    data["comments"].setdefault(str(comment_id), {})[labeller] = rec
    data["updated"] = rec["updated"]
    save(folder, data)
    mirror(folder, comment_id, latest(data, comment_id))
    return dict(rec, labeller=labeller)


def clear_label(folder: str, comment_id: str, labeller: str | None = None) -> dict:
    """Removes one labeller's record (or every labeller's) for a comment."""
    data = load(folder)
    recs = data["comments"].get(str(comment_id)) or {}
    if labeller:
        recs.pop(str(labeller), None)
    else:
        recs = {}
    if recs:
        data["comments"][str(comment_id)] = recs
    else:
        data["comments"].pop(str(comment_id), None)
    data["updated"] = time.time()
    save(folder, data)
    mirror(folder, comment_id, latest(data, comment_id))
    return data


def discard(folder: str) -> int:
    """Removes labels.json and clears the human_* slots of every training
    row. Returns the number of rows cleared."""
    data = load(folder)
    p = path(folder)
    if os.path.isfile(p):
        os.remove(p)
    n = 0
    for cid in list(data.get("comments") or {}):
        n += mirror(folder, cid, None)
    if not data.get("comments"):
        n += _rewrite_rows(folder, None, None)
    return n


def latest(data: dict, comment_id: str) -> dict | None:
    """The newest labeller's record for a comment, with "labeller" added."""
    recs = (data.get("comments") or {}).get(str(comment_id)) or {}
    best, who = None, None
    for lab, rec in recs.items():
        if isinstance(rec, dict) and (best is None or float(rec.get("updated") or 0) > float(best.get("updated") or 0)):
            best, who = rec, lab
    return dict(best, labeller=who) if best else None


def _rewrite_rows(folder: str, comment_id: str | None, entry: dict | None) -> int:
    """training.jsonl rows with key == comment_id (every row when comment_id
    is None) get their human_* slots set from entry (cleared when None).
    Same tmp + os.replace as report.annotate; never appends."""
    p = os.path.join(folder, TRAINING)
    if not os.path.isfile(p):
        return 0
    with open(p, "r", encoding="utf-8") as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]
    touched = 0
    for row in rows:
        key = row.get("key", (row.get("comment") or {}).get("id"))
        if comment_id is not None and key != comment_id:
            continue
        row["human_tools"] = list(entry["tools"]) if entry and entry.get("tools") is not None else None
        row["human_verdict"] = entry.get("verdict") if entry else None
        row["human_labeller"] = entry.get("labeller") if entry else None
        row["annotated_at"] = time.time() if entry else None
        touched += 1
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
    os.replace(tmp, p)
    return touched


def mirror(folder: str, comment_id: str, entry: dict | None) -> int:
    return _rewrite_rows(folder, str(comment_id), entry)


def counts(data: dict, total: int) -> dict:
    labelled = sum(1 for recs in (data.get("comments") or {}).values()
                   if any(isinstance(r, dict) and (r.get("verdict") or r.get("tools")) for r in recs.values()))
    return {"labelled": labelled, "total": int(total)}
