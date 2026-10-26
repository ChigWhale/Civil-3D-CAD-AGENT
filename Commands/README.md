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

## What the model-space snapshot covers

`GET /modelspace` (ModelSpaceCollector + CivilCoverage.cs) reads, without creating
anything in the drawing: lines / arcs / polylines / circles / ellipses / splines /
points / texts / dimensions / hatches / blocks; alignments with profiles; gravity
pipe networks; **pressure networks** (pipes, fittings, appurtenances -- needs
AeccPressurePipesMgd.dll); surfaces with their **border loops** (from the
triangles, skipped above 400k triangles); corridors with **corridor feature lines**
per baseline and code; feature lines; **parcels with loop segments**; **COGO points**
and point groups; sample lines; catchments; gradings; intersections; survey
figures; view frames; profile / section views; Civil 3D **labels**; and every
**paper-space layout** with its viewports (model-space window each) and title texts.
Optional API members are read by name (CivilCoverage) so a member missing on a
release degrades to a missing field, never a failed snapshot.

The spatial index (`spatial.py`) puts all of it in cells: linework and chains as
segments, blocks / texts / labels / structures / fittings / appurtenances / COGO
points / intersections as point pieces, parcel and surface loops as rings,
viewports as rings tagged `viewport`, alignments as linework tagged `alignment`.

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

## Naming layers from their words

A layer name is read as `[xref|]D-MAJR-MINR[-MINR…][-FUTR]` against the abbreviation
dictionary in `layer_words.json` (`layer_words.py`): the major's tag key is the
**family** (`sswr` → `sanitary`), the minors' words are appended to make the **tag**
(`C-SSWR-PIPE` → `sanitary_pipe`, `C-WATR-HYDR` → `water_hydrant`), and the discipline
or a state token sets the piece's **state** (`V-` existing, `C-` proposed, `-FUTR`
future, `DEMO` / `ABNDN` / `TEMP`). Unknown tokens are kept as themselves, so an
unfamiliar layer still gets a stable name and is reported as *unverified*.
Precedence: pad layers → hand rules in `layer_tags.json` (layer / block name) → an
edited entry in `layer_tags.learned.json` → derivation → hand rules on entity type →
the type → `other`. The first time a layer family is seen its derivation is written
to `layer_tags.learned.json`; set `"edited": true` there (or use `edit_learned`) to
override it. Every query word may be a full tag or a family (`water` = every
`water_*`), and `find_entities`, checks (`state`, `other_state`) and the gate
(`state`, `proposed_state` on each conflict) understand states.

Tools: `list_tags` (precedence, families, dictionary and learned summaries, how the
drawing's layers were named), `explain_layer(layer)`, `rename_word(abbr, word, kind,
tag_key, glyph)` (edits the dictionary for every layer using that abbreviation),
`edit_learned(family, tag, family_of, state, note)` (overrides one family),
`define_tag(..., family)` (pins a hand rule). Both server processes reload the rules
and the dictionary when the files change on disk.

## Exemplars and playbooks (representation → generalization)

A finished plan of one type (water, sanitary, street …) is exported as an
**exemplar bundle** under `CAD_EXEMPLAR_DIR/<type>/<name>/` (outside the repo; it
holds client geometry): `snapshot.json`, `base.json` and `delta.json` (the drawing
split by the type's **base spec** `playbooks/<type>/base.json`, which people write:
kept categories, xrefs, tags and kinds are the base, everything else the designer
added), `index_finished/` and `index_base/` (site map, summary, `cells.jsonl` with one
sentence and one card per cell), `diff.json` (base → finished, cell by cell),
`relations.json` (per delta tag: feet, points, region share, station/offset/side per
alignment, nearest base and delta tags, spacing) and `unmatched.json` (layers the
vocabulary has no word for). `exemplars.py` is also a CLI (`export`, `export-json`,
`brief`, `list`); `playbooks.py init <type>` writes a base spec template.

| Tool | Does |
|---|---|
| `export_exemplar(dashboard, plan_type, name, notes, overwrite)` | reads the drawing and writes the bundle; lists delta layers to name with `define_tag` |
| `list_exemplars(plan_type)` / `read_exemplar(plan_type, name, part)` | what is on disk; one part of a bundle |
| `playbook_brief(plan_type, max_chars)` | bounded, coordinate-free digest of every bundle of a type + the output contract |
| `save_playbook(plan_type, markdown, checks_json)` | validates and writes `playbooks/<type>/playbook.md` + `checks.json` |
| `get_playbook` / `list_playbooks` | read back |
| `check_drawing(dashboard, plan_type, checks_json, scope)` | evaluate checks now, whole drawing or the latest run's new handles |
| `calibrate_checks(plan_type, checks_json)` | evaluate checks over every bundle's delta; a check the exemplars fail is a bad check |

**Checks** (`checks.py`) are declarative, evaluated against the spatial index:
`count`, `within`, `no_crossing`, `inside`, `exists_along`, `label_per`, `spacing`,
`offset`; common fields `id, kind, tag, name, layer, scope (delta|all), severity
(error|warn), why`. `run_on_dashboard(..., checks="water")` (or `POST /run` with
`checks`) evaluates them after the run over what it created: results under the run's
`checks`, a `run_checked` event, and an attention item of kind `checks` on failure.
The run still finishes and nothing is undone.

## Sheets (cutting layouts from model space)

A plan type's `playbooks/<type>/sheets.json` says what a sheet of that type looks like
(`sheets.py`): the template layout to clone (`template.file` + `template.layout`, the
title block), where the viewport sits on the paper (`viewport`, inches), the scales allowed
(feet per inch: 40 = 1" = 40'), `overlap`, `margin`, `max_sheets`, `twist_deg`,
`layout_name` (`{type_code}-{n:02d}`), what the sheets are about (`subject.tags`, default the
base spec's delta tags), what stays visible in the viewport (`visible.tags` / `layers` /
`xrefs`; everything else is frozen per viewport; `hidden` wins), the title-block attributes to
fill, the `furniture` (blocks or mtext with an `anchor` -- `sheet` = paper point, `viewport` =
offset from a `corner` -- a `rotation` (`north` follows the twist) and a `when`: `always`,
`first_sheet`, `last_sheet`, `{tag_present}` or a checks.py check with `include_if`), and
`tables` (rows = a tag's entities on that sheet with station/offset along the alignment).

The planner (`sheets.plan_windows`) finds the extent of the subject in the spatial index
(the drawing's main cluster when the subject names nothing), picks the first scale whose
grid needs at most `max_sheets`, tiles the extent with overlapping windows the viewport
shows at that scale, drops windows that hold none of the subject, and numbers the rest
in page order (rows from the top, columns west to east). `sheet_payloads` turns each into
ONE plugin `sequence`: `manage_layouts clone_from_template` (or `create`) →
`create_viewport` (view centre, scale, twist, `freeze_layers`, annotation scale) →
`set_block_attributes` on `@0` (the title block leads the clone's `created` list) →
`insert_block` / `draw_mtext` / `create_table` with `layout`. A sheet exists whole or not
at all.

C# side: `TargetSpace(tr, db, req)` lets `insert_block`, `draw_text`, `draw_mtext`,
`draw_leader`, `create_table`, `draw_polyline` and `draw_rectangle` take a `layout`;
`manage_layouts` gained `clone_from_template` (page setup via `CopyFrom`, entities via
`WblockCloneObjects`, template viewports skipped) and `delete`; `create_viewport` gained
`twist_deg`, `freeze_layers`, `annotation_scale` and replies with the model window/ring;
`set_viewport` edits one. ViewCenter lives in the viewport's DCS, so `Dcs2Wcs` (twist,
target) is used both to set it and to read it back -- the snapshot's `layouts[].viewports[]`
now carry `model_ring` (twisted), `frozen_layers` (names), `locked`, `annotation_scale`, and
every layout lists its `blocks` (position, rotation, attributes) and `tables`.

Runtime: the `cut_sheets` block (dashboard palette, expanded at run time like driveways)
and the chat tools `get_sheet_spec` / `save_sheet_spec` / `plan_sheets` (dry run, in words)
/ `cut_sheets` (runs it on the dashboard) / `list_sheets`. The gate ignores viewport
frames (`spatial.conflicts`), the verifier expects them (`create_viewport` in
`_payload_pieces`), the index keeps `windows`, and two check kinds read them: `covered`
(every subject probe inside an on viewport) and `sheet_has` (a block / table / text on
every layout, from the snapshot). Exemplar bundles gain `sheets.json`
(`sheets.sheet_stats`: scales, viewport rectangles, overlap, margin, frozen sets,
furniture) and the brief a "Sheets in the exemplars" section; `get_sheet_spec` on a type
with no spec suggests one from those numbers.

Not done: match lines between adjacent sheets, Civil 3D pipe/structure tables, Sheet Set
Manager (layouts are named predictably for it), an along-alignment window strategy for
plan-and-profile sets. Untested against Civil 3D so far: `clone_from_template` on a real
.dwt, `FreezeLayersInViewport`, annotation-scale creation.

## Pressure runs (real water parts)

`Commands/CadCommands.Pressure.cs`: `list_pressure_parts` (read-only: networks, every
pressure parts list with pipes / fittings / appurtenances by description, type, family,
nominal diameter, elbow angle) and `lay_pressure_run` (`path`, `pipe_size`, `fittings:
auto` + `bend_tolerance_deg`, `appurtenances: [{point, part, size}]`, `network`,
`parts_list`, `layer`, `apply`). apply=false resolves every part against the list and
reports `proposed` {segments, points}; apply=true creates the network when missing
(`PressurePipeNetwork.Create` + `PartsListId`), `AddLinePipe` per segment, `AddFitting`
(the elbow nearest the deflection) at bends with `ConnectToPipe` on both ports, and
`AddAppurtenance` per point, in one transaction. Parts are matched by description
substring or nominal diameter; appurtenances by `PressurePartType` name (Hydrant, Valve
...) or description. Python: `pressure_run.plan` builds the path as the alignment offset
to a side between two stations with hydrants every N ft; the `lay_pressure_run` block
(expanded at run time) and the chat tools `list_pressure_parts` / `lay_pressure_run`
(compute with the gate's verdict, or a background dashboard run). The gate reads the
path from the payload (`spatial._one_payload_pieces`). Untested live: part matching
against a real catalog, `ConnectToPipe` port numbering, appurtenance connection (they
are placed on the path, not tied in). Gravity networks (sanitary, storm) are not created
yet.

## Sanitary profile sheets

`Commands/CadCommands.Profiles.cs`, three intents, all on the application thread:

* `profile_context` (read-only): per alignment the station range, whether it starts at
  1+00 (`restation {needed, shift}`), the EG surface sampled every `sample_step` ft
  (`Alignment.PointLocation` + `Surface.FindElevationAtXY`, off-surface points skipped),
  existing profiles and profile views, the pipes and structures of the drawing's gravity
  networks that lie along it (`Alignment.StationOffset` within `offset_tolerance`; a pipe
  needs both ends near the alignment, an end past the alignment's ends counts as
  `partial`), each pipe with `start/end_station`, `start/end_invert` (= endpoint Z - inner
  diameter / 2; the raw Z is reported too), diameter, slope; each structure with station,
  rim, sump, part size. Plus the surfaces, networks, the style catalogues (profile view
  styles with their vertical exaggeration, band sets, profile styles and label sets, pipe
  profile / structure / major station label styles) with the drawing's defaults, and the
  drawing extent. `surface`: exact or unique substring, blank = "EG" or the only surface.
* `draw_profile_views`: `{apply, span, surface, restation, restation_to, station_host:
  flowline|eg, station_increment, drag_to_datum {pipe, station, lift}, styles {...},
  views: [{name, alignment, alignment_handle, station_start, station_end, elevation_min,
  elevation_max, insertion, pipes: [handles], structures: [handles]}], proposed}`. apply=false
  validates (alignments, surface, styles, free names, handles) and reports; apply=true, in
  ONE transaction: restation (`ReferencePointStation`), the EG profile
  (`Profile.CreateFromSurface`, reused when an EG profile from that surface exists), the
  flowline profile (`Profile.CreateByLayout` + a PVI per pipe end, lowest invert wins),
  `ProfileView.Create` with user-specified station and elevation ranges, the band items
  pointed at EG / flowline, `Part.AddToProfileView` for the view's parts (the new
  `ProfileViewPart` found by diffing model space), `PipeProfileLabel.Create` per pipe,
  `StructureProfileLabel.Create` twice per manhole (`structure_label`,
  `structure_alignment_label`), `ProfileStationLabelGroup.Create` on the host profile every
  `station_increment`, then pipe labels and station sub-entities dragged so their text sits
  `lift` model units above the datum line. Any failure aborts: "nothing was kept". Styles
  are picked like grading labels: exact name, unique substring, else the drawing default
  with a warning; the flowline profile prefers a "no display" style when the drawing has one.
* `label_profile_views`: the same label rules on views that already exist (`views: [names]`,
  blank = all); parts shown in a view are found by their ProfileViewPart's extents.

Python: `profiles.py` plans from the context (windows of 1000 ft from 1+00, the remainder
as one view; the datum is a multiple of 4 ft chosen so the content -- inverts, sumps, rims,
surface -- sits in the middle of the 36 or 40 ft span (`centred_datum`); a window the span
cannot hold is split at the 1+00 nearest its middle, a half that still does not fit gets a
raised top and a warning; views placed east of the drawing, one row per alignment) and emits
`proposed` boxes with fill lines so the verifier counts the labels as planned.

Labels, as drawn 2026-09-16 on the test drawing: label styles are a tree
(`LabelStyle.ChildrenCount` / indexer), so the catalogue lists children ("MH", "MH —> Alignment"
under "— SANITARY PROFILE [STRUCTURES] —") and names are matched loosely (`NormStyle`: any run
of dashes before ">" is one arrow, spacing and case ignored), so the spec can say "MH ->
Alignment". Two rows inside each view: bottom (station labels every 1+00 on the surface
profile, then the "MH" label per manhole, then the pipe labels), top ("MH —> Alignment"); each
label is dragged to its row and settled against the labels already there, so two pipes over
one run stack rather than overlap. Furniture may be an xref (`{"xref": "x-border",
"source_file": ...}`, `attach_xref` with `layout`): the border, north arrow and notes
come from the x-border xref, not blocks.

Order of work (2026-09-17): a run goes **draw the views -> arrange them -> create and place
labels and blocks, last**. `draw_profile_views` creates the views, profiles and parts only
(no labels, no blocks -- nothing with a position that a move could leave behind); the
`draw_profile_views` block then adds a deferred `profile_place` step, planned at its turn:
`arrange_profile_views` (the drawing's views into their sheet windows, two per sheet,
alphabetical; idempotent -- views already in a window are not moved) and
`drag_profile_labels`, which on each view that has no labels yet CREATES them (pipe,
"MH", "MH —> Alignment", station group) and then places everything by dimension anchor,
plotted inches from the graph view top or bottom (`profile.dimension_anchor`: top -3.0 for
"MH —> Alignment", bottom 0.75 for "MH", station 0.5, pipe 0.2 + pipe_row 0.35 per extra
pipe over one run) via the per-label `Label.DimensionAnchorOption` / `DimensionAnchorValue`
and the per-sub-entity pair for station labels (the label-group `DefaultDimensionAnchor*`
pair crashes Civil 3D). ONE "Ano-ProfDimLine" dynamic block per view spans its whole station
range along the datum (`dim_line.lift` above it; `dim_line.property` "Distance1" = span, then
shifted onto the view's start).
`arrange_profile_views` carries a moved view's dimension-line blocks along (`dim_lines.block`
in its request; the labels are the view's children and follow by themselves), so a later
`cut_profile_sheets` (which arranges again) leaves nothing behind. The creating transaction
also sets the pipe network's "Default Profile Label Placement" for structures around each
`StructureProfileLabel.Create` (`dimension_anchor.at_creation`), then puts it back.

**Create Sanitary Profile** (block `create_sanitary_profile`, chat tool of the same name;
2026-09-17) is the two blocks as one run in a set order: 1. `draw_profile_views` with
`skip_failures` -- JUST the views (no labels, no blocks), alphabetical by alignment, one
transaction per alignment, an alignment that throws is rolled back and listed under
`skipped` with the step that failed; alignments whose views already exist are left out
(`profiles.plan(skip_existing=True)`); 2. the deferred `profile_sheets` (arrange two to a
window, centred, then the sheets); 3. LAST the deferred `profile_labels`: one
`drag_profile_labels` over the views, which creates the labels and the dimension line and
anchors the pipe and station labels to the view bottom. The "MH" and "MH —> Alignment"
labels are created and left where their styles put them (`do.anchors` false,
`dimension_anchor.at_creation` false).

Sheets (2026-09-16 afternoon): the span picks the template layout -- `profile.sheets["36"]`
= "DWG to PDF (34x22)", `["40"]` = "DWG to PDF (36x24)" in the
`layout template .dwt` -- with its paper size,
`slots` (paper inches where each view's datum corner goes; the first slot is the upper view)
and a per-span band set. Each sheet is ONE viewport the size of the sheet border
(`create_viewport` with `fit: "layout"` takes the extents of what the cloned layout carries,
else the plot paper size), so the planner lays each sheet's views out as a unit: a "sheet
window" of paper x scale in model space, one row per alignment, a view's insertion = window
origin + slot x scale. `sheets.profile_sheet_payloads` groups the drawing's views the same
way (per alignment and span, in station order), recovers the window from the first view's
Location and slot, and warns when a view is not where the planner put it. `manage_layouts
list` with `source_file` lists a template file's layouts (the "From template" choices) with
paper sizes. Title block and furniture as for plan sheets plus `{alignment}` `{stations}`
`{views}`. Blocks `draw_profile_views` and
`cut_profile_sheets` (the latter expanded at its turn in a run, so both can go in one run);
chat tools `profile_context`, `plan_profile_views`, `draw_profile_views`,
`cut_profile_sheets`, `produce_profile_sheets`. Spec: `playbooks/sanitary/sheets.json`.
Untested live: every Civil 3D
call above (compiled against 2025; signatures checked against AeccDbMgd 13.7), the
centreline-Z convention for inverts, band item write-back, `DraggedOffset` on station
sub-entities, and whether the real profile view extents fit the default slots.

### Grading sheets from a key map (2026-09-21)

A grading set's sheet windows are not tiled: they are drawn in a separate **key map**
drawing (same real-world coordinates as the plan; only its viewport is at a small scale),
one rectangle per sheet with the sheet's number written inside. The linework is seldom
closed polylines, so `keymap.py` builds the planar graph of it (endpoints snapped within
`snap`, crossings and T-junctions split, dangling tails pruned) and takes the faces; a key
text names the smallest face it sits in. `sheets.plan_from_cells` turns cells into a plan:
scale = cell width / viewport width (a 1200 x 840 ft cell on the 30 x 21 in viewport is
1" = 40'), twist from the cell's rotation, layout `{type_code}-{key}` (GR-12), `{key}` in
the title block. No plugin change: `HttpBridge.ResolveDocument` opens the key map .dwg by
path. Blocks `read_key_map` (reads and keeps the cells for the dashboard, no plugin step)
and `cut_grading_sheets` (pick cells; `label_grading` then `space_labels` once over the whole
drawing -- `label_scope: cells` runs them inside each cell's `window` instead -- then one
sequence per cell); chat tools `read_key_map`, `list_key_map`,
`plan_grading_sheets`, `cut_grading_sheets`. Spec: `playbooks/grading/sheets.json`
(`key_map` and `grading` sections). Untested live: the twist sign on a real rotated cell, text
insertion points for keys that are not left-justified, the template on the cut.

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
| `CadCommands.Pressure.cs` | list_pressure_parts, lay_pressure_run |
| `CadCommands.Profiles.cs` | profile_context, draw_profile_views, label_profile_views |

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
  clear of linework, of each other and of each other's leaders by greedy candidate
  placement with local-search repair, scored on a raster occupancy grid (below).
* **align** — `curb_transition`, `drive_curb`: rotated to the back of curb's tangent at the
  anchor (folded so text reads left to right) and, when the style then puts the text on
  the road side, flipped in place so it sits on the outside of the curb — never dragged.
  Outside is the side toward the label's pad (the tagged one, else the nearest on
  `pad_layers` within `side_reach`, 80 ft), else away from a `centreline_layer`
  (`c-rway-center`) line within `centreline_reach` (40 ft: a road's own centreline is
  close, a rear easement's 100 ft off is not this curb's), else toward `row_layer`
  (`c-rway-line`) within `side_reach`; a reference that lies within 15° of the curb
  tangent is ignored. Rotations are normalised to [0, 2π) before they are set: Civil 3D
  silently ignores a negative `RotationAngle`. The flip is Civil 3D's own label
  flip (`AllowsFlipping` / `Flipped`), else a 180° rotation when the style is plan
  readable and does not flip anchors with the text; a label that can do neither is
  reported as a problem and left rotated. `curb_side: keep` rotates only. `curb_layer`
  may be one suffix or a list. Done first, so their rotated geometry is what the
  positioned labels keep clear of.
* `pad_fp`, `drive_slope`, `swale_slope` and untagged slope labels are outside the spec and
  listed as skipped unless `include_other`. `kinds` limits a run to the tags named.

1. Linework within reach of the labels (model space and xrefs; every visible layer unless
   `obstacle_layers` / `ignore_layers` say otherwise; surfaces, corridors, hatches and
   views are never obstacles) is rasterised into cells about a quarter of a text height across,
   dilated by `clearance`. Text and mtext go into a second "label" plane as whole boxes.
   Labels and mleaders that are not being moved are exploded and stamped from what they
   really draw: text, borders and markers into the label plane, leaders and slope arrow
   lines into a third "leader" plane (a label that will not explode falls back to an
   estimated text block and a straight leader; the count is in `grid.fixed_fallback`).
2. Each label is tried undragged and dragged in each of 8 directions (`directions`, in
   preference order) at `leader_lengths` (default 1, 2, 3, 4.5 text heights). Every candidate
   is measured by applying the offset and reading the label's extents back, so the text
   box is what Civil 3D actually draws. Its leader is modelled as a straight segment from
   the anchor to the middle of the text's nearer vertical side.
3. Cost = `line` × linework cells under the text + `label` × other-label cells under the
   text + `leader_label` × (leader cells under the text + other-label cells along the
   leader) + `leader_leader` × leader cells along the leader + `leader` × linework cells
   under the leader + `length` × leader length in text heights + `direction` × preference
   rank − `stay` when it is the current position (`weights`, defaults
   10 / 25 / 25 / 10 / 1 / 1 / 0.5 / 2). `label_gap` (ft, default 0) grows the text box
   when counting, so placed labels keep that much from other text and leaders.
4. Labels are placed most-constrained first (fewest clean candidates), each taking its
   cheapest candidate given the grid so far and stamping its text box and leader; then up
   to `max_passes` repair passes revisit every label that still overlaps anything and move
   it when a cheaper spot has opened up.

Selection: `handles`, or `types` (`elevation` / `slope` / `surface` / `all`) + `layer`
suffix + `window`. Labels already dragged or pinned stay put and count as obstacles unless
`include_dragged` / `include_pinned` (counts per kind in `skipped_dragged_by_kind` /
`skipped_pinned_by_kind`). Compute rolls every temporary measurement, rotation and flip
back; draw applies everything in one transaction. The report lists, per positioned label,
the chosen offset (or undragged), its text box, `line_hits`, `label_hits`,
`leader_label_hits`, `leader_cross_hits`, `cost`, `moved` and `clean`; per aligned label
`rotation_deg`, `curb_distance`, `side` (outside / inside / centred / unknown),
`side_method` (already / flipped / rotated_180 / none), `outward_ref` and `outward_detail`
(what each reference measured); plus grid
statistics, so a label that could not be cleared is visible rather than hidden.
Deterministic.

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
