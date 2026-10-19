using System;
using System.Collections.Generic;

namespace FeatureLineSync
{
    /// <summary>
    /// 2D geometry on a feature line's PI polyline (straight segments and bulge arcs).
    /// FeatureLine is not a Curve in the managed API, so distances along the line
    /// are computed here instead of asked from Civil 3D.
    /// </summary>
    public static class FlGeometry
    {
        public const double Eps = 1e-9;

        public static double Dist2D(double ax, double ay, double bx, double by)
        {
            double dx = bx - ax, dy = by - ay;
            return Math.Sqrt(dx * dx + dy * dy);
        }

        /// <summary>2D length of a segment from a to b with the given bulge (0 = straight).</summary>
        public static double SegmentLength2D(double ax, double ay, double bx, double by, double bulge)
        {
            double chord = Dist2D(ax, ay, bx, by);
            if (Math.Abs(bulge) < Eps || chord < Eps) return chord;
            double theta = 4.0 * Math.Atan(Math.Abs(bulge));       // included angle
            double r = chord / (2.0 * Math.Sin(theta / 2.0));
            return r * theta;
        }

        /// <summary>Point at arc-length fraction t (0..1) along the segment a→b with bulge.</summary>
        public static (double x, double y) PointAt(double ax, double ay, double bx, double by, double bulge, double t)
        {
            t = Math.Clamp(t, 0.0, 1.0);
            double chord = Dist2D(ax, ay, bx, by);
            if (Math.Abs(bulge) < Eps || chord < Eps)
                return (ax + (bx - ax) * t, ay + (by - ay) * t);

            ArcOf(ax, ay, bx, by, bulge, out double cx, out double cy, out double r, out double startAng, out double sweep);
            double ang = startAng + sweep * t;
            return (cx + r * Math.Cos(ang), cy + r * Math.Sin(ang));
        }

        /// <summary>
        /// Nearest position on the segment to p: t is the arc-length fraction (0..1), dist the 2D distance from p.
        /// </summary>
        public static (double t, double dist) Project(double ax, double ay, double bx, double by, double bulge, double px, double py)
        {
            double chord = Dist2D(ax, ay, bx, by);
            if (chord < Eps) return (0.0, Dist2D(ax, ay, px, py));

            if (Math.Abs(bulge) < Eps)
            {
                double dx = bx - ax, dy = by - ay;
                double t = ((px - ax) * dx + (py - ay) * dy) / (chord * chord);
                t = Math.Clamp(t, 0.0, 1.0);
                var (qx, qy) = (ax + dx * t, ay + dy * t);
                return (t, Dist2D(qx, qy, px, py));
            }

            ArcOf(ax, ay, bx, by, bulge, out double cx, out double cy, out double r, out double startAng, out double sweep);
            double angP = Math.Atan2(py - cy, px - cx);
            // angle of p relative to the arc start, measured in the sweep direction
            double rel = angP - startAng;
            if (sweep > 0) { while (rel < 0) rel += 2 * Math.PI; while (rel >= 2 * Math.PI) rel -= 2 * Math.PI; }
            else { while (rel > 0) rel -= 2 * Math.PI; while (rel <= -2 * Math.PI) rel += 2 * Math.PI; }
            double tArc = rel / sweep;                                  // 0..(2π/|sweep|)
            double best;
            if (tArc >= 0 && tArc <= 1) best = tArc;
            else
            {
                // outside the arc: nearer endpoint
                double da = Dist2D(ax, ay, px, py), db = Dist2D(bx, by, px, py);
                best = da <= db ? 0.0 : 1.0;
            }
            var (ex, ey) = PointAt(ax, ay, bx, by, bulge, best);
            return (best, Dist2D(ex, ey, px, py));
        }

        private static void ArcOf(double ax, double ay, double bx, double by, double bulge,
                                  out double cx, out double cy, out double r, out double startAng, out double sweep)
        {
            double chord = Dist2D(ax, ay, bx, by);
            double theta = 4.0 * Math.Atan(bulge);                       // signed included angle (+ = CCW)
            r = Math.Abs(chord / (2.0 * Math.Sin(theta / 2.0)));
            // sagitta-based centre: midpoint of chord offset perpendicular by r*cos(theta/2)
            double mx = (ax + bx) / 2.0, my = (ay + by) / 2.0;
            double ux = (bx - ax) / chord, uy = (by - ay) / chord;      // chord direction
            double nx = -uy, ny = ux;                                   // left normal
            double d = r * Math.Cos(theta / 2.0);                        // signed distance from chord midpoint to centre
            // for CCW (bulge > 0) with |theta| < π the centre lies on the left; cos(theta/2) handles > π
            double sign = bulge > 0 ? 1.0 : -1.0;
            cx = mx + nx * d * sign;
            cy = my + ny * d * sign;
            startAng = Math.Atan2(ay - cy, ax - cx);
            sweep = theta;
        }

        /// <summary>Length of each PI segment of a line, honouring closure.</summary>
        public static List<double> SegmentLengths(IReadOnlyList<double[]> pis, IReadOnlyList<double> bulges, bool closed)
        {
            var result = new List<double>();
            int n = pis.Count;
            int segs = closed ? n : n - 1;
            for (int i = 0; i < segs; i++)
            {
                double[] a = pis[i], b = pis[(i + 1) % n];
                double bulge = i < bulges.Count ? bulges[i] : 0.0;
                result.Add(SegmentLength2D(a[0], a[1], b[0], b[1], bulge));
            }
            return result;
        }

        /// <summary>Locate p on the line: nearest segment, arc-length fraction on it, and the distance off the line.</summary>
        public static (int seg, double t, double dist) Locate(FlLine line, double px, double py)
        {
            int n = line.Pis.Count;
            int segs = line.SegmentCount;
            int bestSeg = 0; double bestT = 0, bestDist = double.MaxValue;
            for (int i = 0; i < segs; i++)
            {
                double[] a = line.Pis[i], b = line.Pis[(i + 1) % n];
                double bulge = i < line.Bulges.Count ? line.Bulges[i] : 0.0;
                var (t, dist) = Project(a[0], a[1], b[0], b[1], bulge, px, py);
                if (dist < bestDist - Eps) { bestDist = dist; bestSeg = i; bestT = t; }
            }
            return (bestSeg, bestT, bestDist);
        }

        /// <summary>Point on the line at a segment index and arc-length fraction.</summary>
        public static (double x, double y) PointOn(FlLine line, int seg, double t)
        {
            int n = line.Pis.Count;
            double[] a = line.Pis[seg], b = line.Pis[(seg + 1) % n];
            double bulge = seg < line.Bulges.Count ? line.Bulges[seg] : 0.0;
            return PointAt(a[0], a[1], b[0], b[1], bulge, t);
        }

        /// <summary>
        /// Civil 3D's managed FeatureLine has no IsClosed getter. Decide from Length2D:
        /// closed when the open polyline sum plus the closing segment explains Length2D better.
        /// </summary>
        public static bool DetectClosed(IReadOnlyList<double[]> pis, IReadOnlyList<double> bulges, double length2D)
        {
            if (pis.Count < 3) return false;
            double open = 0;
            for (int i = 0; i < pis.Count - 1; i++)
            {
                double bulge = i < bulges.Count ? bulges[i] : 0.0;
                open += SegmentLength2D(pis[i][0], pis[i][1], pis[i + 1][0], pis[i + 1][1], bulge);
            }
            double closingBulge = bulges.Count >= pis.Count ? bulges[pis.Count - 1] : 0.0;
            double closing = SegmentLength2D(pis[^1][0], pis[^1][1], pis[0][0], pis[0][1], closingBulge);
            if (closing < Eps) return false;                             // first == last PI: Civil 3D reports it open with a duplicate
            return Math.Abs(length2D - (open + closing)) < Math.Abs(length2D - open);
        }
    }
}
