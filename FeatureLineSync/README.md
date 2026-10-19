# FeatureLineSync — restore feature line grades after the road moved (Civil 3D 2025 plug-in)

The situation it solves: grading feature lines (pad edges and the slope lines
climbing from the road to the pads) had correct elevations and grades. The road
width changed, the lines were moved, and Civil 3D dropped every elevation to 0.
A **Save As** copy made before the move still holds the correct profile, and its
handles are identical to the current drawing.

The method, applied to **each feature line on its own** (no connection logic
between lines):

1. every point of the line that lies on the chosen surface (the corridor / road
   surface) is set to that surface's elevation at its new XY. These are the
   **contact points**, the only fixed ones;
2. every other point is set by working outward from the contact points, segment
   by segment, with the **slope of the corresponding segment in the copy** over
   the segment's current length. Signs are kept: a segment that fell away from
   the road in the copy falls away from it now, whichever way the line is drawn.
   If point 1 is the contact at 700 and the copy's slope 1→2 is +2 %, point 2
   becomes 700 + 0.02 × the current distance;
3. where a run of points sits between two contact points (a pad with both top
   corners on the road), the misclosure at the second is spread linearly along
   the run (traverse adjustment) and reported.

A line with no contact point is listed with the distance to the nearest surface
coverage and **left untouched**; raise the tolerance if it should have been on
the surface. Copying the old elevations verbatim is wrong for
this case (the points moved); it exists only as the `Elevations` mode of
`FLIMPORT`, for restoring a backup.

## Build & load

    dotnet build FeatureLineSync/FeatureLineSync.csproj -c Debug
    dotnet test  FeatureLineSync.Tests            # AutoCAD-free tests of geometry + planner

Output: `FeatureLineSync/bin/Debug/net10.0-windows/FeatureLineSync.dll`. `NETLOAD`
it in Civil 3D 2025 (the `AutoCAD 2025` install is auto-detected). Close Civil 3D
before rebuilding; it locks the DLL. The plug-in is standalone: it is not part of
the CAD-Agent bundle and `CADAgent.csproj` excludes its sources.

## Commands

| Command    | What it does |
|------------|--------------|
| `FLSYNC`   | In the **current** (moved) drawing: pick the copy `.dwg` → surface number → surface tolerance → preview table → `Apply? [Yes/No]`. The copy is read from disk as a side database (or from the open document if it is open); it is never opened or modified. |
| `FLEXPORT` | Writes a JSON snapshot of the current drawing's feature lines. Fallback when the side-database read fails: open the copy, `FLEXPORT`, then `FLIMPORT` in the current drawing. |
| `FLIMPORT` | Same pipeline as `FLSYNC` with a JSON snapshot as the copy, after a `[Grades/Elevations]` prompt. `Grades` = the method above. `Elevations` = restore: `FLSYNC` writes `<drawing>.flsync-backup-<stamp>.json` next to the drawing before applying; `FLIMPORT` it in `Elevations` mode to get the pre-apply values back. |
| `FLGRADES` | Toggle. Writes the grade of every feature line segment next to the segment (`2.00% >`, hundredths of a percent, arrow pointing uphill) as **transient text**: nothing is added to the drawing, nothing is saved or plotted. `[All/Selected]` on turn-on. Labels follow grip edits, the elevation editor and undo, and re-scale on zoom. Run again to turn off. |

### FLGRADES details

- One label per segment between consecutive points of the line (PIs **and** elevation
  points, since the grade changes at an elevation point). Arcs use arc length and the
  local tangent for the text rotation. Text is never upside down.
- The arrow points uphill, toward the end of the segment where the grade is positive, in
  the text's reading direction: `2.00% >` rises to the right, `< 2.00%` rises to the left,
  `0.00%` has no arrow. It is derived from the two endpoint elevations, so it is the same
  whichever way the feature line was drawn.
- Label colour (ACI 6, magenta), height (1.2 % of the view height) and decimals are the
  static `GradeOverlay.ColorIndex` / `HeightFraction` / `Decimals` properties.
- Works on any `FeatureLine` object (editable, corridor, data-reference) because it only
  reads geometry. The overlay is per document and is dropped when the document closes.

Every run is one undo step.

## Prompts

- **Surface**: pick the corridor / road surface, not the overall FG surface. If more
  than 60 % of all points lie on the surface the preview warns: that surface would
  anchor nearly everything and the copy's slopes would be discarded.
- **Surface tolerance** (default 0.5): a point just outside the surface edge still
  takes the surface elevation if the surface has one within this radius
  (8-point ring probe).

## Preview and verification

The preview lists every feature line: handle, name, site, how it was matched
(`handle`, then `name` as fallback), point and anchor counts, grade range before
(copy) and after, the largest misclosure, and its status:

The `Fixed` column shows how many points of the line are on the surface.

| Status | Meaning |
|--------|---------|
| `ok` | contact points set, the copy's slopes set every other point |
| `all fixed` | every point is on the surface; the copy's slopes are not used on this line |
| `no fixed point` | nothing on the surface within the tolerance; untouched |
| `unmatched` | no feature line with that handle or name in the copy; untouched |
| `locked_layer` | layer is locked; untouched |

Suffix `rebuild` = the copy's elevation points are re-inserted on the new
geometry first, so the slopes apply segment by segment as in the copy.

Warnings per line: PI count differs (copy profile mapped by normalized station),
elevation points rebuilt. Where two lines share a location and get different
values, the location is listed; values are not changed, Civil 3D's same-site
rule decides at write time and the verification after the write shows the
result.

After the write every planned point is read back from Civil 3D. Differences are
printed as `site_propagation` (Civil 3D moved a value, usually a crossing point in
the same site), `extra_point` (points on the line the plan did not know about) or
`duplicate_points`. Lines Civil 3D refuses (an elevation point it will not insert)
are rolled back individually and listed; the rest is written.

Skipped and never written: dynamic / corridor feature lines, data references.

## Live test procedure

`acad.exe` normally has production drawings open. Never run `FLSYNC` on them
until it has been exercised on test copies:

1. Synthetic: new drawing from the template, a small sloped TIN surface, two
   feature lines in one site (a slope line starting on the surface and running off
   it, a pad line sharing its far vertex), hand-set elevations, `SAVEAS
   fl_test_before.dwg`, move both lines, `SAVEAS fl_test_after.dwg`. `FLSYNC` in
   `after` against `before`: start point on the surface, slope line grade equal to
   `before`, pad line anchored through the shared vertex. One undo restores all.
2. Real data: `SAVEAS` both production files into a scratch folder as `*_TEST.dwg`,
   close the originals, run `FLSYNC` in `current_TEST` against `before_TEST`, read
   the preview before answering Yes, check the verification block, spot-check
   grades in the feature line elevation editor.
3. Fallback: `FLEXPORT` in `before_TEST`, `FLIMPORT` in `current_TEST`; the preview
   must match run 2.
4. Backup: `FLIMPORT` the `.flsync-backup-*.json` in `Elevations` mode and confirm
   the pre-apply values return.

## Files

| File | Role |
|------|------|
| `SnapshotModel.cs` | JSON model shared by the side-database read, FLEXPORT/FLIMPORT and the backup (schema 1) |
| `FlGeometry.cs` | PI polyline math with bulge arcs: lengths, point at fraction, projection, closure detection |
| `SyncPlanner.cs` | matching, point alignment, fixed points, slope propagation, misclosure spread, shared-location report |
| `FeatureLineReader.cs` | drawing → snapshot (host document or side database) |
| `SurfaceProbe.cs` | "does this point touch the corridor" = `Surface.FindElevationAtXY` + ring probe |
| `FeatureLineWriter.cs` | rebuild elevation points, `SetPointElevation` by re-read index, verify, nested transaction per line |
| `Commands.cs` | the three commands, prompts, preview |
