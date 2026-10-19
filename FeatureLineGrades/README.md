# FeatureLineGrades — live grade labels on feature lines (Civil 3D 2025 plug-in)

Display only. `FLGRADES` writes the grade of every feature line segment next to the
segment as **transient text**. Nothing is added to the drawing, no elevation or slope is
changed, nothing is saved or plotted. This DLL contains that one command and no other.

    2.00% >     rises to the right (arrow points uphill, in the text's reading direction)
    < 2.00%     rises to the left
    0.00%       flat, no arrow

The arrow comes from the two endpoint elevations of the segment, so it points at the
higher end whichever way the feature line was drawn.

## Build & load

    dotnet build FeatureLineGrades/FeatureLineGrades.csproj -c Debug

Output: `FeatureLineGrades/bin/Debug/net10.0-windows/FeatureLineGrades.dll`, also copied
to `bin/Debug/net10.0-windows/`. `NETLOAD` it in Civil 3D 2025. Close Civil 3D before
rebuilding; it locks the DLL.

## Use

1. `FLGRADES` → `[All/Selected]` (Enter = every feature line in model space).
2. Edit as usual: labels rebuild after grip edits, the elevation editor and undo, and
   re-scale when you zoom.
3. `FLGRADES` again turns the labels off. Closing the drawing also removes them.

## Behaviour

- One label per segment between consecutive points of the line. PIs **and** elevation
  points count as points, since the grade changes at an elevation point.
- Hundredths of a percent. Arcs use arc length and the local tangent. Text is never
  upside down.
- Colour ACI 6 (magenta), height 1.2 % of the view height, 2 decimals: the static
  `GradeOverlay.ColorIndex` / `HeightFraction` / `Decimals` properties.
- Any `FeatureLine` object is labelled (editable, corridor, data reference); only geometry
  is read.

## Sources

The code is shared with `FeatureLineSync/` by linked files; this project links only the
read-only ones (`FlGeometry.cs`, `SnapshotModel.cs`, `FeatureLineReader.cs`,
`GradeLabels.cs`, `GradeOverlay.cs`) plus its own `PlugIn.cs`. The label maths is covered
by `FeatureLineSync.Tests/GradeLabelsTests.cs`.
