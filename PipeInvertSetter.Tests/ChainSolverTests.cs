using PipeInvertSetter;
using Xunit;
using static PipeInvertSetter.ChainSolver;

public class ChainSolverTests
{
    private const double H = 1.5, Wall = 0.2, Cover = 3.0;

    // flat surface at 100 → invert ceiling 95.3 everywhere
    private static ChainPipe Pipe(string id, double len = 100, double? min = 0.005, double? max = null, double h = H,
                                  double upSurf = 100, double downSurf = 100)
        => new()
        {
            Id = id, Length = len, InnerHeight = h, MinSlope = min, MaxSlope = max,
            UpCeiling = ElevationMath.MaxInvertForCover(upSurf, Cover, h, Wall),
            DownCeiling = ElevationMath.MaxInvertForCover(downSurf, Cover, h, Wall),
        };

    [Fact]
    public void FlatRun_FollowsMinSlopeFromShallowestStart()
    {
        var r = Solve(new[] { Pipe("A"), Pipe("B") }, OffsetMode.MatchInverts, 0);
        Assert.True(r.Feasible);
        Assert.Equal(95.3, r.Up[0], 6);
        Assert.Equal(94.8, r.Down[0], 6);
        Assert.Equal(94.8, r.Up[1], 6);      // inverts matched through the structure
        Assert.Equal(94.3, r.Down[1], 6);
        Assert.Equal(0.005, r.Slope[0], 9);
    }

    [Fact]
    public void HeldOutfall_PropagatesBackwardsThroughMaxSlope()
    {
        var b = Pipe("B", max: 0.02); b.FixedDown = 90.0;
        var r = Solve(new[] { Pipe("A", max: 0.02), b }, OffsetMode.MatchInverts, 0);
        Assert.True(r.Feasible);
        Assert.Equal(90.0, r.Down[1], 6);
        Assert.Equal(92.0, r.Up[1], 6);      // max slope 2 % over 100 ft
        Assert.Equal(92.0, r.Down[0], 6);
        Assert.Equal(94.0, r.Up[0], 6);      // limited by max slope, below the 95.3 cover ceiling
    }

    [Fact]
    public void TieInAboveCeiling_IsAConflictNotAClamp()
    {
        var b = Pipe("B"); b.FixedDown = 96.0;   // above the 95.3 cover ceiling
        var r = Solve(new[] { Pipe("A"), b }, OffsetMode.MatchInverts, 0);
        Assert.False(r.Feasible);
        Assert.Equal("fixed_invert", r.Conflicts[0].Kind);
        Assert.Equal(1, r.Conflicts[0].PipeIndex);
    }

    [Fact]
    public void TieInReachableOnlyByMinSlope_IsAConflict()
    {
        // downstream fixed at 95.0 but the shallowest start (95.3) minus min slope over 100 ft is 94.8
        var b = Pipe("B"); b.FixedDown = 95.0;
        var r = Solve(new[] { Pipe("A"), b }, OffsetMode.MatchInverts, 0);
        Assert.False(r.Feasible);
        Assert.Equal(95.0 - 94.3, r.Conflicts[0].Delta, 6);
    }

    [Fact]
    public void MatchCrowns_SizeIncrease_DropsOutgoingInvert()
    {
        var r = Solve(new[] { Pipe("A", h: 1.5), Pipe("B", h: 2.0) }, OffsetMode.MatchCrowns, 0);
        Assert.True(r.Feasible);
        Assert.Equal(0.5, r.Drop[1], 9);
        Assert.Equal(r.Down[0] - 0.5, r.Up[1], 9);
    }

    [Fact]
    public void ManualDrop_AppliesAtEveryInteriorStructure()
    {
        var r = Solve(new[] { Pipe("A"), Pipe("B"), Pipe("C") }, OffsetMode.ManualDrop, 0.1);
        Assert.True(r.Feasible);
        Assert.Equal(r.Down[0] - 0.1, r.Up[1], 9);
        Assert.Equal(r.Down[1] - 0.1, r.Up[2], 9);
    }

    [Fact]
    public void InteriorHump_LowersDownstreamEndToKeepCoverAlongLength()
    {
        var a = Pipe("A");
        a.Samples.Add((0.5, 94.0));                 // ground dips mid-pipe: ceiling 94 at t = 0.5
        var r = Solve(new[] { a }, OffsetMode.MatchInverts, 0);
        Assert.True(r.Feasible);
        Assert.Equal(95.3, r.Up[0], 6);
        Assert.Equal(92.7, r.Down[0], 6);           // 0.5·95.3 + 0.5·y ≤ 94 → y ≤ 92.7
    }

    [Fact]
    public void HumpNearUpstream_LowersUpstreamViaBackwardPass()
    {
        var a = Pipe("A", max: 0.01);
        a.Samples.Add((0.1, 90.0));                 // deep dip right after the upstream structure
        var r = Solve(new[] { a }, OffsetMode.MatchInverts, 0);
        Assert.True(r.Feasible);
        // 0.9·x + 0.1·y ≤ 90 with y ≥ x − 1 (max slope) → x ≤ (90 + 0.1) / 1.0 = 90.1
        Assert.Equal(90.1, r.Up[0], 5);
        Assert.Equal(89.1, r.Down[0], 5);
    }

    [Fact]
    public void ContradictorySlopeBounds_AreReportedUpFront()
    {
        var r = Solve(new[] { Pipe("A", min: 0.02, max: 0.01) }, OffsetMode.MatchInverts, 0);
        Assert.False(r.Feasible);
        Assert.Equal("slope_bounds", r.Conflicts[0].Kind);
    }
}
