"""The dashboard's prompt block: free text -> ordered tool names -> filled
parameters -> tool calls through the spatial gate.

The three stages are the redline pipeline's, reused unchanged where they
are text-in / tool-names-out:

  compose.py   the instruction -> an ordered list of MCP tool names
               (redline.sequence with its own system prompt and examples)
  helpers.py   per-tool Python helpers that fill parameters from what they
               can see: the prompt's words, the anchor's digest, the spatial
               index, the lookups' results. Whatever they leave open goes to
               the redline LLM filler (redline.fill).
  plan.py      the plan phase: reads run live, writes are filled and shown
  run.py       the execute phase: the plan's writes, in order, through
               redline.act.Executor (the MCP server's own functions, in
               process, so the gate and the verify apply)
  record.py    every plan lands in the redline labelset (source "prompt")
  session.py   the one module that imports cad_mcp_server; built in a thread

The dashboard server (dashboard/server.py, _run_prompt_block) drives it.
Nothing here opens a drawing or chooses a file: the block runs on the
dashboard's own assigned drawing.
"""
