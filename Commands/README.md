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

The original intents (`draw_line`, `draw_arc`, `draw_shape`, `create_layer`,
`add_to_layer`, `edit_layer`, `remove_from_layer`, `delete`) stay in
`ModelSpaceBuilder.cs`; the three draw intents now return the same `created`
JSON as everything else, and `edit_layer` accepts frozen/locked/plottable/
linetype/lineweight/rgb/description/new_name.

`ModelSpaceCollector` additionally emits `circles`, `ellipses`, `splines`,
`points`, `texts`, `dimensions`, `hatches` and `layers` so objects these commands
create come back with handles from `get_model_space`.

## Known limits

* `fillet_entities` / `chamfer_entities` work on lines, arcs and circles; polyline
  corners go through `fillet_polyline`; explode anything else first.
* `dimension_continue` continues rotated/aligned dimensions only.
* Text is mirrored geometrically by `mirror_entities` (MIRRTEXT is not consulted).
* Plotting to PDF/paper is not exposed; `save_drawing` writes DWG/DXF.
* Not yet exercised against a live Civil 3D session — compiled against AutoCAD 2025
  only. Test each family in a scratch drawing before relying on it.
