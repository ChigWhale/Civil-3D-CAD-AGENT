"""A standalone label set for fine-tuning: a comment's text typed by a
person, and the ordered TOOL NAMES they would use to address it. Nothing
else: no metrics, no parameters, no run to grade. The comments may come from
anywhere (handwriting, Bluebeam, memory); typing them removes the reading
step as a source of error.

    python -m redline.labelset                 # open the window
    python -m redline.labelset --list          # print the examples
    python -m redline.labelset --export FILE   # copy the fine-tuning rows to FILE
    python -m redline.labelset --print-url     # the page URL, nothing opened

Storage: <label set folder>/labelset.json (the document the window edits)
and labelset.jsonl beside it, rewritten on every save, one row per example
in the text-to-text shape ({"text", "tools"} plus who and when). The folder
is CAD_REDLINE_LABELSET_DIR, else <review dir>/labelset, so a process aimed
at a stub plugin keeps its label set under dashboard/uploads-test like
everything else. Examples stay until they are deleted one by one or the set
is discarded.

The dashboard serves the page at /labelset.html and the routes under
/redline/labelset; this module never imports cad_mcp_server on that path.
The tool vocabulary the window offers is a tools.txt snapshot written by the
command line (which may import the server), else the pure bin tables.
"""
import argparse
import getpass
import json
import os
import sys
import time
import urllib.parse

from . import config, tools, view

FILE = "labelset.json"
JSONL = "labelset.jsonl"
TOOLS_FILE = "tools.txt"
SUBDIR = "labelset"
ENV_DIR = "CAD_REDLINE_LABELSET_DIR"
PAGE = "/labelset.html"
PROBE = "/redline/labelset"
VERSION = 1
MAX_TEXT = 2000
MAX_NOTE = 500
SOURCES = ("typed", "handwriting", "markups", "prompt", "other")   # prompt = the dashboard's prompt block


# ── folder and document ──────────────────────────────────────────────────────

def whoami() -> str:
    try:
        return getpass.getuser() or "me"
    except Exception:
        return "me"


def folder(settings=None) -> str:
    env = os.environ.get(ENV_DIR, "").strip()
    if env:
        return env
    settings = settings or config.load()
    return os.path.join(settings.review_dir, SUBDIR)


def path(fold: str) -> str:
    return os.path.join(fold, FILE)


def jsonl_path(fold: str) -> str:
    return os.path.join(fold, JSONL)


def empty() -> dict:
    return {"version": VERSION, "updated": None, "tools_version": None, "next_id": 1, "examples": []}


def load(fold: str) -> dict:
    """The label set document; the empty one when missing or unreadable."""
    p = path(fold)
    if not os.path.isfile(p):
        return empty()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return empty()
    if not isinstance(data, dict) or not isinstance(data.get("examples"), list):
        return empty()
    data.setdefault("version", VERSION)
    data["examples"] = [e for e in data["examples"] if isinstance(e, dict) and e.get("id")]
    if not isinstance(data.get("next_id"), int):
        data["next_id"] = max([_id_number(e["id"]) for e in data["examples"]] + [0]) + 1
    return data


def _id_number(ex_id) -> int:
    try:
        return int(str(ex_id).rsplit("-", 1)[-1])
    except ValueError:
        return 0


def save(fold: str, data: dict) -> None:
    """Writes the document (tmp + os.replace) and rewrites the JSONL beside it."""
    os.makedirs(fold, exist_ok=True)
    p = path(fold)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, p)
    write_jsonl(fold, data)


# ── examples ─────────────────────────────────────────────────────────────────

def validate(text, tool_names, known: list | None = None) -> str | None:
    """None when acceptable, else the reason. A chain names at least one
    tool, and only tools from the offered vocabulary when `known` is given."""
    if not isinstance(text, str) or not text.strip():
        return "the comment text is empty"
    if len(text) > MAX_TEXT:
        return f"the comment text is longer than {MAX_TEXT} characters"
    if not isinstance(tool_names, list) or not tool_names:
        return "the chain needs at least one block"
    for t in tool_names:
        if not isinstance(t, str) or not t.strip():
            return "tools must be non-empty tool names"
    if known is not None:
        unknown = [t for t in tool_names if t not in known]
        if unknown:
            return "unknown tools: " + ", ".join(unknown)
    return None


def find(data: dict, ex_id: str) -> dict | None:
    for e in data.get("examples") or []:
        if e.get("id") == ex_id:
            return e
    return None


def add(fold: str, text: str, tool_names: list, labeller: str = "", note: str = "", source: str = "typed",
        known: list | None = None) -> dict:
    """Appends one example. Raises ValueError when the text or chain is unusable."""
    err = validate(text, tool_names, known)
    if err:
        raise ValueError(err)
    data = load(fold)
    n = int(data.get("next_id") or 1)
    now = time.time()
    ex = {"id": f"ex-{n:04d}", "text": text.strip(), "tools": [str(t) for t in tool_names],
          "labeller": str(labeller or "").strip() or whoami(), "note": str(note or "")[:MAX_NOTE],
          "source": source if source in SOURCES else "typed", "tools_version": data.get("tools_version"),
          "created": now, "updated": now}
    data["examples"].append(ex)
    data["next_id"] = n + 1
    data["updated"] = now
    save(fold, data)
    return dict(ex)


def update(fold: str, ex_id: str, text=None, tool_names=None, labeller=None, note=None, source=None,
           known: list | None = None) -> dict | None:
    """Changes the given fields of one example; None when there is no such
    example. Raises ValueError when the result would be unusable."""
    data = load(fold)
    ex = find(data, ex_id)
    if ex is None:
        return None
    new_text = ex.get("text") if text is None else text
    new_tools = ex.get("tools") if tool_names is None else tool_names
    err = validate(new_text, new_tools, known)
    if err:
        raise ValueError(err)
    ex["text"] = str(new_text).strip()
    ex["tools"] = [str(t) for t in new_tools]
    if labeller is not None and str(labeller).strip():
        ex["labeller"] = str(labeller).strip()
    if note is not None:
        ex["note"] = str(note or "")[:MAX_NOTE]
    if source in SOURCES:
        ex["source"] = source
    ex["updated"] = time.time()
    ex["tools_version"] = data.get("tools_version") or ex.get("tools_version")
    data["updated"] = ex["updated"]
    save(fold, data)
    return dict(ex)


def remove(fold: str, ex_id: str) -> bool:
    data = load(fold)
    before = len(data["examples"])
    data["examples"] = [e for e in data["examples"] if e.get("id") != ex_id]
    if len(data["examples"]) == before:
        return False
    data["updated"] = time.time()
    save(fold, data)
    return True


def discard(fold: str) -> int:
    """Removes the document and the JSONL. Returns how many examples went."""
    data = load(fold)
    n = len(data.get("examples") or [])
    for name in (FILE, JSONL):
        p = os.path.join(fold, name)
        if os.path.isfile(p):
            os.remove(p)
    return n


# ── the fine-tuning rows ─────────────────────────────────────────────────────

def rows(data: dict) -> list:
    """One row per example: the text-to-text pair plus who and when."""
    out = []
    for e in data.get("examples") or []:
        out.append({"id": e.get("id"), "text": e.get("text"), "tools": list(e.get("tools") or []),
                    "labeller": e.get("labeller"), "note": e.get("note") or "", "source": e.get("source") or "typed",
                    "tools_version": e.get("tools_version"), "created": e.get("created"), "updated": e.get("updated")})
    return out


def jsonl_text(data: dict) -> str:
    return "".join(json.dumps(r, default=str) + "\n" for r in rows(data))


def write_jsonl(fold: str, data: dict) -> int:
    os.makedirs(fold, exist_ok=True)
    p = jsonl_path(fold)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(jsonl_text(data))
    os.replace(tmp, p)
    return len(data.get("examples") or [])


def export(fold: str, out_path: str) -> int:
    """Writes the rows to out_path (a copy of labelset.jsonl). Returns the count."""
    data = load(fold)
    parent = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(parent, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(jsonl_text(data))
    return len(data.get("examples") or [])


# ── the vocabulary the window offers ────────────────────────────────────────

def snapshot_tools(fold: str, log=print) -> int:
    """Writes tools.txt (the offered tool names with one-line descriptions)
    from the MCP registry and records the vocabulary version in the
    document. Command-line side only: importing the server costs a second.
    Leaves an older snapshot in place when the server cannot be imported."""
    try:
        text = tools.text()
        version = tools.version()
    except ImportError as e:
        log(f"  (tool list not refreshed: {e})")
        return 0
    os.makedirs(fold, exist_ok=True)
    p = os.path.join(fold, TOOLS_FILE)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    os.replace(tmp, p)
    data = load(fold)
    if data.get("tools_version") != version:
        data["tools_version"] = version
        save(fold, data)
    return len(tools.names())


def tools_block(fold: str) -> dict:
    """{names, bins, risks, bin_order, tier_order, labels, descriptions}:
    the snapshot when there is one, else every name in the bin tables."""
    return view._tools_block(fold)


def known_tools(fold: str) -> list:
    return list(tools_block(fold).get("names") or [])


# ── the window ───────────────────────────────────────────────────────────────

def payload(fold: str) -> dict:
    data = load(fold)
    examples = list(reversed(data.get("examples") or []))
    return {"folder": fold, "file": path(fold), "jsonl": jsonl_path(fold), "count": len(examples),
            "updated": data.get("updated"), "tools_version": data.get("tools_version"),
            "examples": examples, "tools": tools_block(fold), "whoami": whoami()}


def url_for(base: str) -> str:
    return f"{str(base).rstrip('/')}{PAGE}"


def show(settings, log=print) -> bool:
    """Make sure the dashboard serves the page, then open the window on it.
    Never raises; returns whether a window or browser opened."""
    try:
        base = str(settings.dashboard_base).rstrip("/")
        reason = view.ensure_dashboard(base, log=log, probe=PROBE)
        url = url_for(base)
        if reason:
            log(f"labelset: {reason}\n          the page is at {url}")
            return False
        how = view.open_window(url, log=log)
        log(f"labelset: {url}" + ("" if how != "none" else "  (no browser could be started)"))
        return how != "none"
    except Exception as e:
        log(f"labelset: could not open the window ({e})")
        return False


# ── command line ─────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m redline.labelset",
                                 description="Type reviewer comments and drag tool-name blocks into the chain that "
                                             "addresses each one: a label set for fine-tuning. No metrics, no parameters.")
    ap.add_argument("--list", action="store_true", help="print the examples and exit")
    ap.add_argument("--export", metavar="FILE", help="write the fine-tuning rows (JSONL) to FILE and exit")
    ap.add_argument("--print-url", dest="print_url", action="store_true", help="print the page URL instead of opening it")
    ap.add_argument("--dir", dest="dir", help=f"the label set folder (default {ENV_DIR}, else <review dir>/{SUBDIR})")
    ap.add_argument("--review-dir", dest="review_dir", help="the review folder the default label set folder sits under")
    ap.add_argument("--dashboard-base", dest="dashboard_base", help="the dashboard URL (default from the config)")
    ap.add_argument("--no-refresh", dest="no_refresh", action="store_true",
                    help="do not refresh the offered tool list from the MCP server before opening")
    args = ap.parse_args(argv)
    if args.dir:
        os.environ[ENV_DIR] = str(args.dir)
    if args.review_dir:
        os.environ["CAD_REDLINE_DIR"] = str(args.review_dir)
    if args.dashboard_base:
        os.environ["CAD_DASHBOARD_BASE"] = str(args.dashboard_base)
    settings = config.load()
    fold = folder(settings)
    if args.list:
        data = load(fold)
        print(f"{len(data['examples'])} example(s) in {path(fold)}")
        for e in data["examples"]:
            print(f"{e['id']}  {e.get('labeller')}  {' -> '.join(e.get('tools') or [])}\n    {e.get('text')}")
        return 0
    if args.export:
        n = export(fold, args.export)
        print(f"{n} row(s) written to {args.export}")
        return 0
    if args.print_url or not view.is_enabled(settings):
        # CAD_REDLINE_VIEW=0 (tests, headless use): say where the page is, touch nothing
        print(url_for(settings.dashboard_base))
        return 0
    if not args.no_refresh:
        n = snapshot_tools(fold)
        if n:
            print(f"labelset: {n} tools offered (tools.txt refreshed in {fold})")
    return 0 if show(settings) else 1


if __name__ == "__main__":
    sys.exit(main())
