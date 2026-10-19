using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.Civil;
using Autodesk.Civil.DatabaseServices;

namespace FeatureLineSync
{
    /// <summary>
    /// Reads every editable feature line of a drawing into an FlSnapshot.
    /// Works on the active document, on another open document, or on a side database
    /// (the copy drawing read from disk without opening it).
    /// </summary>
    public static class FeatureLineReader
    {
        public const string EditableRxClass = "AeccDbFeatureLine";

        public static FlSnapshot ReadHost(Document doc)
        {
            using DocumentLock lk = doc.LockDocument();
            using Transaction tr = doc.Database.TransactionManager.StartTransaction();
            FlSnapshot snap = Read(doc.Database, tr, doc.Name);
            tr.Commit();
            return snap;
        }

        /// <summary>Read a .dwg from disk as a side database. The copy must not be open in this session.</summary>
        public static FlSnapshot ReadSide(string path)
        {
            Database original = HostApplicationServices.WorkingDatabase;
            using var db = new Database(false, true);
            db.ReadDwgFile(path, FileOpenMode.OpenForReadAndAllShare, true, null);
            db.CloseInput(true);
            try
            {
                HostApplicationServices.WorkingDatabase = db;
                using Transaction tr = db.TransactionManager.StartTransaction();
                FlSnapshot snap = Read(db, tr, path);
                tr.Commit();
                return snap;
            }
            finally
            {
                HostApplicationServices.WorkingDatabase = original;
            }
        }

        public static FlSnapshot Read(Database db, Transaction tr, string source)
        {
            var snap = new FlSnapshot { Source = source };
            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
            foreach (ObjectId id in ms)
            {
                if (id.IsErased) continue;
                Autodesk.AutoCAD.DatabaseServices.DBObject? obj;
                try { obj = tr.GetObject(id, OpenMode.ForRead, false); }
                catch { continue; }
                if (obj is not FeatureLine fl) continue;
                try
                {
                    FlLine? line = ReadOne(tr, fl, out string? skip);
                    if (line != null) snap.Lines.Add(line);
                    else if (skip != null) snap.Skipped.Add(skip);
                }
                catch (System.Exception ex)
                {
                    snap.Skipped.Add($"{fl.Handle}: read failed: {ex.Message}");
                }
            }
            return snap;
        }

        public static FlLine? ReadOne(Transaction tr, FeatureLine fl, out string? skipReason)
        {
            skipReason = null;
            string rx = fl.GetRXClass().Name;
            string name = Safe(() => fl.Name, "");
            if (!string.Equals(rx, EditableRxClass, StringComparison.OrdinalIgnoreCase))
            {
                skipReason = $"{fl.Handle} '{name}': {rx} is not an editable feature line (dynamic/corridor)";
                return null;
            }
            if (Safe(() => fl.IsReferenceObject, false))
            {
                skipReason = $"{fl.Handle} '{name}': data reference, read-only";
                return null;
            }

            FlLine line = ReadGeometry(fl);
            line.Site = Safe(() =>
            {
                ObjectId sid = fl.SiteId;
                if (sid.IsNull) return "";
                return tr.GetObject(sid, OpenMode.ForRead) is Site site ? site.Name : "";
            }, "");
            line.LayerLocked = Safe(() => tr.GetObject(fl.LayerId, OpenMode.ForRead) is LayerTableRecord ltr && ltr.IsLocked, false);
            return line;
        }

        /// <summary>
        /// Geometry and identity of any feature line (editable, corridor or reference): PIs, bulges, closure,
        /// elevation points. No site / layer-lock lookups, so no transaction is needed beyond the open object.
        /// </summary>
        public static FlLine ReadGeometry(FeatureLine fl)
        {
            var line = new FlLine
            {
                Handle = fl.Handle.ToString(),
                Name = Safe(() => fl.Name, ""),
                Layer = Safe(() => fl.Layer, ""),
                RxClass = Safe(() => fl.GetRXClass().Name, ""),
                Style = Safe(() => fl.StyleName, ""),
                Length2D = Safe(() => fl.Length2D, 0.0),
                Length3D = Safe(() => fl.Length3D, 0.0),
            };

            Point3dCollection pis = fl.GetPoints(FeatureLinePointType.PIPoint);
            foreach (Point3d p in pis) line.Pis.Add(new[] { p.X, p.Y, p.Z });
            // Civil 3D may hand back the first PI again at the end of a closed line
            if (line.Pis.Count > 2 && Same(line.Pis[0], line.Pis[^1])) line.Pis.RemoveAt(line.Pis.Count - 1);

            int n = line.Pis.Count;
            var bulges = new List<double>();
            for (int i = 0; i < n; i++) bulges.Add(Safe(() => fl.GetBulge(i), 0.0));
            line.Closed = FlGeometry.DetectClosed(line.Pis, bulges, line.Length2D);
            line.Bulges = bulges.Take(line.SegmentCount).ToList();
            line.SegLengths2D = FlGeometry.SegmentLengths(line.Pis, line.Bulges, line.Closed);

            Point3dCollection eps = fl.GetPoints(FeatureLinePointType.ElevationPoint);
            foreach (Point3d p in eps)
            {
                var (seg, t, _) = FlGeometry.Locate(line, p.X, p.Y);
                line.ElevationPoints.Add(new FlElevPoint { X = p.X, Y = p.Y, Z = p.Z, Segment = seg, Fraction = t });
            }
            return line;
        }

        private static bool Same(double[] a, double[] b) => FlGeometry.Dist2D(a[0], a[1], b[0], b[1]) < 1e-6;

        internal static T Safe<T>(Func<T> f, T fallback)
        {
            try { return f(); } catch { return fallback; }
        }
    }
}
