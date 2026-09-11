# General 2D drafting command set

`CadCommands` (this folder) is the plugin side of the ordinary CAD vocabulary —
draw, offset, trim, fillet, dimension, hatch, block, layer, query — exposed one
intent per command through `POST /execute` and one MCP tool per intent in
`drafting_tools.py`. The aim is that an agent can answer any drafting request by
composing these, the same way a drafter composes AutoCAD commands.

Deliberately **not** included (held back on request): ERASE, UNDO, REDO. Commands
that inherently remove geometry (trim, break, explode, join, region booleans,
`replace_originals` on `define_block`) report what they removed under `removed`.

## Contract

Every intent is JSON in, JSON out, one transaction:

```json
{ "intent": "offset_entities", "file": "C:\\x.dwg", "handles": ["2F3"], "distance": 5, "side_point": [10, 10] }
→ { "ok": true, "message": "Offset 1 object(s).", "created": ["2F7"] }
```

* `ok` false ⇒ nothing was written; `message` says why.
* `created` / `modified` / `removed` carry hex handles so the next call can use them.
* Points are `[x, y]` (z optional), angles are degrees counter-clockwise from +X,
  layers are created on demand, colours are ACI index / `[r,g,b]` / `bylayer`.
* Handles are resolved before anything is written; a bad handle aborts the whole call.

`GET /intents` lists what is registered; `get_drawing_settings` returns the same
list alongside units and current defaults.

## The spatial gate (MCP layer, not the plugin)

Every write that leaves `cad_mcp_server.py` -- the drafting tools, `run_sequence`,
`draw_driveways`, `draw_rear_swales`, and the `apply=True` stage of `label_grading`
and `space_labels` -- goes through a check against the drawing's spatial index
(`spatial.py`, cached per drawing by `spatial_cache.py` on the plugin's
`GET /revision` counter). Before posting, the proposed geometry is tested for
crossings through existing linework, for landing inside a pad, and for proximity
(off by default; `layer_tags.json` → `conflicts`). Conflicts come back as

```json
{ "ok": false, "blocked": true, "conflicts": [ {"kind": "crossing", "hits": "2A3F", "tag": "pad", "at": [625, 60]} ], "token": "9f3a1c07e2b4" }
```

and nothing is posted until the agent calls `acknowledge_conflicts(dashboard, token)`
and repeats the same call. After a write the index is rebuilt and the reply gains a
`spatial` block: the cells that changed (described in words), `new_handles`, and
`unexpected` -- changed cells outside the area the request should have touched
(`ok: false` when any exist). Handler compute reports carry `proposed` geometry
(points / segments / boxes) so the same check covers labels.

The plugin side of this is `DrawingRevision` (the counter, bumped by database
events and after every bridge write, returned in `X-Drawing-Revision`), the
`labels` array in the model-space snapshot, and `proposed` in the handler reports.
`CAD_SPATIAL_GATE=0` / `CAD_SPATIAL_VERIFY=0` switch the two halves off. The
dashboard runner keeps its warning-only behaviour and adds a `run_verified` event.

## Driving dashboards from the chat (the free worker)

A dashboard run is the worker with no model in it: a sequence of blocks executed
on the dashboard's drawing as its own background task on the dashboard server,
gated and verified by the spatial index. The chat composes, launches and reads
back; it is never in the loop while the run executes.

MCP tools in `cad_mcp_server.py`:

| Tool | Does |
|---|---|
| `list_blocks(dashboard, group, block_type)` | the block palette with fields, same as the UI |
| `run_on_dashboard(dashboard, blocks, on_conflict, label)` | commits a sequence and starts it; returns the run id at once |
| `dashboard_status(dashboard, since)` | digest per dashboard, open attention items, events since a cursor |
| `wait_for_dashboards(dashboard, timeout)` | long poll (≤45 s) until a run pauses / fails / finishes or an attention item appears |
| `resume_dashboard(dashboard, reason)` | lets a run paused on conflicts draw; the reason is logged with the decision |
| `stop_dashboard(dashboard)` | cancels the current run, paused or running |
| `resolve_attention(item, note)` | clears a failed / unexpected-change item once dealt with |
| `run_results(dashboard, step)` | the plugin's full reply per step of the latest run (label reports, created handles) |
| `open_dashboard()` | opens the UI in the browser, starting the server first if needed |

`label_grading` and `space_labels` default to `background=True`: they take minutes,
longer than a chat tool call stays open, so the MCP tool submits a one-block run
(policy `pause`) and returns the run id at once; the report is read with
`run_results`. In a run, those two commands are gated at their turn: a compute pass
first, its `proposed` labels checked against the drawing, then the apply step.

`on_conflict` is the run's policy when the pre-run check finds conflicts: `pause`
(default from the chat: draw nothing, queue an attention item, wait), `warn` (the
browser default: log and draw), `stop` (fail the run). Every run ends with the
spatial verification on its record; a change outside the planned area also
queues an attention item. Claude Desktop cannot be pinged, so all of this is a
pull: the server keeps a sequence number on every event, a digest per dashboard
and the attention queue (persisted to `dashboard/uploads/runs.json`), and the
chat asks when the user asks. Server routes: `POST /dashboards/{id}/run` (with
`policy`), `/resume`, `/stop`, `GET /dashboards/{id}/status`, `GET /status?since=`,
`GET /wait?since=&timeout=`, `GET /attention`, `POST /attention/{id}/resolve`.

## Sequences

`{"intent": "sequence", "steps": [ {...}, {...} ]}` runs several intents inside one
outer transaction. `"@n"` in any string field is replaced by the first handle step
`n` created, `"@n[i]"` by the i-th. A failing step aborts the outer transaction, so
earlier steps are rolled back too. MCP tool: `run_sequence`.

## Threading

`HttpBridge` runs most intents on a worker thread holding the document lock. Intents
registered with `appThread: true` (boundary tracing, hatch by point, xref attach,
wblock export, layouts, zoom, save) touch `Editor` / `LayoutManager` and are executed
inline on AutoCAD's application thread instead; a sequence containing one of them
runs entirely on that thread.

## Files

| File | Intents |
|---|---|
| `CadCommands.Core.cs` | registry, JSON/geometry helpers, handle resolution, `sequence` |
| `CadCommands.Draw.cs` | draw_point, draw_construction_line, draw_polyline, draw_rectangle, draw_polygon, draw_circle, draw_arc_by, draw_ellipse, draw_spline, draw_donut, draw_revision_cloud, draw_wipeout, create_region, region_boolean, trace_boundary |
| `CadCommands.Modify.cs` | move/copy/rotate/scale/mirror/stretch/align_entities, offset_entities, array_entities, fillet_entities, chamfer_entities, fillet_polyline, trim_entities, extend_entities, break_entity, lengthen_entity, join_entities, explode_entities, mark_along_entity, edit_polyline, edit_spline, reverse_curves, set_properties, match_properties, set_draw_order |
| `CadCommands.Annotate.cs` | draw_text, draw_mtext, edit_text, create_hatch, edit_hatch, dimension_linear/angular/radial/arc_length/ordinate/continue, edit_dimension, draw_leader, draw_tolerance, create_table, set_table_cell, list_styles, create_text_style, create_dim_style |
| `CadCommands.Organize.cs` | list_layers, layer_state, isolate_layers, set_current_layer, purge_unused, rename_named_object, get/set_drawing_settings, define_block, insert_block, list_block_definitions, set_block_attributes, define_attribute, sync_block_attributes, export_block, attach_xref, attach_image, attach_pdf, group_entities, manage_layouts, create_viewport, zoom_view, save_drawing |
| `CadCommands.Query.cs` | list_intents, get_properties, select_entities, entity_key_points, closest_point_on, intersections, tangent_points, measure_entities, angle_between, point_along, bounding_box, find_duplicates |
| `GradingLabels.cs` | label_grading, list_surface_label_styles (also the `grading_labels` handler behind `POST /handler`) |
| `LabelSpacing.cs` | space_labels (also the `label_spacing` handler behind `POST /handler`) |

The original intents (`draw_line`, `draw_arc`, `draw_shape`, `create_layer`,
`add_to_layer`, `edit_layer`, `remove_from_layer`, `delete`) stay in
`ModelSpaceBuilder.cs`; the three draw intents now return the same `created`
JSON as everything else, and `edit_layer` accepts frozen/locked/plottable/
linetype/lineweight/rgb/description/new_name.

`ModelSpaceCollector` additionally emits `circles`, `ellipses`, `splines`,
`points`, `texts`, `dimensions`, `hatches` and `layers` so objects these commands
create come back with handles from `get_model_space`.

## Grading labels (`GradingLabels.cs`)

`label_grading` places Civil 3D surface elevation labels (SEL) and two-point surface
slope labels (SSL) on the finished grade surface, one per target point, from ordinary
2D linework. Pads, lot lines, curb, driveways and swales are found by layer suffix in
model space **and inside xrefs** (`x-plat (ph1a)|c-lots-line` is a lot line), so the
collector walks block references and works in world coordinates.

| kind | style | where |
|---|---|---|
| `pad_fp` | FP | area centroid of each pad (`c-lots-pad`, `-30`, `-40`, `-50`) |
| `lot_row` | elevation | `c-lots-line` ∩ `c-rway-line` |
| `lot_setback` | elevation | `c-lots-line` ∩ `c-lots-stbk` |
| `swale_meet` | elevation | `c-lots-fl` ∩ `c-lots-line` (where neighbouring swale legs land) |
| `swale_high` | elevation | highest FG elevation sampled along each pad's swale pieces |
| `lot_corner` | elevation | `c-lots-line` ∩ `c-lots-line` (and polyline corners) more than 1 ft off the right of way, within `pad_reach` of a pad |
| `curb_transition` | TC | ends of `c-road-back` arc pieces that coincide with the end of a straight piece; ignored within 5 ft of `c-rway-center` (closing chord of a closed curb) |
| `drive_curb` | TC | where the driveway's flare arc lands on `c-road-back`: the outermost `c-driv` piece end sitting on the curb on the `drive_curb_side` (right by default, looking from the pad to the road; `left` / `both`); falls back to the centreline point with a warning when no flare touches the curb |
| `drive_slope` | slope | that same centreline point → where it reaches `c-rway-line` |
| `swale_slope` | slope | each swale bend (junction of two `c-lots-fl` pieces with a direction change, off the lot lines) → cast square to the run, away from the road, onto `c-lots-line`; two per pad |

Request fields: `apply` (default true through `/execute`; the handler's `compute`
stage never writes), `surface` (default: the one surface whose name contains "FG";
data-shortcut references are surfaces in the drawing and count, flagged `is_reference`
with `reference_stale` / `source_exists` in the report),
`fp_style` / `elevation_style` / `tc_style` / `slope_style` / `marker_style` (all
optional: blank means the drawing's default surface spot elevation / slope label style
from its Surface feature settings; a given name matches exactly, else as a unique
case-insensitive substring, and a name that misses falls back to the default with a
warning that lists what exists), `layer` (default `0`), `kinds` (subset of the table), `window`
(`[[xmin,ymin],[xmax,ymax]]`), `existing_tolerance` (a surface label of the same type
within this distance means "already labelled", skip), `pad_reach`, and `layers`
(`{pad, curb, lot_line, setback, right_of_way, centreline, swale, driveway}` suffix
overrides).

The report carries `styles` (per group: requested, used, default?), `labels` (kind, style, point, end, pad), `skipped` (with the reason:
already labelled, no surface coverage, no driveway reaching the curb, no swale bend...),
`warnings` (empty layers, odd counts per pad), `by_kind` counts and, after `apply`, the
`created` handles. Every label created carries an Xrecord (`CADAGENT_GRADING`: kind, pad) that
`space_labels` reads. Label creation is one transaction: any failure rolls every label back.
Style resolution, surface lookup and the label/xref walk run on the application thread.
Where the label text ends up (its dragged state) is out of scope.

`list_surface_label_styles` (handler stage `context`) lists the surfaces, the spot
elevation / slope label styles, marker styles, how many linework pieces each layer
contributed and how many surface labels already exist, so exact names can be copied
into the request.

## Label spacing (`LabelSpacing.cs`)

`space_labels` puts grading labels where they belong, two treatments chosen per label from
the kind tag `label_grading` wrote (an untagged spot elevation whose anchor is within
`curb_tolerance` of `curb_layer` counts as a curb label; `mode` forces one treatment):

* **position** — `lot_row`, `lot_setback`, `swale_meet`, `swale_high`, `lot_corner`: dragged
  clear of linework and of each other by greedy candidate placement with local-search
  repair, scored on a raster occupancy grid (below).
* **align** — `curb_transition`, `drive_curb`: rotated to the back of curb's tangent at the
  anchor (folded so text reads left to right) and otherwise left where they are. Done
  first, so their rotated boxes are obstacles for the positioned labels.
* `pad_fp`, `drive_slope`, `swale_slope` and untagged slope labels are outside the spec and
  listed as skipped unless `include_other`. `kinds` limits a run to the tags named.

1. Linework within reach of the labels (model space and xrefs; every visible layer unless
   `obstacle_layers` / `ignore_layers` say otherwise; surfaces, corridors, hatches and
   views are never obstacles) is rasterised into cells about half a text height across,
   dilated by `clearance`. Text, mtext and mleaders go into a second "label" grid as whole
   boxes; labels that are not being moved contribute their text block only (a dragged
   label's leader and a two-point slope label's line are not obstacles).
2. Each label is tried undragged and dragged in each of 8 directions (`directions`, in
   preference order) at `leader_lengths` (default 1, 2, 3, 4.5 text heights). Every candidate
   is measured by applying the offset and reading the label's extents back, so the text
   box is what Civil 3D actually draws.
3. Cost = `line` × linework cells under the text + `label` × other-label cells under the
   text + `leader` × linework cells under the leader + `length` × leader length in text
   heights + `direction` × preference rank − `stay` when it is the current position
   (`weights`, defaults 10 / 25 / 1 / 1 / 0.5 / 2).
4. Labels are placed most-constrained first (fewest clean candidates), each taking its
   cheapest candidate given the grid so far; then up to `max_passes` repair passes revisit
   every label that still overlaps and move it when a cheaper spot has opened up.

Selection: `handles`, or `types` (`elevation` / `slope` / `surface` / `all`) + `layer`
suffix + `window`. Labels already dragged or pinned stay put and count as obstacles unless
`include_dragged` / `include_pinned`. Compute rolls every temporary measurement back;
draw applies all offsets in one transaction. The report lists, per label, the chosen
offset (or undragged), its text box, `line_hits`, `label_hits`, `cost`, `moved` and
`clean`, plus grid statistics, so a label that could not be cleared is visible rather
than hidden. Deterministic.

## Known limits

* `fillet_entities` / `chamfer_entities` work on lines, arcs and circles; polyline
  corners go through `fillet_polyline`; explode anything else first.
* `dimension_continue` continues rotated/aligned dimensions only.
* Text is mirrored geometrically by `mirror_entities` (MIRRTEXT is not consulted).
* Plotting to PDF/paper is not exposed; `save_drawing` writes DWG/DXF.
* Not yet exercised against a live Civil 3D session — compiled against AutoCAD 2025
  only. Test each family in a scratch drawing before relying on it. `label_grading`
  uses the drawing's default label styles unless told otherwise; run
  `list_surface_label_styles` to see the defaults and what else exists.
