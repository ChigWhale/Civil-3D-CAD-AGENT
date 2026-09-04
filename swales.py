"""Rear yard swale geometry, shared by the MCP tool and the dashboard block.

Per pad, four polylines are produced:

    leg  <- P0 ===10ft=== Pb ================= P1 ->  leg
             (upstream)                     (downstream)

The pad's rear edge -- the frontage edge facing AWAY from the road -- is offset
5ft further from the road into the rear yard. That offset line is split at 10ft
from its upstream end, and a 45-degree leg runs roadward from each outer end
until it meets a lot line, giving the usual rear swale with side legs draining
to the street.

This deliberately mirrors driveways.py step for step, and borrows its helpers
rather than restating them:

  * which way the road lies is settled by casting the pad's major axis at the
    back of curb (so curves and cul-de-sac bulbs behave, and a pad whose axis
    meets no curb is reported rather than guessed at),
  * "upstream" is the flow heading of the alignment where the pad sits, which
    unlike station does not tie for every lot around a bulb,
  * each pad is worked against the ONE street it fronts, out of however many
    alignments were given, so a lot in reach of two streets gets one swale.
"""

import math

from driveways import (
    MAX_OFFSET,
    PAD_LAYER_SUFFIXES,
    ROAD_LAYER,
    _add,
    _arcs_on_layer,
    _dot,
    _fronting,
    _front_edges,
    _major_axis,
    _mid,
    _norm,
    _prepare,
    _pt,
    _ray_road,
    _ray_segments,
    _resolve_alignments,
    _scale,
    _segments_on_layer,
    _station_and_heading,
    _v,
    is_pad_layer,
)

LOT_LINE_SUFFIX = "c-lots-line"

REAR_OFFSET = 5.0     # ft, from the back of pad into the rear yard
FIRST_LEG = 10.0      # ft, the upstream part the line is split into
LEG_ANGLE = 45.0      # degrees, the side legs' turn toward the road


def _layers_present(data: dict) -> list:
    """Sorted layer names in a snapshot, for error messages."""
    names = set()
    for category in ("lines", "arcs", "polylines"):
        for entity in data.get(category) or []:
            if entity.get("layer"):
                names.add(str(entity["layer"]))
    return sorted(names)


def _swale_for_pad(pad, road_segments, road_arcs, lot_lines, align,
                   offset, first_leg, angle_deg, lot_line_suffix):
    """One pad's swale. Returns (payloads, note) or ([], reason-it-was-skipped)."""
    verts = [_pt(v) for v in pad.get("vertices", [])]
    if len(verts) < 3:
        return [], "fewer than 3 vertices"

    major = _major_axis(verts)
    centroid = (sum(v[0] for v in verts) / len(verts),
                sum(v[1] for v in verts) / len(verts))
    fronts = _front_edges(verts)
    centreline = align["segments"]

    # Which way along the major axis the road lies -- the driveway's probe,
    # unchanged, so the two tools can never disagree about a pad's front.
    probes = [centroid] + [_mid(edge) for edge in fronts]
    toward, nearest = None, None
    for direction in (major, _scale(major, -1.0)):
        for probe in probes:
            hit = _ray_road(probe, direction, road_segments, road_arcs, centreline)
            if hit and (nearest is None or hit[0] < nearest):
                nearest, toward = hit[0], direction
    if toward is None:
        return [], "the pad's major axis never meets the back of curb"

    # The rear edge is the frontage edge facing away from the road; the offset
    # pushes it further from the road still, into the rear yard.
    rear = min(fronts, key=lambda e: _dot(_v(centroid, _mid(e)), toward))
    away = _scale(toward, -1.0)
    shift = _scale(away, offset)
    ends = (_add(rear[0], shift), _add(rear[1], shift))

    # Upstream end first, so the short part is measured from the right side.
    _, heading = _station_and_heading(align["points"], _mid(rear))
    p0, p1 = (ends if _dot(_v(ends[0], ends[1]), heading) >= 0
              else (ends[1], ends[0]))

    run = _v(p0, p1)
    run_length = math.hypot(run[0], run[1])
    if run_length <= first_leg:
        return [], (f"offset rear line is {run_length:.1f}ft, shorter than the "
                    f"{first_leg:.0f}ft first part")
    direction = _scale(run, 1.0 / run_length)
    split = _add(p0, _scale(direction, first_leg))

    payloads = [
        {"intent": "draw_shape", "closed": False,
         "vertices": [[p0[0], p0[1]], [split[0], split[1]]]},      # upstream part
        {"intent": "draw_shape", "closed": False,
         "vertices": [[split[0], split[1]], [p1[0], p1[1]]]},      # the remainder
    ]

    # A leg from each outer end, turned `angle_deg` toward the road, cast until
    # it meets a lot line. Each starts exactly on the end of its own part, so
    # the pieces are connected without extra joining geometry.
    turn = math.radians(angle_deg)
    for end, outward in ((p0, _scale(direction, -1.0)), (p1, direction)):
        leg_dir = _norm(_add(_scale(outward, math.cos(turn)),
                             _scale(toward, math.sin(turn))))
        hit = _ray_segments(end, leg_dir, lot_lines)
        if hit is None:
            return [], f"a {angle_deg:.0f}-degree leg met no '{lot_line_suffix}' line"
        _, landing, _ = hit
        payloads.append({"intent": "draw_shape", "closed": False,
                         "vertices": [[end[0], end[1]], [landing[0], landing[1]]]})

    return payloads, {"pad_rear_length": round(math.dist(rear[0], rear[1]), 3),
                      "swale_length": round(run_length, 3)}


def plan(data: dict, alignment=None, layer: str = "0", offset: float = REAR_OFFSET,
         first_leg: float = FIRST_LEG, angle_deg: float = LEG_ANGLE,
         lot_line_suffix: str = LOT_LINE_SUFFIX, max_offset: float = MAX_OFFSET):
    """Works out every rear yard swale in a model space snapshot.

    `alignment` is one name, a comma separated list, a list, or nothing for
    every alignment in the drawing -- exactly as draw_driveways takes it.

    Returns (payloads, summary). Raises ValueError when the drawing cannot
    support the job at all -- no pads, no road, no lot lines, unknown alignment
    -- since those are worth reporting rather than silently drawing nothing.
    """
    pads = [pl for pl in data.get("polylines", []) if is_pad_layer(pl.get("layer", ""))]
    if not pads:
        raise ValueError("No pads found: no polylines on a layer ending in any "
                         f"of {list(PAD_LAYER_SUFFIXES)}.")

    on_road = lambda name: str(name).lower() == ROAD_LAYER
    road_segments = _segments_on_layer(data, on_road, include_arcs=False)
    road_arcs = _arcs_on_layer(data, on_road)
    if not road_segments and not road_arcs:
        raise ValueError(f"No back of curb found on layer '{ROAD_LAYER}'.")

    wanted_lot = str(lot_line_suffix).strip().lower()
    lot_lines = _segments_on_layer(
        data, lambda name: str(name).lower().endswith(wanted_lot))
    if not lot_lines:
        # Lot line naming varies between plats, so name what IS in the drawing
        # instead of leaving the user to guess which layer to point this at.
        raise ValueError("No lot lines found: nothing on a layer ending in "
                         f"'{lot_line_suffix}'. Layers this block can see: "
                         f"{_layers_present(data)} (xrefed layers only appear "
                         "here once they are asked for by name).")

    chosen = _resolve_alignments(data, alignment)
    prepared = [p for p in (_prepare(a) for a in chosen) if p]
    if not prepared:
        raise ValueError("None of the requested alignments have usable geometry: "
                         f"{[a.get('name') for a in chosen]}")

    payloads, drawn, skipped, unserved = [], [], [], 0
    per_alignment = {p["name"]: 0 for p in prepared}
    for pad in pads:
        verts = [_pt(v) for v in pad.get("vertices", [])]
        if len(verts) < 3:
            skipped.append({"pad": pad.get("handle"), "why": "fewer than 3 vertices"})
            continue

        # The one street this pad fronts, out of those given.
        distance, align = _fronting(prepared, _front_edges(verts))
        if distance > max_offset:
            unserved += 1
            continue

        built, note = _swale_for_pad(pad, road_segments, road_arcs, lot_lines, align,
                                     offset, first_leg, angle_deg, wanted_lot)
        if not built:
            skipped.append({"pad": pad.get("handle"), "why": note})
            continue
        payloads.extend({**p, "layer": layer} for p in built)
        per_alignment[align["name"]] += 1
        drawn.append({"pad": pad.get("handle"), "alignment": align["name"], **note})

    return payloads, {
        "drawn": len(drawn),
        "skipped": len(skipped),
        "not_on_these_alignments": unserved,
        "pads_seen": len(pads),
        "layer": layer,
        "offset": offset,
        "first_leg": first_leg,
        "angle_deg": angle_deg,
        "lot_line_layer": lot_line_suffix,
        "max_offset": max_offset,
        "alignments": [p["name"] for p in prepared],
        "drawn_per_alignment": per_alignment,
        "details": drawn[:20],
        "skipped_details": skipped[:20],
    }
