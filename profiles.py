"""Sanitary sewer profile views, planned from what the plugin reads.

The plugin's `profile_context` intent reports, per alignment, the station
range, surface elevations sampled along it, and the pipes and manholes that
sit on it with their stations, inverts and rims. This module turns that into
the profile views to draw: station windows from 1+00, an elevation range per
view, and a place in model space for each -- and builds the `draw_profile_views`
request the plugin executes in one transaction. Nothing here talks to Civil 3D.

Rules (the conventions, decided 2026-09-16):

  * Alignments start at 1+00; an alignment that does not is restationed.
  * Windows are 1000 ft from 1+00 (1+00-11+00, 11+00-21+00, ...); a remainder or
    an alignment shorter than that is one window (1+00-6+57).
  * The elevation span of a view is 36 or 40 ft; the datum is a multiple of
    4 ft chosen so the content (pipe inverts, sumps, rims, surface) sits in the
    middle of the span, the top is datum + span.
  * A window whose elevations do not fit the span is split at the 1+00 station
    nearest its middle (1000 -> 2 x 500); a half that still does not fit gets a
    raised top and a warning, never a clipped pipe.
  * Sheets: the views go two to a sheet, stacked, in alphabetical order of
    alignment (SS-A before SS-B) then station, packed as full as they go -- the
    last view of one alignment shares a sheet with the first of the next. The
    span picks the template layout (36 ft -> the 34x22 layout, 40 ft -> the
    36x24 one). Each sheet is ONE viewport the size of the sheet, so a sheet's
    views are placed in model space as a unit -- a "sheet window" of paper size
    x scale -- with each view's datum corner at its slot (paper inches from the
    window's lower-left). Sheet windows sit east of the drawing in a grid.
"""

import math
import re

PROFILE_DEFAULTS = {
    "scale": 40.0,                  # feet per paper inch, horizontal (1000 ft = 25 in)
    "span": 40.0,                   # ft of elevation per view: 36 or 40
    "window": 1000.0,               # ft of station per view
    "min_window": 100.0,            # never split below this
    "station_start": 100.0,         # 1+00
    "datum_round": 4.0,             # datum is a multiple of this (the grids start on 4 ft)
    "station_host": "eg",           # eg | flowline: which profile carries the station labels
    "station_increment": 100.0,
    "surface": "",                  # EG surface name; blank = the plugin's pick
    "name_format": "{alignment} PV{n}",
    "restation": True,
    "drag_lift": 5.0,               # model units above the datum line (below the top line) for dragged label text
    "placement": {"east_margin": 500.0, "row_gap": 200.0, "col_gap": 200.0, "sheets_per_row": 4,
                  "vertical_exaggeration": 10.0},
    # per span: the template layout to clone, its paper size (in), where each view's datum
    # corner goes on the paper (in, from the lower-left; first slot = first view of the
    # sheet), and style overrides for that span (blank = the drawing default)
    "sheets": {
        # align "center": every view is centred on center_x (in; blank = half the paper) and a
        # slot's x is ignored; align "left": a view's datum corner sits at the slot's x
        "36": {"layout": "DWG to PDF (34x22)", "paper": [34.0, 22.0], "slots": [[4.0, 11.2], [4.0, 1.1]],
               "align": "center", "center_x": None, "band_set": "", "profile_view": "",
               "border": {"xref": "", "source_file": "", "at": [0.0, 0.0], "scale": 1.0, "layer": "0"}},
        "40": {"layout": "DWG to PDF (36x24)", "paper": [36.0, 24.0], "slots": [[4.0, 12.2], [4.0, 1.1]],
               "align": "center", "center_x": None, "band_set": "", "profile_view": "",
               "border": {"xref": "", "source_file": "", "at": [0.0, 0.0], "scale": 1.0, "layer": "0"}},
    },
    # the dimension-line block between manholes, one per pipe just above the datum,
    # stacked in rows of row_height model units when pipes share a run; the pipe label sits
    # on its line. Blank block = no dimension lines. source_file = a .dwg to import it from.
    "dim_line": {"block": "Ano-ProfDimLine", "source_file": "", "layer": "", "row_height": 12.0, "lift": 4.0,
                 "property": "Distance1"},   # the dynamic grip that stretches it (read with block_dynamic_properties)
    # structure profile labels sit by their dimension anchor, in plotted inches from the graph
    # view's top line ("MH -> Alignment", negative = hanging below it) or bottom line ("MH")
    # plotted inches from the graph view top (negative = hangs below it) / bottom: the
    # "MH -> Alignment" label (top), the "MH" label, the station labels, the pipe labels
    # (first row) and each further row when two pipes share a run
    "dimension_anchor": {"top": -3.0, "bottom": 0.75, "station": 0.5, "pipe": 0.2, "pipe_row": 0.35},
    "styles": {
        "profile_view": "", "band_set": "",
        "eg_profile": "", "eg_label_set": "_No Labels",
        "flowline_profile": "", "flowline_label_set": "_No Labels",
        "pipe_label": "Prop -> Pipe Size -> Slope",
        "structure_label": "MH",
        "structure_alignment_label": "MH -> Alignment",
        "station_label": "Flowline -> Major",
    },
}

SPANS = (36.0, 40.0)


class ProfileError(ValueError):
    pass


# ── Spec ─────────────────────────────────────────────────────────────────────


def profile_spec(spec: dict = None) -> dict:
    """The profile section of a sheets.json over the defaults (dicts merged a
    level deep; the per-span sheets table a level deeper)."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in PROFILE_DEFAULTS.items()}
    out["sheets"] = {k: dict(v) for k, v in PROFILE_DEFAULTS["sheets"].items()}
    given = (spec or {}).get("profile") if isinstance(spec, dict) and "profile" in spec else spec
    for k, v in (given or {}).items():
        if k == "sheets" and isinstance(v, dict):
            for span, entry in v.items():
                if isinstance(entry, dict):
                    out["sheets"][str(span)] = {**out["sheets"].get(str(span), {}), **entry}
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        elif v is not None:
            out[k] = v
    for k in ("scale", "span", "window", "min_window", "station_start", "datum_round", "station_increment", "drag_lift"):
        out[k] = float(out[k])
    return out


def sheet_layout(ps: dict, span: float) -> dict:
    """The sheet entry for a span: layout name, paper [w, h] in, slots [[x, y], ...]."""
    key = f"{int(round(float(span)))}"
    entry = (ps.get("sheets") or {}).get(key)
    if not entry:
        raise ProfileError(f"no sheet layout for a {span:g} ft span: add profile.sheets[{key!r}] to the sheet spec")
    paper = entry.get("paper") or [36.0, 24.0]
    slots = entry.get("slots") or [[4.0, 12.2], [4.0, 1.1]]
    border = entry.get("border") if isinstance(entry.get("border"), dict) else {}
    align = str(entry.get("align") or "center").lower()
    cx = entry.get("center_x")
    return {"layout": str(entry.get("layout") or ""), "paper": [float(paper[0]), float(paper[1])],
            "slots": [[float(s[0]), float(s[1])] for s in slots],
            "align": "left" if align == "left" else "center",
            "center_x": float(cx) if cx not in (None, "") else float(paper[0]) / 2.0,
            "band_set": str(entry.get("band_set") or ""), "profile_view": str(entry.get("profile_view") or ""),
            "border": {"xref": str(border.get("xref") or ""), "source_file": str(border.get("source_file") or ""),
                       "at": list(border.get("at") or [0.0, 0.0]), "scale": float(border.get("scale") or 1.0),
                       "layer": str(border.get("layer") or "0")}}


def slot_offset(lay: dict, i: int, length_ft: float, scale: float) -> tuple:
    """(dx, dy) in model feet from a sheet window's lower-left to the datum corner
    of the view in slot i: centred on the sheet's centreline (each view by its
    own length) or, with align "left", at the slot's x."""
    slot = lay["slots"][i]
    dy = slot[1] * scale
    if lay.get("align") == "left":
        return (round(slot[0] * scale, 4), round(dy, 4))
    return (round(lay["center_x"] * scale - float(length_ft) / 2.0, 4), round(dy, 4))


def natural_key(name) -> tuple:
    """'SS-A [1A]' < 'SS-B [1A]' < 'SS-C ...'; numbers compare as numbers."""
    parts = re.split(r"(\d+)", str(name or "").lower())
    return tuple(int(p) if p.isdigit() else p for p in parts)


# ── Stations ─────────────────────────────────────────────────────────────────


def station_text(s: float) -> str:
    """1234.5 -> '12+34.50'; whole feet print without decimals ('1+00')."""
    s = float(s)
    whole = int(math.floor(s / 100.0 + 1e-9))
    rest = s - whole * 100.0
    if abs(rest - round(rest)) < 1e-6:
        return f"{whole}+{int(round(rest)):02d}"
    return f"{whole}+{rest:05.2f}"


def windows(start: float, end: float, length: float = 1000.0) -> list:
    """[(s0, s1), ...] from `start` in steps of `length`; the remainder (or an
    alignment shorter than `length`) is one window."""
    start, end, length = float(start), float(end), float(length)
    if end <= start + 1e-6:
        raise ProfileError(f"alignment ends ({station_text(end)}) before it starts ({station_text(start)})")
    if length <= 0:
        raise ProfileError("window length must be positive")
    out, s = [], start
    while s < end - 1e-6:
        e = min(s + length, end)
        out.append((round(s, 4), round(e, 4)))
        s = e
    return out


def split_station(s0: float, s1: float, min_window: float = 100.0):
    """The station nearest the middle of a window that is a multiple of 100,
    leaving both halves at least `min_window` long; None when it cannot split."""
    if s1 - s0 < 2 * min_window - 1e-6:
        return None
    mid = (s0 + s1) / 2.0
    cand = round(mid / 100.0) * 100.0
    if cand - s0 < min_window - 1e-6 or s1 - cand < min_window - 1e-6:
        cand = mid
    return round(cand, 4)


# ── Elevations ───────────────────────────────────────────────────────────────


def datum(low: float, step: float = 4.0) -> float:
    return math.floor(float(low) / step + 1e-9) * step


def centred_datum(low: float, high: float, span: float, step: float = 4.0) -> float:
    """The datum that puts the content (low..high) in the middle of the span,
    on a multiple of `step`, still holding the content; the plain rounded-down
    datum when the content fills the span."""
    content = float(high) - float(low)
    if content >= span:
        return datum(low, step)
    d = datum(float(low) - (span - content) / 2.0, step)
    if float(high) > d + span + 1e-9 or d > float(low) + 1e-9:
        d = datum(low, step)
    return d


def _interp(a, b, s):
    (sa, ea), (sb, eb) = a, b
    if abs(sb - sa) < 1e-9:
        return min(ea, eb)
    t = (s - sa) / (sb - sa)
    return ea + t * (eb - ea)


def elevation_window(samples: list, pipes: list, structures: list, s0: float, s1: float) -> dict:
    """The lowest and highest elevations a view of [s0, s1] must show, and where
    they come from. Pipes crossing a window edge are cut at it."""
    low, high, low_src, high_src = None, None, "", ""

    def take(e, src):
        nonlocal low, high, low_src, high_src
        if e is None:
            return
        e = float(e)
        if low is None or e < low:
            low, low_src = e, src
        if high is None or e > high:
            high, high_src = e, src

    for sm in samples or []:
        st = float(sm["station"])
        if s0 - 1e-6 <= st <= s1 + 1e-6 and sm.get("elevation") is not None:
            take(sm["elevation"], "surface")
    # the surface between two samples that straddle a window edge
    ss = sorted((float(sm["station"]), float(sm["elevation"])) for sm in samples or [] if sm.get("elevation") is not None)
    for edge in (s0, s1):
        for a, b in zip(ss, ss[1:]):
            if a[0] < edge < b[0]:
                take(_interp(a, b, edge), "surface")
    for p in pipes or []:
        ps0, ps1 = float(p["start_station"]), float(p["end_station"])
        lo_s, hi_s = min(ps0, ps1), max(ps0, ps1)
        if hi_s < s0 - 1e-6 or lo_s > s1 + 1e-6:
            continue
        a, b = (ps0, float(p["start_invert"])), (ps1, float(p["end_invert"]))
        for st in (max(lo_s, s0), min(hi_s, s1)):
            take(_interp(a, b, st), f"pipe {p.get('name') or p.get('handle')}")
        dia = float(p.get("diameter") or 0.0)
        if dia:
            for st in (max(lo_s, s0), min(hi_s, s1)):
                take(_interp(a, b, st) + dia, f"pipe {p.get('name') or p.get('handle')} crown")
    for st_ in structures or []:
        st = float(st_["station"])
        if s0 - 1e-6 <= st <= s1 + 1e-6:
            take(st_.get("rim"), f"rim {st_.get('name') or st_.get('handle')}")
            take(st_.get("sump"), f"sump {st_.get('name') or st_.get('handle')}")
    return {"low": low, "high": high, "low_source": low_src, "high_source": high_src}


def fit_windows(samples, pipes, structures, s0, s1, span, ps) -> list:
    """The views that show [s0, s1] within `span` feet of elevation: the window
    itself, else two halves with their own datums, else a raised top with a
    warning. Each entry: station_start, station_end, elevation_min/max, low, high,
    split (bool), warnings."""
    ew = elevation_window(samples, pipes, structures, s0, s1)
    if ew["low"] is None:
        raise ProfileError(f"no elevations between {station_text(s0)} and {station_text(s1)}: the surface does not "
                           "cover the alignment there and no pipe or structure sits in the window")
    d = centred_datum(ew["low"], ew["high"], span, ps["datum_round"])
    view = {"station_start": s0, "station_end": s1, "elevation_min": d, "elevation_max": d + span,
            "low": round(ew["low"], 3), "high": round(ew["high"], 3), "low_source": ew["low_source"],
            "high_source": ew["high_source"], "split": False, "warnings": []}
    if ew["high"] <= d + span + 1e-9:
        return [view]
    cut = split_station(s0, s1, ps["min_window"])
    if cut is not None and s1 - s0 > ps["window"] / 2.0 + 1e-6:
        halves = fit_windows(samples, pipes, structures, s0, cut, span, ps) + \
                 fit_windows(samples, pipes, structures, cut, s1, span, ps)
        for h in halves:
            h["split"] = True
        return halves
    top = math.ceil(ew["high"] / ps["datum_round"]) * ps["datum_round"]
    view["elevation_max"] = top
    view["warnings"].append(
        f"{station_text(s0)}-{station_text(s1)} needs {top - d:g} ft of elevation ({ew['low_source']} at {ew['low']:.2f} to "
        f"{ew['high_source']} at {ew['high']:.2f}); the top was raised from {d + span:g} to {top:g} to show it all")
    return [view]


# ── The plan ─────────────────────────────────────────────────────────────────


def _pick_alignments(context: dict, wanted) -> list:
    """The alignments to draw, in alphabetical (natural) order of name."""
    aligns = list(context.get("alignments") or [])
    if not aligns:
        raise ProfileError("the drawing has no alignments (profile_context reported none)")
    names = [str(a.get("name")) for a in aligns]
    out = []
    if not wanted:
        out = list(aligns)
    else:
        if isinstance(wanted, str):
            wanted = [w.strip() for w in wanted.split(",") if w.strip()]
        for w in wanted:
            exact = [a for a in aligns if str(a.get("name")).lower() == w.lower()]
            part = exact or [a for a in aligns if w.lower() in str(a.get("name")).lower()]
            if len(part) != 1:
                raise ProfileError(f"alignment {w!r} " + ("matches several: " + ", ".join(str(a['name']) for a in part)
                                                          if part else "is not in the drawing; it has " + ", ".join(names)))
            if part[0] not in out:
                out.append(part[0])
    out.sort(key=lambda a: natural_key(a.get("name")))
    return out


def _vertical_exaggeration(context: dict, style_name: str, ps: dict) -> float:
    pvs = (context.get("styles") or {}).get("profile_view") or []
    want = str(style_name or "").strip().lower()
    chosen = None
    if want:
        chosen = next((s for s in pvs if str(s.get("name", "")).lower() == want), None) or \
                 next((s for s in pvs if want in str(s.get("name", "")).lower()), None)
    if chosen is None:
        default = str((context.get("defaults") or {}).get("profile_view") or "").lower()
        chosen = next((s for s in pvs if str(s.get("name", "")).lower() == default), None)
    ve = (chosen or {}).get("vertical_exaggeration")
    try:
        ve = float(ve)
    except (TypeError, ValueError):
        ve = None
    return ve if ve and ve > 0 else float(ps["placement"]["vertical_exaggeration"])


def _existing_names(context: dict) -> set:
    names = set()
    for a in context.get("alignments") or []:
        for pv in a.get("existing_profile_views") or []:
            if pv.get("name"):
                names.add(str(pv["name"]).lower())
    for pv in context.get("profile_views") or []:
        if pv.get("name"):
            names.add(str(pv["name"]).lower())
    return names


def _snapshot_extent(data: dict):
    """(min_x, min_y, max_x, max_y) over everything in a snapshot with coordinates."""
    xs, ys = [], []

    def add(p):
        try:
            xs.append(float(p[0])); ys.append(float(p[1]))
        except (TypeError, ValueError, IndexError):
            pass

    for e in data.get("lines") or []:
        add(e.get("start_point")); add(e.get("end_point"))
    for e in data.get("polylines") or []:
        for v in e.get("vertices") or []:
            add(v)
    for e in (data.get("blocks") or []) + (data.get("texts") or []):
        add(e.get("position"))
    for a in data.get("alignments") or []:
        for ent in a.get("entities") or []:
            add(ent.get("start")); add(ent.get("end"))
    for n in data.get("pipe_networks") or []:
        for p in n.get("pipes") or []:
            add(p.get("start_point")); add(p.get("end_point"))
        for s in n.get("structures") or []:
            add(s.get("insertion_point"))
    for pv in data.get("profile_views") or []:
        for p in pv.get("extents") or []:
            add(p)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _origin(context: dict, ps: dict, data: dict = None) -> tuple:
    """Top-left of the placement grid: east of everything, level with its top."""
    ext = context.get("drawing_extent") or {}
    boxes = []
    try:
        boxes.append((float(ext["min"][0]), float(ext["min"][1]), float(ext["max"][0]), float(ext["max"][1])))
    except (KeyError, TypeError, ValueError, IndexError):
        pass
    if data:
        se = _snapshot_extent(data)
        if se:
            boxes.append(se)
    for a in context.get("alignments") or []:
        for pv in a.get("existing_profile_views") or []:
            ring = pv.get("extents") or []
            pts = ring if isinstance(ring, list) and ring and isinstance(ring[0], (list, tuple)) else []
            if pts:
                boxes.append((min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)))
            elif isinstance(ring, dict):
                boxes.append((ring["min"][0], ring["min"][1], ring["max"][0], ring["max"][1]))
    if not boxes:
        raise ProfileError("nothing in the drawing to place the profile views beside (no drawing extent)")
    max_x = max(b[2] for b in boxes)
    max_y = max(b[3] for b in boxes)
    return (round(max_x + ps["placement"]["east_margin"], 4), round(max_y, 4))


def plan(context: dict, span: float = None, alignments=None, styles: dict = None, spec: dict = None,
         data: dict = None, station_host: str = "", surface: str = "", restation=None,
         skip_existing: bool = False) -> tuple:
    """(request, summary): the `draw_profile_views` request for the plugin and a
    summary in words. `context` is the plugin's profile_context reply; `spec`
    a sheets.json (or its profile section); overrides win over the spec."""
    ps = profile_spec(spec)
    if span not in (None, "", 0):
        ps["span"] = float(span)
    if ps["span"] not in SPANS:
        raise ProfileError(f"span must be one of {', '.join(f'{s:g}' for s in SPANS)} ft, not {ps['span']:g}")
    if station_host:
        ps["station_host"] = str(station_host).strip().lower()
    if ps["station_host"] not in ("flowline", "eg"):
        raise ProfileError("station_host must be flowline or eg")
    if surface:
        ps["surface"] = surface
    if restation is not None:
        ps["restation"] = bool(restation)
    st = {**ps["styles"], **{k: v for k, v in (styles or {}).items() if v not in (None,)}}
    sheet = sheet_layout(ps, ps["span"])
    for k in ("band_set", "profile_view"):
        if sheet.get(k) and not (styles or {}).get(k):
            st[k] = sheet[k]                    # the span's band set / view style

    aligns = _pick_alignments(context, alignments)
    ve = _vertical_exaggeration(context, st.get("profile_view"), ps)
    scale = float(ps["scale"])
    taken = _existing_names(context)
    origin = _origin(context, ps, data)
    paper_w, paper_h = sheet["paper"]
    slots = sheet["slots"]
    per = len(slots)
    win_w, win_h = paper_w * scale, paper_h * scale
    per_row = max(1, int(ps["placement"].get("sheets_per_row", 4) or 4))
    row_pitch = win_h + float(ps["placement"]["row_gap"])
    col_pitch = win_w + float(ps["placement"]["col_gap"])
    grid_in = ps["span"] * ve / scale                  # paper inches of grid per view
    warnings = []
    if per >= 2:
        ys = sorted((s[1] for s in slots), reverse=True)
        if ys[0] - ys[1] < grid_in - 1e-6:
            warnings.append(f"the slots for the {ps['span']:g} ft sheet are {ys[0] - ys[1]:g} in apart but a view's grid is "
                            f"{grid_in:g} in tall at {ve:g}x; the views overlap on the paper -- move profile.sheets[\"{int(ps['span'])}\"].slots")
        if ys[0] + grid_in > paper_h + 1e-6:
            warnings.append(f"the upper view's grid reaches {ys[0] + grid_in:g} in, above the {paper_h:g} in paper")

    # 1. every alignment's windows, in alphabetical order of alignment
    restationed, ordered = [], []          # ordered: (alignment record, fitted view) in sheet order
    per_alignment = {}
    skipped_existing = []
    for a in aligns:
        name = str(a.get("name"))
        start, end = float(a.get("start_station") or 0.0), float(a.get("end_station") or 0.0)
        shift = 0.0
        if ps["restation"] and abs(start - ps["station_start"]) > 1e-6:
            shift = ps["station_start"] - start
            restationed.append({"alignment": name, "from_start": round(start, 4), "to_start": ps["station_start"],
                                "shift": round(shift, 4)})
        elif not ps["restation"] and abs(start - ps["station_start"]) > 1e-6:
            warnings.append(f"{name} starts at {station_text(start)}, not {station_text(ps['station_start'])}; "
                            "restation is off so its views are cut from where it starts")
        s_start, s_end = start + shift, end + shift
        samples = [{"station": float(x["station"]) + shift, "elevation": x.get("elevation")} for x in a.get("samples") or []]
        pipes = [{**p, "start_station": float(p["start_station"]) + shift, "end_station": float(p["end_station"]) + shift}
                 for p in a.get("pipes") or []]
        structures = [{**s, "station": float(s["station"]) + shift} for s in a.get("structures") or []]
        if not pipes and not structures:
            warnings.append(f"{name}: no pipe network parts sit on this alignment; the views show the surface only")
        fitted = []
        for s0, s1 in windows(s_start, s_end, ps["window"]):
            fitted.extend(fit_windows(samples, pipes, structures, s0, s1, ps["span"], ps))
        per_alignment[name] = {"start": s_start, "end": s_end, "shift": shift, "pipes": pipes, "structures": structures, "views": []}
        if skip_existing and any(
                ps["name_format"].format(alignment=name, n=n, start=station_text(v["station_start"]),
                                         end=station_text(v["station_end"])).lower() in taken
                for n, v in enumerate(fitted, start=1)):
            # drawn on an earlier run: left as it is, not drawn again
            per_alignment[name]["skipped"] = "its profile views already exist"
            skipped_existing.append(name)
            continue
        for n, v in enumerate(fitted, start=1):
            ordered.append((a, n, v))

    # 2. two to a sheet in that order. A window that already holds one view and no
    # second (drawn earlier, not yet on a sheet) is filled first; then new sheet
    # windows in a grid east of the drawing
    views, sheets_out = [], []
    open_windows = unpaired_windows(data, sheet, scale, ps["span"]) if per >= 2 else []
    for w in open_windows:
        sheets_out.append({"n": len(sheets_out) + 1, "alignments": [w["alignment"]], "layout": sheet["layout"],
                           "paper": [paper_w, paper_h], "origin": w["origin"], "scale": scale, "views": [w["name"]],
                           "existing": [w["name"]], "filled": True,
                           "view_center": [round(w["origin"][0] + win_w / 2.0, 4), round(w["origin"][1] + win_h / 2.0, 4)]})
    k_new = 0
    for i, (a, n, v) in enumerate(ordered):
        name = str(a.get("name"))
        pa = per_alignment[name]
        length = v["station_end"] - v["station_start"]
        target = next((sh for sh in sheets_out if len(sh["views"]) < per), None)
        if target is None:
            col, row = k_new % per_row, k_new // per_row
            k_new += 1
            win_x = round(origin[0] + col * col_pitch, 4)
            win_y = round(origin[1] - row * row_pitch - win_h, 4)
            target = {"n": len(sheets_out) + 1, "alignments": [], "layout": sheet["layout"], "paper": [paper_w, paper_h],
                      "origin": [win_x, win_y], "scale": scale, "views": [], "existing": [],
                      "view_center": [round(win_x + win_w / 2.0, 4), round(win_y + win_h / 2.0, 4)]}
            sheets_out.append(target)
        sh = target
        slot_i = len(sh["views"])
        slot = slots[slot_i]
        win_x, win_y = sh["origin"]
        vname = ps["name_format"].format(alignment=name, n=n, start=station_text(v["station_start"]),
                                         end=station_text(v["station_end"]))
        if vname.lower() in taken:
            raise ProfileError(f"profile view '{vname}' already exists in the drawing; delete it or change name_format")
        taken.add(vname.lower())
        in_view_pipes = [p["handle"] for p in pa["pipes"]
                         if min(p["start_station"], p["end_station"]) <= v["station_end"] + 1e-6
                         and max(p["start_station"], p["end_station"]) >= v["station_start"] - 1e-6]
        in_view_structs = [s["handle"] for s in pa["structures"]
                           if v["station_start"] - 1e-6 <= s["station"] <= v["station_end"] + 1e-6]
        dx, dy = slot_offset(sheet, slot_i, length, scale)
        ins = [round(win_x + dx, 4), round(win_y + dy, 4)]
        height = (v["elevation_max"] - v["elevation_min"]) * ve
        view = {"alignment": name, "alignment_handle": a.get("handle"), "name": vname, "n": n,
                "station_start": v["station_start"], "station_end": v["station_end"],
                "elevation_min": v["elevation_min"], "elevation_max": v["elevation_max"],
                "insertion": ins, "sheet": sh["n"], "slot": slot_i,
                "box": {"min": [ins[0], round(ins[1] - 60.0, 4)], "max": [round(ins[0] + length, 4), round(ins[1] + height, 4)]},
                "pipes": in_view_pipes, "structures": in_view_structs, "split": v["split"],
                "low": v["low"], "high": v["high"], "warnings": list(v["warnings"])}
        if dx / scale + length / scale > paper_w + 1e-6 or dx < -1e-6:
            warnings.append(f"{vname} is {length / scale:g} in long and runs past the {paper_w:g} in paper")
        warnings.extend(f"{vname}: {w}" for w in v["warnings"])
        views.append(view)
        pa["views"].append(view)
        sh["views"].append(vname)
        if name not in sh["alignments"]:
            sh["alignments"].append(name)

    summary_rows = []
    for a in aligns:
        name = str(a.get("name"))
        pa = per_alignment[name]
        summary_rows.append({"alignment": name, "start": station_text(pa["start"]), "end": station_text(pa["end"]),
                             "length": round(pa["end"] - pa["start"], 2), "restation_shift": round(pa["shift"], 4),
                             "views": [{"name": v["name"], "stations": f"{station_text(v['station_start'])}-{station_text(v['station_end'])}",
                                        "datum": v["elevation_min"], "top": v["elevation_max"], "split": v["split"],
                                        "sheet": v["sheet"], "pipes": len(v["pipes"]), "structures": len(v["structures"])} for v in pa["views"]]})

    request = {
        "intent": "draw_profile_views",
        "apply": False,
        "span": ps["span"],
        "surface": ps["surface"] or "",
        "restation": bool(ps["restation"]),
        "restation_to": ps["station_start"],
        "station_host": ps["station_host"],
        "station_increment": ps["station_increment"],
        "drag_to_datum": {"pipe": True, "station": True, "lift": ps["drag_lift"]},
        "dim_lines": dict(ps.get("dim_line") or {}),
        "dimension_anchor": dict(ps.get("dimension_anchor") or {}),
        "styles": st,
        "views": [{k: v for k, v in view.items() if k not in ("box", "low", "high", "warnings", "split", "n", "sheet", "slot")}
                  for view in views],
        "alignments": [{"name": str(a.get("name")), "handle": a.get("handle")} for a in aligns],
        "sheet_windows": [{"n": s["n"], "alignments": s["alignments"], "origin": s["origin"],
                           "size": [round(win_w, 4), round(win_h, 4)], "views": s["views"]} for s in sheets_out],
    }
    request["proposed"] = proposed_from_windows(sheets_out, win_w, win_h)
    summary = {"skipped_existing": skipped_existing,
               "span": ps["span"], "station_host": ps["station_host"], "vertical_exaggeration": ve, "scale": scale,
               "origin": list(origin), "count": len(views), "alignments": summary_rows, "sheets": sheets_out,
               "layout": sheet["layout"], "paper": [paper_w, paper_h], "slots": slots,
               "restationed": restationed, "warnings": warnings, "styles": st,
               "boxes": [v["box"] for v in views]}
    return request, summary


def unpaired_windows(data: dict, lay: dict, scale: float, span: float = 40.0) -> list:
    """Sheet windows in the drawing that hold one view of this span and no second:
    a view not yet shown by any sheet's viewport whose window (its datum corner
    less its slot-0 offset) has nothing at the second slot. Oldest first, by
    name. [{name, alignment, origin, span}]"""
    if not data or len(lay.get("slots") or []) < 2:
        return []
    span_wanted = span
    pvs = []
    for pv in data.get("profile_views") or []:
        try:
            span = int(round(float(pv.get("elevation_max")) - float(pv.get("elevation_min"))))
            loc = pv.get("location") or (pv.get("extents") or [None])[0]
            x, y = float(loc[0]), float(loc[1])
            length = float(pv.get("station_end")) - float(pv.get("station_start"))
        except (TypeError, ValueError, IndexError):
            continue
        if span != int(round(float(span_wanted))):
            continue            # a 36 ft view never shares a sheet with a 40 ft one
        pvs.append({"name": str(pv.get("name") or ""), "alignment": str(pv.get("alignment") or ""), "span": span,
                    "x": x, "y": y, "length": length})
    # a view inside an existing sheet viewport is spoken for
    windows = []
    for lay_ in data.get("layouts") or []:
        for vp in lay_.get("viewports") or []:
            win = vp.get("model_window")
            try:
                (x0, y0), (x1, y1) = win
                windows.append((x0, y0, x1, y1))
            except (TypeError, ValueError):
                continue
    free = [v for v in pvs if not any(x0 - 1e-6 <= v["x"] <= x1 + 1e-6 and y0 - 1e-6 <= v["y"] <= y1 + 1e-6 for x0, y0, x1, y1 in windows)]
    out = []
    for v in sorted(free, key=lambda q: (natural_key(q["alignment"]), q["name"])):
        dx0, dy0 = slot_offset(lay, 0, v["length"], scale)
        origin = [round(v["x"] - dx0, 4), round(v["y"] - dy0, 4)]
        taken = False
        for o in free:
            if o is v:
                continue
            dx1, dy1 = slot_offset(lay, 1, o["length"], scale)
            if abs(o["x"] - (origin[0] + dx1)) <= 1.0 and abs(o["y"] - (origin[1] + dy1)) <= 1.0:
                taken = True
                break
        # a view sitting at the LOWER slot of someone's window is the second of a pair
        is_second = any(abs(v["x"] - (round(o["x"] - slot_offset(lay, 0, o["length"], scale)[0], 4) + slot_offset(lay, 1, v["length"], scale)[0])) <= 1.0
                        and abs(v["y"] - (round(o["y"] - slot_offset(lay, 0, o["length"], scale)[1], 4) + slot_offset(lay, 1, v["length"], scale)[1])) <= 1.0
                        for o in free if o is not v)
        if not taken and not is_second:
            out.append({"name": v["name"], "alignment": v["alignment"], "origin": origin, "span": v["span"]})
    return out


def proposed_from_windows(sheets_out: list, win_w: float, win_h: float, fill_every: float = 100.0) -> dict:
    """The gate's view of the plan: every sheet window as a box plus horizontal
    fill lines through it, so the verifier counts everything drawn inside (the
    views, their bands, the labels) as planned -- planned cells cover chords,
    not areas."""
    boxes, segments = [], []
    for s in sheets_out:
        x0, y0 = s["origin"]
        x1, y1 = round(x0 + win_w, 4), round(y0 + win_h, 4)
        boxes.append({"min": [x0, y0], "max": [x1, y1]})
        y = y0 + fill_every
        while y < y1 - 1e-6:
            segments.append([[x0, round(y, 4)], [x1, round(y, 4)]])
            y += fill_every
    return {"boxes": boxes, "segments": segments}


def proposed(request: dict) -> dict:
    """The proposed geometry of a request built by plan()."""
    if isinstance(request.get("proposed"), dict):
        return request["proposed"]
    return {"boxes": [], "segments": []}


def describe(summary: dict) -> list:
    """One line per alignment, in words, then the sheets."""
    out = []
    for a in summary.get("alignments") or []:
        parts = []
        for v in a["views"]:
            tag = " [split]" if v["split"] else ""
            parts.append(f"{v['stations']} datum {v['datum']:g}-{v['top']:g}{tag} ({v['pipes']} pipes, {v['structures']} MH, sheet {v['sheet']})")
        shift = a.get("restation_shift") or 0
        out.append(f"{a['alignment']}: {len(a['views'])} view(s) {a['start']}-{a['end']}: " + "; ".join(parts)
                   + (f"; restationed by {shift:+g} ft" if shift else ""))
    sheets = summary.get("sheets") or []
    if sheets:
        paper = summary.get("paper") or ["?", "?"]
        out.append(f"{len(sheets)} sheet(s) on layout '{summary.get('layout')}' ({paper[0]:g} x {paper[1]:g} in, "
                   f"1\" = {summary.get('scale', 40):g}'): " + "; ".join(f"{s['n']}: {', '.join(s['views'])}" for s in sheets))
    out.extend(f"warning: {w}" for w in summary.get("warnings") or [])
    return out
