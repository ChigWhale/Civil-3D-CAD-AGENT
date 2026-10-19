using FeatureLineSync;

namespace FeatureLineSync.Tests;

internal static class TestLines
{
    public static FlLine Straight(string handle, params (double x, double y, double z)[] pis) => Make(handle, false, pis);

    public static FlLine Closed(string handle, params (double x, double y, double z)[] pis) => Make(handle, true, pis);

    public static FlLine Make(string handle, bool closed, params (double x, double y, double z)[] pis)
    {
        var line = new FlLine { Handle = handle, Name = "L" + handle, Closed = closed };
        foreach (var p in pis) line.Pis.Add(new[] { p.x, p.y, p.z });
        for (int i = 0; i < line.SegmentCount; i++) line.Bulges.Add(0.0);
        Finish(line);
        return line;
    }

    /// <summary>Add an elevation point at a segment fraction (XY computed from the geometry).</summary>
    public static FlLine WithElevationPoint(this FlLine line, int seg, double frac, double z)
    {
        var (x, y) = FlGeometry.PointOn(line, seg, frac);
        line.ElevationPoints.Add(new FlElevPoint { X = x, Y = y, Z = z, Segment = seg, Fraction = frac });
        return line;
    }

    public static void Finish(FlLine line)
    {
        line.SegLengths2D = FlGeometry.SegmentLengths(line.Pis, line.Bulges, line.Closed);
        line.Length2D = line.SegLengths2D.Sum();
    }

    public static FlSnapshot Snap(params FlLine[] lines)
    {
        var s = new FlSnapshot();
        s.Lines.AddRange(lines);
        return s;
    }

    /// <summary>Probe that answers only at listed XY (within 1e-6).</summary>
    public static Func<double, double, double?> ProbeAt(params (double x, double y, double z)[] hits) =>
        (x, y) =>
        {
            foreach (var h in hits)
                if (FlGeometry.Dist2D(h.x, h.y, x, y) < 1e-6) return h.z;
            return null;
        };
}
