using System;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;

namespace CADAgent
{
    /// <summary>
    /// Collects geometry that lives INSIDE block references — chiefly xrefs.
    ///
    /// ModelSpaceCollector walks model space itself, so an xref shows up there as
    /// a single BlockReference and the polylines inside it are invisible. Lot pads
    /// are normally xrefed in (their layers arrive in the "xrefname|layername"
    /// form), so anything that has to reason about pads needs this walk.
    ///
    /// Everything is returned in WORLD coordinates: each nested entity is
    /// transformed by the accumulated block transform of the references it sits
    /// inside, so callers can treat the result exactly like model space geometry.
    ///
    /// Results are filtered by layer suffix rather than returning whole xrefs:
    /// a site xref holds far more than the caller wants, and /modelspace already
    /// runs up against a 1 MB response limit.
    /// </summary>
    public static class NestedCollector
    {
        private const int MaxDepth = 8;  // xrefs nest; guard against a cycle

        public static string Collect(Document doc, IList<string> layerSuffixes)
        {
            var lines = new JArray();
            var arcs = new JArray();
            var polylines = new JArray();

            Database db = doc.Database;
            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace],
                                                        OpenMode.ForRead);

                foreach (ObjectId id in ms)
                {
                    try
                    {
                        if (tr.GetObject(id, OpenMode.ForRead) is BlockReference br)
                            Walk(br, tr, br.BlockTransform, layerSuffixes, 0,
                                 lines, arcs, polylines);
                    }
                    catch { }  // a broken or unresolved reference must not stop the rest
                }

                tr.Commit();
            }

            return new JObject
            {
                ["lines"] = lines,
                ["arcs"] = arcs,
                ["polylines"] = polylines,
            }.ToString();
        }

        private static void Walk(BlockReference reference, Transaction tr, Matrix3d xform,
                                 IList<string> layerSuffixes, int depth,
                                 JArray lines, JArray arcs, JArray polylines)
        {
            if (depth > MaxDepth) return;

            BlockTableRecord definition;
            try
            {
                // For an xref this is the xref's own model space, but only once
                // the xref is resolved and loaded — an unloaded one yields nothing.
                definition = (BlockTableRecord)tr.GetObject(reference.BlockTableRecord,
                                                            OpenMode.ForRead);
            }
            catch { return; }

            foreach (ObjectId id in definition)
            {
                Entity entity;
                try { entity = tr.GetObject(id, OpenMode.ForRead) as Entity; }
                catch { continue; }
                if (entity == null) continue;

                if (entity is BlockReference nested)
                {
                    // Compose transforms so depth-2 geometry still lands in world space.
                    Walk(nested, tr, xform * nested.BlockTransform, layerSuffixes,
                         depth + 1, lines, arcs, polylines);
                    continue;
                }

                if (!LayerMatches(entity.Layer, layerSuffixes)) continue;

                // Lines, arcs and polylines are read directly. Anything else on a
                // wanted layer -- a Civil 3D parcel segment, a feature line -- is
                // EXPLODED into those primitives first. Lot lines in particular
                // arrive as AECC_PARCEL_SEGMENT, which no type check below would
                // ever see, however well its layer name matched. The pieces carry
                // the parent's layer and handle: the layer is what was asked for,
                // and exploded copies are not in the database to have their own.
                string layer = entity.Layer;
                string handle = entity.Handle.ToString();
                foreach (Entity piece in Primitives(entity))
                try
                {
                    if (piece is Line ln)
                    {
                        lines.Add(new JObject
                        {
                            ["handle"] = handle,
                            ["layer"] = layer,
                            ["start_point"] = Pt(ln.StartPoint.TransformBy(xform)),
                            ["end_point"] = Pt(ln.EndPoint.TransformBy(xform)),
                        });
                    }
                    else if (piece is Arc ar)
                    {
                        // Uniform scale is assumed: xrefs are all but always inserted
                        // 1:1. The scale and rotation are read back off the transform
                        // rather than hard-coded so a scaled insert still lands right.
                        Vector3d unit = Vector3d.XAxis.TransformBy(xform);
                        double scale = unit.Length;
                        double spin = Math.Atan2(unit.Y, unit.X);
                        arcs.Add(new JObject
                        {
                            ["handle"] = handle,
                            ["layer"] = layer,
                            ["center"] = Pt(ar.Center.TransformBy(xform)),
                            ["radius"] = ar.Radius * scale,
                            ["start_angle"] = ar.StartAngle + spin,
                            ["end_angle"] = ar.EndAngle + spin,
                        });
                    }
                    else if (piece is Polyline pl)
                    {
                        // A mirrored insert reverses every arc's sense, so the
                        // bulge sign follows the transform's handedness.
                        Vector3d ax = Vector3d.XAxis.TransformBy(xform);
                        Vector3d ay = Vector3d.YAxis.TransformBy(xform);
                        double handedness = (ax.X * ay.Y - ax.Y * ay.X) < 0 ? -1.0 : 1.0;

                        var verts = new JArray();
                        var bulges = new JArray();
                        for (int i = 0; i < pl.NumberOfVertices; i++)
                        {
                            Point3d p = pl.GetPoint3dAt(i).TransformBy(xform);
                            verts.Add(new JArray(p.X, p.Y));
                            // Bulge i belongs to the segment leaving vertex i.
                            // Without it a curved curb is read as a chord between
                            // its vertices, a sagitta away from the real curb.
                            bulges.Add(pl.GetBulgeAt(i) * handedness);
                        }
                        polylines.Add(new JObject
                        {
                            ["handle"] = handle,
                            ["layer"] = layer,
                            ["closed"] = pl.Closed,
                            ["vertices"] = verts,
                            ["bulges"] = bulges,
                        });
                    }
                }
                catch { }  // one unreadable entity must not lose the rest
            }
        }

        /// <summary>
        /// The entity itself if it is a line, arc or polyline; otherwise whatever
        /// lines, arcs and polylines it explodes into (a parcel segment gives one
        /// line or one arc). An entity that cannot be exploded yields nothing.
        /// Exploded pieces are not database-resident and are disposed once used.
        /// </summary>
        private static IEnumerable<Entity> Primitives(Entity entity)
        {
            if (entity is Line || entity is Arc || entity is Polyline)
            {
                yield return entity;
                yield break;
            }

            var pieces = new DBObjectCollection();
            if (!TryExplode(entity, pieces)) yield break;
            try
            {
                foreach (DBObject piece in pieces)
                    if (piece is Line || piece is Arc || piece is Polyline)
                        yield return (Entity)piece;
            }
            finally
            {
                foreach (DBObject piece in pieces) piece.Dispose();
            }
        }

        private static bool TryExplode(Entity entity, DBObjectCollection into)
        {
            try { entity.Explode(into); return true; }
            catch { return false; }   // eNotApplicable and friends: just skip it
        }

        /// <summary>Suffix match, so "c-lots-pad" also picks up the xref-dependent
        /// "x-plat (ph1a)|c-lots-pad" without the caller knowing the xref's name.</summary>
        private static bool LayerMatches(string layer, IList<string> suffixes)
        {
            if (string.IsNullOrEmpty(layer)) return false;
            if (suffixes == null || suffixes.Count == 0) return true;

            string lower = layer.ToLowerInvariant();
            foreach (string suffix in suffixes)
                if (!string.IsNullOrWhiteSpace(suffix) &&
                    lower.EndsWith(suffix.Trim().ToLowerInvariant()))
                    return true;
            return false;
        }

        private static JArray Pt(Point3d p) => new JArray(p.X, p.Y, p.Z);
    }
}
