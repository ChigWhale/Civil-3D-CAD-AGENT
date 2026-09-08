using System;
using System.Collections.Generic;
using System.Linq;

namespace PipeInvertSetter
{
    /// <summary>
    /// Pure, AutoCAD-free solver for a RUN of pipes in flow order (pipe k goes from
    /// structure k to structure k+1).  Finds the SHALLOWEST profile — upstream first,
    /// then each next structure as shallow as the upstream choice allows — that satisfies:
    /// <list type="bullet">
    ///   <item>cover ceilings at both ends and at interior samples of every pipe,</item>
    ///   <item>min/max slope of every pipe,</item>
    ///   <item>the offset rule at every interior structure (match crowns / inverts / drop),</item>
    ///   <item>any fixed (tie-in) inverts.</item>
    /// </list>
    /// Each pipe's constraints form a convex polygon in (upstream, downstream) invert space.
    /// A backward pass projects "what upstream inverts still allow a feasible completion"
    /// onto an interval per pipe; a forward pass then marches downstream taking the
    /// maximum at every step.  An empty interval is a conflict — reported with the delta to
    /// the shallowest unconstrained profile, never clamped.
    /// </summary>
    public static class ChainSolver
    {
        public sealed class ChainPipe
        {
            public string Id = "";
            public double InnerHeight;
            public double Length;
            /// <summary>Highest upstream invert allowed by cover at the upstream end.</summary>
            public double UpCeiling;
            /// <summary>Highest downstream invert allowed by cover at the downstream end.</summary>
            public double DownCeiling;
            public double? MinSlope, MaxSlope;
            /// <summary>Interior samples: t in (0,1) from the upstream end, and the invert ceiling there.</summary>
            public List<(double t, double ceiling)> Samples = new();
            /// <summary>Tie-in: required upstream invert (e.g. hold existing).</summary>
            public double? FixedUp;
            /// <summary>Tie-in: required downstream invert (e.g. existing outfall).</summary>
            public double? FixedDown;
        }

        public sealed class ChainConflict
        {
            public int PipeIndex;
            /// <summary>"fixed_invert" | "slope_bounds" | "cover_vs_slope".</summary>
            public string Kind = "";
            public double Delta;
            public string Message = "";
        }

        public sealed class ChainResult
        {
            public double[] Up = Array.Empty<double>();
            public double[] Down = Array.Empty<double>();
            public double[] Slope = Array.Empty<double>();
            /// <summary>Offset applied at interior structure j (1..n-1): outgoing invert = incoming − Drop[j].</summary>
            public double[] Drop = Array.Empty<double>();
            public List<ChainConflict> Conflicts = new();
            public bool Feasible => Conflicts.Count == 0;
            public int Iterations;
        }

        /// <summary>Offset at a structure for the given mode: outgoing invert = incoming invert − drop.</summary>
        public static double DropAtStructure(OffsetMode mode, double incomingHeight, double outgoingHeight, double manualDrop) =>
            mode switch
            {
                OffsetMode.MatchCrowns  => outgoingHeight - incomingHeight,   // crowns equal
                OffsetMode.MatchInverts => 0.0,
                OffsetMode.ManualDrop   => manualDrop,
                _ => 0.0,
            };

        public static ChainResult Solve(IList<ChainPipe> pipes, OffsetMode mode, double manualDrop)
        {
            var r = new ChainResult();
            int n = pipes.Count;
            r.Up = new double[n]; r.Down = new double[n]; r.Slope = new double[n]; r.Drop = new double[Math.Max(n, 1)];
            if (n == 0) return r;

            for (int k = 0; k < n; k++)
            {
                var p = pipes[k];
                if (p.MinSlope.HasValue && p.MaxSlope.HasValue && p.MinSlope > p.MaxSlope + 1e-12)
                    r.Conflicts.Add(new ChainConflict
                    {
                        PipeIndex = k, Kind = "slope_bounds", Delta = p.MinSlope.Value - p.MaxSlope.Value,
                        Message = $"Pipe '{p.Id}': rule set minimum slope {p.MinSlope:0.0000} exceeds maximum slope {p.MaxSlope:0.0000}.",
                    });
                if (p.Length <= 0)
                    r.Conflicts.Add(new ChainConflict { PipeIndex = k, Kind = "slope_bounds", Message = $"Pipe '{p.Id}' has zero horizontal length." });
            }
            if (r.Conflicts.Count > 0) return r;

            for (int j = 1; j < n; j++)
                r.Drop[j] = DropAtStructure(mode, pipes[j - 1].InnerHeight, pipes[j].InnerHeight, manualDrop);

            if (!March(pipes, r.Drop, useFixed: true, r.Up, r.Down, out int badPipe))
            {
                // Explain: solve again without any tie-in and compare.
                var freeUp = new double[n]; var freeDown = new double[n];
                bool freeOk = March(pipes, r.Drop, useFixed: false, freeUp, freeDown, out int badFree);
                if (!freeOk)
                {
                    var p = pipes[badFree];
                    r.Conflicts.Add(new ChainConflict
                    {
                        PipeIndex = badFree, Kind = "cover_vs_slope",
                        Message = $"Pipe '{p.Id}': no invert pair satisfies its cover samples together with slope bounds [{Fmt(p.MinSlope)}, {Fmt(p.MaxSlope)}].",
                    });
                    return r;
                }
                bool explained = false;
                for (int k = 0; k < n; k++)
                {
                    var p = pipes[k];
                    if (p.FixedUp is double fu && Math.Abs(fu - freeUp[k]) > 1e-9)
                    {
                        r.Conflicts.Add(Explain(k, p.Id, "upstream", fu, freeUp[k]));
                        explained = true;
                    }
                    if (p.FixedDown is double fd && Math.Abs(fd - freeDown[k]) > 1e-9)
                    {
                        r.Conflicts.Add(Explain(k, p.Id, "downstream", fd, freeDown[k]));
                        explained = true;
                    }
                }
                if (!explained)
                    r.Conflicts.Add(new ChainConflict
                    {
                        PipeIndex = badPipe, Kind = "fixed_invert",
                        Message = $"Pipe '{pipes[badPipe].Id}': the tie-in constraints cannot all be met together with cover and slope bounds.",
                    });
                return r;
            }

            for (int k = 0; k < n; k++)
                r.Slope[k] = (r.Up[k] - r.Down[k]) / pipes[k].Length;
            return r;
        }

        private static ChainConflict Explain(int k, string id, string end, double fixedValue, double freeValue)
        {
            double delta = fixedValue - freeValue;
            string msg = delta > 0
                ? $"Pipe '{id}': the held/tie-in {end} invert {fixedValue:0.000} is {delta:0.000} above the shallowest profile that cover and slope allow ({freeValue:0.000}); lower the tie-in, relax cover, or allow a steeper slope upstream."
                : $"Pipe '{id}': the held/tie-in {end} invert {fixedValue:0.000} is {-delta:0.000} below what the maximum-slope bound lets the run reach ({freeValue:0.000}); allow a steeper slope or add a drop structure.";
            return new ChainConflict { PipeIndex = k, Kind = "fixed_invert", Delta = delta, Message = msg };
        }

        // ── the march ──────────────────────────────────────────────────────

        /// <summary>
        /// Backward: per-pipe interval of upstream inverts that still admit a feasible completion.
        /// Forward: take the maximum upstream invert, then the maximum feasible downstream invert, and so on.
        /// </summary>
        private static bool March(IList<ChainPipe> pipes, double[] drop, bool useFixed, double[] up, double[] down, out int badPipe)
        {
            int n = pipes.Count;
            var loU = new double[n]; var hiU = new double[n];
            badPipe = -1;

            for (int k = n - 1; k >= 0; k--)
            {
                var H = HalfPlanes(pipes[k], useFixed);
                if (k < n - 1)
                {
                    // downstream invert of pipe k = incoming invert at structure k+1 = upstream of pipe k+1 + drop
                    if (!double.IsNegativeInfinity(loU[k + 1])) H.Add((0, -1, -(loU[k + 1] + drop[k + 1])));
                    if (!double.IsPositiveInfinity(hiU[k + 1])) H.Add((0, 1, hiU[k + 1] + drop[k + 1]));
                }
                if (!Project(H, out double lo, out double hi, out _, out _)) { badPipe = k; return false; }
                loU[k] = lo; hiU[k] = hi;
            }

            double u = hiU[0];
            for (int k = 0; k < n; k++)
            {
                var p = pipes[k];
                double v = Math.Min(p.DownCeiling, u - (p.MinSlope ?? 0.0) * p.Length);
                foreach (var (t, c) in p.Samples)
                    if (t > 1e-9) v = Math.Min(v, (c - (1 - t) * u) / t);
                if (useFixed && p.FixedDown is double fd) v = Math.Min(v, fd);
                if (k < n - 1) v = Math.Min(v, hiU[k + 1] + drop[k + 1]);

                // lower side (max slope, tie-in, downstream interval) must still hold — guaranteed by the
                // backward pass up to rounding; guard against numerical drift.
                double lower = double.NegativeInfinity;
                if (p.MaxSlope is double ms) lower = Math.Max(lower, u - ms * p.Length);
                if (useFixed && p.FixedDown is double fd2) lower = Math.Max(lower, fd2);
                if (k < n - 1 && !double.IsNegativeInfinity(loU[k + 1])) lower = Math.Max(lower, loU[k + 1] + drop[k + 1]);
                if (v < lower - 1e-6) { badPipe = k; return false; }
                if (v < lower) v = lower;

                up[k] = u; down[k] = v;
                if (k < n - 1) u = v - drop[k + 1];
            }
            return true;
        }

        /// <summary>Constraints of one pipe as half-planes a·u + b·v ≤ c (u = upstream, v = downstream invert).</summary>
        private static List<(double a, double b, double c)> HalfPlanes(ChainPipe p, bool useFixed)
        {
            var H = new List<(double, double, double)>
            {
                (1, 0, p.UpCeiling),
                (0, 1, p.DownCeiling),
                (-1, 1, -(p.MinSlope ?? 0.0) * p.Length),        // v ≤ u − sMin·L
            };
            if (p.MaxSlope is double mx) H.Add((1, -1, mx * p.Length)); // u − v ≤ sMax·L
            foreach (var (t, c) in p.Samples) H.Add((1 - t, t, c));
            if (useFixed && p.FixedUp is double fu) { H.Add((1, 0, fu)); H.Add((-1, 0, -fu)); }
            if (useFixed && p.FixedDown is double fd) { H.Add((0, 1, fd)); H.Add((0, -1, -fd)); }
            return H;
        }

        private const double Big = 1e6;

        /// <summary>
        /// Exact projection of the polygon {a·u + b·v ≤ c} onto both axes by vertex enumeration.
        /// Returns false when the polygon is empty.  A bound at ±<see cref="Big"/> means unbounded.
        /// </summary>
        private static bool Project(List<(double a, double b, double c)> H, out double uMin, out double uMax, out double vMin, out double vMax)
        {
            var hs = new List<(double a, double b, double c)>(H)
            {
                (-1, 0, Big), (0, -1, Big), (1, 0, Big), (0, 1, Big),
            };
            uMin = vMin = double.PositiveInfinity;
            uMax = vMax = double.NegativeInfinity;
            bool any = false;

            for (int i = 0; i < hs.Count; i++)
                for (int j = i + 1; j < hs.Count; j++)
                {
                    var (a1, b1, c1) = hs[i];
                    var (a2, b2, c2) = hs[j];
                    double det = a1 * b2 - a2 * b1;
                    if (Math.Abs(det) < 1e-12) continue;
                    double u = (c1 * b2 - c2 * b1) / det;
                    double v = (a1 * c2 - a2 * c1) / det;

                    bool ok = true;
                    foreach (var (a, b, c) in hs)
                        if (a * u + b * v > c + 1e-7 * (1 + Math.Abs(c))) { ok = false; break; }
                    if (!ok) continue;

                    any = true;
                    uMin = Math.Min(uMin, u); uMax = Math.Max(uMax, u);
                    vMin = Math.Min(vMin, v); vMax = Math.Max(vMax, v);
                }

            if (!any) return false;
            if (uMin <= -Big + 1) uMin = double.NegativeInfinity;
            if (vMin <= -Big + 1) vMin = double.NegativeInfinity;
            if (uMax >= Big - 1) uMax = double.PositiveInfinity;
            if (vMax >= Big - 1) vMax = double.PositiveInfinity;
            return true;
        }

        private static string Fmt(double? v) => v.HasValue ? v.Value.ToString("0.0000") : "none";
    }
}
