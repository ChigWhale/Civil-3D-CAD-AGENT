"""The tool vocabulary the sequencer picks from: the MCP tools Claude Desktop
sees (cad_mcp_server.py plus drafting_tools.py), minus the ones that manage
the agent rather than touch the drawing.

Importing cad_mcp_server costs about a second and starts nothing: the
dashboard and the MCP loop only run under its __main__ guard.
"""
import hashlib
import re
import sys

from . import config

# Bookkeeping, orchestration and learning tools: not things a drafter does to
# a drawing in answer to a comment.
EXCLUDE = frozenset({
    "call", "run_sequence", "list_cad_commands",
    "list_dashboards", "list_blocks", "run_on_dashboard", "dashboard_status", "wait_for_dashboards",
    "run_results", "resume_dashboard", "stop_dashboard", "resolve_attention", "open_dashboard",
    "acknowledge_conflicts",
    "export_exemplar", "list_exemplars", "read_exemplar",
    "playbook_brief", "save_playbook", "get_playbook", "list_playbooks", "get_sheet_spec", "save_sheet_spec",
    "edit_learned", "define_tag", "rename_word", "explain_layer", "list_tags",
    "calibrate_checks", "classify_image", "save_drawing",
})

MAX_DESC = 120
_CATALOG: list | None = None


def _first_sentence(doc: str) -> str:
    text = " ".join(str(doc or "").split())
    if not text:
        return ""
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    first = m.group(1) if m else text
    return first if len(first) <= MAX_DESC else first[:MAX_DESC - 1].rstrip() + "…"


def _registered() -> list:
    if str(config.ROOT) not in sys.path:
        sys.path.insert(0, str(config.ROOT))
    try:
        import cad_mcp_server
    except Exception as e:   # the packaged Lite build has no server module
        raise ImportError(f"the tool vocabulary is read from cad_mcp_server.py, which could not be imported: {e}") from e
    return list(cad_mcp_server.mcp._tool_manager.list_tools())


def catalog(refresh: bool = False) -> list:
    """[{"name", "description"}] sorted by name, excluded tools left out."""
    global _CATALOG
    if _CATALOG is None or refresh:
        out = []
        for t in _registered():
            if t.name in EXCLUDE:
                continue
            out.append({"name": t.name, "description": _first_sentence(getattr(t, "description", ""))})
        out.sort(key=lambda d: d["name"])
        _CATALOG = out
    return list(_CATALOG)


def names() -> list:
    return [d["name"] for d in catalog()]


def text() -> str:
    return "\n".join(f"- {d['name']}: {d['description']}" if d["description"] else f"- {d['name']}" for d in catalog())


def version() -> str:
    return hashlib.sha1("\n".join(names()).encode("utf-8")).hexdigest()[:12]


_STRIP = re.compile(r"^[\s\"'`*\-•\d.)(\[]+|[\s\"'`*,;:.)(\]]+$")


def normalise(name, known: list | None = None) -> str | None:
    """The catalog name a model's answer means, or None. Case, quotes, list
    markers, a trailing `()` and spaces or dashes for underscores are forgiven."""
    if not isinstance(name, str):
        return None
    s = _STRIP.sub("", name)
    s = re.sub(r"\(\s*\)$", "", s).strip().lower()
    s = re.sub(r"[\s-]+", "_", s)
    if not s:
        return None
    pool = known if known is not None else names()
    if s in pool:
        return s
    return None
