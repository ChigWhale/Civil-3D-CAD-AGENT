"""Sheet-wide reviewer notes, kept per drawing.

A starred note at the top of a sheet ("* fix WL's & riprap & verticals") is
not about one spot; it is an instruction for the whole sheet. The pipeline
does not act on it. It records it here, shows it in the report as work for
a person, and hands it to the planner as a standing note for every located
comment on the same drawing, so a chain drawn for "move hydrant" can respect
"all hydrants to face the street". A person applies them and marks them
done (status "done" in the file below, or `mark_done()` here).

Storage: <review_dir>/directives/<drawing stem>.jsonl, one JSON object per
line, appended; the same wording on the same drawing is recorded once.
"""
import json
import os
import re
import time

from .types import Directive

FOLDER = "directives"
UNMATCHED = "_unmatched"


def _stem(dwg_path: str | None) -> str:
    if not dwg_path:
        return UNMATCHED
    base = os.path.splitext(os.path.basename(str(dwg_path).replace("/", "\\")))[0]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base) or UNMATCHED


def path_for(review_dir: str, dwg_path: str | None) -> str:
    return os.path.join(review_dir, FOLDER, _stem(dwg_path) + ".jsonl")


def normalise(text: str) -> str:
    t = str(text or "").strip().lstrip("*★☆ ").lower()
    return re.sub(r"[^a-z0-9'\"/&+.-]+", " ", t).strip()


def load(review_dir: str, dwg_path: str | None = None, include_done: bool = False) -> list:
    """Directives for one drawing (or every drawing when dwg_path is None)."""
    folder = os.path.join(review_dir, FOLDER)
    if not os.path.isdir(folder):
        return []
    names = [_stem(dwg_path) + ".jsonl"] if dwg_path is not None else sorted(os.listdir(folder))
    out = []
    for n in names:
        p = os.path.join(folder, n)
        if not os.path.isfile(p):
            continue
        with open(p, "r", encoding="utf-8") as f:
            for ln in f:
                if not ln.strip():
                    continue
                try:
                    d = Directive(**json.loads(ln))
                except (ValueError, TypeError):
                    continue
                if include_done or d.status != "done":
                    out.append(d)
    return out


def _write_all(path: str, rows: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for d in rows:
            f.write(json.dumps(d.to_dict(), default=str) + "\n")
    os.replace(tmp, path)


def add(review_dir: str, review_id: str, comment, dwg_path: str | None, layout_name: str | None,
        sheet_no: str | None = None) -> Directive:
    """Record a sheet-wide note; the same wording already on file for this
    drawing is returned instead of duplicated."""
    path = path_for(review_dir, dwg_path)
    existing = load(review_dir, dwg_path, include_done=True) if os.path.isfile(path) else []
    key = normalise(comment.text)
    for d in existing:
        if normalise(d.text) == key:
            return d
    d = Directive(id=f"{review_id}:{comment.id}", text=str(comment.text).strip(), review_id=review_id,
                  page_no=comment.page_no, created=time.time(), dwg_path=dwg_path, layout_name=layout_name,
                  sheet_no=sheet_no, comment_id=comment.id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(d.to_dict(), default=str) + "\n")
    return d


def mark_done(review_dir: str, directive_id: str, done: bool = True) -> Directive | None:
    folder = os.path.join(review_dir, FOLDER)
    if not os.path.isdir(folder):
        return None
    for n in sorted(os.listdir(folder)):
        path = os.path.join(folder, n)
        rows = []
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    try:
                        rows.append(Directive(**json.loads(ln)))
                    except (ValueError, TypeError):
                        pass
        hit = next((d for d in rows if d.id == directive_id), None)
        if hit is None:
            continue
        hit.status = "done" if done else "open"
        hit.done_at = time.time() if done else None
        _write_all(path, rows)
        return hit
    return None


def notes_for(review_dir: str, dwg_path: str | None, extra: list | None = None) -> list:
    """The open directive texts the planner is shown for a drawing: those on
    file plus any found in the current review (extra), without repeats."""
    seen, out = set(), []
    for d in list(load(review_dir, dwg_path)) + list(extra or []):
        k = normalise(d.text)
        if k and k not in seen:
            seen.add(k)
            out.append(d.text.strip())
    return out
