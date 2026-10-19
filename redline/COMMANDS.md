# Comment reviewer: commands and tools

Everything runs from the repo folder in a terminal, with Ollama running. The
`review` and `markups` commands also need Civil 3D open with the CAD-Agent
plugin loaded (port 5199) when `--locate`, `--dry-run` or `--run` is used.

    cd C:\GitHub-Repo-Destination\CAD-Agent

## The workflow

1. **Read** the comments (handwriting on a scan, or Bluebeam text boxes).
2. **Name the tools** for each comment, in order (names only).
3. **Gather what the parameters need**: open the drawing printed on the sheet,
   place each comment in model space, pick the object under its arrow.
4. **Fill the parameters and run the tools**, one tool at a time, through the
   spatial gate.

Each command runs steps 1 and 2 by default; the flags add steps 3 and 4.

## Commands

### Transcribe only (handwriting; nothing planned, nothing opened)

    python -m redline transcribe scans.pdf
    python -m redline transcribe scans.pdf --page 3

`ocr` is another name for `transcribe`. Output: `dashboard\uploads\reviews\ocr-only\<timestamp>\ocr.json`
plus the crops the model read and a `p<N>_regions.png` overlay per page.

### Handwritten redlines (scanned PDFs)

    python -m redline review scans.pdf                       # read + name the tools
    python -m redline review scans.pdf other.pdf --page 1    # several PDFs, one page
    python -m redline review scans.pdf --locate              # + open the drawing, place each comment (nothing drawn)
    python -m redline review scans.pdf --dry-run             # + fill every tool's parameters; lookups run, writes do not
    python -m redline review scans.pdf --run                 # + run the tools, writes included

### Typed comments (no scan; tries the model alone)

    python -m redline review --text "MOVE HYD 20' N" --text "DELETE THIS LINE"

### Bluebeam text boxes and callouts (unflattened PDF)

    python -m redline markups marked.pdf                     # read the text boxes + name the tools
    python -m redline markups marked.pdf --locate            # + open the drawing, place each text box
    python -m redline markups marked.pdf --dry-run           # + fill parameters; lookups run, writes do not
    python -m redline markups marked.pdf --run               # + run the tools, writes included
    python -m redline markups marked.pdf --run --on-conflict warn

`bluebeam` is another name for `markups`. Only FreeText annotations (Text Box,
Callout) are read; clouds, arrows, pen, sticky notes are counted and ignored.
A flattened PDF has no annotations and reports zero text boxes.

### Flags

| flag | meaning |
|---|---|
| `--page N` | only this page; repeat for more |
| `--review-id NAME` | name of the output folder (default: a timestamp) |
| `--locate` | open the drawing printed on the sheet in Civil 3D and place each comment in model space; nothing drawn |
| `--dry-run` | implies `--locate`; fill every tool's parameters and run the read-only steps; write nothing |
| `--run` | implies `--locate`; fill the parameters and run the tools, writes included, through the spatial gate |
| `--on-conflict pause\|warn` | a write the gate blocks: `pause` (default) stops that comment; `warn` acknowledges and draws |
| `--text "..."` | (review only) a typed comment; repeatable; no PDF needed |
| `--ocr openai\|fake`, `--planner openai\|fake` | model backends (`fake` for tests) |
| `--model-base URL` | the OpenAI-compatible endpoint (default Ollama's `http://127.0.0.1:11434/v1`) |
| `--vlm NAME`, `--llm NAME` | the vision and language models (defaults `qwen3-vl:4b-instruct`, `qwen2.5-coder:7b`) |
| `--project-root DIR` | folders searched for `.dwg` files, `;` separated |
| `--review-dir DIR` | where the output folders go |
| `--dpi N` | page render resolution (default 150) |
| `CAD_REDLINE_PROMPT=full\|short` | (environment or `redline.json` `prompt`) the sequencer prompt: `full` (default) carries the tool list and examples; `short` sends the comment line only, for a model fine-tuned with `finetune/` |

### Output

Every run writes `dashboard\uploads\reviews\<review id>\`:

- `report.md`: one row per comment with its text, where it landed, the tool
  sequence; with `--dry-run`/`--run` the tool calls with their parameters and
  what happened; a "Where each comment landed" list with the picked object.
- `report.json`: the same as data.
- `training.jsonl`: one row per comment with empty `human_tools` /
  `human_params` / `human_verdict` slots for a person's correction.
- `tools.txt`: the tool names the model could choose from.
- `pages\`: the rendered pages; `crops\`: the handwriting crops (review only).

Statuses in a dry run or run: `done`, `dry_run`, `needs_human` (a parameter
could not be filled without guessing), `paused` (the gate blocked a write),
`failed`, `not_located`.

### Order of trust

`--dry-run` on every new sheet and read the parameters; then `--run` on a
copy of the drawing before a production file. The dashboard records every
run; Civil 3D's undo is the way back.

### Tests

    python tests/test_redline_sequence.py      # tool vocabulary, sequencer parsing and retry
    python tests/test_redline_markups.py       # Bluebeam reader, rotation mapping, markups review
    python tests/test_redline_sheet.py         # title block parsing, text layer, drawing candidates, layout choice
    python tests/test_redline_fill.py          # parameter schemas, validation, handle guard, executor
    python tests/test_spatial_summary.py       # object summaries, the pick (object_at), nearby, enclosing
    python tests/test_redline_transform.py     # page -> paper -> model transform
    python tests/test_redline_marks.py         # ink detector
    python tests/run_redline.py                # end to end on the stub plugin (starts its own servers on 5399/8799)

## The results window

Every `review`, `markups` and `transcribe` run opens its results in a window
when it ends: the sheet with a numbered pin on every comment, each comment's
text, where it landed, the object under its arrow, the tool chain with a
status mark per step, the parameters the model filled (read-only, under
Details), the outcome and the reason, and a Metrics tab. The window is a
page served by the dashboard (`/redline.html`), opened as an Edge app window
when Edge is installed, else in the default browser.

    python -m redline view                    # reopen the latest run
    python -m redline view 20261013-101500    # a given run (its folder name)
    python -m redline view ocr-only/20261013-101500   # a transcribe run
    python -m redline view --list             # the run ids, newest first
    python -m redline view --print-url        # the page URL without opening it
    python -m redline markups marked.pdf --no-view    # run without opening the window

`CAD_REDLINE_VIEW=0` in the environment (or `"view": false` in `redline.json`)
turns the automatic opening off everywhere; tests set it. The dashboard must
be running; the command starts it when it is not, and restarts it when it is
an older version without the page (only when it is the one `launcher.py`
started; otherwise it prints what to run). The dashboard looks in the same
review folder the command wrote to (`CAD_REDLINE_DIR`, else the data dir, else
`dashboard\uploads\reviews`); a run written somewhere else with
`--review-dir` is not visible to it, and the page says which folder it looked in.

### Labelling in the window

Each comment has three verdict buttons (**Right**, **Wrong**, **Can't
tell**) and a tray. The library at the bottom holds one block per tool name,
grouped by bin; drag blocks into the tray (or click them) to record the chain
you would have used, reorder them, drag one out to remove it. **Blocks are
tool names only; no parameter is ever shown or entered.** Labels save as you
go to `labels.json` in the run's folder, keyed by comment and by labeller
(your Windows user name unless you change it in the header), and are
mirrored into that run's `training.jsonl` rows (`human_tools`,
`human_verdict`, `human_labeller`). **Discard labels** removes the file and
clears those rows. Nothing about a label ever leaves the run's folder.

### Metrics glossary

Every rate is shown with its 95% Wilson interval, the range the true rate is
likely in given how few comments were measured.

| metric | meaning |
|---|---|
| bin | which group a tool call belongs to: lookup, draw, modify, text, layer, block, sheet, civil |
| risk tier | what a wrong call would cost: read, draw, modify, delete, design; a chain's class is its riskiest step |
| doability | four checks on a comment before anything runs: **G**rounded (an object or enclosing shape under the arrow), **A**ction covered (every tool in the chain exists), **P**arameters determinable (no step left unfilled), **U**nambiguous (no rival object within the pick tolerance); score = checks passed / 4 |
| coverage | comments the pipeline attempted (done or dry run) over all comments |
| precision | attempted comments a person marked Right over those marked Right or Wrong |
| risk–coverage | coverage and the wrong-rate at each doability threshold: raising the bar attempts fewer comments and gets more of them right |
| calibration, ECE | whether a doability score of 0.75 really succeeds about 75% of the time; ECE is the average gap |
| tool-set precision / recall / F1 | the model's chain against the labelled chain as sets of tools, with the lookups counted as one class |
| order score | 1 minus the edit distance between the two chains over the longer length |
| first wrong step | the position of the first tool that does not match the labelled chain |
| step accuracy by position | at each step, how often the model is right given it was right so far |
| pass^k | for a comment run k times, the chance every run was right; what unattended use needs |
| kappa | agreement between two labellers beyond chance; above 0.7 the labels are trustworthy |

## The label set (standalone, for fine-tuning)

A separate window for building training examples by hand, with no run
behind them: you type the comment's text yourself (so it does not matter
whether it came from handwriting, Bluebeam or memory, and the reading step
cannot add an error) and drag tool-name blocks into the chain that addresses
it, the same library and tray as the results window. No metrics, and no
parameter is ever shown or entered.

    python -m redline.labelset                 # open the window (refreshes the offered tool list first)
    python -m redline.labelset --list          # print the examples
    python -m redline.labelset --export FILE   # copy the fine-tuning rows (JSONL) to FILE
    python -m redline.labelset --print-url     # the page URL without opening it
    python -m redline.labelset --dir DIR       # another label set folder
    python -m redline.labelset --no-refresh    # do not import the MCP server for the tool list

The window (`/labelset.html` on the dashboard): a text box for the comment, a
tray for its chain, **Save example** (Ctrl+Enter), and the saved examples on
the right with **Edit** and **Delete**. Examples keep until deleted one by
one or **Discard all**. **Download JSONL** hands you the fine-tuning file.

Storage: `labelset.json` (what the window edits) and `labelset.jsonl` beside
it, rewritten on every save, one row per example:

    {"id": "ex-0001", "text": "MOVE HYD 20' N", "tools": ["object_at", "move_entities"],
     "labeller": "nick", "note": "", "source": "typed", "tools_version": "…", "created": …, "updated": …}

The folder is `CAD_REDLINE_LABELSET_DIR`, else `labelset` under the review
folder (so a process aimed at a stub plugin keeps its set under
`dashboard\uploads-test`). `tools.txt` in that folder is the snapshot of
tool names the blocks come from, written when the command opens the window;
without it the page offers every name in the bin tables.

    python tests/test_redline_labelset.py      # the document, jsonl mirror, validation, export, CLI (no browser)

### From labels to a model

`finetune/` (its own README) builds a dataset from the label set plus every
labelled run row, fine-tunes a small model, serves it through Ollama as
`cad-sequencer` and scores it against the stock model on held-out comments:

    python -m finetune setup                                   # once: the training environment
    python -m finetune build                                   # labels -> train / holdout
    python -m finetune eval --llm qwen2.5-coder:7b --prompt full   # the baseline
    python -m finetune train && python -m finetune deploy && python -m finetune eval

To run the pipeline on the tuned model set `CAD_REDLINE_LLM=cad-sequencer`
and `CAD_REDLINE_PROMPT=short`.

## Tools

The names the model chooses from, read from the MCP server's registry
(`cad_mcp_server.py` and `drafting_tools.py`). Read-only tools run in a dry
run; the rest are writes and run only with `--run`. Descriptions are each
tool's own first sentence.

### Look things up (read only)

| tool | read/write | what it does |
|---|---|---|
| `object_at` | read | The ONE object under a point: what a reviewer's arrow tip or a pick means. |
| `nearby` | read | Every object within `radius` ft of a point, summarised and nearest first, no layer tag needed: handle, type (line, poly… |
| `describe_location` | read | What is at a point, in words and in civil terms. |
| `find_entities` | read | Handles of entities within `radius` ft of a point, of one tag or family or (tag left empty) of any kind. |
| `select_entities` | read | SELECT / QSELECT: handles (with a short summary each) of objects matching mode "all", "window" (fully inside 2 corner p… |
| `spatial_map` | read | The drawing as a coarse character map, north up, plus its regions. |
| `get_model_space` | read | Returns a Civil 3D drawing as JSON. |
| `query_model_space` | read | Returns one category of data from the most recently fetched model space JSON. |
| `list_drawings` | read | Lists the drawings currently open in AutoCAD, with their full paths. |
| `get_properties` | read | LIST: every property of the given objects (type, layer, colour, linetype, bounds, and full geometry: points, radii, ver… |
| `entity_key_points` | read | Object snap points: endpoints, midpoints, centers, quadrants, vertices, nodes, insertion points and bounding-box center… |
| `bounding_box` | read | Bounding box (min, max, center, width, height) of a set of objects and of each one. |
| `measure_distance` | read | Distance, delta X/Y and bearing (degrees from +X) between two points. |
| `measure_entities` | read | MEASUREGEOM / AREA / LIST: length, area, radius, included angle, chord and bounds per object plus total length and area. |
| `angle_between` | read | Angle (and acute angle) between two lines/curves at their intersection, plus the intersection point and each direction. |
| `closest_point_on` | read | NEAREST / PERPENDICULAR snap: closest point on each curve to `point` (extend=True allows the curve's extension), with d… |
| `intersections` | read | INTERSECTION / APPARENT INTERSECTION points between two curves, among a set, or of `handles` against `against`. |
| `tangent_points` | read | TANGENT snap: the two points on a circle/arc where lines from from_point touch it. |
| `point_along` | read | Point(s) at a distance (station) or fraction along a curve, with the tangent and left/right normal directions there (fo… |
| `polar_point` | read | Point at `distance` from (x, y) along `angle_deg` (0 = +X, 90 = +Y). |
| `polygon_area` | read | Area, perimeter and centroid of a polygon given as [[x, y], ...] (straight edges only; for a drawn object use measure_e… |
| `find_duplicates` | read | OVERKILL (detect only): groups of identical overlapping lines, arcs, circles, polylines, text, points and block referen… |
| `list_layers` | read | LAYER list: name, colour, linetype, lineweight, on/frozen/locked/plottable, current flag and how many model-space objec… |
| `list_styles` | read | STYLE / DIMSTYLE / MLEADERSTYLE / TABLESTYLE / LINETYPE listing. |
| `list_block_definitions` | read | Block definitions in the drawing: name, entity count, reference count, attribute tags, whether it is an xref (and its p… |
| `block_dynamic_properties` | read | What a block's grips control: its dynamic properties (name, value, units, allowed values, read-only) read from a refere… |
| `get_drawing_settings` | read | UNITS and current defaults: insertion units, linear/angular units and precision, LTSCALE, current layer/text style/dim… |
| `check_drawing` | read | Evaluates acceptance checks against the dashboard's drawing as it is now: a saved playbook's checks (plan_type) or an i… |
| `quantify` | write | Writes a quantity takeoff Excel file for specific objects the user asked about. |

### Draw

| tool | read/write | what it does |
|---|---|---|
| `draw_line` | write | Draws a straight line in the drawing assigned to a dashboard. |
| `draw_polyline` | write | PLINE with arcs and width. |
| `draw_shape` | write | Draws a polyline shape in the drawing assigned to a dashboard. |
| `draw_rectangle` | write | RECTANG: closed polyline from two opposite corners, or corner1 + width/height, optionally rotated about corner1 and wit… |
| `draw_polygon` | write | POLYGON: regular n-gon. |
| `draw_circle` | write | CIRCLE. |
| `draw_arc` | write | Draws an arc in a Civil 3D drawing. |
| `draw_arc_by` | write | ARC by construction method (draw_arc covers center + angles). |
| `draw_ellipse` | write | ELLIPSE (or elliptical arc with start/end angles). |
| `draw_spline` | write | SPLINE through fit points (mode "fit", optional start/end tangent vectors) or from control vertices (mode "control"). |
| `draw_donut` | write | DONUT: filled ring (inner_diameter 0 = filled disc) as a wide polyline. |
| `draw_point` | write | POINT: places point objects (nodes) at [[x, y], ...]. |
| `draw_construction_line` | write | XLINE / RAY: infinite (kind="xline") or semi-infinite (kind="ray") construction line through base_point, towards second… |
| `draw_revision_cloud` | write | REVCLOUD around the polygon [[x, y], ...]; arc_length sets the bump size. |
| `draw_wipeout` | write | WIPEOUT: masking polygon that hides whatever is drawn underneath it. |
| `create_region` | write | REGION from closed curves (closed polylines, circles, or loops of lines/arcs). |
| `region_boolean` | write | UNION / SUBTRACT / INTERSECT of regions: operation applied to `target` with each of `tools` (which are consumed, as in… |
| `trace_boundary` | write | BOUNDARY: closed polyline (or kind="region") of the enclosed area around a point, found from whatever objects surround… |
| `create_hatch` | write | HATCH / GRADIENT-style fill of closed boundaries (handles) or of the area around a point (boundary traced automatically… |

### Modify existing objects

| tool | read/write | what it does |
|---|---|---|
| `move_entities` | write | MOVE by offset [dx, dy] or from_point -> to_point. |
| `copy_entities` | write | COPY: duplicates displaced by offset (or from_point -> to_point); count > 1 repeats the displacement. |
| `rotate_entities` | write | ROTATE about base_point (counter-clockwise positive). |
| `scale_entities` | write | SCALE uniformly about base_point by factor, or by new_length / reference_length. |
| `mirror_entities` | write | MIRROR across the line axis_start-axis_end. |
| `stretch_entities` | write | STRETCH: vertices/endpoints inside the crossing window [[minx,miny],[maxx,maxy]] (or polygon) move by offset; objects e… |
| `align_entities` | write | ALIGN: maps 1 or 2 source points onto destination points (move + rotate, and scale=True also stretches so the second pa… |
| `offset_entities` | write | OFFSET lines, arcs, circles, polylines, ellipses, splines: parallel copy at `distance` on the side of side_point, or th… |
| `array_entities` | write | ARRAY. |
| `fillet_entities` | write | FILLET two lines/arcs/circles with an arc of `radius` (0 = sharp corner join). |
| `chamfer_entities` | write | CHAMFER two lines: bevel with distance_a on the first and distance_b (or distance_a * tan(angle_deg)) on the second. |
| `fillet_polyline` | write | FILLET (or chamfer=True) the corners of one polyline: every corner between straight segments, or only vertex_indices. |
| `trim_entities` | write | TRIM: for each target handle, the piece nearest its pick point (between crossings with the cutting edges) is removed. |
| `extend_entities` | write | EXTEND each target to the nearest boundary beyond the end nearest its pick point (no pick: the end with the closest bou… |
| `break_entity` | write | BREAK: with one point splits the curve in two; with two points removes the piece between them (closed curves: counter-c… |
| `lengthen_entity` | write | LENGTHEN one open curve. |
| `join_entities` | write | JOIN touching lines/arcs/polylines/splines into one object (first handle is the primary). |
| `explode_entities` | write | EXPLODE blocks, polylines, dimensions, hatches, regions, tables into parts. |
| `reverse_curves` | write | REVERSE the direction (start/end) of lines, arcs, polylines, splines. |
| `edit_polyline` | write | PEDIT. |
| `edit_spline` | write | SPLINEDIT. |
| `edit_hatch` | write | HATCHEDIT: change pattern, scale, angle, colours or island style of an existing hatch. |
| `mark_along_entity` | write | DIVIDE (count equal parts) / MEASURE (every segment_length) along a curve, placing points or a named block (marker="BLO… |
| `set_properties` | write | PROPERTIES / CHPROP: set layer, color (ACI index, [r,g,b], "bylayer"), linetype (loaded from acad.lin if needed), linew… |
| `match_properties` | write | MATCHPROP: copy layer/color/linetype/lineweight/linetype_scale/transparency (and text, dimension, hatch settings when b… |
| `set_draw_order` | write | DRAWORDER: position "front", "back", "above" or "below" (the latter two relative to `reference`). |
| `group_entities` | write | GROUP: action create (name + handles), add, remove, dissolve (removes the group, keeps the objects) or list. |
| `delete_objects` | write | Permanently deletes objects from the drawing assigned to a dashboard, by their handle strings (from get_model_space, fi… |

### Text, labels, dimensions, tables

| tool | read/write | what it does |
|---|---|---|
| `draw_text` | write | TEXT: single-line text. |
| `draw_mtext` | write | MTEXT: paragraph text wrapped at `width` (0 = no wrap); "\\P" starts a new line. |
| `edit_text` | write | TEXTEDIT / SCALETEXT / JUSTIFYTEXT: change the content, height, rotation, width, position, justification or style of te… |
| `draw_leader` | write | MLEADER: arrow at points[0], vertices through the rest, then a landing and the text (empty text = leader line only). |
| `dimension_linear` | write | DIMLINEAR / DIMALIGNED between two points; dimension_line_point sets the offset. |
| `dimension_angular` | write | DIMANGULAR between two lines (line_a, line_b), over an arc (arc_handle), or from center + two points. |
| `dimension_radial` | write | DIMRADIUS / DIMDIAMETER on a circle or arc. |
| `dimension_arc_length` | write | DIMARC: arc-length dimension of an arc, placed at arc_point or `offset` outside it. |
| `dimension_ordinate` | write | DIMORDINATE: X or Y datum dimension of a point with its leader ending at leader_end_point. |
| `dimension_continue` | write | DIMCONTINUE / DIMBASELINE from an existing linear dimension through each of `points` in turn (baseline: stacked `spacin… |
| `edit_dimension` | write | DIMEDIT / DIMTEDIT: override text, move the text or dimension line, swap style, or override scale / text height / arrow… |
| `draw_tolerance` | write | TOLERANCE: GD&T feature control frame. |
| `create_table` | write | TABLE at position (top-left). |
| `set_table_cell` | write | Edit table cells: one row/column/text, or cells=[{"row","column","text", "text_height","alignment"}...]; insert_rows /… |
| `create_text_style` | write | STYLE: create or update a text style. |
| `create_dim_style` | write | DIMSTYLE: create (optionally based_on an existing style) or update a dimension style's text height, arrow size, extensi… |

### Layers and settings

| tool | read/write | what it does |
|---|---|---|
| `create_layer` | write | Creates a new layer in the drawing assigned to a dashboard. |
| `edit_layer` | write | Edits an existing layer: color (ACI index) or rgb [r,g,b], visible (on/off), frozen, locked, plottable, linetype (loade… |
| `move_to_layer` | write | Moves objects (identified by their handle strings) to a layer. |
| `set_current_layer` | write | CLAYER: make a layer current (created if missing, thawed if frozen). |
| `layer_state` | write | LAYON/LAYOFF/LAYFRZ/LAYTHW/LAYLCK/LAYULK in bulk. |
| `isolate_layers` | write | LAYISO: show only these layers (others turned off, or mode="freeze"). |
| `purge_unused` | write | PURGE unused named objects: kinds from blocks, layers, linetypes, text_styles, dim_styles (default all). |
| `rename_named_object` | write | RENAME a layer, block, text_style, dim_style, linetype or group. |
| `set_drawing_settings` | write | UNITS / LTSCALE / CLAYER / TEXTSTYLE / DIMSTYLE / COLOR / LINETYPE / LWEIGHT / PDMODE / LIMITS in one call. |

### Blocks and references

| tool | read/write | what it does |
|---|---|---|
| `define_block` | write | BLOCK: define a block from existing objects (copied into the definition with base_point as the insertion point). |
| `insert_block` | write | INSERT a block reference; attributes {TAG: value} fills its attributes. |
| `define_attribute` | write | ATTDEF: add an attribute definition to a block (position in block coordinates, relative to its base point). |
| `set_block_attributes` | write | ATTEDIT: set attribute values {TAG: value} on one block reference. |
| `sync_block_attributes` | write | ATTSYNC: make every reference of a block match its attribute definitions. |
| `export_block` | write | WBLOCK: write a block definition (name) or a set of objects (handles + base_point) to a new .dwg file. |
| `attach_xref` | write | XREF attach (or overlay=True) an external drawing at position. |
| `attach_image` | write | IMAGEATTACH a raster file with its lower-left corner at position; width in drawing units (height follows the aspect rat… |
| `attach_pdf` | write | PDFATTACH one page of a PDF as an underlay. |

### Sheets, layouts and views

| tool | read/write | what it does |
|---|---|---|
| `create_viewport` | write | MVIEW: paper-space viewport in `layout` (center/width/height in paper units) looking at model view_center (a model-spac… |
| `set_viewport` | write | Changes an existing paper-space viewport: on/off, lock, scale, model view centre, twist, layers frozen or thawed in it,… |
| `manage_layouts` | write | LAYOUT: list, create (name), set_current (name), rename (name -> new_name), clone_from_template (name + source_file, a… |
| `zoom_view` | read | ZOOM the user's view: extents, window (points = 2 corners), entities (handles), center (point + height) or scale (facto… |
| `list_sheets` | read | The sheets the dashboard's drawing already has: every layout with its viewports (scale, twist, model window, what the w… |
| `plan_sheets` | read | Dry run of cut_sheets: reads the dashboard's drawing and says how many sheets it takes at which scale, what each window… |
| `cut_sheets` | write | Cuts the sheets on the dashboard, in the background: one all-or-nothing sequence per sheet (template layout cloned, vie… |
| `read_key_map` | write | Reads the grading key map -- ANOTHER drawing, in the same coordinates as the dashboard's, whose model space holds a rec… |
| `list_key_map` | read | The key map cells read for this dashboard (see read_key_map): key, centre, size and twist of each, plus the areas with… |
| `plan_grading_sheets` | read | Dry run of cut_grading_sheets: the sheets the chosen key-map cells make in the dashboard's drawing -- layout name (GR-<… |
| `cut_grading_sheets` | write | Makes the grading sheets on the dashboard, in the background: label_grading then space_labels once over the whole drawi… |

### Civil 3D

| tool | read/write | what it does |
|---|---|---|
| `label_grading` | write | Places the grading-sheet labels on the finished grade (FG) surface, one label per target point, from the drawing's 2D l… |
| `space_labels` | write | Puts grading labels where they belong, two treatments chosen per label: position the finished-grade elevation labels (l… |
| `grading_label_context` | read | Read-only: the surfaces in the drawing -- data-shortcut references included, each flagged is_reference with reference_v… |
| `draw_driveways` | write | Draws a driveway from every lot pad out to the back of road. |
| `draw_rear_swales` | write | Draws a rear yard swale behind every lot pad. |
| `pipe_network_context` | read | Read-only snapshot of a pipe network: every structure (rim, sump, surface elevation, connected pipes) and every pipe (f… |
| `solve_pipe_elevations` | write | Solve (and optionally set) the start/end invert elevations of the same-diameter pipes meeting at one structure so that… |
| `solve_pipe_run` | write | Solve (and optionally set) the inverts of a WHOLE RUN of pipes at once -- every structure along an alignment, an explic… |
| `list_pressure_parts` | read | The drawing's pressure networks (name, parts list, reference surface and alignment, part counts) and every pressure par… |
| `lay_pressure_run` | write | Lays a run of REAL Civil 3D pressure network parts (a water main): pipes along `alignment` offset `offset` ft to `side`… |
| `profile_context` | read | What the drawing offers for sanitary profile views, read-only: per alignment its station range (and whether it starts a… |
| `plan_profile_views` | read | Dry run of draw_profile_views: the profile views the drawing's alignments get -- 1000 ft windows from 1+00 (the whole l… |
| `draw_profile_views` | write | Draws the sanitary profile views plan_profile_views describes, as REAL Civil 3D objects in one all-or-nothing transacti… |
| `cut_profile_sheets` | write | Puts the drawing's profile views on sheets, on the dashboard: two stacked viewports per sheet (the spec's profile.slots… |
| `produce_profile_sheets` | write | The whole sanitary profile sheet production as one dashboard run: draw the profile views (draw_profile_views), then cut… |
| `create_sanitary_profile` | write | The whole sanitary profile production as ONE dashboard run in a set order: 1. |

### Not offered to the model

These manage the agent rather than the drawing and are left out of the vocabulary (`redline/tools.py`, `EXCLUDE`):

`acknowledge_conflicts`, `calibrate_checks`, `call`, `classify_image`, `dashboard_status`, `define_tag`, `edit_learned`, `explain_layer`, `export_exemplar`, `get_playbook`, `get_sheet_spec`, `list_blocks`, `list_cad_commands`, `list_dashboards`, `list_exemplars`, `list_playbooks`, `list_tags`, `open_dashboard`, `playbook_brief`, `read_exemplar`, `rename_word`, `resolve_attention`, `resume_dashboard`, `run_on_dashboard`, `run_results`, `run_sequence`, `save_drawing`, `save_playbook`, `save_sheet_spec`, `stop_dashboard`, `wait_for_dashboards`

_137 tools offered; vocabulary version 925eded5ed1a. Regenerate this list after adding a tool: the sequencer reads the live registry, this file does not._
