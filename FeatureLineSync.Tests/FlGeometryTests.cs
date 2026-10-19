using FeatureLineSync;
using Xunit;

namespace FeatureLineSync.Tests;

public class FlGeometryTests
{
    [Fact]
    public void StraightSegmentLengthIsChord()
    {
        Assert.Equal(5.0, FlGeometry.SegmentLength2D(0, 0, 3, 4, 0), 9);
    }

    [Fact]
    public void SemicircleLengthIsPiR()
    {
        // bulge 1 = 180 degrees; chord 2 -> radius 1
        Assert.Equal(Math.PI, FlGeometry.SegmentLength2D(0, 0, 2, 0, 1.0), 9);
    }

    [Fact]
    public void MidpointOfCcwSemicircleLiesBelowChord()
    {
        var (x, y) = FlGeometry.PointAt(0, 0, 2, 0, 1.0, 0.5);
        Assert.Equal(1.0, x, 9);
        Assert.Equal(-1.0, y, 9);
        var (ex, ey) = FlGeometry.PointAt(0, 0, 2, 0, 1.0, 1.0);
        Assert.Equal(2.0, ex, 9);
        Assert.Equal(0.0, ey, 9);
    }

    [Fact]
    public void MidpointOfCwSemicircleLiesAboveChord()
    {
        var (x, y) = FlGeometry.PointAt(0, 0, 2, 0, -1.0, 0.5);
        Assert.Equal(1.0, x, 9);
        Assert.Equal(1.0, y, 9);
    }

    [Fact]
    public void QuarterArcEndpointIsExact()
    {
        double bulge = Math.Tan(Math.PI / 8);          // 90 degrees
        var (x, y) = FlGeometry.PointAt(0, 0, 10, 10, bulge, 1.0);
        Assert.Equal(10.0, x, 9);
        Assert.Equal(10.0, y, 9);
        double len = FlGeometry.SegmentLength2D(0, 0, 10, 10, bulge);
        double r = Math.Sqrt(200) / 2 / Math.Sin(Math.PI / 4);
        Assert.Equal(r * Math.PI / 2, len, 9);
    }

    [Fact]
    public void ProjectOnStraightSegment()
    {
        var (t, d) = FlGeometry.Project(0, 0, 10, 0, 0, 4, 3);
        Assert.Equal(0.4, t, 9);
        Assert.Equal(3.0, d, 9);
        var (t2, d2) = FlGeometry.Project(0, 0, 10, 0, 0, 15, 0);
        Assert.Equal(1.0, t2, 9);
        Assert.Equal(5.0, d2, 9);
    }

    [Fact]
    public void ProjectOnArc()
    {
        var (t, d) = FlGeometry.Project(0, 0, 2, 0, 1.0, 1, -1);
        Assert.Equal(0.5, t, 9);
        Assert.Equal(0.0, d, 9);
        var (t2, d2) = FlGeometry.Project(0, 0, 2, 0, 1.0, 1, -3);
        Assert.Equal(0.5, t2, 9);
        Assert.Equal(2.0, d2, 9);
    }

    [Fact]
    public void DetectClosedFromLength2D()
    {
        var square = new List<double[]> { new[] { 0.0, 0, 0 }, new[] { 10.0, 0, 0 }, new[] { 10.0, 10, 0 }, new[] { 0.0, 10, 0 } };
        var bulges = new List<double> { 0, 0, 0, 0 };
        Assert.True(FlGeometry.DetectClosed(square, bulges, 40));
        Assert.False(FlGeometry.DetectClosed(square, bulges, 30));
    }

    [Fact]
    public void LocateFindsSegmentAndFraction()
    {
        var line = TestLines.Straight("A", (0, 0, 0), (10, 0, 0), (10, 10, 0));
        var (seg, t, dist) = FlGeometry.Locate(line, 10, 2.5);
        Assert.Equal(1, seg);
        Assert.Equal(0.25, t, 9);
        Assert.Equal(0.0, dist, 9);
    }
}
