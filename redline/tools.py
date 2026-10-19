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

# Tools that only read. Everything else is treated as a write: it is not run
# in a dry run, and a blocked (conflicting) result is honoured.
READ_ONLY = frozenset({
    "describe_location", "object_at", "nearby", "find_entities", "select_entities", "spatial_map", "get_model_space",
    "query_model_space",
    "list_drawings", "get_properties", "entity_key_points", "bounding_box", "measure_distance", "measure_entities",
    "angle_between", "closest_point_on", "intersections", "tangent_points", "point_along", "polar_point",
    "polygon_area", "find_duplicates", "list_layers", "list_styles", "list_block_definitions",
    "block_dynamic_properties", "get_drawing_settings", "check_drawing", "list_sheets", "list_key_map",
    "list_pressure_parts", "grading_label_context", "pipe_network_context", "profile_context",
    "plan_sheets", "plan_grading_sheets", "plan_profile_views", "zoom_view",
})

# ── Bins and risk tiers ──────────────────────────────────────────────────────
# Every offered tool sits in exactly one bin (the sections of COMMANDS.md) and
# one risk tier. Pure tables: nothing here touches the registry, so metrics
# and the results window can import them without importing cad_mcp_server.
BINS = {
    "lookup": frozenset({"object_at", "nearby", "describe_location", "find_entities", "select_entities", "spatial_map",
                         "get_model_space", "query_model_space", "list_drawings", "get_properties", "entity_key_points",
                         "bounding_box", "measure_distance", "measure_entities", "angle_between", "closest_point_on",
                         "intersections", "tangent_points", "point_along", "polar_point", "polygon_area", "find_duplicates",
                         "list_layers", "list_styles", "list_block_definitions", "block_dynamic_properties",
                         "get_drawing_settings", "check_drawing", "quantify"}),
    "draw": frozenset({"draw_line", "draw_polyline", "draw_shape", "draw_rectangle", "draw_polygon", "draw_circle", "draw_arc",
                       "draw_arc_by", "draw_ellipse", "draw_spline", "draw_donut", "draw_point", "draw_construction_line",
                       "draw_revision_cloud", "draw_wipeout", "create_region", "region_boolean", "trace_boundary", "create_hatch"}),
    "modify": frozenset({"move_entities", "copy_entities", "rotate_entities", "scale_entities", "mirror_entities",
                         "stretch_entities", "align_entities", "offset_entities", "array_entities", "fillet_entities",
                         "chamfer_entities", "fillet_polyline", "trim_entities", "extend_entities", "break_entity",
                         "lengthen_entity", "join_entities", "explode_entities", "reverse_curves", "edit_polyline",
                         "edit_spline", "edit_hatch", "mark_along_entity", "set_properties", "match_properties",
                         "set_draw_order", "group_entities", "delete_objects"}),
    "text": frozenset({"draw_text", "draw_mtext", "edit_text", "draw_leader", "dimension_linear", "dimension_angular",
                       "dimension_radial", "dimension_arc_length", "dimension_ordinate", "dimension_continue",
                       "edit_dimension", "draw_tolerance", "create_table", "set_table_cell", "create_text_style",
                       "create_dim_style"}),
    "layer": frozenset({"create_layer", "edit_layer", "move_to_layer", "set_current_layer", "layer_state", "isolate_layers",
                        "purge_unused", "rename_named_object", "set_drawing_settings"}),
    "block": frozenset({"define_block", "insert_block", "define_attribute", "set_block_attributes", "sync_block_attributes",
                        "export_block", "attach_xref", "attach_image", "attach_pdf"}),
    "sheet": frozenset({"create_viewport", "set_viewport", "manage_layouts", "zoom_view", "list_sheets", "plan_sheets",
                        "cut_sheets", "read_key_map", "list_key_map", "plan_grading_sheets", "cut_grading_sheets"}),
    "civil": frozenset({"label_grading", "space_labels", "grading_label_context", "draw_driveways", "draw_rear_swales",
                        "pipe_network_context", "solve_pipe_elevations", "solve_pipe_run", "list_pressure_parts",
                        "lay_pressure_run", "profile_context", "plan_profile_views", "draw_profile_views",
                        "cut_profile_sheets", "produce_profile_sheets", "create_sanitary_profile"}),
}
BIN_ORDER = ("lookup", "draw", "modify", "text", "layer", "block", "sheet", "civil")
BIN_LABELS = {"lookup": "Look things up", "draw": "Draw", "modify": "Modify", "text": "Text, labels, dimensions",
              "layer": "Layers and settings", "block": "Blocks and references", "sheet": "Sheets, layouts and views",
              "civil": "Civil 3D", "other": "Other"}
TIER_ORDER = ("read", "draw", "modify", "delete", "design")      # ascending risk
_BIN_OF = {n: b for b, ns in BINS.items() for n in ns}

# the lookups a chain may start with; collapsed into one class when chains are scored
LOOKUP_CLASS = frozenset({"object_at", "nearby", "find_entities", "describe_location", "select_entities", "spatial_map"})

MAX_DESC = 120
_CATALOG: list | None = None
_REGISTRY: dict | None = None


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


def registry(refresh: bool = False) -> dict:
    """{name: FastMCP Tool} for every registered tool, excluded ones included
    (acknowledge_conflicts is needed to honour a warn policy)."""
    global _REGISTRY
    if _REGISTRY is None or refresh:
        _REGISTRY = {t.name: t for t in _registered()}
    return _REGISTRY


def schema(name: str) -> dict:
    """The tool's parameter JSON schema without `dashboard` (the executor
    supplies it). {} for an unknown tool."""
    t = registry().get(name)
    if t is None:
        return {}
    params = dict(getattr(t, "parameters", None) or {})
    props = {k: v for k, v in (params.get("properties") or {}).items() if k != "dashboard"}
    required = [k for k in (params.get("required") or []) if k != "dashboard"]
    out = {"type": "object", "properties": props, "required": required}
    return out


def takes_dashboard(name: str) -> bool:
    t = registry().get(name)
    return bool(t and "dashboard" in ((getattr(t, "parameters", None) or {}).get("properties") or {}))


def description(name: str, limit: int = 1200) -> str:
    t = registry().get(name)
    doc = " ".join(str(getattr(t, "description", "") or "").split()) if t else ""
    return doc if len(doc) <= limit else doc[:limit - 1].rstrip() + "…"


def is_write(name: str) -> bool:
    return name not in READ_ONLY


# risk tiers: a wrong read costs nothing, a wrong draw is one undo, a wrong
# modify changes something that was right, a delete removes it, a design
# tool changes Civil 3D design data that other work depends on.
RISK = {
    "read": READ_ONLY,
    "delete": frozenset({"delete_objects", "purge_unused"}),
    "design": frozenset({"cut_sheets", "cut_grading_sheets", "read_key_map", "label_grading", "space_labels", "draw_driveways",
                         "draw_rear_swales", "solve_pipe_elevations", "solve_pipe_run", "lay_pressure_run",
                         "draw_profile_views", "cut_profile_sheets", "produce_profile_sheets", "create_sanitary_profile"}),
    "draw": BINS["draw"] | {t for t in BINS["text"] if t.startswith(("draw_", "dimension_", "create_"))}
            | frozenset({"create_layer", "define_block", "insert_block", "define_attribute", "attach_xref", "attach_image",
                         "attach_pdf", "export_block", "create_viewport", "manage_layouts", "group_entities"}),
}


def bin_of(name: str) -> str:
    """The bin a tool name belongs to, or "other" for a name the table does not know."""
    return _BIN_OF.get(str(name), "other")


def risk_of(name: str) -> str:
    """read | draw | modify | delete | design, or "other" for an unknown name."""
    n = str(name)
    if n not in _BIN_OF:
        return "other"
    for tier in ("read", "delete", "design", "draw"):
        if n in RISK[tier]:
            return tier
    return "modify"


def classify(name: str) -> dict:
    return {"bin": bin_of(name), "risk": risk_of(name)}


def chain_class(tool_names: list) -> str:
    """The riskiest tier in a chain: what a wrong run of it would cost.
    "read" for lookups only, "none" for an empty chain; an unknown name
    counts as modify."""
    names = [str(t) for t in (tool_names or [])]
    if not names:
        return "none"
    worst = 0
    for n in names:
        tier = risk_of(n)
        rank = TIER_ORDER.index(tier) if tier in TIER_ORDER else TIER_ORDER.index("modify")
        worst = max(worst, rank)
    return TIER_ORDER[worst]


def classification() -> dict:
    """{name: {bin, risk, description}} for every offered tool (reads the registry)."""
    return {d["name"]: {"bin": bin_of(d["name"]), "risk": risk_of(d["name"]), "description": d["description"]}
            for d in catalog()}


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
