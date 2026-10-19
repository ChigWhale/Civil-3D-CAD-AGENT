using FeatureLineSync;
using Xunit;

namespace FeatureLineSync.Tests;

public class GradeLabelsTests
{
    [Fact]
    public void OneLabelPerSegment_AtMidpoint_WithGrade()
    {
        // 100 long rising 2 %, then 50 long falling 4 %
        var line = TestLines.Straight("A", (0, 0, 100), (100, 0, 102), (150, 0, 100));
        var labels = GradeLabels.Compute(line);

        Assert.Equal(2, labels.Count);
        Assert.Equal(50, labels[0].X, 6);
        Assert.Equal(0, labels[0].Y, 6);
        Assert.Equal(0.02, labels[0].Grade, 9);
        Assert.Equal(101, labels[0].Z, 9);
        Assert.Equal(125, labels[1].X, 6);
        Assert.Equal(-0.04, labels[1].Grade, 9);
    }

    [Fact]
    public void ArrowPointsUphill_InReadingDirection()
    {
        // drawn left to right, rising: uphill is toward the end (right of the text)
        var up = TestLines.Straight("A", (0, 0, 100), (100, 0, 102));
        Assert.Equal("2.00% >", GradeLabels.Compute(up)[0].Text);

        // drawn left to right, falling: uphill is back toward the start (left)
        var down = TestLines.Straight("B", (0, 0, 102), (100, 0, 100));
        Assert.Equal("< 2.00%", GradeLabels.Compute(down)[0].Text);

        // drawn right to left, falling toward the end: the high start is at x=100, the RIGHT of the flipped text
        var rtl = TestLines.Straight("C", (100, 0, 102), (0, 0, 100));
        var l = GradeLabels.Compute(rtl)[0];
        Assert.Equal("2.00% >", l.Text);
        Assert.Equal(0.0, l.Angle, 9);                  // flipped back to readable
        Assert.Equal(-0.02, l.Grade, 9);                // sign still follows the drawing direction
    }

    [Fact]
    public void TextIsNeverUpsideDown()
    {
        var line = TestLines.Straight("A", (0, 0, 0), (-100, -10, 1));      // pointing left and slightly down
        double ang = GradeLabels.Compute(line)[0].Angle;
        Assert.True(ang > -Math.PI / 2 && ang <= Math.PI / 2 + 1e-9);
        Assert.Equal(Math.Atan2(10, 100), ang, 9);
    }

    [Fact]
    public void FlatSegment_NoArrow_RoundsToHundredths()
    {
        var flat = TestLines.Straight("A", (0, 0, 50), (100, 0, 50.00001));
        Assert.Equal("0.00%", GradeLabels.Compute(flat)[0].Text);

        var third = TestLines.Straight("B", (0, 0, 0), (300, 0, 1));           // 0.3333 %
        Assert.Equal("0.33% >", GradeLabels.Compute(third)[0].Text);
        var tenth = TestLines.Straight("C", (0, 0, 0), (100, 0, 12.345));      // 12.345 %
        Assert.Equal("12.35% >", GradeLabels.Compute(tenth)[0].Text);
    }

    [Fact]
    public void ElevationPoint_SplitsSegment_IntoTwoLabels()
    {
        var line = TestLines.Straight("A", (0, 0, 100), (100, 0, 100))
            .WithElevationPoint(0, 0.5, 101);
        var labels = GradeLabels.Compute(line);

        Assert.Equal(2, labels.Count);
        Assert.Equal(25, labels[0].X, 6);
        Assert.Equal(0.02, labels[0].Grade, 9);
        Assert.Equal(75, labels[1].X, 6);
        Assert.Equal(-0.02, labels[1].Grade, 9);
    }

    [Fact]
    public void ClosedLine_LabelsTheClosingSegment()
    {
        var sq = TestLines.Closed("A", (0, 0, 100), (100, 0, 101), (100, 100, 101), (0, 100, 100));
        var labels = GradeLabels.Compute(sq);

        Assert.Equal(4, labels.Count);
        Assert.Equal(0.01, labels[0].Grade, 9);
        Assert.Equal(0.0, labels[1].Grade, 9);
        Assert.Equal(-0.01, labels[2].Grade, 9);
        Assert.Equal(0.0, labels[3].Grade, 9);          // (0,100) back to (0,0), both at 100
        Assert.Equal(0, labels[3].X, 6);
        Assert.Equal(50, labels[3].Y, 6);
    }

    [Fact]
    public void VerticalSegment_ReadsBottomToTop()
    {
        var upward = TestLines.Straight("A", (0, 0, 0), (0, 100, 1));
        var downward = TestLines.Straight("B", (0, 100, 1), (0, 0, 0));
        Assert.Equal(Math.PI / 2, GradeLabels.Compute(upward)[0].Angle, 9);
        Assert.Equal(Math.PI / 2, GradeLabels.Compute(downward)[0].Angle, 9);
        // both: uphill is toward (0,100), the top of the text when read bottom-to-top = right
        Assert.Equal("1.00% >", GradeLabels.Compute(upward)[0].Text);
        Assert.Equal("1.00% >", GradeLabels.Compute(downward)[0].Text);
    }

    [Fact]
    public void ArcSegment_UsesArcLength_AndLocalTangent()
    {
        // quarter circle radius 100 from (100,0) to (0,100), CCW: arc length = 50π
        var line = new FlLine { Handle = "A" };
        line.Pis.Add(new[] { 100.0, 0.0, 0.0 });
        line.Pis.Add(new[] { 0.0, 100.0, Math.PI });        // rises π over 50π: grade 2 %
        line.Bulges.Add(Math.Tan(Math.PI / 8));
        TestLines.Finish(line);

        var l = Assert.Single(GradeLabels.Compute(line));
        Assert.Equal(0.02, l.Grade, 6);
        Assert.Equal(100 * Math.Cos(Math.PI / 4), l.X, 6);  // arc midpoint at 45°
        Assert.Equal(100 * Math.Sin(Math.PI / 4), l.Y, 6);
        // tangent at 45° CCW points up-left (135°), flipped to -45° for reading
        Assert.Equal(-Math.PI / 4, l.Angle, 3);
        Assert.Equal("< 2.00%", l.Text);                    // flipped, rising → high end is the arc end → "left"
    }

    [Fact]
    public void DegenerateLines_GiveNoLabels()
    {
        Assert.Empty(GradeLabels.Compute(TestLines.Straight("A", (0, 0, 0))));
        Assert.Empty(GradeLabels.Compute(TestLines.Straight("B", (0, 0, 0), (0, 0, 5))));   // zero length
    }
}
