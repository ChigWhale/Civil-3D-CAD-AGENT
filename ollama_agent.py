# =============================================================================
# CAD AGENT — Two-agent LangGraph pipeline
#
# Flow:
#   decompose_model_space → planner → executor → executor → ... → END
#
# The planner breaks the user's request into an ordered list of steps.
# The executor calls tools directly from the plan — no LLM tool-calling needed.
#
# Prerequisites:
#   1. Install Ollama:   https://ollama.com/download
#   2. Pull a model:     ollama pull llama3.2
#   3. Install packages: pip install langchain langchain-ollama langgraph
# =============================================================================

import sys
import json
from typing import Annotated
from constraint_solver import solve_offset

from typing_extensions import TypedDict
from langchain_ollama import ChatOllama
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage, AnyMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages


_raw_json = sys.stdin.read() if not sys.stdin.isatty() else ""


# =============================================================================
# Agent state
# =============================================================================

class AgentState(TypedDict):
    messages:         Annotated[list[AnyMessage], add_messages]
    model_space_data: dict
    plan:             list   # [{"tool": "...", "args": {...}}, ...]
    plan_index:       int    # next step to execute
    results:          list   # collected tool outputs


# =============================================================================
# LLM — used only for planning; no tool binding required
# =============================================================================

llm = ChatOllama(
    model="llama3.2",
    temperature=0,
    base_url="http://localhost:11434",
)


# =============================================================================
# Tools — each returns a JSON string that C# routes and executes
# =============================================================================

@tool
def draw_line(layer: str, start_point: list, end_point: list) -> str:
    """Draw a single line between two [x,y,z] points in Civil 3D model space."""
    return json.dumps({
        "intent": "draw_line",
        "layer": layer,
        "start_point": start_point,
        "end_point": end_point,
    })


@tool
def draw_shape(vertices: list, closed: bool = True, layer: str = "0") -> str:
    """Draw a closed or open shape from a list of 2D [x,y] vertices.
    Use closed=true for rectangles and polygons."""
    return json.dumps({
        "intent": "draw_shape",
        "layer": layer,
        "closed": closed,
        "vertices": vertices,
    })


@tool
def create_layer(name: str, color: int = 7, linetype: str = "Continuous") -> str:
    """Create a new layer. color is an AutoCAD color index (1-255)."""
    return json.dumps({
        "intent": "create_layer",
        "name": name,
        "color": color,
        "linetype": linetype,
    })


@tool
def add_to_layer(layer: str, handles: list) -> str:
    """Move objects onto a layer. handles are AutoCAD object handles from the model space data."""
    return json.dumps({
        "intent": "add_to_layer",
        "layer": layer,
        "handles": handles,
    })


@tool
def edit_layer(name: str, color: int = None, linetype: str = None, visible: bool = None) -> str:
    """Edit an existing layer's properties. Only include properties you want to change."""
    payload = {"intent": "edit_layer", "name": name}
    if color    is not None: payload["color"]    = color
    if linetype is not None: payload["linetype"] = linetype
    if visible  is not None: payload["visible"]  = visible
    return json.dumps(payload)


@tool
def remove_from_layer(handles: list) -> str:
    """Move objects back to layer '0'. handles are AutoCAD object handles from the model space data."""
    return json.dumps({
        "intent": "remove_from_layer",
        "handles": handles,
    })


@tool
def delete_objects(objects: list) -> str:
    """Delete objects from the drawing.
    Each entry must have object_type and object_handle from the model space data.
    Example: [{"object_type": "line", "object_handle": "1A2B"}]"""
    return json.dumps({
        "intent": "delete",
        "objects": objects,
    })

@tool
def quantify(quant: list, typeQuant: str, logicType: str) -> str:
    """Provide quantification of a given object(s)
    Example: How much feet of sewer pipes are used in the current modelspace?
    Other examples can be "How many of these objects exist in modelspace?"
    If the prompt suggests an inequality  ("How many pipes are over 100 ft),
    return the correct inequality symbol to satisfy the logic: *index of item of interest* 'symbol' numerical constraint
    Two examples are pipe(i) > 100 and pipe(i) < 100
    If no inequality symbol exists, return *"""
    return json.dumps({
        "intent": "quantify",
        "quant": quant,
    })


TOOLS    = [draw_line, draw_shape, create_layer, add_to_layer,
            edit_layer, remove_from_layer, delete_objects, quantify]
TOOL_MAP = {t.name: t for t in TOOLS}


# =============================================================================
# Helper — balanced-brace JSON extractor
# =============================================================================

def extract_json(text: str):
    start = text.find('{')
    if start == -1:
        return None
    depth, in_string, escape = 0, False, False
    for i, c in enumerate(text[start:], start):
        if escape:              escape = False; continue
        if c == '\\' and in_string: escape = True; continue
        if c == '"':            in_string = not in_string; continue
        if in_string:           continue
        if c == '{':            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


# =============================================================================
# Node 1 — decompose_model_space
# =============================================================================

def decompose_model_space(state: AgentState) -> dict:
    if not _raw_json:
        return {"model_space_data": {}}
    return {"model_space_data": json.loads(_raw_json)}


# =============================================================================
# Node 2 — planner
# Outputs an ordered plan; respects prerequisites and query grammar.
# =============================================================================

PLANNER_PROMPT = """\
You are a planning agent for a Civil 3D / AutoCAD assistant.
Break the user's request into an ordered list of tool calls.

Available tools:
  draw_line      (layer, start_point [x,y,z], end_point [x,y,z])
  draw_shape     (vertices [[x,y],...], closed bool, layer)
  create_layer   (name, color 1-255, linetype)
  add_to_layer   (layer, handles [...])
  edit_layer     (name, color, linetype, visible)
  remove_from_layer (handles [...])
  delete_objects (objects [{object_type, object_handle},...])
  quantify (quant [{object_type, object_handle, length},...])
  respond        (text) — for questions or when no AutoCAD action is needed

Offset constraint:
  When the user requests an offset of an existing line, add a "constraints" field to the draw_line step.
  Use the handle from the model space data to reference the line.
  Leave start_point and end_point as [0,0,0] — the solver will compute them.
  Example:
  {
    "tool": "draw_line",
    "args": {"layer": "0", "start_point": [0,0,0], "end_point": [0,0,0]},
    "constraints": {"type": "offset", "reference_handle": "<handle>", "distance": <value>, "side": "left|right"}
  }

Ordering rules:
  - A layer must be created BEFORE objects are moved to it
  - Follow the grammatical order of the user's request
  - Prerequisites always come first
  - Each step uses exactly one tool

Respond with ONLY a raw JSON object — no markdown, no explanation:
{"steps": [{"tool": "<name>", "args": {<arguments>}}, ...]}"""


def planner(state: AgentState) -> dict:
    system_content = PLANNER_PROMPT
    if state.get("model_space_data"):
        system_content += (
            f"\n\nCurrent model space:\n"
            f"{json.dumps(state['model_space_data'], indent=2)}"
        )

    response = llm.invoke(
        [SystemMessage(content=system_content)] + state["messages"]
    )

    usage = getattr(response, "usage_metadata", {})
    print(f"[tokens] input={usage.get('input_tokens', '?')} "
          f"output={usage.get('output_tokens', '?')} "
          f"total={usage.get('total_tokens', '?')}", file=sys.stderr)

    steps = []
    try:
        raw = extract_json(response.content)
        if raw:
            steps = json.loads(raw).get("steps", [])
    except Exception:
        pass

    return {"plan": steps, "plan_index": 0, "results": []}


# =============================================================================
# Node 3 — executor
# Calls each tool directly from the plan — no LLM involved.
# =============================================================================

def executor(state: AgentState) -> dict:
    step      = state["plan"][state["plan_index"]]
    tool_name = step.get("tool", "")
    args      = step.get("args", {})

    if tool_name in TOOL_MAP:
        result = json.loads(TOOL_MAP[tool_name].func(**args))
    elif tool_name == "respond":
        result = {"intent": "chat", "text": args.get("text", "")}
    else:
        result = {"intent": "chat", "text": f"Unknown tool: {tool_name}"}

    return {
        "plan_index": state["plan_index"] + 1,
        "results":    state["results"] + [result],
    }


def should_execute(state: AgentState):
    if state["plan_index"] < len(state["plan"]):
        return "execute"
    return END


# =============================================================================
# Node 3 — solver
# Resolves geometric constraints before execution.
# Currently supports: offset (parallel + perpendicular distance)
# =============================================================================

def _find_line_by_handle(model_space_data: dict, handle: str):
    """Returns [E1, N1, E2, N2] for a line matching the given handle, or None."""
    for line in model_space_data.get("lines", []):
        if line.get("handle") == handle:
            sp = line["start_point"]
            ep = line["end_point"]
            return [sp[0], sp[1], ep[0], ep[1]]
    return None


def solver_node(state: AgentState) -> dict:
    solved_plan = []
    for step in state["plan"]:
        constraints = step.get("constraints")
        if constraints and constraints.get("type") == "offset":
            handle   = constraints.get("reference_handle", "")
            distance = float(constraints.get("distance", 0))
            side     = constraints.get("side", "left")
            ref_line = _find_line_by_handle(state["model_space_data"], handle)

            if ref_line is None:
                step = {"tool": "respond", "args": {
                    "text": f"Offset failed: no line with handle '{handle}' found in model space."
                }}
            else:
                result = solve_offset(ref_line, distance, side)
                if result["solved"]:
                    coords = result["line2"]
                    step["args"]["start_point"] = [coords[0], coords[1], 0.0]
                    step["args"]["end_point"]   = [coords[2], coords[3], 0.0]
                else:
                    step = {"tool": "respond", "args": {"text": result["message"]}}

        solved_plan.append(step)
    return {"plan": solved_plan}


# =============================================================================
# Graph
# =============================================================================
print("Raw JSON", _raw_json)

graph = StateGraph(AgentState)

graph.add_node("decompose_model_space", decompose_model_space)
graph.add_node("planner",               planner)
graph.add_node("solver",                solver_node)
graph.add_node("execute",               executor)

graph.set_entry_point("decompose_model_space")
graph.add_edge("decompose_model_space", "planner")
graph.add_edge("planner",               "solver")
graph.add_conditional_edges("solver",  should_execute, {"execute": "execute", END: END})
graph.add_conditional_edges("execute", should_execute, {"execute": "execute", END: END})

agent = graph.compile()


# =============================================================================
# Run — output is a JSON array of results for C# to process in order
# =============================================================================

if __name__ == "__main__":
    question = sys.argv[1] if len(sys.argv) > 1 else "What is in the model?"

    result = agent.invoke({
        "messages":         [HumanMessage(content=question)],
        "model_space_data": {},
        "plan":             [],
        "plan_index":       0,
        "results":          [],
    })

    results = result.get("results", [])
    if not results:
        results = [{"intent": "chat",
                    "text": "I couldn't plan a response. Please try rephrasing."}]

    print(json.dumps(results))
