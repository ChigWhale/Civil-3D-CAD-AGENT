using FeatureLineSync;
using Xunit;

namespace FeatureLineSync.Tests;

public class SyncPlannerTests
{
    private static double[] NewZ(LinePlan lp) => lp.Points.Select(p => p.NewZ ?? double.NaN).ToArray();

    [Fact]
    public void TwoConsistentAnchorsReproduceCopyExactly()
    {
        var copy = TestLines.Straight("A", (0, 0, 100), (10, 0, 101), (20, 0, 103));
        var target = TestLines.Straight("A", (0, 0, 0), (10, 0, 0), (20, 0, 0));
        var plan = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), TestLines.ProbeAt((0, 0, 100), (20, 0, 103)), new PlannerOptions());

        LinePlan lp = Assert.Single(plan.Lines);
        Assert.Equal("ok", lp.Status);
        Assert.Equal("handle", lp.MatchedBy);
        Assert.Equal(new[] { 100.0, 101.0, 103.0 }, NewZ(lp).Select(v => Math.Round(v, 9)));
        Assert.Equal(0.0, Assert.Single(lp.Runs).Misclosure, 9);
        Assert.True(lp.Write);
        Assert.Equal(1, lp.Pass);
    }

    [Fact]
    public void ChangedLengthsSpreadMisclosureLinearly()
    {
        var copy = TestLines.Straight("A", (0, 0, 100), (10, 0, 101), (20, 0, 103));      // grades 0.1, 0.2
        var target = TestLines.Straight("A", (0, 0, 0), (12, 0, 0), (20, 0, 0));          // lengths 12, 8
        var plan = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), TestLines.ProbeAt((0, 0, 100), (20, 0, 103)), new PlannerOptions());

        LinePlan lp = Assert.Single(plan.Lines);
        // forward: 101.2, 102.8 ; misclosure 0.2 spread by 12/20 and 20/20
        double[] z = NewZ(lp);
        Assert.Equal(100.0, z[0], 9);
        Assert.Equal(101.32, z[1], 9);
        Assert.Equal(103.0, z[2], 9);
        Assert.Equal(0.2, Assert.Single(lp.Runs).Misclosure, 9);
    }

    [Fact]
    public void SingleAnchorAtStartPropagatesForward()
    {
        var copy = TestLines.Straight("A", (0, 0, 100), (10, 0, 101), (20, 0, 103));
        var target = TestLines.Straight("A", (0, 0, 0), (10, 0, 0), (20, 0, 0));
        var plan = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), TestLines.ProbeAt((0, 0, 50)), new PlannerOptions());
        Assert.Equal(new[] { 50.0, 51.0, 53.0 }, NewZ(Assert.Single(plan.Lines)).Select(v => Math.Round(v, 9)));
    }

    [Fact]
    public void SingleAnchorAtEndPropagatesBackward()
    {
        var copy = TestLines.Straight("A", (0, 0, 100), (10, 0, 101), (20, 0, 103));
        var target = TestLines.Straight("A", (0, 0, 0), (10, 0, 0), (20, 0, 0));
        var plan = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), TestLines.ProbeAt((20, 0, 53)), new PlannerOptions());
        Assert.Equal(new[] { 50.0, 51.0, 53.0 }, NewZ(Assert.Single(plan.Lines)).Select(v => Math.Round(v, 9)));
    }

    [Fact]
    public void GradesAreKeptWhenTargetLengthsScale()
    {
        var copy = TestLines.Straight("A", (0, 0, 100), (10, 0, 101), (20, 0, 103));
        var target = TestLines.Straight("A", (0, 0, 0), (20, 0, 0), (40, 0, 0));
        var plan = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), TestLines.ProbeAt((0, 0, 100)), new PlannerOptions());
        LinePlan lp = Assert.Single(plan.Lines);
        Assert.Equal(new[] { 100.0, 102.0, 106.0 }, NewZ(lp).Select(v => Math.Round(v, 9)));
        Assert.Equal(lp.MinGradeBefore!.Value, lp.MinGradeAfter!.Value, 9);
        Assert.Equal(lp.MaxGradeBefore!.Value, lp.MaxGradeAfter!.Value, 9);
    }

    [Fact]
    public void ClosedRingWithOneAnchorClosesTheLoop()
    {
        var copy = TestLines.Closed("A", (0, 0, 0), (10, 0, 1), (10, 10, 2), (0, 10, 1));
        var target = TestLines.Closed("A", (0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0));
        var plan = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), TestLines.ProbeAt((10, 10, 12)), new PlannerOptions());
        LinePlan lp = Assert.Single(plan.Lines);
        Assert.True(lp.Closed);
        Assert.Equal(new[] { 10.0, 11.0, 12.0, 11.0 }, NewZ(lp).Select(v => Math.Round(v, 9)));
        Assert.Equal(0.0, Assert.Single(lp.Runs).Misclosure, 9);
    }

    [Fact]
    public void ClosedRingWithStretchedSideSpreadsMisclosure()
    {
        var copy = TestLines.Closed("A", (0, 0, 0), (10, 0, 1), (10, 10, 2), (0, 10, 1));
        var target = TestLines.Closed("A", (0, 0, 0), (20, 0, 0), (10, 10, 0), (0, 10, 0));   // sides 20, sqrt(200), 10, 10
        var plan = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), TestLines.ProbeAt((0, 0, 0)), new PlannerOptions());
        LinePlan lp = Assert.Single(plan.Lines);
        double[] z = NewZ(lp);
        double d = Math.Sqrt(200), total = 40 + d;
        // raw forward with grades .1,.1,-.1,-.1: 2, 2+d/10, 1+d/10, d/10 -> misclosure -d/10 spread by cumulative distance
        double delta = -d / 10;
        Assert.Equal(0.0, z[0], 9);
        Assert.Equal(2 + delta * 20 / total, z[1], 9);
        Assert.Equal(2 + d / 10 + delta * (20 + d) / total, z[2], 9);
        Assert.Equal(1 + d / 10 + delta * (30 + d) / total, z[3], 9);
        Assert.Equal(delta, Assert.Single(lp.Runs).Misclosure, 9);
    }

    [Fact]
    public void LinesWithNoPointOnTheSurfaceAreLeftAlone()
    {
        // A starts on the road; B shares A's end but has no point on the surface and is all zero -> untouched
        var cA = TestLines.Straight("A", (0, 0, 100), (10, 0, 102));
        var cB = TestLines.Straight("B", (10, 0, 102), (10, 10, 102.2));
        var tA = TestLines.Straight("A", (0, 0, 0), (12, 0, 0));
        var tB = TestLines.Straight("B", (12, 0, 0), (12, 10, 0));
        var plan = SyncPlanner.Plan(TestLines.Snap(tA, tB), TestLines.Snap(cA, cB), TestLines.ProbeAt((0, 0, 90)), new PlannerOptions());

        LinePlan a = plan.Lines[0], b = plan.Lines[1];
        Assert.Equal("ok", a.Status);
        Assert.Equal(90 + 0.2 * 12, NewZ(a)[1], 9);
        Assert.Equal("unanchored", b.Status);
        Assert.False(b.Write);
        Assert.Equal(1, plan.LinesToWrite);
    }

    [Fact]
    public void ExistingNonZeroElevationsAreKeptAsFixedPoints()
    {
        // nothing on the surface; point 0 still has 700 from before the move, points 1 and 2 were reset to 0
        var copy = TestLines.Straight("A", (0, 0, 100), (10, 0, 101), (20, 0, 103));     // slopes 0.1, 0.2
        var target = TestLines.Straight("A", (0, 0, 700), (10, 0, 0), (20, 0, 0));
        var keep = new PlannerOptions { KeepExistingElevations = true };
        var plan = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), TestLines.ProbeAt(), keep);

        LinePlan lp = Assert.Single(plan.Lines);
        Assert.Equal("ok", lp.Status);
        Assert.Equal("existing", lp.Points[0].AnchorSource);
        Assert.Equal(1, lp.ExistingAnchorCount);
        Assert.Equal(new[] { 700.0, 701.0, 703.0 }, NewZ(lp).Select(v => Math.Round(v, 9)));

        // the surface wins over an existing value at the same point
        var plan2 = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), TestLines.ProbeAt((0, 0, 650)), keep);
        Assert.Equal("surface", plan2.Lines[0].Points[0].AnchorSource);
        Assert.Equal(650.0, NewZ(plan2.Lines[0])[0], 9);

        // default: only the surface fixes points, a stray non-zero value is not trusted
        var plan3 = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), TestLines.ProbeAt(), new PlannerOptions());
        Assert.Equal("unanchored", plan3.Lines[0].Status);
        var plan4 = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), TestLines.ProbeAt((20, 0, 500)), new PlannerOptions());
        Assert.Equal(new[] { 497.0, 498.0, 500.0 }, NewZ(plan4.Lines[0]).Select(v => Math.Round(v, 9)));   // 700 overwritten
    }

    [Fact]
    public void SlopesKeepTheirSignInBothDirectionsFromTheContactPoint()
    {
        // pad edge: road contact in the middle; copy falls 2% to the left and rises 5% to the right of it
        var copy = TestLines.Straight("A", (0, 0, 99.8), (10, 0, 100), (20, 0, 100.5), (30, 0, 101));   // -0.02, +0.05, +0.05
        var target = TestLines.Straight("A", (0, 0, 0), (12, 0, 0), (22, 0, 0), (36, 0, 0));           // lengths 12, 10, 14
        var plan = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), TestLines.ProbeAt((12, 0, 650)), new PlannerOptions());
        double[] z = NewZ(Assert.Single(plan.Lines));
        Assert.Equal(650 - 0.02 * 12, z[0], 9);
        Assert.Equal(650.0, z[1], 9);
        Assert.Equal(650 + 0.05 * 10, z[2], 9);
        Assert.Equal(650 + 0.05 * 10 + 0.05 * 14, z[3], 9);
    }

    [Fact]
    public void DisagreeingSharedPointIsReportedButNotChanged()
    {
        // both lines fixed at their road end, both meet at (10,10) with different slopes after the move
        var cA = TestLines.Straight("A", (0, 0, 0), (10, 10, 1.4142135623730951));      // slope 0.1 over sqrt(200)
        var cB = TestLines.Straight("B", (20, 0, 0), (10, 10, 0.7071067811865476));     // slope 0.05
        var tA = TestLines.Straight("A", (0, 0, 0), (10, 10, 0));
        var tB = TestLines.Straight("B", (20, 0, 0), (10, 10, 0));
        var plan = SyncPlanner.Plan(TestLines.Snap(tA, tB), TestLines.Snap(cA, cB), TestLines.ProbeAt((0, 0, 0), (20, 0, 0)), new PlannerOptions());

        Assert.Single(plan.Conflicts);
        Assert.Equal(1.4142135623730951, NewZ(plan.Lines[0])[1], 9);
        Assert.Equal(0.7071067811865476, NewZ(plan.Lines[1])[1], 9);
    }

    [Fact]
    public void MissingElevationPointsAreRebuiltOnTargetGeometry()
    {
        var copy = TestLines.Straight("A", (0, 0, 100), (10, 0, 101)).WithElevationPoint(0, 0.5, 100.2);
        var target = TestLines.Straight("A", (0, 0, 0), (20, 0, 0));
        var plan = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), TestLines.ProbeAt((0, 0, 100)), new PlannerOptions());
        LinePlan lp = Assert.Single(plan.Lines);
        Assert.True(lp.RebuildElevationPoints);
        Assert.Equal(3, lp.Points.Count);
        AlignedPoint ep = lp.Points[1];
        Assert.False(ep.IsPI);
        Assert.Equal(10.0, ep.X, 9);
        Assert.Equal(0.0, ep.Y, 9);
        // grade 0.04 over 10, then 0.16 over 10
        Assert.Equal(100.4, NewZ(lp)[1], 9);
        Assert.Equal(102.0, NewZ(lp)[2], 9);
    }

    [Fact]
    public void ExistingElevationPointsAtSameFractionAreReused()
    {
        var copy = TestLines.Straight("A", (0, 0, 100), (10, 0, 101)).WithElevationPoint(0, 0.5, 100.2);
        var target = TestLines.Straight("A", (0, 0, 0), (20, 0, 0)).WithElevationPoint(0, 0.501, 0);
        var plan = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), TestLines.ProbeAt((0, 0, 100)), new PlannerOptions());
        LinePlan lp = Assert.Single(plan.Lines);
        Assert.False(lp.RebuildElevationPoints);
        Assert.Equal(target.ElevationPoints[0].X, lp.Points[1].X, 9);
    }

    [Fact]
    public void PiCountMismatchFallsBackToNormalizedStation()
    {
        var copy = TestLines.Straight("A", (0, 0, 100), (10, 0, 101), (20, 0, 103));
        var target = TestLines.Straight("A", (0, 0, 0), (30, 0, 0));
        var plan = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), TestLines.ProbeAt((0, 0, 100)), new PlannerOptions());
        LinePlan lp = Assert.Single(plan.Lines);
        Assert.Contains(lp.Warnings, w => w.Contains("PI count differs"));
        Assert.Equal(2, lp.Points.Count);
        Assert.Equal(103.0, lp.Points[1].CopyZ, 9);
        // average copy grade 3/20 = 0.15 over 30 units
        Assert.Equal(104.5, NewZ(lp)[1], 9);
    }

    [Fact]
    public void ElevationsModeCopiesByIndex()
    {
        var copy = TestLines.Straight("A", (0, 0, 100), (10, 0, 101), (20, 0, 103));
        var target = TestLines.Straight("A", (0, 0, 0), (12, 0, 0), (20, 0, 0));
        var plan = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), null, new PlannerOptions { Mode = SyncMode.Elevations });
        LinePlan lp = Assert.Single(plan.Lines);
        Assert.Equal(new[] { 100.0, 101.0, 103.0 }, NewZ(lp));
        Assert.True(lp.Write);
        Assert.Empty(lp.Points.Where(p => p.AnchorZ.HasValue));
    }

    [Fact]
    public void MatchesByNameWhenHandleDiffersAndReportsUnmatched()
    {
        var copy = TestLines.Straight("A1", (0, 0, 100), (10, 0, 101));
        copy.Name = "PAD-1";
        var extra = TestLines.Straight("ZZ", (50, 0, 1), (60, 0, 2));
        var target = TestLines.Straight("B7", (0, 0, 0), (10, 0, 0));
        target.Name = "PAD-1";
        var orphan = TestLines.Straight("C3", (70, 0, 0), (80, 0, 0));
        orphan.Name = "";
        var plan = SyncPlanner.Plan(TestLines.Snap(target, orphan), TestLines.Snap(copy, extra), TestLines.ProbeAt((0, 0, 5)), new PlannerOptions());
        Assert.Equal("name", plan.Lines[0].MatchedBy);
        Assert.Equal("unmatched", plan.Lines[1].Status);
        Assert.False(plan.Lines[1].Write);
        Assert.Single(plan.UnmatchedInCopy);
        Assert.Contains("ZZ", plan.UnmatchedInCopy[0]);
    }

    [Fact]
    public void LockedLayerIsReportedAndSkipped()
    {
        var copy = TestLines.Straight("A", (0, 0, 100), (10, 0, 101));
        var target = TestLines.Straight("A", (0, 0, 0), (10, 0, 0));
        target.LayerLocked = true;
        var plan = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), TestLines.ProbeAt((0, 0, 5)), new PlannerOptions());
        Assert.Equal("locked_layer", Assert.Single(plan.Lines).Status);

    }

    [Fact]
    public void AllPointsOnTheSurfaceMeansNoSlopeIsUsed()
    {
        var copy = TestLines.Straight("A", (0, 0, 100), (10, 0, 101), (20, 0, 103));
        var target = TestLines.Straight("A", (0, 0, 0), (10, 0, 0), (20, 0, 0));
        var plan = SyncPlanner.Plan(TestLines.Snap(target), TestLines.Snap(copy), (x, y) => 5.0 + x, new PlannerOptions());
        LinePlan lp = Assert.Single(plan.Lines);
        Assert.Equal(3, lp.AnchorCount);
        Assert.Equal(new[] { 5.0, 15.0, 25.0 }, NewZ(lp).Select(v => Math.Round(v, 9)));
        Assert.Contains(lp.Warnings, w => w.Contains("every point is fixed"));
        Assert.Contains(plan.Warnings, w => w.Contains("probably not the corridor surface"));
    }

    [Fact]
    public void PropagateRequiresAnAnchor()
    {
        Assert.Throws<ArgumentException>(() => SyncPlanner.Propagate(new[] { 0.1 }, new[] { 10.0 }, new Dictionary<int, double>(), false));
    }
}
