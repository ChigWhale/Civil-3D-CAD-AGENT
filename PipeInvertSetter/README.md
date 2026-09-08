# PipeInvertSetter — Pipe Elevation Solver (Civil 3D 2025 plug-in)

Sets the start/end invert elevations of the **same-diameter pipes meeting at one
structure** so that

1. minimum cover from the network's **reference surface** holds along each pipe's
   full length (surface is sampled at both ends and every ~10 ft between), and
2. the pipes keep a chosen relationship at the shared structure:
   `MatchCrowns` (default), `MatchInverts`, or `ManualDrop(x)`.

It is deliberately conservative: it **fails loud** on bad preconditions, **reports
conflicts instead of guessing**, and writes **all pipes in one transaction or none**.

## Build & load

    dotnet build PipeInvertSetter/PipeInvertSetter.csproj -c Release
    dotnet test  PipeInvertSetter.Tests            # AutoCAD-free tests of the math

Output: `PipeInvertSetter/bin/Release/net8.0-windows/PipeInvertSetter.dll`.
`NETLOAD` it directly, or load `CADAgent.dll` (which references it and exposes the
solver over HTTP). Civil 3D auto-detected from `C:\Program Files\Autodesk\AutoCAD 20xx`.

## Commands

| Command         | What it does |
|-----------------|--------------|
| `PIPEDASH`      | Opens the **dashboard palette** (see below): a *Run along alignment* tab with the whole profile in front of you, editable by hand; a *Solve at structure* tab; and overview tabs with **every pipe and every structure in the network**. |
| `PIPESOLVERUN`  | Command-line run solver: pick an alignment (or Enter, then two structures) → cover → offset mode → flow direction → hold start/end → printed profile table → Apply Y/N. |
| `PIPESOLVE`     | Single structure: pick structure → cover (Enter = rule set) → offset mode → governing pipe → printed report → Apply Y/N. |
| `PIPESOLVEJSON` | Same as `PIPESOLVE`, report written to a JSON file. |

## The dashboard: elevations along a run, set algorithmically or by hand

Tab **Run along alignment**:

1. choose an alignment (structures within *± ft* of it, in station order) or *Pick start/end…*
   two structures (shortest network path between them);
2. set cover (blank = rule set), offset mode, drop, flow direction (`auto` reads the
   pipes' flow directions, else the ground fall), *Hold start* / *Hold end* for tie-ins;
3. **Solve run** fills the two blue columns (*New inv up / down*) with the shallowest
   feasible profile; the sketch shows surface, rim ticks, required-cover line (dashed),
   old inverts (grey) and new (blue, red where in conflict);
4. **or type inverts by hand** into the blue cells — **Re-check edits** recomputes cover
   at both ends and along the pipe, slope, rule violations, and the offset actually
   present at each structure, exactly as for a solved run;
5. **Apply run** writes every pipe in one transaction (all-or-nothing, verified against
   Civil 3D's cover check). *Copy JSON* hands the same report to the Python layer.

Structures that also carry pipes outside the run are listed (with a warning) and those
laterals are left untouched — use the structure tab there afterwards.

Run algorithm (`ChainSolver`): every pipe's constraints (cover at ends + interior samples,
min/max slope, tie-ins) form a convex polygon in (upstream, downstream) invert space.
A backward pass projects the set of upstream inverts that still admit a feasible
completion; a forward pass marches downstream taking the shallowest choice each step.
Empty set ⇒ conflict, with the delta to the shallowest *unconstrained* profile so the
tie-in can be renegotiated. JSON: handler `pipe_run`, MCP tool `solve_pipe_run`.

## Algorithm (what the report shows)

Per pipe connected to the structure:

* `surface` at the structure end and far end, plus interior samples.
* cover is measured to the **outside top** of the pipe: `maxInvert = surface − cover − innerHeight − wallThickness`.
* `coverMinimum` comes from the request, else the pipe's rule set (any parameter whose
  key contains *cover* + *min*), else `Pipe.MinimumCover`. Missing everywhere → error.
* min/max slope come from the rule set (*slope* + *min*/*max*). Units are auto-calibrated
  against `Pipe.Slope` vs. geometry and echoed as `slopeUnits` / `slopeUnitsReason`.
  All raw rule parameters are echoed in `ruleParams` so nothing is a black box.
* Reconciliation at the structure (`ElevationMath.ReconcileAtStructure`): the governing
  pipe (explicit, or the most restrictive cover) fixes the datum; the others follow the
  offset mode. `structureInvertOverride` replaces the governing datum.
* Far end (`ElevationMath.FitFarEnd`): shallowest invert satisfying cover along the
  length and the slope bounds. If none exists the pipe gets a `slope_vs_cover` conflict
  with the delta and a `suggestedStructureInvert`.
* Sanity: invert above surface or deeper than `maxDepthBelowSurface` (50 ft default) is a
  conflict — the elevation-datum-mismatch guard.
* `Pipe.StartPoint/EndPoint.Z` is written directly (not `ApplyRules()`, which would
  re-run Civil 3D's own rule engine over the solved values). Whether that Z is the
  centerline or the invert is detected per pipe from `CoverOfStartPoint` and echoed as
  `zConvention`.
* After the write, `CoverOfStartPoint/Endpoint` and `IsMinCoverViolated` are read back
  from Civil 3D; any disagreement rolls the transaction back.

## Preconditions that stop the solve (nothing is written)

* pipes in scope don't share one inner diameter (explicit list: error; auto-discovery:
  the dominant diameter is used and the rest are listed under `warnings`)
* no reference surface on the pipe or its network
* surface has no coverage at a pipe end
* cover minimum unavailable (no input, no rule)
* any conflict (see above)

## JSON boundary

`POST http://127.0.0.1:5199/handler` (via CADAgent.dll):

```json
{ "handler": "pipe_elevation", "stage": "context | compute | draw", "file": "C:\\...\\x.dwg",
  "request": { "structure": "MH-3", "coverMinimum": 3.0, "offsetMode": "MatchCrowns",
               "manualDrop": 0.1, "pipes": ["P-1","P-2"], "governingPipe": "P-1",
               "structureInvertOverride": 94.25, "maxDepthBelowSurface": 50, "slopeUnits": "auto" } }
```

MCP tools in `cad_mcp_server.py`: `pipe_network_context` and `solve_pipe_elevations`
(`apply=False` to compute, `apply=True` to draw). The report carries per-pipe old/new
invert + crown + cover at both ends, min cover along the pipe, old/new slope, rule
bounds, violations, and a `conflicts` list with `kind`, both constraints, `delta`,
and `suggestedStructureInvert`. Conflict *resolution* is the caller's job.

## Extending

Implement `ICadObjectHandler` (`GetContext` / `Compute` / `Draw`) and register it in
`HandlerRegistry`; the `/handler` endpoint and MCP helper dispatch by name.
