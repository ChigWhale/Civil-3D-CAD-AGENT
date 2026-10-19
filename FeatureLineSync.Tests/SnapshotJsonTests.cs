using FeatureLineSync;
using Xunit;

namespace FeatureLineSync.Tests;

public class SnapshotJsonTests
{
    [Fact]
    public void RoundTripKeepsGeometryAndMetadata()
    {
        var line = TestLines.Straight("2A3F", (0, 0, 100), (10, 0, 101)).WithElevationPoint(0, 0.25, 100.3);
        line.Name = "PAD-12-N"; line.Site = "Grading"; line.Layer = "C-GRAD"; line.LayerLocked = true;
        var snap = TestLines.Snap(line);
        snap.Source = @"C:\proj\before.dwg";
        snap.Skipped.Add("1F: dynamic");

        FlSnapshot back = FlSnapshot.FromJson(snap.ToJson());

        FlLine b = Assert.Single(back.Lines);
        Assert.Equal("2A3F", b.Handle);
        Assert.Equal("PAD-12-N", b.Name);
        Assert.Equal("Grading", b.Site);
        Assert.True(b.LayerLocked);
        Assert.Equal(2, b.Pis.Count);
        Assert.Equal(101.0, b.Pis[1][2]);
        Assert.Equal(0.25, Assert.Single(b.ElevationPoints).Fraction);
        Assert.Equal(10.0, Assert.Single(b.SegLengths2D));
        Assert.Equal(@"C:\proj\before.dwg", back.Source);
        Assert.Single(back.Skipped);
    }

    [Fact]
    public void UnsupportedSchemaIsRejected()
    {
        Assert.Throws<InvalidOperationException>(() => FlSnapshot.FromJson("{\"schema\": 2, \"lines\": []}"));
    }
}
