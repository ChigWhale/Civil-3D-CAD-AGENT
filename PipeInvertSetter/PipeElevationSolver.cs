using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.Civil.DatabaseServices;
using Autodesk.Civil.DatabaseServices.Styles;
using Autodesk.Civil.Runtime;
using CivilSurface = Autodesk.Civil.DatabaseServices.Surface;

namespace PipeInvertSetter
{
    /// <summary>Solver inputs after handles/names have been resolved to ObjectIds.</summary>
    public sealed class SolverInput
    {
        public ObjectId StructureId;
        public double? CoverMinimum;
        public OffsetMode Mode = OffsetMode.MatchCrowns;
        public double ManualDrop;
        public List<ObjectId>? PipeIds;
        public ObjectId GoverningPipeId = ObjectId.Null;
        public double? StructureInvertOverride;
        public double MaxDepthBelowSurface = 50.0;
        public string SlopeUnits = "auto";
    }

    /// <summary>
    /// Same-diameter pipe elevation solver at a shared structure.
    /// <para>
    /// Compute: surface-driven cover ceilings at both ends and along the pipe, rule-set
    /// slope bounds, offset reconciliation at the structure, sanity checks — no writes.
    /// Draw: Compute, then write every pipe in one transaction only when the report is clean,
    /// and verify against Civil 3D's own cover calculation before committing.
    /// </para>
    /// </summary>
    public static class PipeElevationSolver
    {
        private const double DiameterTolerance = 1e-6;
        private const double ZConventionTolerance = 0.02;
        private const double UnchangedTolerance = 1e-4;
        private const double PostApplyCoverTolerance = 0.05;

        // ── internal per-pipe working state ────────────────────────────────

        private sealed class PipeWork
        {
            public ObjectId Id;
            public PipeReport Report = new();
            public bool StructureIsStart;
            public bool CenterlineZ = true;           // detected Z convention
            public double InnerHeight, WallThickness, Length2D;
            public Point3d StartPt, EndPt;
            public double SurfAtStructure, SurfAtFar;
            public double CoverMin;
            public double? MinSlope, MaxSlope;
            public FlowRelation Relation;
            public double[] SampleT = Array.Empty<double>();   // 0..1 along pipe from structure end
            public double[] SampleSurf = Array.Empty<double>();
            public double? NewInvertAtStructure, NewInvertAtFar;

            public double InvertFromZ(double z) => CenterlineZ ? z - InnerHeight / 2.0 : z;
            public double ZFromInvert(double inv) => CenterlineZ ? inv + InnerHeight / 2.0 : inv;
            public double OuterTopOffset => InnerHeight + WallThickness;
        }

        // ═══════════════════════════════════════════════════════════════════
        //  COMPUTE
        // ═══════════════════════════════════════════════════════════════════

        public static SolveReport Compute(Document doc, SolverInput input)
        {
            using DocumentLock lk = doc.LockDocument();
            using Transaction tr = doc.Database.TransactionManager.StartTransaction();
            SolveReport report = ComputeCore(tr, input, out _);
            tr.Commit();          // read-only; commit is cheaper than abort
            return report;
        }

        private static SolveReport ComputeCore(Transaction tr, SolverInput input, out List<PipeWork> works)
        {
            var report = new SolveReport
            {
                Stage = "compute",
                OffsetMode = input.Mode.ToString(),
                ManualDrop = input.ManualDrop,
                CoverMinimumInput = input.CoverMinimum,
                StructureInvertOverride = input.StructureInvertOverride,
            };
            works = new List<PipeWork>();

            try
            {
                // ── structure + network ────────────────────────────────────
                if (tr.GetObject(input.StructureId, OpenMode.ForRead, false) is not Structure structure)
                    throw new InvalidOperationException("The selected object is not a pipe network structure.");

                Network? network = tr.GetObject(structure.NetworkId, OpenMode.ForRead, false) as Network;
                report.Structure = DescribeStructure(tr, structure, network);

                // ── pipes in scope ─────────────────────────────────────────
                List<ObjectId> pipeIds = ResolvePipeScope(tr, structure, input, report);
                if (pipeIds.Count == 0)
                    throw new InvalidOperationException($"No pipes connected to structure '{structure.Name}' are in scope.");

                // ── per-pipe geometry, surface, rules ──────────────────────
                foreach (ObjectId pid in pipeIds)
                {
                    if (tr.GetObject(pid, OpenMode.ForRead, false) is not Pipe pipe)
                        throw new InvalidOperationException($"Object {pid.Handle} is not a pipe.");

                    bool atStart = pipe.StartStructureId == input.StructureId;
                    bool atEnd   = pipe.EndStructureId == input.StructureId;
                    if (!atStart && !atEnd)
                        throw new InvalidOperationException($"Pipe '{pipe.Name}' ({pid.Handle}) is not connected to structure '{structure.Name}'.");

                    var w = new PipeWork
                    {
                        Id = pid,
                        StructureIsStart = atStart,
                        InnerHeight = pipe.InnerHeight,
                        WallThickness = pipe.WallThickness,
                        Length2D = pipe.Length2DCenterToCenter > 0 ? pipe.Length2DCenterToCenter : pipe.Length2D,
                        StartPt = pipe.StartPoint,
                        EndPt = pipe.EndPoint,
                    };
                    w.Report.Name = pipe.Name;
                    w.Report.Handle = pid.Handle.ToString();
                    w.Report.InnerDiameter = pipe.InnerDiameterOrWidth;
                    w.Report.OuterHeight = pipe.OuterHeight;
                    w.Report.WallThickness = pipe.WallThickness;
                    w.Report.Length2D = w.Length2D;
                    w.Report.StructureEnd = atStart ? "start" : "end";
                    w.Report.IsGoverning = pid == input.GoverningPipeId;

                    // reference surface — fail loud when missing
                    CivilSurface surface = ResolveSurface(tr, pipe, network);

                    // surface elevations at both endpoints (coverage check)
                    Point3d sPt = atStart ? w.StartPt : w.EndPt;
                    Point3d fPt = atStart ? w.EndPt : w.StartPt;
                    w.SurfAtStructure = SampleSurface(surface, sPt, pipe.Name, "structure end");
                    w.SurfAtFar       = SampleSurface(surface, fPt, pipe.Name, "far end");
                    SampleAlongPipe(surface, sPt, fPt, w);

                    // rules
                    (string ruleSet, Dictionary<string, double> parms) = ReadRuleParams(tr, pipe);
                    w.Report.RuleSet = ruleSet;
                    w.Report.RuleParams = parms;

                    // cover minimum: explicit input, else rule, else Pipe.MinimumCover
                    if (input.CoverMinimum.HasValue)
                    {
                        w.CoverMin = input.CoverMinimum.Value;
                        w.Report.CoverSource = "input";
                    }
                    else if (FindParam(parms, out string coverKey, out double coverVal, "cover", "min"))
                    {
                        w.CoverMin = coverVal;
                        w.Report.CoverSource = "rule:" + coverKey;
                    }
                    else if (SafeDouble(() => pipe.MinimumCover) is double pm && pm > 0)
                    {
                        w.CoverMin = pm;
                        w.Report.CoverSource = "pipe.MinimumCover";
                    }
                    else
                    {
                        throw new InvalidOperationException(
                            $"coverMinimum was not specified and pipe '{pipe.Name}' has no cover rule " +
                            $"(rule set '{ruleSet}', keys: {string.Join(", ", parms.Keys)}).");
                    }
                    w.Report.CoverMinimum = w.CoverMin;
                    if (FindParam(parms, out _, out double maxCover, "cover", "max")) w.Report.MaxCoverRule = maxCover;

                    // slope bounds (raw; unit interpretation decided below)
                    if (FindParam(parms, out _, out double minS, "slope", "min")) w.MinSlope = minS;
                    if (FindParam(parms, out _, out double maxS, "slope", "max")) w.MaxSlope = maxS;

                    // Z convention + old state
                    DetectZConvention(pipe, w, report);
                    w.Relation = DetectFlow(structure, pipe, pid, atStart);
                    w.Report.Flow = w.Relation.ToString().ToLowerInvariant();

                    FillOldState(tr, pipe, w, atStart);
                    works.Add(w);
                }

                // ── identical diameter precondition ────────────────────────
                double dia0 = works[0].Report.InnerDiameter;
                var mismatched = works.Where(x => Math.Abs(x.Report.InnerDiameter - dia0) > DiameterTolerance).ToList();
                if (mismatched.Count > 0)
                    throw new InvalidOperationException(
                        "Pipes in scope do not share one diameter: " +
                        string.Join("; ", works.Select(x => $"{x.Report.Name}={x.Report.InnerDiameter:0.###}")) +
                        ". Nothing was changed.");

                // ── slope units ────────────────────────────────────────────
                ApplySlopeUnits(tr, works, input, report);

                // ── reconcile at the structure ─────────────────────────────
                var sideInputs = works.Select(w => new ElevationMath.StructureSideInput
                {
                    Id = w.Report.Handle,
                    InnerHeight = w.InnerHeight,
                    MaxInvert = w.Report.AtStructure.MaxInvertForCover,
                    Relation = w.Relation,
                    IsGoverning = w.Report.IsGoverning,
                }).ToList();

                var side = ElevationMath.ReconcileAtStructure(sideInputs, input.Mode, input.ManualDrop, input.StructureInvertOverride);
                report.GoverningPipe = works.First(w => w.Report.Handle == side.GoverningId).Report.Name;
                foreach (var w in works) w.Report.IsGoverning = w.Report.Handle == side.GoverningId;

                foreach (var (id, invert, maxInvert) in side.CoverViolations)
                {
                    PipeWork w = works.First(x => x.Report.Handle == id);
                    report.Conflicts.Add(new ConflictReport
                    {
                        Pipe = w.Report.Name, Handle = id, Kind = "cover_at_structure",
                        ConstraintA = $"offset rule wants invert {invert:0.000} at structure",
                        ConstraintB = $"cover {w.CoverMin:0.##} allows at most {maxInvert:0.000}",
                        Delta = invert - maxInvert,
                        Message = $"Pipe '{w.Report.Name}': {input.Mode} places its invert {invert - maxInvert:0.000} above the cover ceiling at the structure.",
                        SuggestedStructureInvert = maxInvert - (invert - (input.StructureInvertOverride ?? works.First(x => x.Report.Handle == side.GoverningId).Report.AtStructure.MaxInvertForCover)),
                    });
                    w.Report.Status = "conflict";
                }

                // ── far ends: cover + slope feasibility ────────────────────
                foreach (var w in works)
                {
                    double invS = side.InvertById[w.Report.Handle];
                    w.NewInvertAtStructure = invS;

                    // Cover ceiling at the far end, tightened by every interior sample so
                    // cover holds along the whole length, not just at the connection.
                    double farCeiling = w.Report.FarEnd.MaxInvertForCover;
                    for (int i = 0; i < w.SampleT.Length; i++)
                    {
                        double t = w.SampleT[i];
                        double c = ElevationMath.MaxInvertForCover(w.SampleSurf[i], w.CoverMin, w.InnerHeight, w.WallThickness);
                        if (t < 0.05)
                        {
                            if (invS > c + 1e-6)
                                AddConflict(report, w, "cover_along_length",
                                    $"structure-side invert {invS:0.000}", $"surface near structure allows {c:0.000}",
                                    invS - c, $"Pipe '{w.Report.Name}': cover fails {t * w.Length2D:0.#} ft from the structure.", c);
                            continue;
                        }
                        farCeiling = Math.Min(farCeiling, (c - invS * (1 - t)) / t);
                    }

                    var fit = ElevationMath.FitFarEnd(invS, farCeiling, w.Length2D, w.Relation, w.MinSlope, w.MaxSlope);
                    if (!fit.Feasible)
                    {
                        AddConflict(report, w, "slope_vs_cover",
                            $"structure invert {invS:0.000} with slope bounds [{Fmt(w.MinSlope)}, {Fmt(w.MaxSlope)}]",
                            $"far-end cover ceiling {farCeiling:0.000}",
                            fit.RequiredStructureInvertShift, $"Pipe '{w.Report.Name}': {fit.Reason}",
                            invS + fit.RequiredStructureInvertShift);
                        continue;
                    }

                    w.NewInvertAtFar = fit.FarInvert;
                    w.Report.NewSlope = fit.Slope;
                    FillNewState(w, invS, fit.FarInvert);

                    // sanity: datum mismatch guard
                    SanityCheck(report, w, input.MaxDepthBelowSurface);

                    if (w.MinSlope.HasValue && fit.Slope + 1e-9 < w.MinSlope.Value) w.Report.Violations.Add($"slope {fit.Slope:0.0000} < min {w.MinSlope:0.0000}");
                    if (w.MaxSlope.HasValue && fit.Slope - 1e-9 > w.MaxSlope.Value) w.Report.Violations.Add($"slope {fit.Slope:0.0000} > max {w.MaxSlope:0.0000}");
                    if (w.Report.MaxCoverRule is double mc)
                    {
                        if (w.Report.AtStructure.NewCover > mc + 1e-6) w.Report.Violations.Add($"cover at structure {w.Report.AtStructure.NewCover:0.00} > max {mc:0.00}");
                        if (w.Report.FarEnd.NewCover > mc + 1e-6) w.Report.Violations.Add($"cover at far end {w.Report.FarEnd.NewCover:0.00} > max {mc:0.00}");
                    }
                    if (w.Report.Status == "ok" &&
                        Math.Abs(invS - w.Report.AtStructure.OldInvert) < UnchangedTolerance &&
                        Math.Abs(fit.FarInvert - w.Report.FarEnd.OldInvert) < UnchangedTolerance)
                        w.Report.Status = "unchanged";
                }
            }
            catch (Exception ex)
            {
                report.Errors.Add(ex.Message);
            }

            report.Pipes = works.Select(w => w.Report).ToList();
            report.Ok = report.Errors.Count == 0 && report.Conflicts.Count == 0;
            return report;
        }

        // ═══════════════════════════════════════════════════════════════════
        //  DRAW (transactional write)
        // ═══════════════════════════════════════════════════════════════════

        public static SolveReport Draw(Document doc, SolverInput input)
        {
            using DocumentLock lk = doc.LockDocument();
            using Transaction tr = doc.Database.TransactionManager.StartTransaction();

            SolveReport report = ComputeCore(tr, input, out List<PipeWork> works);
            report.Stage = "draw";

            if (!report.Ok)
            {
                report.Errors.Add("Not applied: resolve the errors/conflicts above first (all-or-nothing).");
                tr.Abort();
                return report;
            }

            var failures = new List<string>();
            try
            {
                foreach (var w in works)
                {
                    if (w.NewInvertAtStructure is not double invS || w.NewInvertAtFar is not double invF) continue;
                    var pipe = (Pipe)tr.GetObject(w.Id, OpenMode.ForWrite, false);

                    double startInv = w.StructureIsStart ? invS : invF;
                    double endInv   = w.StructureIsStart ? invF : invS;
                    Point3d sp = pipe.StartPoint, ep = pipe.EndPoint;
                    pipe.StartPoint = new Point3d(sp.X, sp.Y, w.ZFromInvert(startInv));
                    pipe.EndPoint   = new Point3d(ep.X, ep.Y, w.ZFromInvert(endInv));
                }

                // Verify against Civil 3D's own cover computation before committing.
                foreach (var w in works)
                {
                    if (w.NewInvertAtStructure is null) continue;
                    var pipe = (Pipe)tr.GetObject(w.Id, OpenMode.ForRead, false);

                    double? cs = SafeDouble(() => pipe.CoverOfStartPoint);
                    double? ce = SafeDouble(() => pipe.CoverOfEndpoint);
                    EndReport startRep = w.StructureIsStart ? w.Report.AtStructure : w.Report.FarEnd;
                    EndReport endRep   = w.StructureIsStart ? w.Report.FarEnd : w.Report.AtStructure;
                    startRep.Civil3DCoverAfterApply = cs;
                    endRep.Civil3DCoverAfterApply = ce;

                    if (cs is double a && startRep.NewCover is double na && Math.Abs(a - na) > PostApplyCoverTolerance)
                        failures.Add($"Pipe '{w.Report.Name}': Civil 3D reports start cover {a:0.000} but solver expected {na:0.000}.");
                    if (ce is double b && endRep.NewCover is double nb && Math.Abs(b - nb) > PostApplyCoverTolerance)
                        failures.Add($"Pipe '{w.Report.Name}': Civil 3D reports end cover {b:0.000} but solver expected {nb:0.000}.");

                    bool violated = false;
                    try
                    {
                        violated = MinCoverViolated(pipe, w.CoverMin);
                    }
                    catch { /* older builds throw for pipes without a surface; the cover comparison above still guards */ }
                    w.Report.Civil3DMinCoverViolatedAfterApply = violated;
                    if (violated) failures.Add($"Pipe '{w.Report.Name}': Civil 3D flags minimum cover ({w.CoverMin:0.##}) violated after the write.");
                }
            }
            catch (Exception ex)
            {
                failures.Add("Write failed: " + ex.Message);
            }

            if (failures.Count > 0)
            {
                tr.Abort();
                report.Errors.AddRange(failures);
                report.Errors.Add("Transaction rolled back; the drawing was not modified.");
                report.Ok = false;
                report.Applied = false;
                return report;
            }

            tr.Commit();
            report.Applied = true;
            foreach (var p in report.Pipes) if (p.Status == "ok") p.Status = "applied";
            return report;
        }

        // ═══════════════════════════════════════════════════════════════════
        //  CONTEXT (read-only overview of a whole network)
        // ═══════════════════════════════════════════════════════════════════

        public static ContextReport GetContext(Document doc, ObjectId structureId, ObjectId networkId)
        {
            var ctx = new ContextReport();
            using DocumentLock lk = doc.LockDocument();
            using Transaction tr = doc.Database.TransactionManager.StartTransaction();
            try
            {
                Structure? focus = structureId.IsNull ? null : tr.GetObject(structureId, OpenMode.ForRead, false) as Structure;
                if (focus != null) networkId = focus.NetworkId;
                if (networkId.IsNull) throw new InvalidOperationException("No structure or network specified.");

                if (tr.GetObject(networkId, OpenMode.ForRead, false) is not Network net)
                    throw new InvalidOperationException("Object is not a pipe network.");

                ctx.Network = net.Name;
                ctx.NetworkHandle = networkId.Handle.ToString();
                ctx.ReferenceSurface = SafeString(() => net.ReferenceSurfaceName);
                ctx.PartsList = SafeString(() => net.PartsListName);

                if (focus != null)
                {
                    ctx.Structure = DescribeStructure(tr, focus, net);
                    ctx.ConnectedPipes = SafeArray(() => focus.GetConnectedPipeNames()).ToList();
                }

                foreach (ObjectId sid in net.GetStructureIds())
                {
                    if (tr.GetObject(sid, OpenMode.ForRead, false) is not Structure s) continue;
                    ctx.Structures.Add(new NetworkStructureRow
                    {
                        Name = s.Name, Handle = sid.Handle.ToString(),
                        X = s.Location.X, Y = s.Location.Y,
                        Rim = SafeDouble(() => s.RimElevation) ?? double.NaN,
                        Sump = SafeDouble(() => s.SumpElevation) ?? double.NaN,
                        SurfaceElevation = SafeDouble(() => s.SurfaceElevationAtInsertionPoint) ?? double.NaN,
                        ConnectedPipes = s.ConnectedPipesCount,
                        Pipes = string.Join(", ", SafeArray(() => s.GetConnectedPipeNames())),
                    });
                }

                foreach (ObjectId pid in net.GetPipeIds())
                {
                    if (tr.GetObject(pid, OpenMode.ForRead, false) is not Pipe pipe) continue;
                    var row = new NetworkPipeRow
                    {
                        Name = pipe.Name, Handle = pid.Handle.ToString(),
                        From = StructureName(tr, pipe.StartStructureId),
                        To = StructureName(tr, pipe.EndStructureId),
                        InnerDiameter = pipe.InnerDiameterOrWidth,
                        Length2D = pipe.Length2DCenterToCenter > 0 ? pipe.Length2DCenterToCenter : pipe.Length2D,
                        Flow = pipe.FlowDirection.ToString(),
                        CoverStart = SafeDouble(() => pipe.CoverOfStartPoint) ?? double.NaN,
                        CoverEnd = SafeDouble(() => pipe.CoverOfEndpoint) ?? double.NaN,
                    };

                    // Z convention: prefer the cover-derived invert when a surface is available.
                    double half = pipe.InnerHeight / 2.0;
                    bool centerline = true;
                    try
                    {
                        CivilSurface surf = ResolveSurface(tr, pipe, net);
                        double surfZ = surf.FindElevationAtXY(pipe.StartPoint.X, pipe.StartPoint.Y);
                        double invFromCover = surfZ - row.CoverStart - pipe.InnerHeight - pipe.WallThickness;
                        if (!double.IsNaN(invFromCover) &&
                            Math.Abs(pipe.StartPoint.Z - invFromCover) < Math.Abs(pipe.StartPoint.Z - half - invFromCover))
                            centerline = false;
                    }
                    catch { }
                    row.StartInvert = centerline ? pipe.StartPoint.Z - half : pipe.StartPoint.Z;
                    row.EndInvert   = centerline ? pipe.EndPoint.Z - half : pipe.EndPoint.Z;
                    row.Slope = ElevationMath.Slope(row.StartInvert, row.EndInvert, row.Length2D);

                    (string rs, var parms) = ReadRuleParams(tr, pipe);
                    row.RuleSet = rs;
                    if (FindParam(parms, out _, out double mc, "cover", "min")) row.MinCoverRule = mc;
                    else row.MinCoverRule = SafeDouble(() => pipe.MinimumCover) ?? 0;
                    if (row.MinCoverRule > 0)
                    {
                        try { row.MinCoverViolated = MinCoverViolated(pipe, row.MinCoverRule); }
                        catch { row.MinCoverViolated = Math.Min(row.CoverStart, row.CoverEnd) < row.MinCoverRule; }
                    }
                    ctx.Pipes.Add(row);
                }
            }
            catch (Exception ex)
            {
                ctx.Errors.Add(ex.Message);
            }
            tr.Commit();
            return ctx;
        }

        // ═══════════════════════════════════════════════════════════════════
        //  helpers
        // ═══════════════════════════════════════════════════════════════════

        private static StructureReport DescribeStructure(Transaction tr, Structure s, Network? net)
        {
            return new StructureReport
            {
                Name = s.Name,
                Handle = s.ObjectId.Handle.ToString(),
                Network = net?.Name ?? "",
                PartsList = net == null ? "" : SafeString(() => net.PartsListName),
                ReferenceSurface = SafeString(() => s.RefSurfaceName) is { Length: > 0 } rs ? rs
                                   : (net == null ? "" : SafeString(() => net.ReferenceSurfaceName)),
                X = s.Location.X, Y = s.Location.Y,
                Rim = SafeDouble(() => s.RimElevation) ?? double.NaN,
                Sump = SafeDouble(() => s.SumpElevation) ?? double.NaN,
                SurfaceElevation = SafeDouble(() => s.SurfaceElevationAtInsertionPoint) ?? double.NaN,
                ConnectedPipes = s.ConnectedPipesCount,
            };
        }

        private static List<ObjectId> ResolvePipeScope(Transaction tr, Structure structure, SolverInput input, SolveReport report)
        {
            if (input.PipeIds is { Count: > 0 })
                return input.PipeIds.Distinct().ToList();   // explicit list: strict diameter check later

            // Auto-discover: every connected pipe, filtered to the dominant diameter.
            var byDia = new Dictionary<double, List<(ObjectId id, string name)>>();
            int count = structure.ConnectedPipesCount;
            for (int i = 0; i < count; i++)
            {
                ObjectId pid = structure.get_ConnectedPipe(i);
                if (pid.IsNull || pid.IsErased) continue;
                if (tr.GetObject(pid, OpenMode.ForRead, false) is not Pipe p) continue;
                double key = Math.Round(p.InnerDiameterOrWidth, 6);
                if (!byDia.TryGetValue(key, out var list)) byDia[key] = list = new();
                list.Add((pid, p.Name));
            }
            if (byDia.Count == 0) return new List<ObjectId>();

            // Governing pipe's diameter wins; otherwise the most common (ties → largest).
            double chosen;
            if (!input.GoverningPipeId.IsNull && byDia.FirstOrDefault(kv => kv.Value.Any(x => x.id == input.GoverningPipeId)) is { Value: not null } g)
                chosen = g.Key;
            else
                chosen = byDia.OrderByDescending(kv => kv.Value.Count).ThenByDescending(kv => kv.Key).First().Key;

            foreach (var kv in byDia.Where(kv => kv.Key != chosen))
                report.Warnings.Add($"Excluded (diameter {kv.Key:0.###} ≠ {chosen:0.###}): {string.Join(", ", kv.Value.Select(x => x.name))}");

            return byDia[chosen].Select(x => x.id).ToList();
        }

        internal static CivilSurface ResolveSurface(Transaction tr, Pipe pipe, Network? net)
        {
            ObjectId sid = ObjectId.Null;
            try { sid = pipe.RefSurfaceId; } catch { }
            if (sid.IsNull && net != null) { try { sid = net.ReferenceSurfaceId; } catch { } }
            if (sid.IsNull || sid.IsErased)
                throw new InvalidOperationException(
                    $"No reference surface is assigned to pipe '{pipe.Name}' or its network" +
                    (net == null ? "" : $" '{net.Name}'") + ". Assign one (Network Properties → Reference Surface) and retry.");

            if (tr.GetObject(sid, OpenMode.ForRead, false) is not CivilSurface surface)
                throw new InvalidOperationException($"Reference surface of pipe '{pipe.Name}' is not a Civil 3D surface.");
            return surface;
        }

        internal static double SampleSurface(CivilSurface surface, Point3d pt, string pipeName, string where)
        {
            try
            {
                double z = surface.FindElevationAtXY(pt.X, pt.Y);
                if (double.IsNaN(z) || double.IsInfinity(z)) throw new InvalidOperationException("NaN elevation");
                return z;
            }
            catch (Exception ex)
            {
                throw new InvalidOperationException(
                    $"Reference surface has no coverage at the {where} of pipe '{pipeName}' ({pt.X:0.##}, {pt.Y:0.##}): {ex.Message}");
            }
        }

        /// <summary>Interior samples (excluding the two endpoints) from the structure end toward the far end.</summary>
        private static void SampleAlongPipe(CivilSurface surface, Point3d from, Point3d to, PipeWork w)
        {
            int n = (int)Math.Clamp(Math.Round(w.Length2D / 10.0), 4, 50);
            var ts = new List<double>();
            var zs = new List<double>();
            for (int i = 1; i < n; i++)
            {
                double t = (double)i / n;
                double x = from.X + (to.X - from.X) * t;
                double y = from.Y + (to.Y - from.Y) * t;
                try
                {
                    double z = surface.FindElevationAtXY(x, y);
                    if (double.IsNaN(z)) continue;
                    ts.Add(t); zs.Add(z);
                }
                catch { /* gaps inside the surface: endpoints are still enforced */ }
            }
            w.SampleT = ts.ToArray();
            w.SampleSurf = zs.ToArray();
        }

        internal static (string ruleSet, Dictionary<string, double> parms) ReadRuleParams(Transaction tr, Part part)
        {
            var dict = new Dictionary<string, double>(StringComparer.OrdinalIgnoreCase);
            string name = "";
            try
            {
                ObjectId rsId = part.RuleSetStyleId;
                if (!rsId.IsNull && tr.GetObject(rsId, OpenMode.ForRead, false) is NetworkRuleSetStyle rs)
                {
                    name = rs.Name;
                    foreach (ObjectId rid in rs.GetRuleIds()) CollectRule(tr, rid, dict);
                }
                // Part-level overrides win over the rule set.
                foreach (ObjectId rid in SafeIds(() => part.GetOverriddenRuleIds())) CollectRule(tr, rid, dict);
            }
            catch { }
            return (name, dict);
        }

        private static void CollectRule(Transaction tr, ObjectId ruleId, Dictionary<string, double> dict)
        {
            try
            {
                if (tr.GetObject(ruleId, OpenMode.ForRead, false) is not NetworkRule rule) return;
                foreach (ParamDouble p in rule.ParamsDouble)
                {
                    try { dict[p.Key] = p.Value; } catch { }
                }
            }
            catch { }
        }

        /// <summary>Finds a rule parameter whose key or display name contains every fragment (case-insensitive).</summary>
        internal static bool FindParam(Dictionary<string, double> parms, out string key, out double value, params string[] fragments)
        {
            foreach (var kv in parms)
            {
                string k = kv.Key.Replace(" ", "");
                if (fragments.All(f => k.IndexOf(f, StringComparison.OrdinalIgnoreCase) >= 0))
                {
                    key = kv.Key; value = kv.Value; return true;
                }
            }
            key = ""; value = 0; return false;
        }

        private static void DetectZConvention(Pipe pipe, PipeWork w, SolveReport report)
        {
            double? cover = SafeDouble(() => w.StructureIsStart ? pipe.CoverOfStartPoint : pipe.CoverOfEndpoint);
            double z = w.StructureIsStart ? w.StartPt.Z : w.EndPt.Z;
            if (cover is double c && !double.IsNaN(c))
            {
                double invFromCover = w.SurfAtStructure - c - w.OuterTopOffset;
                double dCenter = Math.Abs((z - w.InnerHeight / 2.0) - invFromCover);
                double dInvert = Math.Abs(z - invFromCover);
                if (dCenter <= ZConventionTolerance) w.CenterlineZ = true;
                else if (dInvert <= ZConventionTolerance) w.CenterlineZ = false;
                else
                {
                    w.CenterlineZ = dCenter <= dInvert;
                    report.Warnings.Add($"Pipe '{pipe.Name}': could not confirm whether StartPoint.Z is centerline or invert " +
                                        $"(Δcenter={dCenter:0.000}, Δinvert={dInvert:0.000}); assuming {(w.CenterlineZ ? "centerline" : "invert")}.");
                }
            }
            w.Report.ZConvention = w.CenterlineZ ? "centerline" : "invert";
        }

        private static FlowRelation DetectFlow(Structure s, Pipe pipe, ObjectId pid, bool atStart)
        {
            try
            {
                int n = s.ConnectedPipesCount;
                for (int i = 0; i < n; i++)
                {
                    if (s.get_ConnectedPipe(i) != pid) continue;
                    if (s.IsConnectedPipeFlowingIn(i)) return FlowRelation.Incoming;
                    if (s.IsConnectedPipeFlowingOut(i)) return FlowRelation.Outgoing;
                }
            }
            catch { }

            return pipe.FlowDirection switch
            {
                FlowDirectionType.StartToEnd => atStart ? FlowRelation.Outgoing : FlowRelation.Incoming,
                FlowDirectionType.EndToStart => atStart ? FlowRelation.Incoming : FlowRelation.Outgoing,
                _ => FlowRelation.Unknown,
            };
        }

        private static void FillOldState(Transaction tr, Pipe pipe, PipeWork w, bool atStart)
        {
            Point3d sPt = atStart ? w.StartPt : w.EndPt;
            Point3d fPt = atStart ? w.EndPt : w.StartPt;
            double invS = w.InvertFromZ(sPt.Z);
            double invF = w.InvertFromZ(fPt.Z);

            w.Report.AtStructure = new EndReport
            {
                Structure = StructureName(tr, atStart ? pipe.StartStructureId : pipe.EndStructureId),
                StructureHandle = (atStart ? pipe.StartStructureId : pipe.EndStructureId).Handle.ToString(),
                X = sPt.X, Y = sPt.Y, Surface = w.SurfAtStructure,
                OldInvert = invS, OldCrown = invS + w.InnerHeight,
                OldCover = ElevationMath.CoverAtInvert(w.SurfAtStructure, invS, w.InnerHeight, w.WallThickness),
                MaxInvertForCover = ElevationMath.MaxInvertForCover(w.SurfAtStructure, w.CoverMin, w.InnerHeight, w.WallThickness),
            };
            ObjectId farId = atStart ? pipe.EndStructureId : pipe.StartStructureId;
            w.Report.FarEnd = new EndReport
            {
                Structure = farId.IsNull ? "(free end)" : StructureName(tr, farId),
                StructureHandle = farId.IsNull ? "" : farId.Handle.ToString(),
                X = fPt.X, Y = fPt.Y, Surface = w.SurfAtFar,
                OldInvert = invF, OldCrown = invF + w.InnerHeight,
                OldCover = ElevationMath.CoverAtInvert(w.SurfAtFar, invF, w.InnerHeight, w.WallThickness),
                MaxInvertForCover = ElevationMath.MaxInvertForCover(w.SurfAtFar, w.CoverMin, w.InnerHeight, w.WallThickness),
            };
            w.Report.OldSlope = w.Relation == FlowRelation.Incoming
                ? ElevationMath.Slope(invF, invS, w.Length2D)
                : ElevationMath.Slope(invS, invF, w.Length2D);
        }

        private static void FillNewState(PipeWork w, double invS, double invF)
        {
            w.Report.AtStructure.NewInvert = invS;
            w.Report.AtStructure.NewCrown = invS + w.InnerHeight;
            w.Report.AtStructure.NewCover = ElevationMath.CoverAtInvert(w.SurfAtStructure, invS, w.InnerHeight, w.WallThickness);
            w.Report.FarEnd.NewInvert = invF;
            w.Report.FarEnd.NewCrown = invF + w.InnerHeight;
            w.Report.FarEnd.NewCover = ElevationMath.CoverAtInvert(w.SurfAtFar, invF, w.InnerHeight, w.WallThickness);

            // minimum cover along the length after the change
            double minCover = Math.Min(w.Report.AtStructure.NewCover.Value, w.Report.FarEnd.NewCover.Value);
            double minAt = w.Report.AtStructure.NewCover <= w.Report.FarEnd.NewCover ? 0 : w.Length2D;
            for (int i = 0; i < w.SampleT.Length; i++)
            {
                double inv = invS + (invF - invS) * w.SampleT[i];
                double c = ElevationMath.CoverAtInvert(w.SampleSurf[i], inv, w.InnerHeight, w.WallThickness);
                if (c < minCover) { minCover = c; minAt = w.SampleT[i] * w.Length2D; }
            }
            w.Report.NewMinCoverAlongPipe = minCover;
            w.Report.NewMinCoverAtDistance = minAt;
        }

        private static void SanityCheck(SolveReport report, PipeWork w, double maxDepth)
        {
            foreach (var (label, end) in new[] { ("structure end", w.Report.AtStructure), ("far end", w.Report.FarEnd) })
            {
                double inv = end.NewInvert ?? double.NaN;
                if (double.IsNaN(inv) || double.IsInfinity(inv))
                    AddConflict(report, w, "sanity", "computed invert", "finite number", 0, $"Pipe '{w.Report.Name}': invert at {label} is not a number.", null);
                else if (inv > end.Surface)
                    AddConflict(report, w, "sanity", $"invert {inv:0.000}", $"surface {end.Surface:0.000}", inv - end.Surface,
                        $"Pipe '{w.Report.Name}': invert at {label} is above the ground surface — check the elevation datum.", null);
                else if (end.Surface - inv > maxDepth)
                    AddConflict(report, w, "sanity", $"invert {inv:0.000}", $"surface {end.Surface:0.000} − {maxDepth:0.#} ft limit", end.Surface - inv - maxDepth,
                        $"Pipe '{w.Report.Name}': invert at {label} is {end.Surface - inv:0.0} ft below the surface, beyond the {maxDepth:0.#} ft sanity limit — likely a datum mismatch.", null);
            }
        }

        private static void ApplySlopeUnits(Transaction tr, List<PipeWork> works, SolverInput input, SolveReport report)
        {
            string units = (input.SlopeUnits ?? "auto").ToLowerInvariant();
            string reason;
            if (units == "ratio" || units == "percent")
            {
                reason = "specified by input";
            }
            else
            {
                units = "ratio";
                reason = "default (no pipe with measurable slope to calibrate against)";
                foreach (var w in works)
                {
                    if (tr.GetObject(w.Id, OpenMode.ForRead, false) is not Pipe pipe) continue;
                    double geom = Math.Abs(w.Report.OldSlope);
                    double? api = SafeDouble(() => Math.Abs(pipe.Slope));
                    if (geom < 1e-5 || api is null) continue;
                    bool percent = Math.Abs(api.Value - geom * 100) < Math.Abs(api.Value - geom);
                    units = percent ? "percent" : "ratio";
                    reason = $"calibrated on '{w.Report.Name}': Pipe.Slope={api:0.####} vs geometric {geom:0.####}";
                    break;
                }
                if (reason.StartsWith("default") && works.Any(w => (w.MinSlope ?? 0) >= 1.0 || (w.MaxSlope ?? 0) >= 1.0))
                {
                    units = "percent";
                    reason = "rule values ≥ 1.0 look like percentages";
                }
            }

            report.SlopeUnits = units;
            report.SlopeUnitsReason = reason;
            if (units == "percent")
                foreach (var w in works)
                {
                    if (w.MinSlope.HasValue) w.MinSlope /= 100.0;
                    if (w.MaxSlope.HasValue) w.MaxSlope /= 100.0;
                }
            foreach (var w in works) { w.Report.MinSlope = w.MinSlope; w.Report.MaxSlope = w.MaxSlope; }
        }

        private static void AddConflict(SolveReport report, PipeWork w, string kind, string a, string b, double delta, string message, double? suggested)
        {
            report.Conflicts.Add(new ConflictReport
            {
                Pipe = w.Report.Name, Handle = w.Report.Handle, Kind = kind,
                ConstraintA = a, ConstraintB = b, Delta = delta, Message = message,
                SuggestedStructureInvert = suggested,
            });
            w.Report.Status = "conflict";
        }

        /// <summary>Civil 3D's own along-the-length cover check (the same one Pipe Properties uses).</summary>
        internal static bool MinCoverViolated(Pipe pipe, double minCover)
        {
            Point3d[] pts = Array.Empty<Point3d>();
            double[] a = Array.Empty<double>(), b = Array.Empty<double>();
            return pipe.IsMinCoverViolated(minCover, ref pts, ref a, ref b);
        }

        internal static string StructureName(Transaction tr, ObjectId id)
        {
            if (id.IsNull || id.IsErased) return "";
            try { return (tr.GetObject(id, OpenMode.ForRead, false) as Structure)?.Name ?? ""; } catch { return ""; }
        }


        /// <summary>
        /// Decides whether Pipe.StartPoint/EndPoint.Z is the centerline (true) or the invert (false)
        /// by comparing it with the invert implied by Civil 3D's own cover value at that end.
        /// </summary>
        internal static bool DetectCenterlineZ(Pipe pipe, bool atStart, double surfaceZ, out string? warning)
        {
            warning = null;
            double h = pipe.InnerHeight, wall = pipe.WallThickness;
            double z = atStart ? pipe.StartPoint.Z : pipe.EndPoint.Z;
            double? cover = SafeDouble(() => atStart ? pipe.CoverOfStartPoint : pipe.CoverOfEndpoint);
            if (cover is not double c || double.IsNaN(c)) return true;

            double invFromCover = surfaceZ - c - h - wall;
            double dCenter = Math.Abs((z - h / 2.0) - invFromCover);
            double dInvert = Math.Abs(z - invFromCover);
            if (dCenter <= ZConventionTolerance) return true;
            if (dInvert <= ZConventionTolerance) return false;
            bool centerline = dCenter <= dInvert;
            warning = $"Pipe '{pipe.Name}': could not confirm whether StartPoint.Z is centerline or invert " +
                      $"(Δcenter={dCenter:0.000}, Δinvert={dInvert:0.000}); assuming {(centerline ? "centerline" : "invert")}.";
            return centerline;
        }

        /// <summary>
        /// "ratio" or "percent": how rule-set slope values should be read. Calibrated against
        /// Pipe.Slope vs. the geometric slope of the first pipe with a measurable fall.
        /// </summary>
        internal static (string units, string reason) CalibrateSlopeUnits(
            Transaction tr, string requested, IEnumerable<(ObjectId id, string name, double geomSlope, double? minS, double? maxS)> pipes)
        {
            string u = (requested ?? "auto").ToLowerInvariant();
            if (u == "ratio" || u == "percent") return (u, "specified by input");

            bool anyBig = false;
            foreach (var (id, name, geom, minS, maxS) in pipes)
            {
                if ((minS ?? 0) >= 1.0 || (maxS ?? 0) >= 1.0) anyBig = true;
                if (Math.Abs(geom) < 1e-5) continue;
                if (tr.GetObject(id, OpenMode.ForRead, false) is not Pipe pipe) continue;
                double? api = SafeDouble(() => Math.Abs(pipe.Slope));
                if (api is null) continue;
                double g = Math.Abs(geom);
                bool percent = Math.Abs(api.Value - g * 100) < Math.Abs(api.Value - g);
                return (percent ? "percent" : "ratio", $"calibrated on '{name}': Pipe.Slope={api:0.####} vs geometric {g:0.####}");
            }
            return anyBig ? ("percent", "rule values ≥ 1.0 look like percentages")
                          : ("ratio", "default (no pipe with measurable slope to calibrate against)");
        }

        internal static string Fmt(double? v) => v.HasValue ? v.Value.ToString("0.0000") : "none";
        internal static double? SafeDouble(Func<double> f) { try { return f(); } catch { return null; } }
        internal static string SafeString(Func<string> f) { try { return f() ?? ""; } catch { return ""; } }
        internal static string[] SafeArray(Func<string[]> f) { try { return f() ?? Array.Empty<string>(); } catch { return Array.Empty<string>(); } }
        internal static IEnumerable<ObjectId> SafeIds(Func<ObjectIdCollection> f)
        {
            ObjectIdCollection? c = null;
            try { c = f(); } catch { }
            if (c == null) yield break;
            foreach (ObjectId id in c) yield return id;
        }
    }
}
