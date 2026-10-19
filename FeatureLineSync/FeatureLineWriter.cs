using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.Civil;
using Autodesk.Civil.DatabaseServices;

namespace FeatureLineSync
{
    public sealed class ApplyReport
    {
        public bool Applied { get; set; }
        public int LinesWritten { get; set; }
        public int LinesFailed { get; set; }
        public int PointsSet { get; set; }
        public int PointsInserted { get; set; }
        public List<string> Errors { get; } = new();
        /// <summary>Per-line failures: that line was rolled back, the others were written.</summary>
        public List<string> LineFailures { get; } = new();
        /// <summary>Post-write differences between the plan and what Civil 3D reports.</summary>
        public List<string> Differences { get; } = new();
    }

    /// <summary>
    /// Writes a SyncPlan to the drawing: one document lock, one outer transaction (one undo step),
    /// a nested transaction per line so a line that Civil 3D refuses is rolled back on its own.
    /// Elevations are always set through Civil 3D's own point list (re-read after any rebuild):
    /// each planned point is matched by XY to the nearest point of GetPoints(AllPoints) and set by that index.
    /// </summary>
    public static class FeatureLineWriter
    {
        public const double VerifyTolerance = 0.005;
        public const double MatchTolerance = 0.05;

        public static ApplyReport Apply(Document doc, SyncPlan plan)
        {
            var rep = new ApplyReport();
            Database db = doc.Database;
            using DocumentLock lk = doc.LockDocument();
            using Transaction tr = db.TransactionManager.StartTransaction();
            var written = new List<LinePlan>();
            try
            {
                foreach (LinePlan lp in plan.Lines.Where(l => l.Write).OrderBy(l => l.Pass))
                {
                    if (!TryHandle(db, lp.Target.Handle, out ObjectId id))
                    {
                        rep.LineFailures.Add($"{lp.Target.Handle} '{lp.Target.Name}': handle not found in the drawing");
                        rep.LinesFailed++;
                        continue;
                    }
                    using Transaction ltr = db.TransactionManager.StartTransaction();
                    try
                    {
                        var fl = (FeatureLine)ltr.GetObject(id, OpenMode.ForWrite);
                        if (lp.RebuildElevationPoints) rep.PointsInserted += RebuildElevationPoints(fl, lp);
                        int set = SetElevations(fl, lp, out List<string> missing);
                        if (missing.Count > 0)
                            throw new InvalidOperationException($"{missing.Count} planned point(s) not found on the line: {string.Join("; ", missing.Take(3))}");
                        ltr.Commit();
                        rep.PointsSet += set;
                        rep.LinesWritten++;
                        written.Add(lp);
                    }
                    catch (System.Exception ex)
                    {
                        ltr.Abort();
                        rep.LineFailures.Add($"{lp.Target.Handle} '{lp.Target.Name}': {ex.Message} (line left unchanged)");
                        rep.LinesFailed++;
                    }
                }

                // verify what Civil 3D now reports (same-site crossing propagation can overwrite values)
                foreach (LinePlan lp in written)
                {
                    if (!TryHandle(db, lp.Target.Handle, out ObjectId id)) continue;
                    var fl = (FeatureLine)tr.GetObject(id, OpenMode.ForRead);
                    rep.Differences.AddRange(Verify(fl, lp));
                }
            }
            catch (System.Exception ex)
            {
                rep.Errors.Add("Write failed: " + ex.Message);
            }

            if (rep.Errors.Count > 0)
            {
                tr.Abort();
                rep.Errors.Add("Transaction rolled back; the drawing was not modified.");
                return rep;
            }
            tr.Commit();
            rep.Applied = true;
            return rep;
        }

        /// <summary>Delete the line's elevation points and insert the planned ones on the target geometry.</summary>
        private static int RebuildElevationPoints(FeatureLine fl, LinePlan lp)
        {
            Point3dCollection existing = fl.GetPoints(FeatureLinePointType.ElevationPoint);
            foreach (Point3d p in existing) fl.DeleteElevationPoint(p);

            int inserted = 0;
            foreach (AlignedPoint ap in lp.Points.Where(p => !p.IsPI))
            {
                InsertWithNudge(fl, lp, ap);
                inserted++;
            }
            return inserted;
        }

        private static void InsertWithNudge(FeatureLine fl, LinePlan lp, AlignedPoint ap)
        {
            double z = ap.NewZ ?? ap.CopyZ;
            if (TryInsert(fl, new Point3d(ap.X, ap.Y, z))) return;
            foreach (double nudge in new[] { 1e-4, -1e-4, 1e-3, -1e-3 })
            {
                var (x, y) = FlGeometry.PointOn(lp.Target, ap.Segment, ap.Fraction + nudge);
                if (TryInsert(fl, new Point3d(x, y, z))) { ap.X = x; ap.Y = y; return; }
            }
            throw new InvalidOperationException($"could not insert elevation point #{ap.Index} at ({ap.X:0.00}, {ap.Y:0.00})");
        }

        private static bool TryInsert(FeatureLine fl, Point3d p)
        {
            try { fl.InsertElevationPoint(p); return true; }
            catch { return false; }
        }

        /// <summary>Set every planned elevation using the XY Civil 3D reports for the point.</summary>
        private static int SetElevations(FeatureLine fl, LinePlan lp, out List<string> missing)
        {
            missing = new List<string>();
            Point3dCollection pts = fl.GetPoints(FeatureLinePointType.AllPoints);
            var actual = new List<Point3d>();
            foreach (Point3d p in pts) actual.Add(p);

            int set = 0;
            foreach (AlignedPoint ap in lp.Points)
            {
                if (ap.NewZ is not double z) continue;
                int k = Nearest(actual, ap.X, ap.Y, out double d);
                if (k < 0 || d > MatchTolerance) { missing.Add($"#{ap.Index} ({ap.X:0.00}, {ap.Y:0.00})"); continue; }
                fl.SetPointElevation(k, z);          // index into GetPoints(AllPoints)
                set++;
            }

            // safety net: read back; a point that did not take the index write is set again by location
            Point3dCollection after = fl.GetPoints(FeatureLinePointType.AllPoints);
            var now = new List<Point3d>();
            foreach (Point3d p in after) now.Add(p);
            foreach (AlignedPoint ap in lp.Points)
            {
                if (ap.NewZ is not double z) continue;
                int k = Nearest(now, ap.X, ap.Y, out double d);
                if (k < 0 || d > MatchTolerance) continue;
                if (Math.Abs(now[k].Z - z) <= VerifyTolerance) continue;
                try { fl.SetPointRelativeElevation(now[k], false, z); } catch { /* verification reports it */ }
            }
            return set;
        }

        private static List<string> Verify(FeatureLine fl, LinePlan lp)
        {
            var diffs = new List<string>();
            Point3dCollection pts = fl.GetPoints(FeatureLinePointType.AllPoints);
            var actual = new List<Point3d>();
            foreach (Point3d p in pts) actual.Add(p);
            var used = new HashSet<int>();
            foreach (AlignedPoint ap in lp.Points)
            {
                if (ap.NewZ is not double z) continue;
                int k = Nearest(actual, ap.X, ap.Y, out double d);
                if (k < 0 || d > MatchTolerance) { diffs.Add($"{lp.Target.Handle} #{ap.Index}: point missing after write"); continue; }
                used.Add(k);
                if (Math.Abs(actual[k].Z - z) > VerifyTolerance)
                    diffs.Add($"{lp.Target.Handle} #{ap.Index} ({ap.X:0.00}, {ap.Y:0.00}): site_propagation planned {z:0.000} drawing {actual[k].Z:0.000}");
            }
            int extra = actual.Count - used.Count;
            if (extra > 0) diffs.Add($"{lp.Target.Handle}: extra_point {extra} point(s) on the line not in the plan (crossing points?)");
            int planned = lp.Points.Count(p => !p.IsPI);
            int have = FeatureLineReader.Safe(() => fl.ElevationPointsCount, planned);
            if (have != planned) diffs.Add($"{lp.Target.Handle}: duplicate_points elevation points {have}, planned {planned}");
            return diffs;
        }

        private static int Nearest(List<Point3d> pts, double x, double y, out double dist)
        {
            int best = -1; dist = double.MaxValue;
            for (int i = 0; i < pts.Count; i++)
            {
                double d = FlGeometry.Dist2D(pts[i].X, pts[i].Y, x, y);
                if (d < dist) { dist = d; best = i; }
            }
            return best;
        }

        public static bool TryHandle(Database db, string key, out ObjectId id)
        {
            id = ObjectId.Null;
            string k = key.Trim();
            if (k.StartsWith("0x", StringComparison.OrdinalIgnoreCase)) k = k.Substring(2);
            if (!long.TryParse(k, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out long h)) return false;
            return db.TryGetObjectId(new Handle(h), out id) && !id.IsNull && !id.IsErased;
        }
    }
}
