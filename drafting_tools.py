"""General-purpose 2D drafting tools for the CAD Agent MCP server.

One MCP tool per drafting intent implemented in Commands/CadCommands.*.cs, so an
agent can build a sequence of ordinary CAD operations (draw, offset, trim, fillet,
dimension, hatch, block, layer ...) for any request. Every tool returns JSON with
"ok", a message and the handles it "created" / "modified" / "removed", which is
what the next tool in a sequence takes as input.

Conventions shared by every tool here:
  * dashboard   -- REQUIRED id of the dashboard whose drawing to work on. The user
                   assigns drawings to dashboards; call list_dashboards() if unsure.
  * points      -- [x, y] lists in drawing units; angles are degrees, counter-clockwise
                   from +X; "handle" / "handles" are the hex handle strings returned by
                   get_model_space, select_entities or an earlier tool.
  * layer       -- created on the fly if it does not exist; "0" when omitted.
  * Nothing here erases objects on request (no ERASE / UNDO / REDO tools); trim,
    break, explode, join and replace-style edits report what they removed.

Registered from cad_mcp_server.py via register(mcp, observed, post, file_for) so the
tools share the server's dashboard routing, activity reporting and HTTP bridge.
"""

import json
import math


def register(mcp, observed, post, file_for):
    """Attach every drafting tool to the FastMCP instance."""

    def call(intent: str, dashboard: str, **fields) -> str:
        payload = {"intent": intent}
        payload.update({k: v for k, v in fields.items() if v is not None})
        return post(payload, file_for(dashboard))

    # ══════════════════════════════════════════════════════════════════════════
    # Sequences
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    @observed
    def run_sequence(dashboard: str, steps: list[dict]) -> str:
        """Runs several drafting commands as ONE all-or-nothing transaction.

        steps: list of {"intent": <intent name>, ...its fields...} in order, using
        the same field names the individual tools use (the intent name is the tool
        name, e.g. "draw_rectangle", "offset_entities", "fillet_entities").
        Refer to objects made by earlier steps with "@n" (first handle created by
        step n, 0-based) or "@n[i]" (the i-th). Example:
          [{"intent":"draw_rectangle","corner1":[0,0],"corner2":[40,20],"layer":"A"},
           {"intent":"offset_entities","handles":["@0"],"distance":2,"side_point":[20,10]},
           {"intent":"create_hatch","handles":["@1"],"pattern":"ANSI31","scale":10}]
        If any step fails, every step is rolled back and the report names which one.
        Call list_cad_commands for the full intent list."""
        return call("sequence", dashboard, steps=steps)

    @mcp.tool()
    @observed
    def list_cad_commands(dashboard: str) -> str:
        """Lists every drafting intent the plugin accepts (for run_sequence) together
        with the current drawing settings (units, current layer/styles, extents)."""
        return call("get_drawing_settings", dashboard)

    # ══════════════════════════════════════════════════════════════════════════
    # Pure geometry helpers (no drawing access)
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    def polar_point(x: float, y: float, angle_deg: float, distance: float) -> str:
        """Point at `distance` from (x, y) along `angle_deg` (0 = +X, 90 = +Y).
        Use it to turn 'go 25 ft at 30 degrees' into coordinates before drawing."""
        a = math.radians(angle_deg)
        return json.dumps({"point": [round(x + distance * math.cos(a), 6),
                                     round(y + distance * math.sin(a), 6)]})

    @mcp.tool()
    def measure_distance(x1: float, y1: float, x2: float, y2: float) -> str:
        """Distance, delta X/Y and bearing (degrees from +X) between two points."""
        dx, dy = x2 - x1, y2 - y1
        return json.dumps({"distance": round(math.hypot(dx, dy), 6), "dx": round(dx, 6),
                           "dy": round(dy, 6), "angle_deg": round(math.degrees(math.atan2(dy, dx)) % 360, 6),
                           "midpoint": [round((x1 + x2) / 2, 6), round((y1 + y2) / 2, 6)]})

    @mcp.tool()
    def polygon_area(vertices: list[list[float]]) -> str:
        """Area, perimeter and centroid of a polygon given as [[x, y], ...]
        (straight edges only; for a drawn object use measure_entities)."""
        n = len(vertices)
        if n < 3:
            return json.dumps({"error": "need at least 3 vertices"})
        a = cx = cy = 0.0
        perim = 0.0
        for i in range(n):
            (x0, y0), (x1, y1) = vertices[i][:2], vertices[(i + 1) % n][:2]
            cross = x0 * y1 - x1 * y0
            a += cross; cx += (x0 + x1) * cross; cy += (y0 + y1) * cross
            perim += math.hypot(x1 - x0, y1 - y0)
        a /= 2
        if abs(a) < 1e-12:
            return json.dumps({"area": 0, "perimeter": round(perim, 6), "centroid": None})
        return json.dumps({"area": round(abs(a), 6), "perimeter": round(perim, 6),
                           "centroid": [round(cx / (6 * a), 6), round(cy / (6 * a), 6)],
                           "counter_clockwise": a > 0})

    # ══════════════════════════════════════════════════════════════════════════
    # Draw
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    @observed
    def draw_point(dashboard: str, points: list[list[float]], layer: str = "0",
                   display_mode: int | None = None, display_size: float | None = None) -> str:
        """POINT: places point objects (nodes) at [[x, y], ...]. display_mode /
        display_size set PDMODE / PDSIZE (3 = X marker, 34 = circle+cross)."""
        return call("draw_point", dashboard, points=points, layer=layer,
                    display_mode=display_mode, display_size=display_size)

    @mcp.tool()
    @observed
    def draw_construction_line(dashboard: str, base_point: list[float],
                               second_point: list[float] | None = None,
                               angle_deg: float | None = None, kind: str = "xline",
                               layer: str = "0") -> str:
        """XLINE / RAY: infinite (kind="xline") or semi-infinite (kind="ray") construction
        line through base_point, towards second_point or at angle_deg. Useful as a
        cutting/boundary edge for trim_entities / extend_entities."""
        return call("draw_construction_line", dashboard, base_point=base_point,
                    second_point=second_point, angle_deg=angle_deg, kind=kind, layer=layer)

    @mcp.tool()
    @observed
    def draw_polyline(dashboard: str, vertices: list[list[float]], layer: str = "0",
                      closed: bool = False, bulges: list[float] | None = None,
                      width: float = 0.0, elevation: float | None = None,
                 layout: str | None = None) -> str:
        """PLINE with arcs and width. bulges[i] = tan(included_angle/4) for the segment
        leaving vertex i (0 = straight, 1 = semicircle, negative = clockwise). Prefer
        this over draw_shape when a segment must be curved or the line needs width. layout: a paper-space layout name puts it on that sheet instead of model space."""
        return call("draw_polyline", dashboard, vertices=vertices, layer=layer, closed=closed,
                    bulges=bulges, width=width, elevation=elevation, layout=layout)

    @mcp.tool()
    @observed
    def draw_rectangle(dashboard: str, corner1: list[float], corner2: list[float] | None = None,
                       width: float | None = None, height: float | None = None,
                       rotation_deg: float = 0.0, layer: str = "0",
                       fillet_radius: float = 0.0, chamfer_distance: float = 0.0,
                 layout: str | None = None) -> str:
        """RECTANG: closed polyline from two opposite corners, or corner1 + width/height,
        optionally rotated about corner1 and with rounded (fillet_radius) or
        bevelled (chamfer_distance) corners. layout: a paper-space layout name puts it on that sheet instead of model space."""
        return call("draw_rectangle", dashboard, corner1=corner1, corner2=corner2, width=width,
                    height=height, rotation_deg=rotation_deg, layer=layer,
                    fillet_radius=fillet_radius, chamfer_distance=chamfer_distance, layout=layout)

    @mcp.tool()
    @observed
    def draw_polygon(dashboard: str, sides: int, center: list[float] | None = None,
                     radius: float | None = None, mode: str = "inscribed",
                     rotation_deg: float = 0.0, edge_start: list[float] | None = None,
                     edge_end: list[float] | None = None, layer: str = "0") -> str:
        """POLYGON: regular n-gon. Either center + radius (mode "inscribed" = vertices on
        the circle, "circumscribed" = edges tangent to it) or one edge from edge_start
        to edge_end (the polygon grows counter-clockwise from that edge)."""
        return call("draw_polygon", dashboard, sides=sides, center=center, radius=radius, mode=mode,
                    rotation_deg=rotation_deg, edge_start=edge_start, edge_end=edge_end, layer=layer)

    @mcp.tool()
    @observed
    def draw_circle(dashboard: str, method: str = "center_radius", center: list[float] | None = None,
                    radius: float | None = None, diameter: float | None = None,
                    points: list[list[float]] | None = None, side_point: list[float] | None = None,
                    tangent_handles: list[str] | None = None,
                    tangent_points: list[list[float]] | None = None, layer: str = "0") -> str:
        """CIRCLE. method: "center_radius", "center_diameter", "two_points" (diameter ends
        in points), "three_points", "two_points_radius" (+ side_point picks which of the
        two solutions), or "tangent_tangent_radius" (tangent_handles = two lines/arcs/
        circles, tangent_points = where on each the circle should touch, radius)."""
        return call("draw_circle", dashboard, method=method, center=center, radius=radius,
                    diameter=diameter, points=points, side_point=side_point,
                    tangent_handles=tangent_handles, tangent_points=tangent_points, layer=layer)

    @mcp.tool()
    @observed
    def draw_arc_by(dashboard: str, method: str, points: list[list[float]] | None = None,
                    start: list[float] | None = None, end: list[float] | None = None,
                    center: list[float] | None = None, radius: float | None = None,
                    angle_deg: float | None = None, chord_length: float | None = None,
                    direction_deg: float | None = None, clockwise: bool = False,
                    layer: str = "0") -> str:
        """ARC by construction method (draw_arc covers center + angles). method:
        "three_points" (points [p1, p2, p3]), "start_center_end" (+clockwise),
        "start_center_angle" (angle_deg, negative = clockwise), "start_center_length"
        (chord_length), "start_end_radius" (negative radius = major arc),
        "start_end_angle" (included angle_deg, negative = clockwise),
        "start_end_direction" (direction_deg = tangent at start)."""
        return call("draw_arc_by", dashboard, method=method, points=points, start=start, end=end,
                    center=center, radius=radius, angle_deg=angle_deg, chord_length=chord_length,
                    direction_deg=direction_deg, clockwise=clockwise, layer=layer)

    @mcp.tool()
    @observed
    def draw_ellipse(dashboard: str, center: list[float], major_axis_end: list[float] | None = None,
                     major_radius: float | None = None, rotation_deg: float = 0.0,
                     ratio: float | None = None, minor_radius: float | None = None,
                     start_angle_deg: float | None = None, end_angle_deg: float | None = None,
                     layer: str = "0") -> str:
        """ELLIPSE (or elliptical arc with start/end angles). Major axis from
        major_axis_end or major_radius + rotation_deg; minor from ratio (minor/major)
        or minor_radius."""
        return call("draw_ellipse", dashboard, center=center, major_axis_end=major_axis_end,
                    major_radius=major_radius, rotation_deg=rotation_deg, ratio=ratio,
                    minor_radius=minor_radius, start_angle_deg=start_angle_deg,
                    end_angle_deg=end_angle_deg, layer=layer)

    @mcp.tool()
    @observed
    def draw_spline(dashboard: str, points: list[list[float]], mode: str = "fit", degree: int = 3,
                    closed: bool = False, start_tangent: list[float] | None = None,
                    end_tangent: list[float] | None = None, fit_tolerance: float = 0.0,
                    layer: str = "0") -> str:
        """SPLINE through fit points (mode "fit", optional start/end tangent vectors) or
        from control vertices (mode "control")."""
        return call("draw_spline", dashboard, points=points, mode=mode, degree=degree, closed=closed,
                    start_tangent=start_tangent, end_tangent=end_tangent,
                    fit_tolerance=fit_tolerance, layer=layer)

    @mcp.tool()
    @observed
    def draw_donut(dashboard: str, center: list[float], inner_diameter: float,
                   outer_diameter: float, layer: str = "0") -> str:
        """DONUT: filled ring (inner_diameter 0 = filled disc) as a wide polyline."""
        return call("draw_donut", dashboard, center=center, inner_diameter=inner_diameter,
                    outer_diameter=outer_diameter, layer=layer)

    @mcp.tool()
    @observed
    def draw_revision_cloud(dashboard: str, vertices: list[list[float]],
                            arc_length: float | None = None, layer: str = "0") -> str:
        """REVCLOUD around the polygon [[x, y], ...]; arc_length sets the bump size."""
        return call("draw_revision_cloud", dashboard, vertices=vertices, arc_length=arc_length, layer=layer)

    @mcp.tool()
    @observed
    def draw_wipeout(dashboard: str, vertices: list[list[float]], layer: str = "0") -> str:
        """WIPEOUT: masking polygon that hides whatever is drawn underneath it."""
        return call("draw_wipeout", dashboard, vertices=vertices, layer=layer)

    @mcp.tool()
    @observed
    def create_region(dashboard: str, handles: list[str], layer: str | None = None,
                      delete_sources: bool = False) -> str:
        """REGION from closed curves (closed polylines, circles, or loops of lines/arcs).
        Regions carry exact area and can be combined with region_boolean."""
        return call("create_region", dashboard, handles=handles, layer=layer, delete_sources=delete_sources)

    @mcp.tool()
    @observed
    def region_boolean(dashboard: str, operation: str, target: str, tools: list[str]) -> str:
        """UNION / SUBTRACT / INTERSECT of regions: operation applied to `target` with each
        of `tools` (which are consumed, as in AutoCAD). Returns the resulting area."""
        return call("region_boolean", dashboard, operation=operation, target=target, tools=tools)

    @mcp.tool()
    @observed
    def trace_boundary(dashboard: str, point: list[float], kind: str = "polyline", layer: str = "0") -> str:
        """BOUNDARY: closed polyline (or kind="region") of the enclosed area around a point,
        found from whatever objects surround it. Feed it to create_hatch / measure_entities."""
        return call("trace_boundary", dashboard, point=point, kind=kind, layer=layer)

    # ══════════════════════════════════════════════════════════════════════════
    # Modify: transforms
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    @observed
    def move_entities(dashboard: str, handles: list[str], offset: list[float] | None = None,
                      from_point: list[float] | None = None, to_point: list[float] | None = None) -> str:
        """MOVE by offset [dx, dy] or from_point -> to_point."""
        return call("move_entities", dashboard, handles=handles, offset=offset,
                    **{"from": from_point, "to": to_point})

    @mcp.tool()
    @observed
    def copy_entities(dashboard: str, handles: list[str], offset: list[float] | None = None,
                      from_point: list[float] | None = None, to_point: list[float] | None = None,
                      count: int = 1) -> str:
        """COPY: duplicates displaced by offset (or from_point -> to_point); count > 1
        repeats the displacement. Returns the new handles."""
        return call("copy_entities", dashboard, handles=handles, offset=offset, count=count,
                    **{"from": from_point, "to": to_point})

    @mcp.tool()
    @observed
    def rotate_entities(dashboard: str, handles: list[str], base_point: list[float], angle_deg: float,
                        copy: bool = False, count: int = 1, reference_angle_deg: float | None = None) -> str:
        """ROTATE about base_point (counter-clockwise positive). copy=True keeps the
        originals and returns the rotated copies; reference_angle_deg rotates from that
        angle to angle_deg instead of by angle_deg."""
        return call("rotate_entities", dashboard, handles=handles, base_point=base_point,
                    angle_deg=angle_deg, copy=copy, count=count, reference_angle_deg=reference_angle_deg)

    @mcp.tool()
    @observed
    def scale_entities(dashboard: str, handles: list[str], base_point: list[float], factor: float = 1.0,
                       reference_length: float | None = None, new_length: float | None = None,
                       copy: bool = False) -> str:
        """SCALE uniformly about base_point by factor, or by new_length / reference_length."""
        return call("scale_entities", dashboard, handles=handles, base_point=base_point, factor=factor,
                    reference_length=reference_length, new_length=new_length, copy=copy)

    @mcp.tool()
    @observed
    def mirror_entities(dashboard: str, handles: list[str], axis_start: list[float], axis_end: list[float],
                        copy: bool = True) -> str:
        """MIRROR across the line axis_start-axis_end. copy=True (default) keeps the source."""
        return call("mirror_entities", dashboard, handles=handles, axis_start=axis_start, axis_end=axis_end, copy=copy)

    @mcp.tool()
    @observed
    def stretch_entities(dashboard: str, handles: list[str], offset: list[float],
                         window: list[list[float]] | None = None,
                         polygon: list[list[float]] | None = None) -> str:
        """STRETCH: vertices/endpoints inside the crossing window [[minx,miny],[maxx,maxy]]
        (or polygon) move by offset; objects entirely inside move whole; the rest is untouched."""
        return call("stretch_entities", dashboard, handles=handles, offset=offset, window=window, polygon=polygon)

    @mcp.tool()
    @observed
    def align_entities(dashboard: str, handles: list[str], source_points: list[list[float]],
                       destination_points: list[list[float]], scale: bool = False, copy: bool = False) -> str:
        """ALIGN: maps 1 or 2 source points onto destination points (move + rotate, and
        scale=True also stretches so the second pair coincides)."""
        return call("align_entities", dashboard, handles=handles, source_points=source_points,
                    destination_points=destination_points, scale=scale, copy=copy)

    # ══════════════════════════════════════════════════════════════════════════
    # Modify: derive
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    @observed
    def offset_entities(dashboard: str, handles: list[str], distance: float | None = None,
                        side_point: list[float] | None = None, through_point: list[float] | None = None,
                        layer: str | None = None) -> str:
        """OFFSET lines, arcs, circles, polylines, ellipses, splines: parallel copy at
        `distance` on the side of side_point, or through through_point. Returns new handles."""
        return call("offset_entities", dashboard, handles=handles, distance=distance,
                    side_point=side_point, through_point=through_point, layer=layer)

    @mcp.tool()
    @observed
    def array_entities(dashboard: str, handles: list[str], kind: str = "rectangular",
                       rows: int = 1, columns: int = 1, row_spacing: float = 0.0, column_spacing: float = 0.0,
                       angle_deg: float = 0.0, center: list[float] | None = None, count: int | None = None,
                       fill_angle_deg: float = 360.0, rotate_items: bool = True,
                       path_handle: str | None = None, spacing: float | None = None,
                       align_to_path: bool = True, base_point: list[float] | None = None) -> str:
        """ARRAY. kind "rectangular": rows/columns with spacings (angle_deg rotates the grid);
        "polar": count copies about center over fill_angle_deg; "path": copies along
        path_handle every `spacing` (or `count` evenly), aligned to the path tangent.
        The originals stay; the new handles are returned."""
        return call("array_entities", dashboard, handles=handles, kind=kind, rows=rows, columns=columns,
                    row_spacing=row_spacing, column_spacing=column_spacing, angle_deg=angle_deg, center=center,
                    count=count, fill_angle_deg=fill_angle_deg, rotate_items=rotate_items,
                    path_handle=path_handle, spacing=spacing, align_to_path=align_to_path, base_point=base_point)

    @mcp.tool()
    @observed
    def fillet_entities(dashboard: str, handle_a: str, handle_b: str, radius: float,
                        pick_a: list[float] | None = None, pick_b: list[float] | None = None,
                        trim: bool = True, layer: str | None = None) -> str:
        """FILLET two lines/arcs/circles with an arc of `radius` (0 = sharp corner join).
        pick_a / pick_b say which parts to KEEP (default: the ends closest to each other);
        trim=False leaves both objects untouched. For polyline corners use fillet_polyline."""
        return call("fillet_entities", dashboard, handle_a=handle_a, handle_b=handle_b, radius=radius,
                    pick_a=pick_a, pick_b=pick_b, trim=trim, layer=layer)

    @mcp.tool()
    @observed
    def chamfer_entities(dashboard: str, handle_a: str, handle_b: str, distance_a: float,
                         distance_b: float | None = None, angle_deg: float | None = None,
                         pick_a: list[float] | None = None, pick_b: list[float] | None = None,
                         trim: bool = True, layer: str | None = None) -> str:
        """CHAMFER two lines: bevel with distance_a on the first and distance_b (or
        distance_a * tan(angle_deg)) on the second. pick points choose the kept sides."""
        return call("chamfer_entities", dashboard, handle_a=handle_a, handle_b=handle_b, distance_a=distance_a,
                    distance_b=distance_b, angle_deg=angle_deg, pick_a=pick_a, pick_b=pick_b, trim=trim, layer=layer)

    @mcp.tool()
    @observed
    def fillet_polyline(dashboard: str, handle: str, radius: float,
                        vertex_indices: list[int] | None = None, chamfer: bool = False) -> str:
        """FILLET (or chamfer=True) the corners of one polyline: every corner between
        straight segments, or only vertex_indices. Corners too short for the radius are skipped."""
        return call("fillet_polyline", dashboard, handle=handle, radius=radius,
                    vertex_indices=vertex_indices, chamfer=chamfer)

    # ══════════════════════════════════════════════════════════════════════════
    # Modify: cut and merge
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    @observed
    def trim_entities(dashboard: str, handles: list[str], cutting_handles: list[str],
                      pick_points: list[list[float]], extend_cutting_edges: bool = False) -> str:
        """TRIM: for each target handle, the piece nearest its pick point (between
        crossings with the cutting edges) is removed. The target is replaced by the
        remaining pieces; the report maps old handle -> new handles.
        extend_cutting_edges=True treats cutters as infinite (Edge=Extend)."""
        return call("trim_entities", dashboard, handles=handles, cutting_handles=cutting_handles,
                    pick_points=pick_points, extend_cutting_edges=extend_cutting_edges)

    @mcp.tool()
    @observed
    def extend_entities(dashboard: str, handles: list[str], boundary_handles: list[str],
                        pick_points: list[list[float]] | None = None, extend_boundaries: bool = False) -> str:
        """EXTEND each target to the nearest boundary beyond the end nearest its pick
        point (no pick: the end with the closest boundary). Handles are preserved."""
        return call("extend_entities", dashboard, handles=handles, boundary_handles=boundary_handles,
                    pick_points=pick_points, extend_boundaries=extend_boundaries)

    @mcp.tool()
    @observed
    def break_entity(dashboard: str, handle: str, first_point: list[float],
                     second_point: list[float] | None = None) -> str:
        """BREAK: with one point splits the curve in two; with two points removes the
        piece between them (closed curves: counter-clockwise from first to second)."""
        return call("break_entity", dashboard, handle=handle, first_point=first_point, second_point=second_point)

    @mcp.tool()
    @observed
    def lengthen_entity(dashboard: str, handle: str, mode: str = "delta", value: float = 0.0,
                        end: str | None = None, near_point: list[float] | None = None,
                        point: list[float] | None = None) -> str:
        """LENGTHEN one open curve. mode "delta" (+/- value), "percent", "total" (new
        length), or "to_point" (extend/shorten toward `point`). Which end: end="start"/"end"
        or near_point. Lines and arcs change in place; other curves may be replaced."""
        return call("lengthen_entity", dashboard, handle=handle, mode=mode, value=value, end=end,
                    near_point=near_point, point=point)

    @mcp.tool()
    @observed
    def join_entities(dashboard: str, handles: list[str], result: str = "auto") -> str:
        """JOIN touching lines/arcs/polylines/splines into one object (first handle is
        the primary). result="polyline" forces a polyline; "auto" keeps collinear lines as
        a line and concentric arcs as an arc."""
        return call("join_entities", dashboard, handles=handles, result=result)

    @mcp.tool()
    @observed
    def explode_entities(dashboard: str, handles: list[str], keep_attributes_as_text: bool = True) -> str:
        """EXPLODE blocks, polylines, dimensions, hatches, regions, tables into parts.
        Block attributes become plain text (BURST behaviour) unless keep_attributes_as_text=False."""
        return call("explode_entities", dashboard, handles=handles, keep_attributes_as_text=keep_attributes_as_text)

    @mcp.tool()
    @observed
    def mark_along_entity(dashboard: str, handle: str, count: int | None = None,
                          segment_length: float | None = None, marker: str = "point",
                          from_end: str = "start", include_ends: bool = False,
                          layer: str | None = None, align_to_curve: bool = True, block_scale: float = 1.0) -> str:
        """DIVIDE (count equal parts) / MEASURE (every segment_length) along a curve,
        placing points or a named block (marker="BLOCKNAME") at each station."""
        return call("mark_along_entity", dashboard, handle=handle, count=count, segment_length=segment_length,
                    marker=marker, from_end=from_end, include_ends=include_ends, layer=layer,
                    align_to_curve=align_to_curve, block_scale=block_scale)

    @mcp.tool()
    @observed
    def edit_polyline(dashboard: str, handle: str, action: str, index: int | None = None,
                      point: list[float] | None = None, bulge: float | None = None,
                      width: float | None = None, start_width: float | None = None,
                      end_width: float | None = None, elevation: float | None = None,
                      tolerance: float | None = None) -> str:
        """PEDIT. action: close, open, reverse, add_vertex (point, optional index/bulge),
        remove_vertex (index), move_vertex (index, point), set_bulge (index, bulge),
        set_width (width, or index + start_width/end_width), straighten (all arcs to
        lines), simplify (drop collinear vertices within tolerance), set_elevation,
        set_start_vertex (index, closed only), to_polyline (convert a line/arc/circle/spline)."""
        return call("edit_polyline", dashboard, handle=handle, action=action, index=index, point=point, bulge=bulge,
                    width=width, start_width=start_width, end_width=end_width, elevation=elevation, tolerance=tolerance)

    @mcp.tool()
    @observed
    def edit_spline(dashboard: str, handle: str, action: str, index: int | None = None,
                    point: list[float] | None = None, degree: int | None = None) -> str:
        """SPLINEDIT. action: add_fit_point (point, optional index), remove_fit_point (index),
        move_fit_point (index, point), move_control_point (index, point), elevate_degree
        (degree), reverse, to_polyline."""
        return call("edit_spline", dashboard, handle=handle, action=action, index=index, point=point, degree=degree)

    @mcp.tool()
    @observed
    def reverse_curves(dashboard: str, handles: list[str]) -> str:
        """REVERSE the direction (start/end) of lines, arcs, polylines, splines."""
        return call("reverse_curves", dashboard, handles=handles)

    # ══════════════════════════════════════════════════════════════════════════
    # Properties and draw order
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    @observed
    def set_properties(dashboard: str, handles: list[str], layer: str | None = None,
                       color: str | int | list[int] | None = None, linetype: str | None = None,
                       lineweight: str | float | None = None, linetype_scale: float | None = None,
                       transparency: int | None = None, visible: bool | None = None) -> str:
        """PROPERTIES / CHPROP: set layer, color (ACI index, [r,g,b], "bylayer"), linetype
        (loaded from acad.lin if needed), lineweight (mm or "bylayer"), linetype_scale,
        transparency (0-90 %) or visibility on many objects at once."""
        return call("set_properties", dashboard, handles=handles, layer=layer, color=color, linetype=linetype,
                    lineweight=lineweight, linetype_scale=linetype_scale, transparency=transparency, visible=visible)

    @mcp.tool()
    @observed
    def match_properties(dashboard: str, source: str, targets: list[str],
                         properties: list[str] | None = None) -> str:
        """MATCHPROP: copy layer/color/linetype/lineweight/linetype_scale/transparency (and
        text, dimension, hatch settings when both objects are that kind) from source to targets."""
        return call("match_properties", dashboard, source=source, targets=targets, properties=properties)

    @mcp.tool()
    @observed
    def set_draw_order(dashboard: str, handles: list[str], position: str = "front",
                       reference: str | None = None) -> str:
        """DRAWORDER: position "front", "back", "above" or "below" (the latter two relative
        to `reference`). Send hatches to the back so linework stays visible."""
        return call("set_draw_order", dashboard, handles=handles, position=position, reference=reference)

    # ══════════════════════════════════════════════════════════════════════════
    # Annotate
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    @observed
    def draw_text(dashboard: str, text: str, position: list[float], height: float | None = None,
                  rotation_deg: float = 0.0, justification: str = "left", style: str | None = None,
                  layer: str = "0", width_factor: float | None = None, color: str | int | None = None,
                 layout: str | None = None) -> str:
        """TEXT: single-line text. justification: left, center, right, middle, top_left,
        top_center, top_right, middle_left, middle_right, bottom_left/center/right. layout: a paper-space layout name puts it on that sheet instead of model space."""
        return call("draw_text", dashboard, text=text, position=position, height=height, rotation_deg=rotation_deg,
                    justification=justification, style=style, layer=layer, width_factor=width_factor, color=color, layout=layout)

    @mcp.tool()
    @observed
    def draw_mtext(dashboard: str, text: str, position: list[float], width: float = 0.0,
                   height: float | None = None, rotation_deg: float = 0.0, attachment: str = "top_left",
                   style: str | None = None, layer: str = "0", line_spacing_factor: float | None = None,
                   background_mask: bool | None = None, color: str | int | None = None,
                 layout: str | None = None) -> str:
        """MTEXT: paragraph text wrapped at `width` (0 = no wrap); "\\\\P" starts a new line.
        attachment is the corner/side of the box that sits at position. layout: a paper-space layout name puts it on that sheet instead of model space."""
        return call("draw_mtext", dashboard, text=text, position=position, width=width, height=height,
                    rotation_deg=rotation_deg, attachment=attachment, style=style, layer=layer,
                    line_spacing_factor=line_spacing_factor, background_mask=background_mask, color=color, layout=layout)

    @mcp.tool()
    @observed
    def edit_text(dashboard: str, handle: str, text: str | None = None, height: float | None = None,
                  rotation_deg: float | None = None, width: float | None = None,
                  position: list[float] | None = None, justification: str | None = None,
                  attachment: str | None = None, style: str | None = None,
                  width_factor: float | None = None) -> str:
        """TEXTEDIT / SCALETEXT / JUSTIFYTEXT: change the content, height, rotation, width,
        position, justification or style of text, mtext, a leader's text or a dimension's text."""
        return call("edit_text", dashboard, handle=handle, text=text, height=height, rotation_deg=rotation_deg,
                    width=width, position=position, justification=justification, attachment=attachment,
                    style=style, width_factor=width_factor)

    @mcp.tool()
    @observed
    def create_hatch(dashboard: str, handles: list[str] | None = None, point: list[float] | None = None,
                     pattern: str = "SOLID", scale: float = 1.0, angle_deg: float = 0.0,
                     layer: str = "0", color: str | int | None = None,
                     background_color: str | int | None = None, island_style: str = "normal",
                     associative: bool = True, keep_boundary: bool = False, pattern_type: str = "predefined") -> str:
        """HATCH / GRADIENT-style fill of closed boundaries (handles) or of the area around
        a point (boundary traced automatically; keep_boundary keeps that polyline).
        pattern: "SOLID" or an acad.pat name (ANSI31, EARTH, GRAVEL, AR-CONC, DOTS ...)."""
        return call("create_hatch", dashboard, handles=handles, point=point, pattern=pattern, scale=scale,
                    angle_deg=angle_deg, layer=layer, color=color, background_color=background_color,
                    island_style=island_style, associative=associative, keep_boundary=keep_boundary,
                    pattern_type=pattern_type)

    @mcp.tool()
    @observed
    def edit_hatch(dashboard: str, handle: str, pattern: str | None = None, scale: float | None = None,
                   angle_deg: float | None = None, color: str | int | None = None,
                   background_color: str | int | None = None, island_style: str | None = None) -> str:
        """HATCHEDIT: change pattern, scale, angle, colours or island style of an existing hatch."""
        return call("edit_hatch", dashboard, handle=handle, pattern=pattern, scale=scale, angle_deg=angle_deg,
                    color=color, background_color=background_color, island_style=island_style)

    @mcp.tool()
    @observed
    def dimension_linear(dashboard: str, point1: list[float], point2: list[float],
                         dimension_line_point: list[float], kind: str = "aligned",
                         rotation_deg: float | None = None, text_override: str = "",
                         style: str | None = None, layer: str = "0", scale: float | None = None) -> str:
        """DIMLINEAR / DIMALIGNED between two points; dimension_line_point sets the offset.
        kind: "aligned", "horizontal", "vertical", or "rotated" with rotation_deg.
        text_override "" = measured value; use "<>" to embed it, e.g. "<> TYP"."""
        return call("dimension_linear", dashboard, point1=point1, point2=point2,
                    dimension_line_point=dimension_line_point, kind=kind, rotation_deg=rotation_deg,
                    text_override=text_override, style=style, layer=layer, scale=scale)

    @mcp.tool()
    @observed
    def dimension_angular(dashboard: str, arc_point: list[float], line_a: str | None = None,
                          line_b: str | None = None, arc_handle: str | None = None,
                          center: list[float] | None = None, point1: list[float] | None = None,
                          point2: list[float] | None = None, text_override: str = "",
                          style: str | None = None, layer: str = "0") -> str:
        """DIMANGULAR between two lines (line_a, line_b), over an arc (arc_handle), or from
        center + two points. arc_point places the dimension arc."""
        return call("dimension_angular", dashboard, arc_point=arc_point, line_a=line_a, line_b=line_b,
                    arc_handle=arc_handle, center=center, point1=point1, point2=point2,
                    text_override=text_override, style=style, layer=layer)

    @mcp.tool()
    @observed
    def dimension_radial(dashboard: str, handle: str, kind: str = "radius",
                         chord_point: list[float] | None = None, angle_deg: float = 45.0,
                         leader_length: float | None = None, text_override: str = "",
                         style: str | None = None, layer: str = "0") -> str:
        """DIMRADIUS / DIMDIAMETER on a circle or arc. The leader lands at chord_point or at
        angle_deg on the circle; leader_length is the extension beyond it."""
        return call("dimension_radial", dashboard, handle=handle, kind=kind, chord_point=chord_point,
                    angle_deg=angle_deg, leader_length=leader_length, text_override=text_override,
                    style=style, layer=layer)

    @mcp.tool()
    @observed
    def dimension_arc_length(dashboard: str, handle: str, arc_point: list[float] | None = None,
                             offset: float | None = None, text_override: str = "",
                             style: str | None = None, layer: str = "0") -> str:
        """DIMARC: arc-length dimension of an arc, placed at arc_point or `offset` outside it."""
        return call("dimension_arc_length", dashboard, handle=handle, arc_point=arc_point, offset=offset,
                    text_override=text_override, style=style, layer=layer)

    @mcp.tool()
    @observed
    def dimension_ordinate(dashboard: str, point: list[float], leader_end_point: list[float],
                           axis: str = "x", text_override: str = "", style: str | None = None,
                           layer: str = "0") -> str:
        """DIMORDINATE: X or Y datum dimension of a point with its leader ending at leader_end_point."""
        return call("dimension_ordinate", dashboard, point=point, leader_end_point=leader_end_point, axis=axis,
                    text_override=text_override, style=style, layer=layer)

    @mcp.tool()
    @observed
    def dimension_continue(dashboard: str, base_dimension: str, points: list[list[float]],
                           mode: str = "continue", spacing: float | None = None, text_override: str = "") -> str:
        """DIMCONTINUE / DIMBASELINE from an existing linear dimension through each of
        `points` in turn (baseline: stacked `spacing` apart from the first extension line)."""
        return call("dimension_continue", dashboard, base_dimension=base_dimension, points=points, mode=mode,
                    spacing=spacing, text_override=text_override)

    @mcp.tool()
    @observed
    def edit_dimension(dashboard: str, handle: str, text_override: str | None = None,
                       text_position: list[float] | None = None, reset_text_position: bool = False,
                       text_rotation_deg: float | None = None, dimension_line_point: list[float] | None = None,
                       style: str | None = None, scale: float | None = None, text_height: float | None = None,
                       arrow_size: float | None = None, decimal_places: int | None = None) -> str:
        """DIMEDIT / DIMTEDIT: override text, move the text or dimension line, swap style,
        or override scale / text height / arrow size / precision on one dimension."""
        return call("edit_dimension", dashboard, handle=handle, text_override=text_override,
                    text_position=text_position, reset_text_position=reset_text_position,
                    text_rotation_deg=text_rotation_deg, dimension_line_point=dimension_line_point,
                    style=style, scale=scale, text_height=text_height, arrow_size=arrow_size,
                    decimal_places=decimal_places)

    @mcp.tool()
    @observed
    def draw_leader(dashboard: str, points: list[list[float]], text: str = "",
                    text_position: list[float] | None = None, height: float | None = None,
                    landing: bool = True, landing_length: float | None = None,
                    arrow_size: float | None = None, style: str | None = None,
                    text_style: str | None = None, layer: str = "0",
                 layout: str | None = None) -> str:
        """MLEADER: arrow at points[0], vertices through the rest, then a landing and the
        text (empty text = leader line only). layout: a paper-space layout name puts it on that sheet instead of model space."""
        return call("draw_leader", dashboard, points=points, text=text, text_position=text_position,
                    height=height, landing=landing, landing_length=landing_length, arrow_size=arrow_size,
                    style=style, text_style=text_style, layer=layer, layout=layout)

    @mcp.tool()
    @observed
    def draw_tolerance(dashboard: str, text: str, position: list[float], style: str | None = None,
                       rotation_deg: float | None = None, layer: str = "0") -> str:
        """TOLERANCE: GD&T feature control frame. text uses AutoCAD's frame syntax, e.g.
        "{\\\\Fgdt;j}%%v0.01%%vA" (j = position symbol, %%v = cell divider)."""
        return call("draw_tolerance", dashboard, text=text, position=position, style=style,
                    rotation_deg=rotation_deg, layer=layer)

    @mcp.tool()
    @observed
    def create_table(dashboard: str, position: list[float], cells: list[list[str]] | None = None,
                     rows: int | None = None, columns: int | None = None, row_height: float | None = None,
                     column_width: float | None = None, text_height: float | None = None,
                     title: str | None = None, style: str | None = None, layer: str = "0",
                 layout: str | None = None) -> str:
        """TABLE at position (top-left). cells = matrix of strings (row 0 is the title row
        when `title` is given, row 1 the header in the default style). layout: a paper-space layout name puts it on that sheet instead of model space."""
        return call("create_table", dashboard, position=position, cells=cells, rows=rows, columns=columns,
                    row_height=row_height, column_width=column_width, text_height=text_height, title=title,
                    style=style, layer=layer, layout=layout)

    @mcp.tool()
    @observed
    def set_table_cell(dashboard: str, handle: str, cells: list[dict] | None = None,
                       row: int | None = None, column: int | None = None, text: str | None = None,
                       insert_rows: int | None = None, insert_columns: int | None = None) -> str:
        """Edit table cells: one row/column/text, or cells=[{"row","column","text",
        "text_height","alignment"}...]; insert_rows / insert_columns append at the end."""
        return call("set_table_cell", dashboard, handle=handle, cells=cells, row=row, column=column, text=text,
                    insert_rows=insert_rows, insert_columns=insert_columns)

    @mcp.tool()
    @observed
    def list_styles(dashboard: str, kind: str = "all") -> str:
        """STYLE / DIMSTYLE / MLEADERSTYLE / TABLESTYLE / LINETYPE listing. kind: all, text,
        dimension, mleader, table, linetype."""
        return call("list_styles", dashboard, kind=kind)

    @mcp.tool()
    @observed
    def create_text_style(dashboard: str, name: str, font: str | None = None, height: float | None = None,
                          width_factor: float | None = None, oblique_deg: float | None = None,
                          bold: bool = False, italic: bool = False, set_current: bool = False) -> str:
        """STYLE: create or update a text style. font is an .shx file ("romans.shx") or a
        TrueType face name ("Arial"). height 0 = ask per text (recommended)."""
        return call("create_text_style", dashboard, name=name, font=font, height=height, width_factor=width_factor,
                    oblique_deg=oblique_deg, bold=bold, italic=italic, set_current=set_current)

    @mcp.tool()
    @observed
    def create_dim_style(dashboard: str, name: str, based_on: str | None = None,
                         text_height: float | None = None, arrow_size: float | None = None,
                         extension_line_offset: float | None = None, extension_line_extension: float | None = None,
                         scale: float | None = None, decimal_places: int | None = None,
                         linear_factor: float | None = None, text_style: str | None = None,
                         text_above_line: bool | None = None, suffix: str | None = None,
                         baseline_spacing: float | None = None, text_gap: float | None = None,
                         set_current: bool = False) -> str:
        """DIMSTYLE: create (optionally based_on an existing style) or update a dimension
        style's text height, arrow size, extension lines, overall scale, precision, etc."""
        return call("create_dim_style", dashboard, name=name, based_on=based_on, text_height=text_height,
                    arrow_size=arrow_size, extension_line_offset=extension_line_offset,
                    extension_line_extension=extension_line_extension, scale=scale, decimal_places=decimal_places,
                    linear_factor=linear_factor, text_style=text_style, text_above_line=text_above_line,
                    suffix=suffix, baseline_spacing=baseline_spacing, text_gap=text_gap, set_current=set_current)

    # ══════════════════════════════════════════════════════════════════════════
    # Layers
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    @observed
    def list_layers(dashboard: str, count_entities: bool = True) -> str:
        """LAYER list: name, colour, linetype, lineweight, on/frozen/locked/plottable,
        current flag and how many model-space objects sit on each."""
        return call("list_layers", dashboard, count_entities=count_entities)

    @mcp.tool()
    @observed
    def layer_state(dashboard: str, action: str, layers: list[str] | str = "all",
                    except_layers: list[str] | None = None) -> str:
        """LAYON/LAYOFF/LAYFRZ/LAYTHW/LAYLCK/LAYULK in bulk. action: on, off, freeze, thaw,
        lock, unlock, plot, noplot; layers = names or "all" (minus except_layers)."""
        return call("layer_state", dashboard, action=action, layers=layers,
                    **{"except": except_layers})

    @mcp.tool()
    @observed
    def isolate_layers(dashboard: str, layers: list[str], mode: str = "off") -> str:
        """LAYISO: show only these layers (others turned off, or mode="freeze"). Restore
        with layer_state(action="on"/"thaw", layers="all")."""
        return call("isolate_layers", dashboard, layers=layers, mode=mode)

    @mcp.tool()
    @observed
    def set_current_layer(dashboard: str, name: str) -> str:
        """CLAYER: make a layer current (created if missing, thawed if frozen)."""
        return call("set_current_layer", dashboard, name=name)

    @mcp.tool()
    @observed
    def purge_unused(dashboard: str, kinds: list[str] | None = None) -> str:
        """PURGE unused named objects: kinds from blocks, layers, linetypes, text_styles,
        dim_styles (default all). Drawn objects are never touched."""
        return call("purge_unused", dashboard, kinds=kinds)

    @mcp.tool()
    @observed
    def rename_named_object(dashboard: str, kind: str, old_name: str, new_name: str) -> str:
        """RENAME a layer, block, text_style, dim_style, linetype or group."""
        return call("rename_named_object", dashboard, kind=kind, old_name=old_name, new_name=new_name)

    # ══════════════════════════════════════════════════════════════════════════
    # Drawing settings
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    @observed
    def get_drawing_settings(dashboard: str) -> str:
        """UNITS and current defaults: insertion units, linear/angular units and precision,
        LTSCALE, current layer/text style/dim style/colour/linetype, limits, extents, layouts."""
        return call("get_drawing_settings", dashboard)

    @mcp.tool()
    @observed
    def set_drawing_settings(dashboard: str, insertion_units: str | None = None, linear_units: str | None = None,
                             linear_precision: int | None = None, angular_units: str | None = None,
                             angular_precision: int | None = None, angle_base_deg: float | None = None,
                             angles_clockwise: bool | None = None, linetype_scale: float | None = None,
                             current_layer: str | None = None, current_text_style: str | None = None,
                             current_dim_style: str | None = None, current_linetype: str | None = None,
                             current_color: str | int | None = None, current_lineweight: str | float | None = None,
                             point_display_mode: int | None = None, point_display_size: float | None = None,
                             limits: list[list[float]] | None = None) -> str:
        """UNITS / LTSCALE / CLAYER / TEXTSTYLE / DIMSTYLE / COLOR / LINETYPE / LWEIGHT /
        PDMODE / LIMITS in one call. insertion_units e.g. "Feet", "Inches", "Meters";
        linear_units decimal|architectural|engineering|fractional|scientific;
        angular_units decimal_degrees|deg_min_sec|grads|radians|surveyor."""
        return call("set_drawing_settings", dashboard, insertion_units=insertion_units, linear_units=linear_units,
                    linear_precision=linear_precision, angular_units=angular_units,
                    angular_precision=angular_precision, angle_base_deg=angle_base_deg,
                    angles_clockwise=angles_clockwise, linetype_scale=linetype_scale, current_layer=current_layer,
                    current_text_style=current_text_style, current_dim_style=current_dim_style,
                    current_linetype=current_linetype, current_color=current_color,
                    current_lineweight=current_lineweight, point_display_mode=point_display_mode,
                    point_display_size=point_display_size, limits=limits)

    # ══════════════════════════════════════════════════════════════════════════
    # Blocks, attributes, references, groups
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    @observed
    def define_block(dashboard: str, name: str, base_point: list[float], handles: list[str],
                     replace_originals: bool = False, description: str | None = None,
                     layer: str | None = None) -> str:
        """BLOCK: define a block from existing objects (copied into the definition with
        base_point as the insertion point). replace_originals=True swaps the objects for
        one block reference, as the BLOCK command does; default leaves them in place."""
        return call("define_block", dashboard, name=name, base_point=base_point, handles=handles,
                    replace_originals=replace_originals, description=description, layer=layer)

    @mcp.tool()
    @observed
    def insert_block(dashboard: str, name: str, position: list[float], scale: float | list[float] = 1.0,
                     rotation_deg: float = 0.0, layer: str = "0", attributes: dict | None = None,
                     source_file: str | None = None, source_block: str | None = None,
                 layout: str | None = None) -> str:
        """INSERT a block reference; attributes {TAG: value} fills its attributes. If the
        block is not defined here, source_file (.dwg) imports it: the whole file as the
        block, or just source_block from inside it. layout: a paper-space layout name puts it on that sheet instead of model space."""
        return call("insert_block", dashboard, name=name, position=position, scale=scale,
                    rotation_deg=rotation_deg, layer=layer, attributes=attributes,
                    source_file=source_file, source_block=source_block, layout=layout)

    @mcp.tool()
    @observed
    def list_block_definitions(dashboard: str, include_anonymous: bool = False) -> str:
        """Block definitions in the drawing: name, entity count, reference count, attribute
        tags, whether it is an xref (and its path)."""
        return call("list_block_definitions", dashboard, include_anonymous=include_anonymous)

    @mcp.tool()
    @observed
    def set_block_attributes(dashboard: str, handle: str, attributes: dict) -> str:
        """ATTEDIT: set attribute values {TAG: value} on one block reference."""
        return call("set_block_attributes", dashboard, handle=handle, attributes=attributes)

    @mcp.tool()
    @observed
    def define_attribute(dashboard: str, block_name: str, tag: str, position: list[float],
                         prompt: str | None = None, default: str = "", height: float | None = None,
                         rotation_deg: float = 0.0, invisible: bool = False, constant: bool = False,
                         justification: str = "left", text_style: str | None = None, layer: str = "0",
                         sync: bool = True) -> str:
        """ATTDEF: add an attribute definition to a block (position in block coordinates,
        relative to its base point). sync=True pushes it to existing references (ATTSYNC)."""
        return call("define_attribute", dashboard, block_name=block_name, tag=tag, position=position,
                    prompt=prompt, default=default, height=height, rotation_deg=rotation_deg,
                    invisible=invisible, constant=constant, justification=justification,
                    text_style=text_style, layer=layer, sync=sync)

    @mcp.tool()
    @observed
    def sync_block_attributes(dashboard: str, block_name: str) -> str:
        """ATTSYNC: make every reference of a block match its attribute definitions."""
        return call("sync_block_attributes", dashboard, block_name=block_name)

    @mcp.tool()
    @observed
    def export_block(dashboard: str, path: str, name: str | None = None,
                     handles: list[str] | None = None, base_point: list[float] | None = None) -> str:
        """WBLOCK: write a block definition (name) or a set of objects (handles + base_point)
        to a new .dwg file."""
        return call("export_block", dashboard, path=path, name=name, handles=handles, base_point=base_point)

    @mcp.tool()
    @observed
    def attach_xref(dashboard: str, path: str, position: list[float], scale: float = 1.0,
                    rotation_deg: float = 0.0, layer: str = "0", overlay: bool = False,
                    name: str | None = None) -> str:
        """XREF attach (or overlay=True) an external drawing at position."""
        return call("attach_xref", dashboard, path=path, position=position, scale=scale,
                    rotation_deg=rotation_deg, layer=layer, overlay=overlay, name=name)

    @mcp.tool()
    @observed
    def attach_image(dashboard: str, path: str, position: list[float], width: float | None = None,
                     height: float | None = None, rotation_deg: float = 0.0, layer: str = "0",
                     name: str | None = None) -> str:
        """IMAGEATTACH a raster file with its lower-left corner at position; width in
        drawing units (height follows the aspect ratio unless given)."""
        return call("attach_image", dashboard, path=path, position=position, width=width, height=height,
                    rotation_deg=rotation_deg, layer=layer, name=name)

    @mcp.tool()
    @observed
    def attach_pdf(dashboard: str, path: str, position: list[float], page: int = 1, scale: float = 1.0,
                   rotation_deg: float = 0.0, layer: str = "0", name: str | None = None) -> str:
        """PDFATTACH one page of a PDF as an underlay."""
        return call("attach_pdf", dashboard, path=path, position=position, page=str(page), scale=scale,
                    rotation_deg=rotation_deg, layer=layer, name=name)

    @mcp.tool()
    @observed
    def group_entities(dashboard: str, action: str = "create", name: str | None = None,
                       handles: list[str] | None = None, description: str | None = None) -> str:
        """GROUP: action create (name + handles), add, remove, dissolve (removes the group,
        keeps the objects) or list."""
        return call("group_entities", dashboard, action=action, name=name, handles=handles, description=description)

    # ══════════════════════════════════════════════════════════════════════════
    # Layouts, view, file
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    @observed
    def manage_layouts(dashboard: str, action: str = "list", name: str | None = None,
                       new_name: str | None = None, set_current: bool = False,
                       source_file: str | None = None, source_layout: str | None = None) -> str:
        """LAYOUT: list, create (name), set_current (name), rename (name -> new_name),
        clone_from_template (name + source_file, a .dwg/.dwt, + source_layout: the page
        setup and everything on that template layout -- title block, border, notes --
        come across, its viewports do not; the reply's title_block is the handle of the
        block to fill with set_block_attributes, and the first handle in created), or
        delete (name; never Model or the current layout -- a person's tool, runs never
        delete layouts)."""
        return call("manage_layouts", dashboard, action=action, name=name, new_name=new_name, set_current=set_current,
                    source_file=source_file, source_layout=source_layout)

    @mcp.tool()
    @observed
    def create_viewport(dashboard: str, layout: str, center: list[float], width: float, height: float,
                        view_center: list[float] | None = None, scale: float | None = None,
                        view_height: float | None = None, locked: bool = True, layer: str = "0",
                        twist_deg: float = 0.0, freeze_layers: list[str] | None = None,
                        annotation_scale: str = "auto") -> str:
        """MVIEW: paper-space viewport in `layout` (center/width/height in paper units)
        looking at model view_center (a model-space point, twist or not) at `scale`
        (paper/model: 0.025 for 1" = 40'). twist_deg rotates the view (north up = 0).
        freeze_layers are frozen in this viewport only (xref|layer names allowed; names
        not in the drawing are reported, not errors). annotation_scale: "auto" gives the
        viewport the annotation scale matching `scale` (created if missing) so annotative
        labels show; a scale name to pick one; "none" to leave it. The reply carries the
        model window and ring the viewport shows."""
        return call("create_viewport", dashboard, layout=layout, center=center, width=width, height=height,
                    view_center=view_center, scale=scale, view_height=view_height, locked=locked, layer=layer,
                    twist_deg=twist_deg, freeze_layers=freeze_layers, annotation_scale=annotation_scale)

    @mcp.tool()
    @observed
    def set_viewport(dashboard: str, handle: str, on: bool | None = None, locked: bool | None = None,
                     scale: float | None = None, view_center: list[float] | None = None,
                     twist_deg: float | None = None, freeze_layers: list[str] | None = None,
                     thaw_layers: list[str] | None = None, annotation_scale: str | None = None) -> str:
        """Changes an existing paper-space viewport: on/off, lock, scale, model view
        centre, twist, layers frozen or thawed in it, annotation scale. Blank fields are
        left alone. Returns the model window it shows afterwards."""
        return call("set_viewport", dashboard, handle=handle, on=on, locked=locked, scale=scale,
                    view_center=view_center, twist_deg=twist_deg, freeze_layers=freeze_layers,
                    thaw_layers=thaw_layers, annotation_scale=annotation_scale)

    @mcp.tool()
    @observed
    def zoom_view(dashboard: str, mode: str = "extents", points: list[list[float]] | None = None,
                  handles: list[str] | None = None, point: list[float] | None = None,
                  height: float | None = None, factor: float | None = None, margin: float = 0.05,
                  regen: bool = True) -> str:
        """ZOOM the user's view: extents, window (points = 2 corners), entities (handles),
        center (point + height) or scale (factor); also REGENs. Purely visual."""
        return call("zoom_view", dashboard, mode=mode, points=points, handles=handles, point=point,
                    height=height, factor=factor, margin=margin, regen=regen)

    @mcp.tool()
    @observed
    def save_drawing(dashboard: str, path: str | None = None) -> str:
        """QSAVE (no path) / SAVEAS (path .dwg) / DXFOUT (path .dxf). Only when the user asks to save."""
        return call("save_drawing", dashboard, path=path)

    # ══════════════════════════════════════════════════════════════════════════
    # Query and inquiry
    # ══════════════════════════════════════════════════════════════════════════

    @mcp.tool()
    @observed
    def get_properties(dashboard: str, handles: list[str]) -> str:
        """LIST: every property of the given objects (type, layer, colour, linetype,
        bounds, and full geometry: points, radii, vertices, text, attributes ...)."""
        return call("get_properties", dashboard, handles=handles)

    @mcp.tool()
    @observed
    def select_entities(dashboard: str, mode: str = "all", points: list[list[float]] | None = None,
                        radius: float | None = None, types: list[str] | None = None,
                        layers: list[str] | None = None, colors: list[int] | None = None,
                        linetypes: list[str] | None = None, block_names: list[str] | None = None,
                        text_contains: str | None = None, min_length: float | None = None,
                        max_length: float | None = None, space: str = "model", limit: int = 500,
                        detail: bool = False) -> str:
        """SELECT / QSELECT: handles (with a short summary each) of objects matching
        mode "all", "window" (fully inside 2 corner points or a polygon), "crossing"
        (touching it), "fence" (crossed by the polyline points) or "point_radius", filtered
        by types (Line, Arc, Circle, Polyline, Spline, Ellipse, Text, MText, Hatch,
        Dimension, BlockReference, ...), layers (wildcards ok), colors, linetypes,
        block_names, text_contains, min/max_length. This is how a description becomes
        handles for every other tool."""
        filters = {k: v for k, v in dict(types=types, layers=layers, colors=colors, linetypes=linetypes,
                                          block_names=block_names, text_contains=text_contains,
                                          min_length=min_length, max_length=max_length).items() if v is not None}
        return call("select_entities", dashboard, mode=mode, points=points, radius=radius,
                    filters=filters or None, space=space, limit=limit, detail=detail)

    @mcp.tool()
    @observed
    def entity_key_points(dashboard: str, handles: list[str]) -> str:
        """Object snap points: endpoints, midpoints, centers, quadrants, vertices, nodes,
        insertion points and bounding-box center of each object. Use these instead of
        guessing coordinates."""
        return call("entity_key_points", dashboard, handles=handles)

    @mcp.tool()
    @observed
    def closest_point_on(dashboard: str, handles: list[str], point: list[float], extend: bool = False) -> str:
        """NEAREST / PERPENDICULAR snap: closest point on each curve to `point` (extend=True
        allows the curve's extension), with distance, distance along the curve and tangent angle."""
        return call("closest_point_on", dashboard, handles=handles, point=point, extend=extend)

    @mcp.tool()
    @observed
    def intersections(dashboard: str, handle_a: str | None = None, handle_b: str | None = None,
                      handles: list[str] | None = None, against: list[str] | None = None,
                      extend: str = "none") -> str:
        """INTERSECTION / APPARENT INTERSECTION points between two curves, among a set, or
        of `handles` against `against`. extend: none, a, b, both (use extensions)."""
        return call("intersections", dashboard, handle_a=handle_a, handle_b=handle_b, handles=handles,
                    against=against, extend=extend)

    @mcp.tool()
    @observed
    def tangent_points(dashboard: str, handle: str, from_point: list[float]) -> str:
        """TANGENT snap: the two points on a circle/arc where lines from from_point touch it."""
        return call("tangent_points", dashboard, handle=handle, from_point=from_point)

    @mcp.tool()
    @observed
    def measure_entities(dashboard: str, handles: list[str]) -> str:
        """MEASUREGEOM / AREA / LIST: length, area, radius, included angle, chord and bounds
        per object plus total length and area."""
        return call("measure_entities", dashboard, handles=handles)

    @mcp.tool()
    @observed
    def angle_between(dashboard: str, handle_a: str, handle_b: str) -> str:
        """Angle (and acute angle) between two lines/curves at their intersection, plus
        the intersection point and each direction."""
        return call("angle_between", dashboard, handle_a=handle_a, handle_b=handle_b)

    @mcp.tool()
    @observed
    def point_along(dashboard: str, handle: str, distance: float | None = None,
                    distances: list[float] | None = None, fraction: float | None = None,
                    from_end: str = "start") -> str:
        """Point(s) at a distance (station) or fraction along a curve, with the tangent and
        left/right normal directions there (for offsets, ticks and labels)."""
        return call("point_along", dashboard, handle=handle, distance=distance, distances=distances,
                    fraction=fraction, **{"from": from_end})

    @mcp.tool()
    @observed
    def bounding_box(dashboard: str, handles: list[str]) -> str:
        """Bounding box (min, max, center, width, height) of a set of objects and of each one."""
        return call("bounding_box", dashboard, handles=handles)

    @mcp.tool()
    @observed
    def find_duplicates(dashboard: str, tolerance: float = 0.0001, layers: list[str] | None = None,
                        types: list[str] | None = None, same_layer_only: bool = True) -> str:
        """OVERKILL (detect only): groups of identical overlapping lines, arcs, circles,
        polylines, text, points and block references. Nothing is deleted."""
        return call("find_duplicates", dashboard, tolerance=tolerance, layers=layers, types=types,
                    same_layer_only=same_layer_only)
