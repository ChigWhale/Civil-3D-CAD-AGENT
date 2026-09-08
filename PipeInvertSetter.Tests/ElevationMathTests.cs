using PipeInvertSetter;
using Xunit;

public class ElevationMathTests
{
    // 18" RCP-ish: inner 1.5, wall 0.2
    private const double H = 1.5, Wall = 0.2;

    [Fact]
    public void CoverCeilingMeasuresToOutsideTop()
    {
        // surface 100, cover 3 → outside top 97 → invert 97 − 1.5 − 0.2
        Assert.Equal(95.3, ElevationMath.MaxInvertForCover(100, 3, H, Wall), 6);
        Assert.Equal(3.0, ElevationMath.CoverAtInvert(100, 95.3, H, Wall), 6);
    }

    [Fact]
    public void SlopeIsPositiveWhenUpstreamHigher()
    {
        Assert.Equal(0.01, ElevationMath.Slope(101, 100, 100), 9);
        Assert.Equal(0.0, ElevationMath.Slope(101, 100, 0), 9);
    }

    [Fact]
    public void OutgoingPipe_TakesShallowestFarEndWithinCoverAndMinSlope()
    {
        // structure upstream at 100; far cover allows 99; min slope 0.5 % over 100 ft → 99.5 max
        var fit = ElevationMath.FitFarEnd(100, 99, 100, FlowRelation.Outgoing, 0.005, 0.10);
        Assert.True(fit.Feasible);
        Assert.Equal(99, fit.FarInvert, 9);      // cover governs (99 < 99.5)
        Assert.Equal(0.01, fit.Slope, 9);
    }

    [Fact]
    public void OutgoingPipe_MinSlopeGovernsWhenCoverIsGenerous()
    {
        var fit = ElevationMath.FitFarEnd(100, 120, 100, FlowRelation.Outgoing, 0.005, null);
        Assert.True(fit.Feasible);
        Assert.Equal(99.5, fit.FarInvert, 9);
        Assert.Equal(0.005, fit.Slope, 9);
    }

    [Fact]
    public void OutgoingPipe_InfeasibleWhenCoverForcesSlopeAboveMax()
    {
        // far cover needs ≤ 90 but max slope 5 % over 100 ft allows no lower than 95
        var fit = ElevationMath.FitFarEnd(100, 90, 100, FlowRelation.Outgoing, 0.005, 0.05);
        Assert.False(fit.Feasible);
        Assert.Equal(5.0, fit.RequiredStructureInvertShift, 9);   // would need to raise the structure side
        Assert.Contains("maximum slope", fit.Reason);
    }

    [Fact]
    public void IncomingPipe_InfeasibleWhenUpstreamCoverBlocksMinSlope()
    {
        // structure downstream at 100; upstream needs ≥ 100.5 for min slope but cover caps it at 100.2
        var fit = ElevationMath.FitFarEnd(100, 100.2, 100, FlowRelation.Incoming, 0.005, null);
        Assert.False(fit.Feasible);
        Assert.Equal(-0.3, fit.RequiredStructureInvertShift, 9);  // lower the structure side by 0.3
    }

    [Fact]
    public void IncomingPipe_FeasibleUsesCoverCeilingBoundedByMaxSlope()
    {
        var fit = ElevationMath.FitFarEnd(100, 101, 100, FlowRelation.Incoming, 0.005, 0.02);
        Assert.True(fit.Feasible);
        Assert.Equal(101, fit.FarInvert, 9);     // cover ceiling 101 < max-slope 102
        Assert.Equal(0.01, fit.Slope, 9);

        var steep = ElevationMath.FitFarEnd(100, 105, 100, FlowRelation.Incoming, 0.005, 0.02);
        Assert.True(steep.Feasible);
        Assert.Equal(102, steep.FarInvert, 9);   // max slope caps it
    }

    [Fact]
    public void ZeroLength_IsReportedNotClamped()
    {
        var fit = ElevationMath.FitFarEnd(100, 99, 0, FlowRelation.Outgoing, null, null);
        Assert.False(fit.Feasible);
    }

    private static ElevationMath.StructureSideInput In(string id, double maxInv, FlowRelation rel, double h = H, bool gov = false)
        => new() { Id = id, MaxInvert = maxInv, Relation = rel, InnerHeight = h, IsGoverning = gov };

    [Fact]
    public void MatchCrowns_SameDiameter_AllInvertsEqualMostRestrictive()
    {
        var r = ElevationMath.ReconcileAtStructure(
            new[] { In("A", 95.0, FlowRelation.Incoming), In("B", 94.2, FlowRelation.Incoming), In("C", 96.0, FlowRelation.Outgoing) },
            OffsetMode.MatchCrowns, 0, null);
        Assert.Equal("B", r.GoverningId);
        Assert.All(r.InvertById.Values, v => Assert.Equal(94.2, v, 9));
        Assert.Empty(r.CoverViolations);
    }

    [Fact]
    public void MatchCrowns_MixedHeights_EqualisesCrowns()
    {
        var r = ElevationMath.ReconcileAtStructure(
            new[] { In("A", 95.0, FlowRelation.Incoming, h: 1.0), In("B", 94.5, FlowRelation.Outgoing, h: 2.0) },
            OffsetMode.MatchCrowns, 0, null);
        // crown ceilings: A 96.0, B 96.5 → A governs (lower crown); B invert = 96.0 − 2.0
        Assert.Equal("A", r.GoverningId);
        Assert.Equal(95.0, r.InvertById["A"], 9);
        Assert.Equal(94.0, r.InvertById["B"], 9);
        Assert.Empty(r.CoverViolations);           // B sits below its own 94.5 ceiling
    }

    [Fact]
    public void ManualDrop_OutgoingSitsBelowLowestIncoming()
    {
        var r = ElevationMath.ReconcileAtStructure(
            new[] { In("A", 100.0, FlowRelation.Incoming), In("B", 100.5, FlowRelation.Incoming), In("C", 100.4, FlowRelation.Outgoing) },
            OffsetMode.ManualDrop, 0.2, null);
        Assert.Equal(100.0, r.InvertById["A"], 9);
        Assert.Equal(100.0, r.InvertById["B"], 9);
        Assert.Equal(99.8, r.InvertById["C"], 9);
        Assert.Empty(r.CoverViolations);
    }

    [Fact]
    public void ExplicitGoverningPipeAndOverrideAreHonoured()
    {
        var r = ElevationMath.ReconcileAtStructure(
            new[] { In("A", 95.0, FlowRelation.Incoming), In("B", 94.2, FlowRelation.Outgoing, gov: true) },
            OffsetMode.MatchInverts, 0, 93.0);
        Assert.Equal("B", r.GoverningId);
        Assert.All(r.InvertById.Values, v => Assert.Equal(93.0, v, 9));
        Assert.Empty(r.CoverViolations);
    }

    [Fact]
    public void OverrideAboveCeiling_IsFlaggedNotSilentlyClamped()
    {
        var r = ElevationMath.ReconcileAtStructure(
            new[] { In("A", 95.0, FlowRelation.Incoming), In("B", 94.2, FlowRelation.Outgoing) },
            OffsetMode.MatchInverts, 0, 96.0);
        Assert.Equal(2, r.CoverViolations.Count);
    }
}
