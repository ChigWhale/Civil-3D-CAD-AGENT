"""The instruction -> an ordered list of tool names.

redline.sequence does the work (vocabulary from the MCP registry, a schema
enum so the model cannot invent a name, parsing, one retry); only the
framing differs: an instruction the user typed for the open drawing, with
an optional anchor point, instead of a reviewer's handwritten comment.
"""
import os
import time

from redline import sequence, tools
from redline.types import Comment, ToolSequence

EXAMPLES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples.json")

SYSTEM_PROMPT = """You are a Civil 3D drafter carrying out an instruction the user typed for the open drawing.
You are given ONE instruction and the list of TOOLS you may call. Answer with the ordered list of tool
names you would call to carry it out: the names only, no parameters, no explanation.
Rules:
1. Use names from TOOLS only, spelled exactly.
2. 1 to 8 tools, in the order you would call them.
3. The user may have picked an anchor point in the drawing with the instruction. When the instruction is
   about an existing object (move it, change it, delete it, label it), start with object_at (the object at
   the anchor), then the tools that change the drawing. Use nearby to see everything around the anchor,
   find_entities for every object of one kind, and describe_location to understand a place before
   drawing something new there.
4. Always answer with at least one tool, even when the instruction is unclear; pick the closest fit.
Answer as JSON: {"tools": ["first_tool", "second_tool"]}"""


def comment_for(text: str) -> Comment:
    """The instruction in the shape the sequencer and the filler read."""
    return Comment(id=f"prompt-{int(time.time() * 1000)}", page_no=0, bbox_px=[0, 0, 0, 0],
                   text=str(text or "").strip(), kind="prompt", confidence=1.0)


def compose(text: str, backend, log=None) -> ToolSequence:
    return sequence.sequence(comment_for(text), backend, tools.text(), tools.names(),
                             examples=sequence.load_examples(EXAMPLES_FILE), log=log,
                             system_prompt=SYSTEM_PROMPT)
