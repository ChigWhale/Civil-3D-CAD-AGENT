using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.Civil.ApplicationServices;
using Autodesk.Civil.DatabaseServices;
using CivilSurface = Autodesk.Civil.DatabaseServices.Surface;
using static PipeInvertSetter.PipeElevationSolver;

namespace PipeInvertSetter
{
    /// <summary>Resolved inputs for the run solver.</summary>
    public sealed class RunInput
    {
        public ObjectId AlignmentId = ObjectId.Null;
        public double OffsetTolerance = 25.0;
        public ObjectId NetworkId = ObjectId.Null;
        public List<ObjectId>? StructureIds;
        public ObjectId StartStructureId = ObjectId.Null, EndStructureId = ObjectId.Null;
        public string FlowDirection = "auto";
        public double? CoverMinimum;
        public OffsetMode Mode = OffsetMode.MatchCrowns;
        public double ManualDrop;
        public bool HoldStart, HoldEnd;
        public Dictionary<ObjectId, double>? FixedInverts;
        public double MaxDepthBelowSurface = 50.0;
        public string SlopeUnits = "auto";
        public Dictionary<ObjectId, PipeInvertOverride>? Overrides;
    }

    /// <summary>
    /// Solves a whole run of pipes (along an alignment, an explicit structure list, or the
    /// shortest network path between two structures) with the same criteria as the
    /// single-structure solver: cover along every pipe, rule-set slope bounds, offset rule
    /// at every structure, tie-ins reported as conflicts.  <see cref="Evaluate"/> re-derives
    /// cover/slope/violations from whatever inverts are in the rows, so hand edits in the
    /// dashboard go through exactly the same checks before <see cref="Apply"/>.
    /// </summary>
    public static class PipeRunSolver
    {
        private const double PostApplyCoverTolerance = 0.05;

        // ═══════════════════════════════════════════════════════════════════
        //  COMPUTE
        // ═══════════════════════════════════════════════════════════════════

        public static RunReport Compute(Document doc, RunInput input)
        {
            var report = new RunReport
            {
                OffsetMode = input.Mode.ToString(),
                ManualDrop = input.ManualDrop,
                CoverMinimumInput = input.CoverMinimum,
            };

            using DocumentLock lk = doc.LockDocument();
            using Transaction tr = doc.Database.TransactionManager.StartTransaction();
            try
            {
                // ── discover the run ──────────────────────────────────────
                Run run = DiscoverRun(tr, doc.Database, input, report);
                Network net = (Network)tr.GetObject(run.NetworkId, OpenMode.ForRead, false);
                report.Network = net.Name;
                report.ReferenceSurface = SafeString(() => net.ReferenceSurfaceName);

                // ── flow direction → orient so index 0 is upstream ────────
                OrientRun(tr, run, input, report);

                // ── per-pipe data ─────────────────────────────────────────
                var rows = new List<RunPipeRow>();
                for (int k = 0; k < run.Pipes.Count; k++)
                {
                    var pipe = (Pipe)tr.GetObject(run.Pipes[k], OpenMode.ForRead, false);
                    ObjectId up = run.Structures[k], down = run.Structures[k + 1];
                    bool upIsStart = pipe.StartStructureId == up;
                    if (!upIsStart && pipe.EndStructureId != up)
                        throw new InvalidOperationException($"Pipe '{pipe.Name}' is not connected to structure '{StructureName(tr, up)}'.");

                    CivilSurface surface = ResolveSurface(tr, pipe, net);
                    Point3d upPt = upIsStart ? pipe.StartPoint : pipe.EndPoint;
                    Point3d dnPt = upIsStart ? pipe.EndPoint : pipe.StartPoint;

                    var row = new RunPipeRow
                    {
                        Index = k, Name = pipe.Name, Handle = run.Pipes[k].Handle.ToString(),
                        Upstream = StructureName(tr, up), Downstream = StructureName(tr, down),
                        InnerDiameter = pipe.InnerDiameterOrWidth,
                        Length2D = pipe.Length2DCenterToCenter > 0 ? pipe.Length2DCenterToCenter : pipe.Length2D,
                        InnerHeight = pipe.InnerHeight, WallThickness = pipe.WallThickness, UpIsStart = upIsStart,
                        SurfaceUp = SampleSurface(surface, upPt, pipe.Name, "upstream end"),
                        SurfaceDown = SampleSurface(surface, dnPt, pipe.Name, "downstream end"),
                    };
                    row.Samples = SampleBetween(surface, upPt, dnPt, row.Length2D);

                    (string rs, var parms) = ReadRuleParams(tr, pipe);
                    row.RuleSet = rs;
                    if (input.CoverMinimum is double cm) { row.CoverMinimum = cm; row.CoverSource = "input"; }
                    else if (FindParam(parms, out string ck, out double cv, "cover", "min")) { row.CoverMinimum = cv; row.CoverSource = "rule:" + ck; }
                    else if (SafeDouble(() => pipe.MinimumCover) is double pm && pm > 0) { row.CoverMinimum = pm; row.CoverSource = "pipe.MinimumCover"; }
                    else throw new InvalidOperationException(
                        $"coverMinimum was not specified and pipe '{pipe.Name}' has no cover rule (rule set '{rs}', keys: {string.Join(", ", parms.Keys)}).");
                    if (FindParam(parms, out _, out double maxC, "cover", "max")) row.MaxCoverRule = maxC;
                    if (FindParam(parms, out _, out double minS, "slope", "min")) row.MinSlope = minS;
                    if (FindParam(parms, out _, out double maxS, "slope", "max")) row.MaxSlope = maxS;

                    bool centerline = DetectCenterlineZ(pipe, upIsStart, row.SurfaceUp, out string? warn);
                    if (warn != null) report.Warnings.Add(warn);
                    row.ZConvention = centerline ? "centerline" : "invert";
                    double half = centerline ? pipe.InnerHeight / 2.0 : 0.0;
                    row.OldUpInvert = upPt.Z - half;
                    row.OldDownInvert = dnPt.Z - half;
                    row.OldSlope = ElevationMath.Slope(row.OldUpInvert, row.OldDownInvert, row.Length2D);
                    rows.Add(row);
                }
                report.Pipes = rows;

                // ── slope units ───────────────────────────────────────────
                (report.SlopeUnits, report.SlopeUnitsReason) = CalibrateSlopeUnits(tr, input.SlopeUnits,
                    rows.Select(r => (IdOf(doc.Database, r.Handle), r.Name, r.OldSlope, r.MinSlope, r.MaxSlope)));
                if (report.SlopeUnits == "percent")
                    foreach (var r in rows) { r.MinSlope /= 100.0; r.MaxSlope /= 100.0; }

                // ── structures ────────────────────────────────────────────
                for (int j = 0; j < run.Structures.Count; j++)
                {
                    var s = (Structure)tr.GetObject(run.Structures[j], OpenMode.ForRead, false);
                    var srow = new RunStructureRow
                    {
                        Index = j, Name = s.Name, Handle = run.Structures[j].Handle.ToString(),
                        Station = run.Station.TryGetValue(run.Structures[j], out double st) ? st : null,
                        Offset = run.Offset.TryGetValue(run.Structures[j], out double off) ? off : null,
                        Rim = SafeDouble(() => s.RimElevation) ?? double.NaN,
                        Surface = j < rows.Count ? rows[j].SurfaceUp : rows[j - 1].SurfaceDown,
                    };
                    var inRun = new HashSet<ObjectId>(run.Pipes);
                    for (int i = 0; i < s.ConnectedPipesCount; i++)
                    {
                        ObjectId pid = s.get_ConnectedPipe(i);
                        if (!inRun.Contains(pid) && tr.GetObject(pid, OpenMode.ForRead, false) is Pipe other)
                            srow.OtherPipes.Add(other.Name);
                    }
                    if (srow.OtherPipes.Count > 0)
                        report.Warnings.Add($"Structure '{s.Name}' also connects {string.Join(", ", srow.OtherPipes)} (not in the run; left untouched — run the structure solver there afterwards).");
                    if (input.FixedInverts != null && input.FixedInverts.TryGetValue(run.Structures[j], out double fx)) srow.FixedInvert = fx;
                    report.Structures.Add(srow);
                }
                if (input.HoldStart) report.Structures[0].FixedInvert ??= rows[0].OldUpInvert;
                if (input.HoldEnd) report.Structures[^1].FixedInvert ??= rows[^1].OldDownInvert;

                // ── chain solve ───────────────────────────────────────────
                var chain = new List<ChainSolver.ChainPipe>();
                for (int k = 0; k < rows.Count; k++)
                {
                    var r = rows[k];
                    var cp = new ChainSolver.ChainPipe
                    {
                        Id = r.Name, InnerHeight = r.InnerHeight, Length = r.Length2D,
                        UpCeiling = ElevationMath.MaxInvertForCover(r.SurfaceUp, r.CoverMinimum, r.InnerHeight, r.WallThickness),
                        DownCeiling = ElevationMath.MaxInvertForCover(r.SurfaceDown, r.CoverMinimum, r.InnerHeight, r.WallThickness),
                        MinSlope = r.MinSlope, MaxSlope = r.MaxSlope,
                    };
                    foreach (double[] sm in r.Samples)
                        cp.Samples.Add((sm[0], ElevationMath.MaxInvertForCover(sm[1], r.CoverMinimum, r.InnerHeight, r.WallThickness)));
                    if (report.Structures[k].FixedInvert is double fu && k == 0) cp.FixedUp = fu;
                    if (report.Structures[k + 1].FixedInvert is double fd) cp.FixedDown = fd;
                    chain.Add(cp);
                }

                var result = ChainSolver.Solve(chain, input.Mode, input.ManualDrop);
                foreach (var c in result.Conflicts)
                {
                    RunPipeRow? pr = c.PipeIndex >= 0 && c.PipeIndex < rows.Count ? rows[c.PipeIndex] : null;
                    report.Conflicts.Add(new ConflictReport
                    {
                        Pipe = pr?.Name ?? "", Handle = pr?.Handle ?? "", Kind = c.Kind, Delta = c.Delta, Message = c.Message,
                        ConstraintA = "tie-in / rule set", ConstraintB = "cover + slope bounds",
                    });
                    if (pr != null) pr.Status = "conflict";
                }
                if (result.Feasible)
                    for (int k = 0; k < rows.Count; k++) { rows[k].NewUpInvert = result.Up[k]; rows[k].NewDownInvert = result.Down[k]; }

                // ── hand overrides from the request ───────────────────────
                if (input.Overrides != null)
                    foreach (var r in rows)
                        if (input.Overrides.TryGetValue(IdOf(doc.Database, r.Handle), out var ov))
                        {
                            if (ov.Up.HasValue) r.NewUpInvert = ov.Up;
                            if (ov.Down.HasValue) r.NewDownInvert = ov.Down;
                        }
            }
            catch (Exception ex)
            {
                report.Errors.Add(ex.Message);
            }
            tr.Commit();

            Evaluate(report, input.MaxDepthBelowSurface);
            return report;
        }

        // ═══════════════════════════════════════════════════════════════════
        //  EVALUATE — derive cover / slope / violations from whatever inverts are in the rows
        // ═══════════════════════════════════════════════════════════════════

        public static void Evaluate(RunReport report, double maxDepth)
        {
            report.Conflicts.RemoveAll(c => c.Kind == "sanity" || c.Kind == "adverse_slope" || c.Kind == "cover" || c.Kind == "offset_rule");
            var mode = PipeElevationHandler.ParseMode(report.OffsetMode);

            foreach (var r in report.Pipes)
            {
                r.Violations.Clear();
                if (r.Status != "conflict") r.Status = "ok";
                if (r.NewUpInvert is not double up || r.NewDownInvert is not double dn)
                {
                    r.NewSlope = null; r.NewCoverUp = null; r.NewCoverDown = null; r.NewMinCoverAlongPipe = null;
                    if (r.Status == "ok") r.Status = "unsolved";
                    continue;
                }

                r.NewCoverUp = ElevationMath.CoverAtInvert(r.SurfaceUp, up, r.InnerHeight, r.WallThickness);
                r.NewCoverDown = ElevationMath.CoverAtInvert(r.SurfaceDown, dn, r.InnerHeight, r.WallThickness);
                r.NewSlope = ElevationMath.Slope(up, dn, r.Length2D);

                double minCover = Math.Min(r.NewCoverUp.Value, r.NewCoverDown.Value);
                foreach (double[] sm in r.Samples)
                {
                    double inv = up + (dn - up) * sm[0];
                    minCover = Math.Min(minCover, ElevationMath.CoverAtInvert(sm[1], inv, r.InnerHeight, r.WallThickness));
                }
                r.NewMinCoverAlongPipe = minCover;

                if (minCover + 1e-6 < r.CoverMinimum)
                    AddConflict(report, r, "cover", $"min cover along pipe {minCover:0.00} < required {r.CoverMinimum:0.00}", r.CoverMinimum - minCover);
                if (r.NewSlope < -1e-9)
                    AddConflict(report, r, "adverse_slope", $"adverse slope {r.NewSlope:0.0000} (downstream invert above upstream)", -r.NewSlope.Value);
                if (r.MinSlope is double mn && r.NewSlope + 1e-9 < mn) r.Violations.Add($"slope {r.NewSlope:0.0000} < min {mn:0.0000}");
                if (r.MaxSlope is double mx && r.NewSlope - 1e-9 > mx) r.Violations.Add($"slope {r.NewSlope:0.0000} > max {mx:0.0000}");
                if (r.MaxCoverRule is double mc)
                {
                    if (r.NewCoverUp > mc + 1e-6) r.Violations.Add($"cover up {r.NewCoverUp:0.00} > max {mc:0.00}");
                    if (r.NewCoverDown > mc + 1e-6) r.Violations.Add($"cover down {r.NewCoverDown:0.00} > max {mc:0.00}");
                }
                foreach (var (label, inv, surf) in new[] { ("upstream", up, r.SurfaceUp), ("downstream", dn, r.SurfaceDown) })
                {
                    if (double.IsNaN(inv) || double.IsInfinity(inv)) AddConflict(report, r, "sanity", $"{label} invert is not a number", 0);
                    else if (inv > surf) AddConflict(report, r, "sanity", $"{label} invert {inv:0.000} is above the surface {surf:0.000} — check the datum", inv - surf);
                    else if (surf - inv > maxDepth) AddConflict(report, r, "sanity", $"{label} invert is {surf - inv:0.0} ft below the surface, beyond the {maxDepth:0.#} ft limit — likely a datum mismatch", surf - inv - maxDepth);
                }
                if (r.Status == "ok" && Math.Abs(up - r.OldUpInvert) < 1e-4 && Math.Abs(dn - r.OldDownInvert) < 1e-4) r.Status = "unchanged";
            }

            // structure rows: in/out inverts and the offset actually present
            for (int j = 0; j < report.Structures.Count; j++)
            {
                var s = report.Structures[j];
                RunPipeRow? inc = j > 0 && j - 1 < report.Pipes.Count ? report.Pipes[j - 1] : null;
                RunPipeRow? outg = j < report.Pipes.Count ? report.Pipes[j] : null;
                s.OldInvertIn = inc?.OldDownInvert; s.NewInvertIn = inc?.NewDownInvert;
                s.OldInvertOut = outg?.OldUpInvert; s.NewInvertOut = outg?.NewUpInvert;
                s.Drop = (s.NewInvertIn is double i && s.NewInvertOut is double o) ? i - o : null;

                if (inc != null && outg != null && s.Drop is double d)
                {
                    double want = ChainSolver.DropAtStructure(mode, inc.InnerHeight, outg.InnerHeight, report.ManualDrop);
                    if (Math.Abs(d - want) > 1e-3)
                        outg.Violations.Add($"offset at {s.Name}: drop {d:0.000} ≠ {report.OffsetMode} {want:0.000}");
                }
                if (s.FixedInvert is double fx)
                {
                    double? actual = j == 0 ? s.NewInvertOut : s.NewInvertIn;
                    if (actual is double a && Math.Abs(a - fx) > 1e-4 && inc != null)
                        AddConflict(report, inc, "sanity", $"tie-in at {s.Name}: invert {a:0.000} ≠ held value {fx:0.000}", a - fx);
                }
            }

            report.Ok = report.Errors.Count == 0 && report.Conflicts.Count == 0 &&
                        report.Pipes.Count > 0 && report.Pipes.All(p => p.NewUpInvert.HasValue && p.NewDownInvert.HasValue);
        }

        private static void AddConflict(RunReport report, RunPipeRow r, string kind, string message, double delta)
        {
            report.Conflicts.Add(new ConflictReport { Pipe = r.Name, Handle = r.Handle, Kind = kind, Message = $"Pipe '{r.Name}': {message}", Delta = delta });
            r.Status = "conflict";
        }

        // ═══════════════════════════════════════════════════════════════════
        //  APPLY — writes the rows' NewUp/NewDown inverts (solved or hand-edited)
        // ═══════════════════════════════════════════════════════════════════

        public static RunReport Apply(Document doc, RunReport report, double maxDepth)
        {
            Evaluate(report, maxDepth);
            report.Stage = "draw";
            if (!report.Ok)
            {
                report.Errors.Add("Not applied: resolve the errors/conflicts first (all-or-nothing).");
                return report;
            }

            using DocumentLock lk = doc.LockDocument();
            using Transaction tr = doc.Database.TransactionManager.StartTransaction();
            var failures = new List<string>();
            try
            {
                foreach (var r in report.Pipes)
                {
                    var pipe = (Pipe)tr.GetObject(IdOf(doc.Database, r.Handle), OpenMode.ForWrite, false);
                    double half = r.ZConvention == "centerline" ? r.InnerHeight / 2.0 : 0.0;
                    double startInv = r.UpIsStart ? r.NewUpInvert!.Value : r.NewDownInvert!.Value;
                    double endInv   = r.UpIsStart ? r.NewDownInvert!.Value : r.NewUpInvert!.Value;
                    Point3d sp = pipe.StartPoint, ep = pipe.EndPoint;
                    pipe.StartPoint = new Point3d(sp.X, sp.Y, startInv + half);
                    pipe.EndPoint   = new Point3d(ep.X, ep.Y, endInv + half);
                }

                foreach (var r in report.Pipes)
                {
                    var pipe = (Pipe)tr.GetObject(IdOf(doc.Database, r.Handle), OpenMode.ForRead, false);
                    double? cs = SafeDouble(() => pipe.CoverOfStartPoint), ce = SafeDouble(() => pipe.CoverOfEndpoint);
                    r.Civil3DCoverUpAfterApply = r.UpIsStart ? cs : ce;
                    r.Civil3DCoverDownAfterApply = r.UpIsStart ? ce : cs;
                    if (r.Civil3DCoverUpAfterApply is double a && Math.Abs(a - r.NewCoverUp!.Value) > PostApplyCoverTolerance)
                        failures.Add($"Pipe '{r.Name}': Civil 3D reports upstream cover {a:0.000} but solver expected {r.NewCoverUp:0.000}.");
                    if (r.Civil3DCoverDownAfterApply is double b && Math.Abs(b - r.NewCoverDown!.Value) > PostApplyCoverTolerance)
                        failures.Add($"Pipe '{r.Name}': Civil 3D reports downstream cover {b:0.000} but solver expected {r.NewCoverDown:0.000}.");
                    try { r.Civil3DMinCoverViolatedAfterApply = MinCoverViolated(pipe, r.CoverMinimum); } catch { }
                    if (r.Civil3DMinCoverViolatedAfterApply) failures.Add($"Pipe '{r.Name}': Civil 3D flags minimum cover ({r.CoverMinimum:0.##}) violated after the write.");
                }
            }
            catch (Exception ex) { failures.Add("Write failed: " + ex.Message); }

            if (failures.Count > 0)
            {
                tr.Abort();
                report.Errors.AddRange(failures);
                report.Errors.Add("Transaction rolled back; the drawing was not modified.");
                report.Ok = false; report.Applied = false;
                return report;
            }
            tr.Commit();
            report.Applied = true;
            foreach (var r in report.Pipes) if (r.Status == "ok") r.Status = "applied";
            return report;
        }

        public static RunReport Draw(Document doc, RunInput input)
        {
            RunReport report = Compute(doc, input);
            return report.Ok ? Apply(doc, report, input.MaxDepthBelowSurface) : report;
        }

        // ═══════════════════════════════════════════════════════════════════
        //  run discovery
        // ═══════════════════════════════════════════════════════════════════

        private sealed class Run
        {
            public ObjectId NetworkId;
            public List<ObjectId> Structures = new();   // ordered
            public List<ObjectId> Pipes = new();        // Pipes[k] joins Structures[k] and Structures[k+1]
            public Dictionary<ObjectId, double> Station = new(), Offset = new();
            public string OrderBasis = "";              // "station" | "listed" | "path"
        }

        private static Run DiscoverRun(Transaction tr, Database db, RunInput input, RunReport report)
        {
            var run = new Run();

            if (input.StructureIds is { Count: >= 2 })
            {
                run.Structures = input.StructureIds.ToList();
                run.NetworkId = ((Structure)tr.GetObject(run.Structures[0], OpenMode.ForRead, false)).NetworkId;
                run.OrderBasis = "listed";
            }
            else if (!input.StartStructureId.IsNull && !input.EndStructureId.IsNull)
            {
                var s0 = (Structure)tr.GetObject(input.StartStructureId, OpenMode.ForRead, false);
                run.NetworkId = s0.NetworkId;
                var net = (Network)tr.GetObject(run.NetworkId, OpenMode.ForRead, false);
                run.Structures = PathStructures(tr, net, input.StartStructureId, input.EndStructureId);
                run.OrderBasis = "path";
            }
            else if (!input.AlignmentId.IsNull)
            {
                var align = (Alignment)tr.GetObject(input.AlignmentId, OpenMode.ForRead, false);
                report.Alignment = align.Name;

                IEnumerable<ObjectId> netIds = input.NetworkId.IsNull
                    ? CivilDocument.GetCivilDocument(db).GetPipeNetworkIds().Cast<ObjectId>()
                    : new[] { input.NetworkId };

                var best = new List<(double st, double off, ObjectId id)>();
                foreach (ObjectId netId in netIds)
                {
                    var net = (Network)tr.GetObject(netId, OpenMode.ForRead, false);
                    var hits = new List<(double st, double off, ObjectId id)>();
                    foreach (ObjectId sid in net.GetStructureIds())
                    {
                        var s = (Structure)tr.GetObject(sid, OpenMode.ForRead, false);
                        double st = 0, off = 0;
                        try { align.StationOffset(s.Location.X, s.Location.Y, ref st, ref off); } catch { continue; }
                        if (Math.Abs(off) <= input.OffsetTolerance) hits.Add((st, off, sid));
                    }
                    if (hits.Count > best.Count) { best = hits; run.NetworkId = netId; }
                    else if (hits.Count > 0 && hits.Count == best.Count)
                        report.Warnings.Add($"Network '{net.Name}' also has {hits.Count} structures on '{align.Name}'; using '{((Network)tr.GetObject(run.NetworkId, OpenMode.ForRead, false)).Name}'. Pass 'network' to choose.");
                }
                if (best.Count < 2)
                    throw new InvalidOperationException($"Fewer than two structures lie within {input.OffsetTolerance:0.#} ft of alignment '{align.Name}'.");

                best.Sort((a, b) => a.st.CompareTo(b.st));
                foreach (var (st, off, id) in best) { run.Structures.Add(id); run.Station[id] = st; run.Offset[id] = off; }
                run.OrderBasis = "station";
            }
            else throw new ArgumentException("Give an alignment, an ordered list of structures, or a start and end structure.");

            // ── chain pipes between consecutive structures ─────────────────
            var network = (Network)tr.GetObject(run.NetworkId, OpenMode.ForRead, false);
            var structures = new List<ObjectId> { run.Structures[0] };
            for (int i = 0; i + 1 < run.Structures.Count; i++)
            {
                ObjectId a = run.Structures[i], b = run.Structures[i + 1];
                ObjectId pipe = ConnectingPipe(tr, a, b);
                if (!pipe.IsNull) { run.Pipes.Add(pipe); structures.Add(b); continue; }

                // not directly connected: bridge through the network path
                List<ObjectId> path = PathStructures(tr, network, a, b);
                if (path.Count < 2)
                    throw new InvalidOperationException($"Structures '{StructureName(tr, a)}' and '{StructureName(tr, b)}' are consecutive on the run but no pipe path joins them.");
                report.Warnings.Add($"'{StructureName(tr, a)}' → '{StructureName(tr, b)}' are not directly connected; bridged through {string.Join(" → ", path.Skip(1).Take(path.Count - 2).Select(s => StructureName(tr, s)))}.");
                for (int j = 0; j + 1 < path.Count; j++)
                {
                    ObjectId p = ConnectingPipe(tr, path[j], path[j + 1]);
                    if (p.IsNull) throw new InvalidOperationException("Network path is broken between consecutive structures.");
                    run.Pipes.Add(p); structures.Add(path[j + 1]);
                }
            }
            run.Structures = structures;
            return run;
        }

        private static ObjectId ConnectingPipe(Transaction tr, ObjectId a, ObjectId b)
        {
            var s = (Structure)tr.GetObject(a, OpenMode.ForRead, false);
            for (int i = 0; i < s.ConnectedPipesCount; i++)
            {
                ObjectId pid = s.get_ConnectedPipe(i);
                if (tr.GetObject(pid, OpenMode.ForRead, false) is not Pipe p) continue;
                ObjectId other = p.StartStructureId == a ? p.EndStructureId : p.StartStructureId;
                if (other == b) return pid;
            }
            return ObjectId.Null;
        }

        /// <summary>Ordered structures from <paramref name="from"/> to <paramref name="to"/> along the shortest network path.</summary>
        private static List<ObjectId> PathStructures(Transaction tr, Network net, ObjectId from, ObjectId to)
        {
            double len = 0;
            ObjectIdCollection parts;
            try { parts = Network.FindShortestNetworkPath(from, to, ref len); }
            catch (Exception ex) { throw new InvalidOperationException($"No network path from '{StructureName(tr, from)}' to '{StructureName(tr, to)}': {ex.Message}"); }

            var pipes = new HashSet<ObjectId>();
            foreach (ObjectId id in parts)
                if (tr.GetObject(id, OpenMode.ForRead, false) is Pipe) pipes.Add(id);

            // walk from 'from' using the pipes on the path
            var order = new List<ObjectId> { from };
            var visited = new HashSet<ObjectId> { from };
            ObjectId cur = from;
            while (cur != to)
            {
                var s = (Structure)tr.GetObject(cur, OpenMode.ForRead, false);
                ObjectId next = ObjectId.Null;
                for (int i = 0; i < s.ConnectedPipesCount && next.IsNull; i++)
                {
                    ObjectId pid = s.get_ConnectedPipe(i);
                    if (!pipes.Contains(pid)) continue;
                    var p = (Pipe)tr.GetObject(pid, OpenMode.ForRead, false);
                    ObjectId other = p.StartStructureId == cur ? p.EndStructureId : p.StartStructureId;
                    if (!other.IsNull && !visited.Contains(other)) next = other;
                }
                if (next.IsNull) return new List<ObjectId>();
                order.Add(next); visited.Add(next); cur = next;
            }
            return order;
        }

        private static void OrientRun(Transaction tr, Run run, RunInput input, RunReport report)
        {
            string fd = (input.FlowDirection ?? "auto").ToLowerInvariant();
            bool reverse = false;
            string basis = run.OrderBasis == "station" ? "station" : "listed order";

            if (fd == "downstation" || fd == "aslisted" || fd == "forward") { report.FlowDirection = fd == "downstation" ? "downstation" : "asListed"; report.FlowDirectionReason = "specified by input"; }
            else if (fd == "upstation" || fd == "reverse") { reverse = true; report.FlowDirection = "upstation"; report.FlowDirectionReason = "specified by input"; }
            else
            {
                int fwd = 0, back = 0;
                for (int k = 0; k < run.Pipes.Count; k++)
                {
                    var p = (Pipe)tr.GetObject(run.Pipes[k], OpenMode.ForRead, false);
                    bool startAtK = p.StartStructureId == run.Structures[k];
                    switch (p.FlowDirection)
                    {
                        case FlowDirectionType.StartToEnd: if (startAtK) fwd++; else back++; break;
                        case FlowDirectionType.EndToStart: if (startAtK) back++; else fwd++; break;
                    }
                }
                if (fwd != back)
                {
                    reverse = back > fwd;
                    report.FlowDirectionReason = $"pipe flow directions: {fwd} with {basis}, {back} against";
                }
                else
                {
                    // fall back to the ground: water runs downhill
                    var s0 = (Structure)tr.GetObject(run.Structures[0], OpenMode.ForRead, false);
                    var sN = (Structure)tr.GetObject(run.Structures[^1], OpenMode.ForRead, false);
                    double z0 = SafeDouble(() => s0.SurfaceElevationAtInsertionPoint) ?? SafeDouble(() => s0.RimElevation) ?? double.NaN;
                    double zN = SafeDouble(() => sN.SurfaceElevationAtInsertionPoint) ?? SafeDouble(() => sN.RimElevation) ?? double.NaN;
                    if (double.IsNaN(z0) || double.IsNaN(zN) || Math.Abs(z0 - zN) < 1e-6)
                        throw new InvalidOperationException("Flow direction could not be determined (pipes are bidirectional and the ground is level end to end). Pass flowDirection = downstation | upstation.");
                    reverse = zN > z0;
                    report.FlowDirectionReason = $"no pipe flow directions set; ground falls from {(reverse ? "last" : "first")} to {(reverse ? "first" : "last")} structure ({Math.Max(z0, zN):0.00} → {Math.Min(z0, zN):0.00})";
                }
                report.FlowDirection = run.OrderBasis == "station" ? (reverse ? "upstation" : "downstation") : (reverse ? "reversed" : "asListed");
            }

            if (reverse)
            {
                run.Structures.Reverse();
                run.Pipes.Reverse();
            }
        }

        // ═══════════════════════════════════════════════════════════════════
        //  helpers
        // ═══════════════════════════════════════════════════════════════════

        private static List<double[]> SampleBetween(CivilSurface surface, Point3d from, Point3d to, double length)
        {
            int n = (int)Math.Clamp(Math.Round(length / 10.0), 4, 50);
            var list = new List<double[]>();
            for (int i = 1; i < n; i++)
            {
                double t = (double)i / n;
                try
                {
                    double z = surface.FindElevationAtXY(from.X + (to.X - from.X) * t, from.Y + (to.Y - from.Y) * t);
                    if (!double.IsNaN(z)) list.Add(new[] { t, z });
                }
                catch { }
            }
            return list;
        }

        internal static ObjectId IdOf(Database db, string handle)
        {
            long h = Convert.ToInt64(handle, 16);
            if (!db.TryGetObjectId(new Handle(h), out ObjectId id) || id.IsNull || id.IsErased)
                throw new InvalidOperationException($"Object with handle {handle} no longer exists.");
            return id;
        }
    }
}
