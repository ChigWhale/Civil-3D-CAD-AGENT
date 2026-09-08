using System;
using System.Collections.Generic;

namespace PipeInvertSetter
{
    /// <summary>How pipes are related to each other at the shared structure.</summary>
    public enum OffsetMode
    {
        /// <summary>All crown (inside-top) elevations equal at the structure.</summary>
        MatchCrowns,
        /// <summary>All invert elevations equal at the structure.</summary>
        MatchInverts,
        /// <summary>Outgoing invert = min(incoming invert) − drop.</summary>
        ManualDrop,
    }

    /// <summary>Which way water moves through a pipe relative to the shared structure.</summary>
    public enum FlowRelation
    {
        /// <summary>Pipe carries flow INTO the structure (structure is the downstream end).</summary>
        Incoming,
        /// <summary>Pipe carries flow OUT OF the structure (structure is the upstream end).</summary>
        Outgoing,
        /// <summary>Direction not defined by the network; treated as outgoing for slope sign.</summary>
        Unknown,
    }

    /// <summary>
    /// Pure, AutoCAD-free elevation arithmetic so the solver rules can be unit-tested
    /// without a running Civil 3D.  All lengths are drawing units (feet), slopes are
    /// dimensionless rise/run (0.005 == 0.5 %).
    /// </summary>
    public static class ElevationMath
    {
        /// <summary>
        /// Highest invert that still satisfies minimum cover at a point.
        /// Civil 3D measures cover from the surface to the OUTSIDE top of the pipe,
        /// which sits (innerHeight + wallThickness) above the invert.
        /// </summary>
        public static double MaxInvertForCover(double surfaceZ, double coverMin, double innerHeight, double wallThickness)
            => surfaceZ - coverMin - innerHeight - wallThickness;

        /// <summary>Cover (surface to outside top of pipe) that results from a given invert.</summary>
        public static double CoverAtInvert(double surfaceZ, double invert, double innerHeight, double wallThickness)
            => surfaceZ - (invert + innerHeight + wallThickness);

        /// <summary>Rise/run slope, positive when the upstream end is higher.</summary>
        public static double Slope(double upstreamInvert, double downstreamInvert, double length2D)
            => length2D <= 0 ? 0.0 : (upstreamInvert - downstreamInvert) / length2D;

        /// <summary>Outcome of fitting one pipe's far end against a fixed structure-side invert.</summary>
        public sealed class FarEndFit
        {
            /// <summary>True when a far-end invert exists that satisfies cover + slope bounds.</summary>
            public bool Feasible;
            /// <summary>Chosen far-end invert (only meaningful when <see cref="Feasible"/>).</summary>
            public double FarInvert;
            /// <summary>Resulting slope (rise/run) of the pipe.</summary>
            public double Slope;
            /// <summary>When infeasible: how far the structure-side invert must move (negative = lower) to resolve.</summary>
            public double RequiredStructureInvertShift;
            /// <summary>Human-readable explanation when infeasible.</summary>
            public string? Reason;
        }

        /// <summary>
        /// Given the structure-side invert (already reconciled across all pipes) find the
        /// shallowest far-end invert that keeps cover at the far end and honours the slope
        /// bounds.  Never clamps silently: an infeasible combination is reported.
        /// </summary>
        /// <param name="structureInvert">Invert at the shared structure.</param>
        /// <param name="farMaxInvert">Highest far-end invert allowed by cover.</param>
        /// <param name="length2D">Horizontal pipe length used for slope.</param>
        /// <param name="relation">Whether the structure is upstream or downstream of the pipe.</param>
        /// <param name="minSlope">Rule-set minimum slope, or null when no rule applies.</param>
        /// <param name="maxSlope">Rule-set maximum slope, or null when no rule applies.</param>
        public static FarEndFit FitFarEnd(double structureInvert, double farMaxInvert, double length2D,
                                          FlowRelation relation, double? minSlope, double? maxSlope)
        {
            var fit = new FarEndFit();
            double sMin = minSlope ?? 0.0;                  // never adverse
            double sMax = maxSlope ?? double.PositiveInfinity;

            if (length2D <= 0)
            {
                fit.Feasible = false;
                fit.Reason = "Pipe has zero horizontal length; slope is undefined.";
                return fit;
            }

            if (relation == FlowRelation.Incoming)
            {
                // Structure is downstream: far end is upstream and must be HIGHER.
                double lowestFar  = structureInvert + sMin * length2D;   // slope >= min
                double highestFar = structureInvert + sMax * length2D;   // slope <= max
                double ceiling    = Math.Min(farMaxInvert, highestFar);  // cover and max slope

                if (ceiling + 1e-9 < lowestFar)
                {
                    fit.Feasible = false;
                    fit.RequiredStructureInvertShift = ceiling - lowestFar;  // negative: lower the structure side
                    fit.Reason = farMaxInvert < highestFar
                        ? $"Far-end cover limits invert to {farMaxInvert:0.000} but minimum slope needs {lowestFar:0.000}; lower the structure-side invert by {lowestFar - ceiling:0.000}."
                        : $"Maximum slope caps far invert at {highestFar:0.000} but minimum slope needs {lowestFar:0.000}; slope bounds are contradictory.";
                    return fit;
                }

                fit.Feasible  = true;
                fit.FarInvert = ceiling;                                   // shallowest legal
                fit.Slope     = Slope(fit.FarInvert, structureInvert, length2D);
                return fit;
            }
            else
            {
                // Structure is upstream (or unknown): far end is downstream and must be LOWER.
                double highestFar = structureInvert - sMin * length2D;   // slope >= min
                double lowestFar  = double.IsPositiveInfinity(sMax)
                                    ? double.NegativeInfinity
                                    : structureInvert - sMax * length2D; // slope <= max
                double ceiling    = Math.Min(farMaxInvert, highestFar);  // cover and min slope

                if (ceiling + 1e-9 < lowestFar)
                {
                    fit.Feasible = false;
                    fit.RequiredStructureInvertShift = lowestFar - ceiling; // positive: raise structure side (usually blocked by cover)
                    fit.Reason = $"Far-end cover limits invert to {farMaxInvert:0.000} but maximum slope allows no lower than {lowestFar:0.000}; raise the structure-side invert by {lowestFar - ceiling:0.000} or relax maximum slope.";
                    return fit;
                }

                fit.Feasible  = true;
                fit.FarInvert = ceiling;
                fit.Slope     = Slope(structureInvert, fit.FarInvert, length2D);
                return fit;
            }
        }

        /// <summary>One pipe's contribution to the reconciliation at the structure.</summary>
        public sealed class StructureSideInput
        {
            public string Id = "";
            public double InnerHeight;
            /// <summary>Highest invert allowed at the structure end by cover.</summary>
            public double MaxInvert;
            public FlowRelation Relation;
            public bool IsGoverning;
        }

        /// <summary>Reconciled structure-side inverts keyed by pipe id.</summary>
        public sealed class StructureSideResult
        {
            public Dictionary<string, double> InvertById = new();
            public string GoverningId = "";
            /// <summary>Pipes whose cover ceiling is violated by the reconciled elevation.</summary>
            public List<(string id, double invert, double maxInvert)> CoverViolations = new();
        }

        /// <summary>
        /// Decides the invert of every pipe at the shared structure.
        /// The governing pipe (explicit, or the one with the lowest cover ceiling) fixes the
        /// datum; the others follow according to <paramref name="mode"/>.
        /// </summary>
        /// <param name="structureInvertOverride">When supplied, replaces the governing pipe's cover-derived invert.</param>
        public static StructureSideResult ReconcileAtStructure(IList<StructureSideInput> pipes, OffsetMode mode,
                                                               double manualDrop, double? structureInvertOverride)
        {
            var result = new StructureSideResult();
            if (pipes.Count == 0) return result;

            // Governing pipe: explicit flag wins, otherwise the most restrictive ceiling.
            StructureSideInput governing = pipes[0];
            bool explicitGoverning = false;
            foreach (var p in pipes) if (p.IsGoverning) { governing = p; explicitGoverning = true; break; }
            if (!explicitGoverning)
            {
                // "Most restrictive" compares on the crown datum for MatchCrowns and on the
                // invert datum otherwise, so the choice is consistent with the match rule.
                foreach (var p in pipes)
                {
                    double key  = mode == OffsetMode.MatchCrowns ? p.MaxInvert + p.InnerHeight : p.MaxInvert;
                    double best = mode == OffsetMode.MatchCrowns ? governing.MaxInvert + governing.InnerHeight : governing.MaxInvert;
                    if (key < best) governing = p;
                }
            }
            result.GoverningId = governing.Id;
            double govInvert = structureInvertOverride ?? governing.MaxInvert;

            switch (mode)
            {
                case OffsetMode.MatchCrowns:
                {
                    double crown = govInvert + governing.InnerHeight;
                    foreach (var p in pipes) result.InvertById[p.Id] = crown - p.InnerHeight;
                    break;
                }
                case OffsetMode.MatchInverts:
                {
                    foreach (var p in pipes) result.InvertById[p.Id] = govInvert;
                    break;
                }
                case OffsetMode.ManualDrop:
                {
                    // Incoming pipes share the governing datum; outgoing pipes sit `drop` lower
                    // than the lowest incoming invert.  With no incoming pipe the governing
                    // datum itself is the reference.
                    double incomingMin = double.PositiveInfinity;
                    foreach (var p in pipes)
                        if (p.Relation == FlowRelation.Incoming)
                            incomingMin = Math.Min(incomingMin, Math.Min(p.MaxInvert, govInvert));
                    if (double.IsPositiveInfinity(incomingMin)) incomingMin = govInvert;

                    foreach (var p in pipes)
                        result.InvertById[p.Id] = p.Relation == FlowRelation.Incoming
                            ? incomingMin
                            : incomingMin - manualDrop;
                    break;
                }
            }

            foreach (var p in pipes)
                if (result.InvertById[p.Id] > p.MaxInvert + 1e-9)
                    result.CoverViolations.Add((p.Id, result.InvertById[p.Id], p.MaxInvert));

            return result;
        }
    }
}
