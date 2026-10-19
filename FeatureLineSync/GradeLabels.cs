using System;
using System.Collections.Generic;
using System.Globalization;

namespace FeatureLineSync
{
    /// <summary>One grade label for the overlay: where it sits on the line and what it says.</summary>
    public sealed class GradeLabel
    {
        /// <summary>2D midpoint of the piece, on the PI polyline.</summary>
        public double X { get; set; }
        public double Y { get; set; }
        /// <summary>Elevation at the midpoint (linear between the two points).</summary>
        public double Z { get; set; }
        /// <summary>Text rotation in radians, already flipped so the text reads left to right.</summary>
        public double Angle { get; set; }
        /// <summary>Signed grade along the line's drawing direction: dz / 2D length.</summary>
        public double Grade { get; set; }
        /// <summary>2D length of the piece.</summary>
        public double Length2D { get; set; }
        /// <summary>Station of the piece start along the line.</summary>
        public double StartStation { get; set; }
        public string Text { get; set; } = "";
    }

    /// <summary>
    /// Grades of a feature line, one per piece between consecutive points (PIs and elevation points),
    /// since the elevation is linear between those. Pure geometry, no AutoCAD types, so it is unit-tested.
    /// </summary>
    public static class GradeLabels
    {
        public const double MinPieceLength = 1e-6;

        public static List<GradeLabel> Compute(FlLine line, int decimals = 2)
        {
            var labels = new List<GradeLabel>();
            int segs = line.SegmentCount;
            if (segs < 1 || line.SegLengths2D.Count < segs || line.Pis.Count < 2) return labels;

            // station at the start of each PI segment; cum[segs] = total length
            var cum = new double[segs + 1];
            for (int i = 0; i < segs; i++) cum[i + 1] = cum[i] + line.SegLengths2D[i];
            double total = cum[segs];

            var pts = new List<(double s, double z)>();
            for (int i = 0; i < segs; i++) pts.Add((cum[i], line.Pis[i][2]));
            pts.Add((total, line.Closed ? line.Pis[0][2] : line.Pis[^1][2]));
            foreach (FlElevPoint ep in line.ElevationPoints)
            {
                int seg = Math.Clamp(ep.Segment, 0, segs - 1);
                double t = Math.Clamp(ep.Fraction, 0.0, 1.0);
                pts.Add((cum[seg] + t * line.SegLengths2D[seg], ep.Z));
            }
            pts.Sort((a, b) => a.s.CompareTo(b.s));

            for (int k = 0; k + 1 < pts.Count; k++)
            {
                double len = pts[k + 1].s - pts[k].s;
                if (len < MinPieceLength) continue;                       // duplicate point
                double dz = pts[k + 1].z - pts[k].z;
                double grade = dz / len;
                double mid = (pts[k].s + pts[k + 1].s) / 2.0;

                var (x, y) = PointAtStation(line, cum, mid);
                // tangent from two nearby points so arcs get the local direction, not the chord's
                double h = Math.Min(len * 0.25, 0.5);
                var (x0, y0) = PointAtStation(line, cum, mid - h);
                var (x1, y1) = PointAtStation(line, cum, mid + h);
                double ang = Math.Atan2(y1 - y0, x1 - x0);
                bool flipped = ReadingAngle(ref ang);

                labels.Add(new GradeLabel
                {
                    X = x, Y = y, Z = pts[k].z + dz / 2.0,
                    Angle = ang, Grade = grade, Length2D = len, StartStation = pts[k].s,
                    Text = Format(grade, flipped, decimals),
                });
            }
            return labels;
        }

        /// <summary>
        /// Rotate a direction into the readable half-plane (-90°, 90°]. Returns true when it was turned 180°,
        /// i.e. the line's drawing direction points to the LEFT of the text.
        /// </summary>
        public static bool ReadingAngle(ref double ang)
        {
            ang = Math.Atan2(Math.Sin(ang), Math.Cos(ang));                // normalise to (-π, π]
            bool flip = Math.Cos(ang) < -1e-12 || (Math.Abs(Math.Cos(ang)) <= 1e-12 && Math.Sin(ang) < 0);
            if (flip) ang = Math.Atan2(Math.Sin(ang + Math.PI), Math.Cos(ang + Math.PI));
            return flip;
        }

        /// <summary>
        /// "2.00% >" / "&lt; 2.00%": the arrow points UPHILL in the text's reading direction, i.e. toward
        /// the end of the piece where the grade is positive. A grade that rounds to zero has no arrow.
        /// </summary>
        public static string Format(double grade, bool flipped, int decimals)
        {
            double pct = Math.Abs(grade) * 100.0;
            string num = pct.ToString("F" + decimals, CultureInfo.InvariantCulture) + "%";
            double half = 0.5 * Math.Pow(10, -decimals);
            if (pct < half) return num;
            // grade > 0: uphill toward the piece end, which is to the right unless the text was flipped
            bool uphillRight = (grade > 0) != flipped;
            return uphillRight ? num + " >" : "< " + num;
        }

        private static (double x, double y) PointAtStation(FlLine line, double[] cum, double s)
        {
            int segs = line.SegmentCount;
            double total = cum[segs];
            if (line.Closed)
            {
                s %= total; if (s < 0) s += total;
            }
            else s = Math.Clamp(s, 0.0, total);

            int seg = 0;
            while (seg < segs - 1 && s >= cum[seg + 1]) seg++;
            double len = line.SegLengths2D[seg];
            double t = len > FlGeometry.Eps ? (s - cum[seg]) / len : 0.0;
            return FlGeometry.PointOn(line, seg, t);
        }
    }
}
