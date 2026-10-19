using System;
using System.Collections.Generic;
using System.Linq;

namespace FeatureLineSync
{
    public enum SyncMode { Grades, Elevations }

    public sealed class PlannerOptions
    {
        public SyncMode Mode { get; set; } = SyncMode.Grades;
        /// <summary>Off by default: only points on the surface are fixed, every other elevation comes from the copy's slopes.
        /// On: a point whose current elevation is not zero (and that the surface does not cover) is kept as a fixed point too.</summary>
        public bool KeepExistingElevations { get; set; } = false;
        /// <summary>|z| below this counts as "zero" (reset by the move).</summary>
        public double ZeroTol { get; set; } = 1e-6;
        /// <summary>Two points on different lines closer than this are the same location (reported when they disagree).</summary>
        public double CoincidenceTol { get; set; } = 0.05;
        /// <summary>Elevation points at the same segment with fractions within this are the same point.</summary>
        public double FractionTol { get; set; } = 0.01;
        /// <summary>Shared locations whose values differ by more than this are reported.</summary>
        public double UnifyTol { get; set; } = 0.005;
        /// <summary>Warn when more than this share of all points lies on the surface.</summary>
        public double AnchorWarnRatio { get; set; } = 0.6;
    }

    /// <summary>One point of a target line, aligned 1:1 with the copy's point list.</summary>
    public sealed class AlignedPoint
    {
        public int Index { get; set; }
        public int Segment { get; set; }
        public double Fraction { get; set; }
        public bool IsPI { get; set; }
        /// <summary>XY on the target line (actual for existing points, computed for rebuilt elevation points).</summary>
        public double X { get; set; }
        public double Y { get; set; }
        public double CopyZ { get; set; }
        public double CurrentZ { get; set; }
        public double? AnchorZ { get; set; }
        /// <summary>surface | existing (non-zero elevation already in the drawing)</summary>
        public string AnchorSource { get; set; } = "";
        public double? NewZ { get; set; }
    }

    public sealed class RunInfo
    {
        public int From { get; set; }
        public int To { get; set; }
        public double Misclosure { get; set; }
        public double Length { get; set; }
    }

    public sealed class LinePlan
    {
        public FlLine Target { get; set; } = new();
        public FlLine? Copy { get; set; }
        /// <summary>handle | name | none</summary>
        public string MatchedBy { get; set; } = "none";
        /// <summary>ok | unanchored | unmatched | locked_layer | degenerate</summary>
        public string Status { get; set; } = "ok";
        public List<string> Warnings { get; } = new();
        public List<AlignedPoint> Points { get; } = new();
        /// <summary>2D length between consecutive aligned points on the target (closed: last entry wraps).</summary>
        public List<double> TargetLens { get; } = new();
        /// <summary>Same on the copy.</summary>
        public List<double> CopyLens { get; } = new();
        public bool RebuildElevationPoints { get; set; }
        public bool Closed { get; set; }
        public List<RunInfo> Runs { get; } = new();
        /// <summary>Station of each aligned point along the target line (cumulative 2D distance).</summary>
        public List<double> StationList { get; } = new();
        public double? MinGradeBefore { get; set; }
        public double? MaxGradeBefore { get; set; }
        public double? MinGradeAfter { get; set; }
        public double? MaxGradeAfter { get; set; }
        public int AnchorCount => Points.Count(p => p.AnchorZ.HasValue);
        public int SurfaceAnchorCount => Points.Count(p => p.AnchorSource == "surface");
        public int ExistingAnchorCount => Points.Count(p => p.AnchorSource == "existing");
        /// <summary>0 = not solved, 1 = solved, 99 = no fixed point (untouched).</summary>
        public int Pass { get; set; }
        public bool Write { get; set; }
    }

    public sealed class SyncPlan
    {
        public List<LinePlan> Lines { get; } = new();
        public List<string> UnmatchedInCopy { get; } = new();
        public List<string> Warnings { get; } = new();
        public List<string> Conflicts { get; } = new();
        public int PointCount => Lines.Sum(l => l.Points.Count);
        public int AnchorCount => Lines.Sum(l => l.AnchorCount);
        public int SurfaceAnchorCount => Lines.Sum(l => l.SurfaceAnchorCount);
        public int LinesToWrite => Lines.Count(l => l.Write);
        public int PointsToWrite => Lines.Where(l => l.Write).Sum(l => l.Points.Count);
    }

    /// <summary>
    /// Pure planning: which elevation every point of every target feature line should get.
    /// Grades mode, per line: every point the surface covers takes the surface elevation; points that
    /// already have a non-zero elevation keep it; the remaining (zero) points are filled from those fixed
    /// points with the copy's slope per segment, over the segment's current length. No cross-line logic.
    /// Elevations mode: copy elevations by aligned index.
    /// </summary>
    public static class SyncPlanner
    {
        public static SyncPlan Plan(FlSnapshot target, FlSnapshot copy, Func<double, double, double?>? probe, PlannerOptions o)
        {
            var plan = new SyncPlan();
            Dictionary<string, FlLine> byHandle = copy.Lines
                .Where(l => !string.IsNullOrEmpty(l.Handle))
                .GroupBy(l => l.Handle.ToUpperInvariant()).ToDictionary(g => g.Key, g => g.First());
            Dictionary<string, FlLine> byName = copy.Lines
                .Where(l => !string.IsNullOrWhiteSpace(l.Name))
                .GroupBy(l => l.Name.Trim().ToUpperInvariant())
                .Where(g => g.Count() == 1).ToDictionary(g => g.Key, g => g.First());
            var usedCopy = new HashSet<FlLine>();

            foreach (FlLine t in target.Lines)
            {
                var lp = new LinePlan { Target = t, Closed = t.Closed };
                plan.Lines.Add(lp);

                FlLine? c = null;
                if (!string.IsNullOrEmpty(t.Handle) && byHandle.TryGetValue(t.Handle.ToUpperInvariant(), out FlLine? ch) && !usedCopy.Contains(ch))
                { c = ch; lp.MatchedBy = "handle"; }
                else if (!string.IsNullOrWhiteSpace(t.Name) && byName.TryGetValue(t.Name.Trim().ToUpperInvariant(), out FlLine? cn) && !usedCopy.Contains(cn))
                { c = cn; lp.MatchedBy = "name"; }

                if (c == null) { lp.Status = "unmatched"; continue; }
                usedCopy.Add(c);
                lp.Copy = c;

                if (t.LayerLocked) { lp.Status = "locked_layer"; lp.Warnings.Add($"layer '{t.Layer}' is locked"); continue; }
                if (t.Pis.Count < 2 || c.Pis.Count < 2) { lp.Status = "degenerate"; lp.Warnings.Add("fewer than two PIs"); continue; }

                Align(lp, o.FractionTol);
                ComputeLengths(lp);
                lp.MinGradeBefore = MinGrade(lp.Points.Select(p => p.CopyZ).ToList(), lp.CopyLens, lp.Closed, out double maxB);
                lp.MaxGradeBefore = lp.MinGradeBefore.HasValue ? maxB : null;
            }

            foreach (FlLine c in copy.Lines)
                if (!usedCopy.Contains(c)) plan.UnmatchedInCopy.Add($"{c.Handle} '{c.Name}'");

            List<LinePlan> candidates = plan.Lines.Where(l => l.Status == "ok").ToList();

            if (o.Mode == SyncMode.Elevations)
            {
                foreach (LinePlan lp in candidates)
                {
                    foreach (AlignedPoint p in lp.Points) p.NewZ = p.CopyZ;
                    lp.Pass = 1; lp.Write = true;
                    FinishGrades(lp);
                }
                Unify(plan, o);
                return plan;
            }

            // ── per line: fixed points from the surface (and existing non-zero elevations), slopes fill the rest ──
            foreach (LinePlan lp in candidates)
            {
                foreach (AlignedPoint p in lp.Points)
                {
                    double? z = probe?.Invoke(p.X, p.Y);
                    if (z.HasValue) { p.AnchorZ = z; p.AnchorSource = "surface"; continue; }
                    if (o.KeepExistingElevations && Math.Abs(p.CurrentZ) > o.ZeroTol) { p.AnchorZ = p.CurrentZ; p.AnchorSource = "existing"; }
                }
                if (lp.AnchorCount > 0)
                {
                    Solve(lp);
                    lp.Pass = 1; lp.Write = true;
                    if (lp.AnchorCount == lp.Points.Count)
                        lp.Warnings.Add("every point is fixed (on the surface or already non-zero); the copy's slopes are not used on this line");
                }
                else
                {
                    lp.Status = "unanchored";
                    lp.Pass = 99;
                    foreach (AlignedPoint p in lp.Points) p.NewZ = p.CopyZ;
                    FinishGrades(lp);
                    lp.Write = false;
                    lp.Warnings.Add("no point on the surface (within the tolerance); left untouched");
                }
            }

            int total = candidates.Sum(l => l.Points.Count);
            int surfaceAnchors = candidates.Sum(l => l.SurfaceAnchorCount);
            if (total > 0 && surfaceAnchors > o.AnchorWarnRatio * total)
                plan.Warnings.Add($"{surfaceAnchors} of {total} points ({100.0 * surfaceAnchors / total:0}%) lie on the surface: " +
                                  "this is probably not the corridor surface, the copy's slopes would be mostly discarded.");
            if (surfaceAnchors == 0 && probe != null)
                plan.Warnings.Add("no point of any feature line lies on the chosen surface (within the tolerance).");

            Unify(plan, o);
            return plan;
        }

        // ── alignment ───────────────────────────────────────────────────────────

        /// <summary>Ordered point list of a line: each PI followed by the elevation points on its segment.</summary>
        internal static List<(int seg, double frac, bool isPI, double x, double y, double z)> OrderedPoints(FlLine line)
        {
            var list = new List<(int, double, bool, double, double, double)>();
            int n = line.Pis.Count;
            int segs = line.SegmentCount;
            for (int i = 0; i < n; i++)
            {
                double[] pi = line.Pis[i];
                list.Add((i, 0.0, true, pi[0], pi[1], pi[2]));
                if (i < segs)
                    foreach (FlElevPoint ep in line.ElevationPoints.Where(e => e.Segment == i).OrderBy(e => e.Fraction))
                        list.Add((i, ep.Fraction, false, ep.X, ep.Y, ep.Z));
            }
            return list;
        }

        internal static void Align(LinePlan lp, double fractionTol)
        {
            FlLine t = lp.Target, c = lp.Copy!;
            var cp = OrderedPoints(c);
            var tp = OrderedPoints(t);

            if (t.Pis.Count != c.Pis.Count || t.Closed != c.Closed)
            {
                AlignByNormalizedStation(lp, tp, cp);
                lp.Warnings.Add($"PI count differs (copy {c.Pis.Count}, drawing {t.Pis.Count}); copy profile mapped by normalized station");
                return;
            }

            bool reuse = tp.Count == cp.Count;
            if (reuse)
                for (int i = 0; i < tp.Count; i++)
                    if (tp[i].isPI != cp[i].isPI || tp[i].seg != cp[i].seg || Math.Abs(tp[i].frac - cp[i].frac) > fractionTol) { reuse = false; break; }

            if (reuse)
            {
                for (int i = 0; i < cp.Count; i++)
                    lp.Points.Add(new AlignedPoint
                    {
                        Index = i, Segment = cp[i].seg, Fraction = cp[i].frac, IsPI = cp[i].isPI,
                        X = tp[i].x, Y = tp[i].y, CurrentZ = tp[i].z, CopyZ = cp[i].z,
                    });
                return;
            }

            // rebuild: the copy's elevation points, placed on the target's geometry
            lp.RebuildElevationPoints = true;
            var currentByKey = tp.ToDictionary(p => (p.seg, Math.Round(p.frac, 3)), p => p.z);
            for (int i = 0; i < cp.Count; i++)
            {
                var p = cp[i];
                double x, y;
                if (p.isPI) { x = t.Pis[p.seg][0]; y = t.Pis[p.seg][1]; }
                else (x, y) = FlGeometry.PointOn(t, p.seg, p.frac);
                currentByKey.TryGetValue((p.seg, Math.Round(p.frac, 3)), out double cur);
                lp.Points.Add(new AlignedPoint { Index = i, Segment = p.seg, Fraction = p.frac, IsPI = p.isPI, X = x, Y = y, CurrentZ = p.isPI ? t.Pis[p.seg][2] : cur, CopyZ = p.z });
            }
            lp.Warnings.Add($"elevation points rebuilt from the copy ({c.ElevationPoints.Count} in copy, {t.ElevationPoints.Count} in drawing)");
        }

        /// <summary>Stations (cumulative 2D distance) of ordered points along a line.</summary>
        internal static List<double> Stations(FlLine line, IEnumerable<(int seg, double frac)> pts)
        {
            var segLens = line.SegLengths2D.Count == line.SegmentCount
                ? line.SegLengths2D
                : FlGeometry.SegmentLengths(line.Pis, line.Bulges, line.Closed);
            var cum = new double[segLens.Count + 1];
            for (int i = 0; i < segLens.Count; i++) cum[i + 1] = cum[i] + segLens[i];
            var result = new List<double>();
            foreach (var (seg, frac) in pts)
            {
                // the last PI of an open line sits at the end of the final segment
                if (seg >= segLens.Count) { result.Add(cum[segLens.Count]); continue; }
                result.Add(cum[seg] + frac * segLens[seg]);
            }
            return result;
        }

        internal static double TotalLength(FlLine line)
        {
            var segLens = line.SegLengths2D.Count == line.SegmentCount
                ? line.SegLengths2D
                : FlGeometry.SegmentLengths(line.Pis, line.Bulges, line.Closed);
            return segLens.Sum();
        }

        private static void AlignByNormalizedStation(LinePlan lp,
            List<(int seg, double frac, bool isPI, double x, double y, double z)> tp,
            List<(int seg, double frac, bool isPI, double x, double y, double z)> cp)
        {
            FlLine t = lp.Target, c = lp.Copy!;
            double cTotal = TotalLength(c), tTotal = TotalLength(t);
            List<double> cSt = Stations(c, cp.Select(p => (p.seg, p.frac)));
            List<double> tSt = Stations(t, tp.Select(p => (p.seg, p.frac)));

            // copy profile as (normalized station, z), with the wrap point for closed lines
            var prof = new List<(double s, double z)>();
            for (int i = 0; i < cp.Count; i++) prof.Add((cTotal > 0 ? cSt[i] / cTotal : 0, cp[i].z));
            if (c.Closed && cp.Count > 0) prof.Add((1.0, cp[0].z));

            for (int i = 0; i < tp.Count; i++)
            {
                double s = tTotal > 0 ? tSt[i] / tTotal : 0;
                lp.Points.Add(new AlignedPoint
                {
                    Index = i, Segment = tp[i].seg, Fraction = tp[i].frac, IsPI = tp[i].isPI,
                    X = tp[i].x, Y = tp[i].y, CurrentZ = tp[i].z, CopyZ = Interp(prof, s),
                });
            }
            // copy lengths follow the normalized spacing of the target points, scaled to the copy's length
            lp.CopyLens.Clear();
            for (int i = 0; i + 1 < tp.Count; i++) lp.CopyLens.Add(tTotal > 0 ? (tSt[i + 1] - tSt[i]) / tTotal * cTotal : 0);
            if (t.Closed && tp.Count > 0) lp.CopyLens.Add(tTotal > 0 ? (tTotal - tSt[^1]) / tTotal * cTotal : 0);
        }

        private static double Interp(List<(double s, double z)> prof, double s)
        {
            if (prof.Count == 0) return 0;
            if (s <= prof[0].s) return prof[0].z;
            for (int i = 0; i + 1 < prof.Count; i++)
            {
                if (s <= prof[i + 1].s)
                {
                    double ds = prof[i + 1].s - prof[i].s;
                    double f = ds > FlGeometry.Eps ? (s - prof[i].s) / ds : 0;
                    return prof[i].z + f * (prof[i + 1].z - prof[i].z);
                }
            }
            return prof[^1].z;
        }

        private static void ComputeLengths(LinePlan lp)
        {
            var keys = lp.Points.Select(p => (p.Segment, p.Fraction)).ToList();
            List<double> tSt = Stations(lp.Target, keys);
            lp.StationList.Clear(); lp.StationList.AddRange(tSt);
            lp.TargetLens.Clear();
            for (int i = 0; i + 1 < tSt.Count; i++) lp.TargetLens.Add(Math.Max(0, tSt[i + 1] - tSt[i]));
            if (lp.Closed && tSt.Count > 0) lp.TargetLens.Add(Math.Max(0, TotalLength(lp.Target) - tSt[^1]));

            if (lp.CopyLens.Count > 0) return;               // already set by the normalized-station fallback
            List<double> cSt = Stations(lp.Copy!, keys);
            for (int i = 0; i + 1 < cSt.Count; i++) lp.CopyLens.Add(Math.Max(0, cSt[i + 1] - cSt[i]));
            if (lp.Closed && cSt.Count > 0) lp.CopyLens.Add(Math.Max(0, TotalLength(lp.Copy!) - cSt[^1]));
        }

        // ── propagation ─────────────────────────────────────────────────────────

        private static void Solve(LinePlan lp)
        {
            double[] grades = Grades(lp.Points.Select(p => p.CopyZ).ToArray(), lp.CopyLens);
            var anchors = new Dictionary<int, double>();
            foreach (AlignedPoint p in lp.Points) if (p.AnchorZ.HasValue) anchors[p.Index] = p.AnchorZ.Value;
            var (z, runs) = Propagate(grades, lp.TargetLens.ToArray(), anchors, lp.Closed);
            for (int i = 0; i < lp.Points.Count; i++) lp.Points[i].NewZ = z[i];
            lp.Runs.Clear(); lp.Runs.AddRange(runs);
            FinishGrades(lp);
        }

        private static void FinishGrades(LinePlan lp)
        {
            lp.MinGradeAfter = MinGrade(lp.Points.Select(p => p.NewZ ?? p.CopyZ).ToList(), lp.TargetLens, lp.Closed, out double max);
            lp.MaxGradeAfter = lp.MinGradeAfter.HasValue ? max : null;
        }

        public static double[] Grades(double[] z, IReadOnlyList<double> lens)
        {
            var g = new double[lens.Count];
            for (int i = 0; i < lens.Count; i++)
            {
                double dz = z[(i + 1) % z.Length] - z[i];
                g[i] = lens[i] > FlGeometry.Eps ? dz / lens[i] : 0.0;
            }
            return g;
        }

        private static double? MinGrade(List<double> z, IReadOnlyList<double> lens, bool closed, out double max)
        {
            max = double.MinValue; double min = double.MaxValue; bool any = false;
            for (int i = 0; i < lens.Count && i < z.Count; i++)
            {
                if (lens[i] <= FlGeometry.Eps) continue;
                double g = (z[(i + 1) % z.Count] - z[i]) / lens[i];
                min = Math.Min(min, g); max = Math.Max(max, g); any = true;
            }
            return any ? min : null;
        }

        /// <summary>
        /// Elevations from anchors and per-segment grades over the target lengths.
        /// Open: n points, n-1 grades/lengths. Closed: n points, n grades/lengths (last wraps to point 0).
        /// Between two anchors the misclosure at the second is spread linearly by cumulative distance.
        /// </summary>
        public static (double[] z, List<RunInfo> runs) Propagate(double[] grades, double[] lens, IDictionary<int, double> anchors, bool closed)
        {
            int n = closed ? lens.Length : lens.Length + 1;
            if (anchors.Count == 0) throw new ArgumentException("at least one anchor is required");

            if (!closed) return PropagateOpen(grades, lens, anchors, n);

            // ring: rotate so the first anchor is index 0, append it as the virtual last point
            int a0 = anchors.Keys.Min();
            var g2 = new double[n]; var l2 = new double[n];
            for (int k = 0; k < n; k++) { g2[k] = grades[(a0 + k) % n]; l2[k] = lens[(a0 + k) % n]; }
            var an2 = new Dictionary<int, double>();
            foreach (var kv in anchors) an2[(kv.Key - a0 + n) % n] = kv.Value;
            an2[n] = anchors[a0];
            var (zr, runsR) = PropagateOpen(g2, l2, an2, n + 1);
            var z = new double[n];
            for (int k = 0; k < n; k++) z[(a0 + k) % n] = zr[k];
            foreach (RunInfo r in runsR) { r.From = (r.From + a0) % n; r.To = (r.To + a0) % n; }
            return (z, runsR);
        }

        private static (double[] z, List<RunInfo> runs) PropagateOpen(double[] grades, double[] lens, IDictionary<int, double> anchors, int n)
        {
            var z = new double[n];
            var runs = new List<RunInfo>();
            var cum = new double[n];
            for (int i = 1; i < n; i++) cum[i] = cum[i - 1] + lens[i - 1];
            List<int> a = anchors.Keys.Where(k => k >= 0 && k < n).OrderBy(k => k).ToList();
            if (a.Count == 0) throw new ArgumentException("anchor index out of range");

            // before the first anchor: backward
            z[a[0]] = anchors[a[0]];
            for (int k = a[0] - 1; k >= 0; k--) z[k] = z[k + 1] - grades[k] * lens[k];

            // between anchors: forward + linear misclosure spread
            for (int j = 0; j + 1 < a.Count; j++)
            {
                int from = a[j], to = a[j + 1];
                z[from] = anchors[from];
                for (int k = from; k < to; k++) z[k + 1] = z[k] + grades[k] * lens[k];
                double delta = anchors[to] - z[to];
                double span = cum[to] - cum[from];
                for (int k = from + 1; k <= to; k++)
                    z[k] += span > FlGeometry.Eps ? delta * (cum[k] - cum[from]) / span : (k == to ? delta : 0);
                runs.Add(new RunInfo { From = from, To = to, Misclosure = delta, Length = span });
            }

            // after the last anchor: forward
            int last = a[^1];
            z[last] = anchors[last];
            for (int k = last; k + 1 < n; k++) z[k + 1] = z[k] + grades[k] * lens[k];
            return (z, runs);
        }

        // ── report shared locations whose lines disagree (values are NOT changed; Civil 3D's
        //    same-site rule decides at write time and the post-write verification shows it) ──

        private static void Unify(SyncPlan plan, PlannerOptions o)
        {
            var lines = plan.Lines.Where(l => l.Write).ToList();
            var index = new PointIndex(o.CoincidenceTol);
            var all = new List<(LinePlan lp, AlignedPoint p)>();
            foreach (LinePlan lp in lines) foreach (AlignedPoint p in lp.Points) all.Add((lp, p));
            for (int i = 0; i < all.Count; i++) index.Add(all[i].p.X, all[i].p.Y, i, "");

            var visited = new bool[all.Count];
            for (int i = 0; i < all.Count; i++)
            {
                if (visited[i]) continue;
                List<int> cluster = index.Within(all[i].p.X, all[i].p.Y).Select(v => (int)v).Where(k => !visited[k]).ToList();
                foreach (int k in cluster) visited[k] = true;
                if (cluster.Count < 2) continue;
                if (cluster.Select(k => all[k].lp).Distinct().Count() < 2) continue;      // same line only

                var values = cluster.Select(k => all[k].p.NewZ ?? all[k].p.CopyZ).ToList();
                double spread = values.Max() - values.Min();
                if (spread <= o.UnifyTol) continue;

                string where = string.Join(", ", cluster.Select(k => $"{all[k].lp.Target.Handle}#{all[k].p.Index}={(all[k].p.NewZ ?? all[k].p.CopyZ):0.000}"));
                plan.Conflicts.Add($"({all[i].p.X:0.00}, {all[i].p.Y:0.00}) differs by {spread:0.000}: {where}");
            }
        }

        /// <summary>Grid hash for coincident-point lookups.</summary>
        private sealed class PointIndex
        {
            private readonly double _tol;
            private readonly Dictionary<(long, long), List<(double x, double y, double v, string tag)>> _cells = new();
            public PointIndex(double tol) { _tol = Math.Max(tol, 1e-6); }
            private (long, long) Cell(double x, double y) => ((long)Math.Floor(x / _tol), (long)Math.Floor(y / _tol));
            public void Add(double x, double y, double v, string tag)
            {
                var c = Cell(x, y);
                if (!_cells.TryGetValue(c, out var list)) _cells[c] = list = new();
                list.Add((x, y, v, tag));
            }
            public IEnumerable<double> Within(double x, double y)
            {
                var (cx, cy) = Cell(x, y);
                for (long i = cx - 1; i <= cx + 1; i++)
                    for (long j = cy - 1; j <= cy + 1; j++)
                        if (_cells.TryGetValue((i, j), out var list))
                            foreach (var e in list)
                                if (FlGeometry.Dist2D(e.x, e.y, x, y) <= _tol) yield return e.v;
            }
            public bool TryNearest(double x, double y, out double v, out string tag)
            {
                v = 0; tag = ""; double best = double.MaxValue;
                var (cx, cy) = Cell(x, y);
                for (long i = cx - 1; i <= cx + 1; i++)
                    for (long j = cy - 1; j <= cy + 1; j++)
                        if (_cells.TryGetValue((i, j), out var list))
                            foreach (var e in list)
                            {
                                double d = FlGeometry.Dist2D(e.x, e.y, x, y);
                                if (d <= _tol && d < best) { best = d; v = e.v; tag = e.tag; }
                            }
                return best < double.MaxValue;
            }
        }
    }
}
