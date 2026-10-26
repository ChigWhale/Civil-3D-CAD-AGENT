# The prompt block

A dashboard block (group *Generated*, type `prompt`) that takes an instruction
in the user's words, has a model name the ordered MCP tool calls, fills their
parameters from what code can see in the drawing, and runs them through the
spatial gate. Built 2026-10-19 on the redline pipeline's stages.

## How it is used

1. Add **Prompt** from the Add-block menu. Type the instruction. Say what it is
   about in the words: "all hydrants", "the water main", "10 ft north of the
   hydrant", "the labels inside the pad". Give an **anchor** (model x,y) when
   "the hydrant" could mean several: it works like a redline comment's arrow
   tip, `object_at` and the lookups start there, "the X" means the nearest X,
   and point parameters are filled from it. A write whose location nothing in
   the drawing grounds is refused: `no location: name an object or give an
   anchor`; a kind the drawing lacks is refused too (`no easement found`).
2. Press **Run sequence**. At the block's turn the model names the tools; each
   step is filled by prefill (the anchor), then the helpers, then the model for
   whatever is left. Read-only lookups run as they are filled so later steps
   can use what they found. The plan appears under the fields (`prompt_planned`),
   then the writes run in order through the MCP server's own functions, in the
   dashboard's process, so the spatial gate and the post-run verification
   apply (`prompt_executed`). Conflicts follow the lane's policy: *warn*
   acknowledges and repeats, *pause* puts the decision on the attention queue
   and waits for Resume, *stop* fails the run.
3. The card keeps the plan: tool, who filled it (`prefill` / `helper` / `llm` /
   `human`), status, the parameters, what a lookup found and what a write did.

A step nobody could fill fails the run with the reason and leaves the plan on
the card with status `needs human`: fix the step's JSON (or remove the step)
and Run executes the fixed plan as it is, without planning again. An executed
plan is planned afresh on the next Run (new lookups, the drawing as it is now);
it is never re-executed as it stands. Editing the prompt or the anchor, or
pressing Re-plan, drops the plan.

(Built first as plan-then-run in two presses, 2026-10-19; changed the same day
at the user's request to run straight through.)

A write whose handle comes from an earlier write (draw a shape, then a leader
to it) shows as `needs fill`; it is filled when its turn comes, with the handles
the earlier write created.

## Where things are

| Piece | Module |
|---|---|
| instruction -> tool names | `compose.py` (redline.sequence with its own system prompt and `examples.json`) |
| parameter helpers | `helpers.py` (`@helper("tool", ...)` functions over a `Request`; `SATISFIED` says when a tool needs no model) |
| quantities in words | `measures.py` (feet and inches, radius vs diameter, counts, degrees, factors; quotes blanked first) |
| words -> kinds, layers | `words.py` (`ALIASES` + the layer-word dictionary reversed; `find`, `matches`, `resolve_layer`) |
| the grammar | `subjects.py` (subject, quantifier, qualifiers; `resolve`, `references`, the singular rule) |
| vectors and points | `geometry.py` (`rep_point`, `toward`/`away`/`along`, `side_vector`, `placement`, `site_extent`) |
| plan phase, plan record, phase detection | `plan.py` (`Planner`, `why_replan`, `phase_of`) |
| execute phase | `run.py` (`Runner`, through `redline.act.Executor`) |
| label set | `record.py` (one row per prompt text, source `prompt`, updated on re-plan) |
| registry, backends, Executor | `session.py` (built in a worker thread) |
| models and fakes | `backends.py` |
| dashboard glue | `dashboard/server.py` `_run_prompt_block`, `dashboard/blocks.py` spec `prompt`, `dashboard/static/app.js` `renderPlan` |

## Writing a helper

```python
from prompting.helpers import helper, Filled

@helper("draw_circle")
def radius_from_text(req, so_far):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:ft|')\s*(?:radius|circle)", req.text, re.I)
    if not m or so_far.get("radius") is not None:
        return None
    params = {"radius": float(m.group(1))}
    return Filled(params, complete=not required_missing(req.schema, {**so_far, **params}),
                  note=f"radius from '{m.group(0)}'")
```

`req` carries the prompt text, the anchor and its digest (`locate.comment_context`),
the spatial index and snapshot, the tool's schema, the lookups' results so far
(`[{"tool", "preview", "created"}]`), the handles a parameter may name, what
prefill knew and a per-step `cache`; `so_far` is what earlier helpers filled.
Return the parameters you are sure of. `grounds` are handles you read from the
index (they become nameable); `refuse` is a plain reason the step cannot be
filled (the model is not asked, the run fails there). Whether a tool still needs
the model comes from `SATISFIED` in helpers.py, not from the order helpers run.
Helpers run in registration order, `"*"` helpers first. Pure code: never import
`cad_mcp_server` here. Previews are truncated strings: recover geometry from the
index and `created` handles, never by parsing a preview.

**The grammar** (`subjects.py`). The subject is the first kind phrase that no
relation word governs: "all hydrants", "the water main", "the labels". Its
quantifier says one or many: all / every / each / both, or a bare plural, is the
set; the / this / it, or a singular, is one. Relation phrases bind the kind
phrase after them: "north of the hydrant", "to the left of the road", "within 50
ft of the main", "inside the pad", "along the sewer", "between A and B", "on
layer X", "next to", "at", "toward", "away from", "here". After the subject
they narrow the set; before it, or with a distance in front, or for a draw tool,
they name a place.

The singular rule: the anchor's pick when it is of that kind; else the nearest
of the kind to the anchor; else the only one in the drawing; else a refusal ("2
hydrants in the drawing: say which (give an anchor or a place)"). Left and right
against a road, alignment, curb or centreline (or with nothing named) follow the
civil convention, left of increasing station; against another line, the line's
own direction; against a point, west and east. "Next to X" places 5 ft from X
away from the nearest road. "10 ft circle" is a diameter. Words are resolved in
`words.ALIASES` first (hydrant, water main, road, sewer, lot line, pad, text ...),
then by composing a family word and a part word through the layer-word
dictionary (water + hydrant -> water_hydrant), then as a literal tag.

## Configuration

- `prompt_llm` (`CAD_PROMPT_LLM`, cad-agent.json) - the model; unset = the redline
  `llm`. The slot a fine-tuned model drops into.
- `CAD_REDLINE_MODEL_BASE`, `CAD_REDLINE_MODEL_TIMEOUT` - as for redline.
- `CAD_REDLINE_PLANNER=fake` - rule-based fakes; refused unless `CAD_PLUGIN_BASE`
  is set (a stub plugin), so a stray variable cannot drive the live server.
- `CAD_REDLINE_LABELSET_DIR` - where the label set lives (else `<review dir>/labelset`).

## Limits

- The typed script syntax (`parse_script`) cannot carry a prompt; the block is
  for the browser and the API (`POST /dashboards/{id}/run` with `blocks`).
- Lookups run during planning against the drawing as it is then. A change made
  between Plan and Run is caught by the MCP gate's revision check and by the
  post-run verification (the created handles' cells count as planned).
- Stop takes effect at the next step boundary; a running tool call finishes.
- Covered by helpers: move/copy (compass, toward/away/along/left/right, counts),
  offset (distance + side), draw_circle/text/mtext/rectangle (sizes, relative
  placement), find_entities/nearby (kind -> tag, whole-site scope), move_to_layer
  and set_properties (real layer names, colours), rotate/scale (angle, factor,
  base point), delete. Not covered: draw_leader, dimension_linear, regions ("in
  Block 2"), stations, bearings, metres. "Along" checks proximity, not
  parallelism. Everything not covered still goes to the model, and a model's
  point with nothing to ground it is refused.

