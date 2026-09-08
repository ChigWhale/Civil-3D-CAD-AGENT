using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.Colors;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;

using AcadEntity = Autodesk.AutoCAD.DatabaseServices.Entity;
using AcIntersect = Autodesk.AutoCAD.DatabaseServices.Intersect;

namespace CADAgent
{
    public static partial class CadCommands
    {
        private static void RegisterModify()
        {
            Add("move_entities",     MoveEntities);
            Add("copy_entities",     CopyEntities);
            Add("rotate_entities",   RotateEntities);
            Add("scale_entities",    ScaleEntities);
            Add("mirror_entities",   MirrorEntities);
            Add("stretch_entities",  StretchEntities);
            Add("align_entities",    AlignEntities);
            Add("offset_entities",   OffsetEntities);
            Add("array_entities",    ArrayEntities);
            Add("fillet_entities",   FilletEntities);
            Add("chamfer_entities",  ChamferEntities);
            Add("fillet_polyline",   FilletPolyline);
            Add("trim_entities",     TrimEntities);
            Add("extend_entities",   ExtendEntities);
            Add("break_entity",      BreakEntity);
            Add("lengthen_entity",   LengthenEntity);
            Add("join_entities",     JoinEntities);
            Add("explode_entities",  ExplodeEntities);
            Add("mark_along_entity", MarkAlongEntity);
            Add("edit_polyline",     EditPolyline);
            Add("edit_spline",       EditSpline);
            Add("reverse_curves",    ReverseCurves);
            Add("set_properties",    SetProperties);
            Add("match_properties",  MatchProperties);
            Add("set_draw_order",    SetDrawOrder);
        }

        // ── shared transform machinery ────────────────────────────────────────────

        private static Vector3d OffsetVector(JObject req)
        {
            if (req["offset"] != null) { var p = P3(req["offset"]); return new Vector3d(p.X, p.Y, p.Z); }
            if (req["from"] != null && req["to"] != null) return P3(req["to"]) - P3(req["from"]);
            throw new ArgumentException("give offset [dx, dy] or from + to points");
        }

        /// <summary>Applies <paramref name="m"/> to each entity in place, or to <paramref name="copies"/> clones of each (m, m², …).</summary>
        private static List<ObjectId> Transform(Transaction tr, Database db, List<ObjectId> ids, Matrix3d m,
                                                bool copy, int copies = 1)
        {
            var result = new List<ObjectId>();
            foreach (var id in ids)
            {
                if (!copy)
                {
                    var e = Open<AcadEntity>(tr, id, OpenMode.ForWrite);
                    e.TransformBy(m);
                    result.Add(id);
                    continue;
                }
                var src = Open<AcadEntity>(tr, id);
                var owner = OwnerSpace(tr, src);
                Matrix3d acc = Matrix3d.Identity;
                for (int k = 0; k < Math.Max(1, copies); k++)
                {
                    acc = m * acc;
                    var clone = (AcadEntity)src.Clone();
                    clone.TransformBy(acc);
                    result.Add(Append(tr, owner, clone));
                }
            }
            return result;
        }

        private static string MoveEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            var v = OffsetVector(req);
            Transform(tr, db, ids, Matrix3d.Displacement(v), copy: false);
            return Ok($"Moved {ids.Count} object(s) by ({R(v.X)}, {R(v.Y)}).", new JObject { ["modified"] = Handles(ids) });
        });

        private static string CopyEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            var v = OffsetVector(req);
            int count = Math.Max(1, I(req["count"], 1));
            var created = Transform(tr, db, ids, Matrix3d.Displacement(v), copy: true, copies: count);
            return Created($"Copied {ids.Count} object(s) {count} time(s).", created);
        });

        private static string RotateEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            Point3d basePt = P3(req["base_point"]);
            double angle = Rad(req["angle_deg"]);
            if (req["reference_angle_deg"] != null) angle -= Rad(req["reference_angle_deg"]);
            bool copy = B(req["copy"]);
            var m = Matrix3d.Rotation(angle, Vector3d.ZAxis, basePt);
            var ids2 = Transform(tr, db, ids, m, copy, Math.Max(1, I(req["count"], 1)));
            return copy ? Created($"Rotated copies of {ids.Count} object(s).", ids2)
                        : Ok($"Rotated {ids.Count} object(s) by {R(Deg(angle))}°.", new JObject { ["modified"] = Handles(ids) });
        });

        private static string ScaleEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            Point3d basePt = P3(req["base_point"]);
            double factor = D(req["factor"], 1);
            if (req["reference_length"] != null && req["new_length"] != null)
                factor = D(req["new_length"]) / D(req["reference_length"]);
            if (factor <= 0) throw new ArgumentException("factor must be > 0");
            bool copy = B(req["copy"]);
            var m = Matrix3d.Scaling(factor, basePt);
            var ids2 = Transform(tr, db, ids, m, copy);
            return copy ? Created($"Scaled copies of {ids.Count} object(s) by {R(factor)}.", ids2)
                        : Ok($"Scaled {ids.Count} object(s) by {R(factor)}.", new JObject { ["modified"] = Handles(ids) });
        });

        private static string MirrorEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            Point3d a = Flat(P3(req["axis_start"])), b = Flat(P3(req["axis_end"]));
            if (a.DistanceTo(b) < Tol) throw new ArgumentException("axis points coincide");
            bool copy = B(req["copy"], true);
            var m = Matrix3d.Mirroring(new Line3d(a, b));
            var ids2 = Transform(tr, db, ids, m, copy);
            return copy ? Created($"Mirrored copies of {ids.Count} object(s).", ids2)
                        : Ok($"Mirrored {ids.Count} object(s) in place.", new JObject { ["modified"] = Handles(ids) });
        });

        private static string StretchEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            var v = OffsetVector(req);
            List<Point3d> poly;
            if (req["polygon"] != null) poly = Points(req["polygon"]).Select(Flat).ToList();
            else if (req["window"] != null)
            {
                var w = Points(req["window"]);
                if (w.Count != 2) throw new ArgumentException("window needs [[minx,miny],[maxx,maxy]]");
                double x0 = Math.Min(w[0].X, w[1].X), x1 = Math.Max(w[0].X, w[1].X);
                double y0 = Math.Min(w[0].Y, w[1].Y), y1 = Math.Max(w[0].Y, w[1].Y);
                poly = new List<Point3d> { new Point3d(x0, y0, 0), new Point3d(x1, y0, 0), new Point3d(x1, y1, 0), new Point3d(x0, y1, 0) };
            }
            else throw new ArgumentException("give polygon or window");
            if (poly.Count < 3) throw new ArgumentException("polygon needs at least 3 points");

            var stretched = new List<ObjectId>(); var moved = new List<ObjectId>(); var untouched = new List<ObjectId>();
            foreach (var id in ids)
            {
                var e = Open<AcadEntity>(tr, id, OpenMode.ForWrite);
                var pts = new Point3dCollection();
                try { e.GetStretchPoints(pts); } catch { }
                var inside = new IntegerCollection();
                for (int i = 0; i < pts.Count; i++)
                    if (PointInPolygon(Flat(pts[i]), poly)) inside.Add(i);

                if (pts.Count == 0 || inside.Count == 0) { untouched.Add(id); continue; }
                if (inside.Count == pts.Count) { e.TransformBy(Matrix3d.Displacement(v)); moved.Add(id); continue; }
                try { e.MoveStretchPointsAt(inside, v); stretched.Add(id); }
                catch { e.TransformBy(Matrix3d.Displacement(v)); moved.Add(id); }
            }
            return Ok($"Stretched {stretched.Count}, moved {moved.Count}, untouched {untouched.Count}.", new JObject
            {
                ["modified"] = Handles(stretched.Concat(moved)),
                ["stretched"] = Handles(stretched), ["moved"] = Handles(moved), ["untouched"] = Handles(untouched),
            });
        });

        private static string AlignEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            var src = Points(req["source_points"]).Select(Flat).ToList();
            var dst = Points(req["destination_points"]).Select(Flat).ToList();
            if (src.Count == 0 || src.Count != dst.Count) throw new ArgumentException("source_points and destination_points need the same count (1 or 2)");

            Matrix3d m = Matrix3d.Displacement(dst[0] - src[0]);
            if (src.Count >= 2)
            {
                double rot = Angle(dst[0], dst[1]) - Angle(src[0], src[1]);
                m = Matrix3d.Rotation(rot, Vector3d.ZAxis, dst[0]) * m;
                if (B(req["scale"]))
                {
                    double ls = src[0].DistanceTo(src[1]), ld = dst[0].DistanceTo(dst[1]);
                    if (ls < Tol) throw new ArgumentException("source points coincide");
                    m = Matrix3d.Scaling(ld / ls, dst[0]) * m;
                }
            }
            bool copy = B(req["copy"]);
            var ids2 = Transform(tr, db, ids, m, copy);
            return copy ? Created($"Aligned copies of {ids.Count} object(s).", ids2)
                        : Ok($"Aligned {ids.Count} object(s).", new JObject { ["modified"] = Handles(ids) });
        });

        // ── offset ────────────────────────────────────────────────────────────────

        private static List<Curve> OffsetCandidates(Curve c, double d)
        {
            var list = new List<Curve>();
            foreach (double sign in new[] { 1.0, -1.0 })
            {
                try
                {
                    foreach (DBObject o in c.GetOffsetCurves(sign * d))
                        if (o is Curve oc) list.Add(oc);
                }
                catch { }
            }
            return list;
        }

        private static string OffsetEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            Point3d? side = P3Opt(req["side_point"]);
            Point3d? through = P3Opt(req["through_point"]);
            double distance = D(req["distance"], 0);
            if (through == null && distance <= 0) throw new ArgumentException("give distance (> 0) with side_point, or through_point");
            if (through == null && side == null) throw new ArgumentException("side_point is required with distance");
            string layer = S(req["layer"]);

            var created = new List<ObjectId>();
            foreach (var id in ids)
            {
                var src = OpenCurve(tr, id);
                Point3d target = through ?? side.Value;
                double d = through != null ? src.GetClosestPointTo(Flat(through.Value), false).DistanceTo(Flat(through.Value)) : distance;
                if (d < Tol) throw new ArgumentException($"through_point lies on {id.Handle}");

                var candidates = OffsetCandidates(src, d);
                if (candidates.Count == 0) throw new ArgumentException($"{src.GetType().Name} {id.Handle} cannot be offset by {R(d)}");
                var best = candidates.OrderBy(c => c.GetClosestPointTo(Flat(target), false).DistanceTo(Flat(target))).First();
                foreach (var c in candidates) if (!ReferenceEquals(c, best)) c.Dispose();

                var owner = OwnerSpace(tr, src);
                best.SetPropertiesFrom(src);
                created.Add(Append(tr, owner, best, layer));
            }
            return Created($"Offset {ids.Count} object(s).", created);
        });

        // ── array ─────────────────────────────────────────────────────────────────

        private static Point3d GroupBasePoint(Transaction tr, List<ObjectId> ids, JObject req)
        {
            if (req["base_point"] != null) return Flat(P3(req["base_point"]));
            Extents3d? ext = null;
            foreach (var id in ids)
            {
                try
                {
                    var e = Open<AcadEntity>(tr, id).GeometricExtents;
                    if (ext == null) ext = e; else { var x = ext.Value; x.AddExtents(e); ext = x; }
                }
                catch { }
            }
            if (ext == null) return Point3d.Origin;
            return new Point3d((ext.Value.MinPoint.X + ext.Value.MaxPoint.X) / 2, (ext.Value.MinPoint.Y + ext.Value.MaxPoint.Y) / 2, 0);
        }

        private static string ArrayEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            string kind = S(req["kind"], "rectangular").ToLowerInvariant();
            var matrices = new List<Matrix3d>();

            if (kind == "rectangular")
            {
                int rows = Math.Max(1, I(req["rows"], 1)), cols = Math.Max(1, I(req["columns"], 1));
                double rs = D(req["row_spacing"]), cs = D(req["column_spacing"]);
                double ang = Rad(req["angle_deg"]);
                var colV = new Vector3d(Math.Cos(ang), Math.Sin(ang), 0) * cs;
                var rowV = new Vector3d(-Math.Sin(ang), Math.Cos(ang), 0) * rs;
                for (int r = 0; r < rows; r++)
                    for (int c = 0; c < cols; c++)
                        if (r != 0 || c != 0) matrices.Add(Matrix3d.Displacement(colV * c + rowV * r));
            }
            else if (kind == "polar")
            {
                Point3d center = Flat(P3(req["center"]));
                int count = I(req["count"], 0);
                if (count < 2) throw new ArgumentException("polar array needs count >= 2");
                double fill = Rad(req["fill_angle_deg"], 360);
                bool full = Math.Abs(Math.Abs(fill) - 2 * Math.PI) < 1e-9;
                double step = full ? fill / count : fill / (count - 1);
                bool rotateItems = B(req["rotate_items"], true);
                Point3d basePt = GroupBasePoint(tr, ids, req);
                for (int k = 1; k < count; k++)
                {
                    var rot = Matrix3d.Rotation(step * k, Vector3d.ZAxis, center);
                    matrices.Add(rotateItems ? rot : Matrix3d.Displacement(basePt.TransformBy(rot) - basePt));
                }
            }
            else if (kind == "path")
            {
                var path = OpenCurve(tr, Resolve(db, S(req["path_handle"])));
                double len = path.GetDistanceAtParameter(path.EndParam) - path.GetDistanceAtParameter(path.StartParam);
                double spacing = D(req["spacing"], 0);
                int count = I(req["count"], 0);
                if (count < 1 && spacing <= 0) throw new ArgumentException("path array needs count or spacing");
                if (count < 1) count = (int)Math.Floor(len / spacing + 1e-9) + 1;
                else if (spacing <= 0) spacing = count > 1 ? len / (count - 1) : 0;
                bool align = B(req["align_to_path"], true);
                Point3d basePt = GroupBasePoint(tr, ids, req);
                double d0 = path.GetDistanceAtParameter(path.StartParam);
                Point3d p0 = path.GetPointAtDist(d0);
                double a0 = Math.Atan2(path.GetFirstDerivative(p0).Y, path.GetFirstDerivative(p0).X);
                // The originals stay where they are; each further item lands k*spacing along the path.
                for (int k = 0; k < count; k++)
                {
                    double d = d0 + k * spacing;
                    if (d > d0 + len + Tol) break;
                    Point3d p = path.GetPointAtDist(d);
                    var m = Matrix3d.Displacement(p - basePt);
                    if (align)
                    {
                        var t = path.GetFirstDerivative(p);
                        double a = Math.Atan2(t.Y, t.X);
                        m = Matrix3d.Rotation(a - a0, Vector3d.ZAxis, p) * m;
                    }
                    matrices.Add(m);
                }
                if (matrices.Count == 0) throw new ArgumentException("no item fits on the path");
            }
            else throw new ArgumentException("kind must be rectangular, polar or path");

            var created = new List<ObjectId>();
            foreach (var id in ids)
            {
                var src = Open<AcadEntity>(tr, id);
                var owner = OwnerSpace(tr, src);
                foreach (var m in matrices)
                {
                    var clone = (AcadEntity)src.Clone();
                    clone.TransformBy(m);
                    created.Add(Append(tr, owner, clone));
                }
            }
            return Created($"{kind} array: {created.Count} new object(s).", created, new JObject { ["items"] = matrices.Count + (kind == "path" ? 0 : 1) });
        });

        // ── fillet / chamfer ──────────────────────────────────────────────────────

        /// <summary>Centres of every circle of radius r tangent to both curves (offset intersection method).</summary>
        public static List<Point3d> FilletCenters(Curve a, Curve b, double r)
        {
            var centers = new List<Point3d>();
            var oa = OffsetCandidates(a, r); var ob = OffsetCandidates(b, r);
            try
            {
                foreach (var ca in oa)
                    foreach (var cb in ob)
                        foreach (var p in Intersect(ca, cb, AcIntersect.ExtendBoth))
                            if (!centers.Any(c => c.DistanceTo(p) < 1e-6)) centers.Add(p);
            }
            finally
            {
                foreach (var c in oa) c.Dispose();
                foreach (var c in ob) c.Dispose();
            }
            return centers;
        }

        public static double TangentScore(Curve a, Curve b, Point3d center, Point3d pickA, Point3d pickB)
        {
            Point3d ta = a.GetClosestPointTo(center, true), tb = b.GetClosestPointTo(center, true);
            return ta.DistanceTo(Flat(pickA)) + tb.DistanceTo(Flat(pickB));
        }

        /// <summary>Default pick points when the caller gives none: the two ends of the curves that are closest to each other.</summary>
        private static (Point3d, Point3d) DefaultPicks(Curve a, Curve b)
        {
            var ea = new[] { a.StartPoint, a.EndPoint }; var eb = new[] { b.StartPoint, b.EndPoint };
            var best = (ea[0], eb[0]); double bd = double.MaxValue;
            foreach (var p in ea) foreach (var q in eb)
                if (p.DistanceTo(q) < bd) { bd = p.DistanceTo(q); best = (p, q); }
            return best;
        }

        /// <summary>Cuts or extends a Line/Arc so that it ends at <paramref name="tp"/> on the side of <paramref name="keep"/>. Circles are left alone.</summary>
        private static void TrimOrExtendTo(Curve curve, Point3d tp, Point3d keep)
        {
            tp = Flat(tp); keep = Flat(keep);
            switch (curve)
            {
                case Line ln:
                {
                    var dir = Dir(ln.StartPoint, ln.EndPoint);
                    double side = Math.Sign((keep - tp).DotProduct(dir));
                    if (side == 0) side = 1;
                    double ds = (Flat(ln.StartPoint) - tp).DotProduct(dir) * side;
                    double de = (Flat(ln.EndPoint) - tp).DotProduct(dir) * side;
                    if (de >= ds) ln.StartPoint = new Point3d(tp.X, tp.Y, ln.StartPoint.Z);
                    else          ln.EndPoint   = new Point3d(tp.X, tp.Y, ln.EndPoint.Z);
                    break;
                }
                case Arc arc:
                {
                    double t = Angle(arc.Center, tp), k = Angle(arc.Center, keep);
                    double s = arc.StartAngle, e = arc.EndAngle;
                    bool keepEndSide = Norm(k - t) <= Norm(e - t);     // keep point lies between tp and the end (going ccw)
                    bool keepStartSide = Norm(t - k) <= Norm(t - s);   // keep point lies between start and tp
                    if (keepEndSide && !keepStartSide)      arc.StartAngle = Norm(t);
                    else if (keepStartSide && !keepEndSide) arc.EndAngle   = Norm(t);
                    else
                    {
                        // Ambiguous (tp beyond an end): keep the end nearest the keep point.
                        if (Flat(arc.EndPoint).DistanceTo(keep) <= Flat(arc.StartPoint).DistanceTo(keep)) arc.StartAngle = Norm(t);
                        else arc.EndAngle = Norm(t);
                    }
                    break;
                }
                case Circle _:
                    break;
                default:
                    throw new ArgumentException($"{curve.GetType().Name} is not supported here; use fillet_polyline for polylines or explode first");
            }
        }

        private static string FilletEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            ObjectId ida = Resolve(db, S(req["handle_a"])), idb = Resolve(db, S(req["handle_b"]));
            if (ida == idb) throw new ArgumentException("handle_a and handle_b are the same object; use fillet_polyline for a polyline corner");
            double radius = D(req["radius"], 0);
            if (radius < 0) throw new ArgumentException("radius must be >= 0");
            bool trim = B(req["trim"], true);
            var a = OpenCurve(tr, ida, OpenMode.ForWrite); var b = OpenCurve(tr, idb, OpenMode.ForWrite);
            var (dpa, dpb) = DefaultPicks(a, b);
            Point3d pa = P3Opt(req["pick_a"]) ?? dpa, pb = P3Opt(req["pick_b"]) ?? dpb;

            if (radius < Tol)
            {
                var xs = Intersect(a, b, AcIntersect.ExtendBoth);
                if (xs.Count == 0) throw new ArgumentException("the objects do not meet, even extended");
                var x = xs.OrderBy(p => p.DistanceTo(Flat(pa)) + p.DistanceTo(Flat(pb))).First();
                TrimOrExtendTo(a, x, pa); TrimOrExtendTo(b, x, pb);
                return Ok("Corner joined (radius 0).", new JObject { ["modified"] = Handles(new[] { ida, idb }), ["corner"] = Arr(x) });
            }

            var centers = FilletCenters(a, b, radius);
            if (centers.Count == 0) throw new ArgumentException($"no fillet of radius {R(radius)} fits between these objects");
            var c = centers.OrderBy(p => TangentScore(a, b, p, pa, pb)).First();
            Point3d ta = a.GetClosestPointTo(c, true), tb = b.GetClosestPointTo(c, true);
            double aa = Angle(c, ta), ab = Angle(c, tb);
            Arc fillet = Norm(ab - aa) <= Math.PI ? MakeArc(c, radius, aa, ab, true) : MakeArc(c, radius, ab, aa, true);
            fillet.SetPropertiesFrom(a);
            ObjectId fid = Append(tr, OwnerSpace(tr, a), fillet, S(req["layer"]));

            if (trim) { TrimOrExtendTo(a, ta, pa); TrimOrExtendTo(b, tb, pb); }
            return Created("Fillet created.", new[] { fid }, new JObject
            {
                ["modified"] = trim ? Handles(new[] { ida, idb }) : new JArray(),
                ["center"] = Arr(c), ["tangent_a"] = Arr(ta), ["tangent_b"] = Arr(tb),
            });
        });

        private static string ChamferEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            ObjectId ida = Resolve(db, S(req["handle_a"])), idb = Resolve(db, S(req["handle_b"]));
            var a = Open<Line>(tr, ida, OpenMode.ForWrite); var b = Open<Line>(tr, idb, OpenMode.ForWrite);
            double d1 = D(req["distance_a"], 0);
            double d2 = req["angle_deg"] != null ? d1 * Math.Tan(Rad(req["angle_deg"])) : D(req["distance_b"], d1);
            if (d1 < 0 || d2 < 0) throw new ArgumentException("distances must be >= 0");
            bool trim = B(req["trim"], true);
            var (dpa, dpb) = DefaultPicks(a, b);
            Point3d pa = Flat(P3Opt(req["pick_a"]) ?? dpa), pb = Flat(P3Opt(req["pick_b"]) ?? dpb);

            var xs = Intersect(a, b, AcIntersect.ExtendBoth);
            if (xs.Count == 0) throw new ArgumentException("the lines are parallel");
            Point3d x = xs[0];

            Vector3d da = DirectionFrom(a, x, pa), dbv = DirectionFrom(b, x, pb);
            Point3d ca = x + da * d1, cb = x + dbv * d2;
            var chamfer = new Line(ca, cb);
            chamfer.SetPropertiesFrom(a);
            ObjectId cid = Append(tr, OwnerSpace(tr, a), chamfer, S(req["layer"]));
            if (trim) { TrimOrExtendTo(a, ca, pa); TrimOrExtendTo(b, cb, pb); }
            return Created("Chamfer created.", new[] { cid }, new JObject
            {
                ["modified"] = trim ? Handles(new[] { ida, idb }) : new JArray(),
                ["corner"] = Arr(x),
            });
        });

        /// <summary>Unit direction along a line from <paramref name="from"/> towards the side the pick point is on.</summary>
        private static Vector3d DirectionFrom(Line ln, Point3d from, Point3d pick)
        {
            var dir = Dir(ln.StartPoint, ln.EndPoint);
            Point3d cp = ln.GetClosestPointTo(pick, true);
            double s = (Flat(cp) - Flat(from)).DotProduct(dir);
            if (Math.Abs(s) < Tol)
                s = (Flat(ln.EndPoint) - from).Length >= (Flat(ln.StartPoint) - from).Length
                    ? (Flat(ln.EndPoint) - from).DotProduct(dir) : (Flat(ln.StartPoint) - from).DotProduct(dir);
            return s >= 0 ? dir : -dir;
        }

        /// <summary>Rounds one polyline corner; returns false (leaving it untouched) if the corner cannot take the radius.</summary>
        private static bool FilletPolylineVertex(Polyline pl, int i, double r, bool chamfer = false)
        {
            int n = pl.NumberOfVertices;
            bool closed = pl.Closed;
            if (!closed && (i <= 0 || i >= n - 1)) return false;
            int prev = (i - 1 + n) % n, next = (i + 1) % n;
            if (Math.Abs(pl.GetBulgeAt(prev)) > Tol || Math.Abs(pl.GetBulgeAt(i)) > Tol) return false;

            Point2d P = pl.GetPoint2dAt(i), A = pl.GetPoint2dAt(prev), Bp = pl.GetPoint2dAt(next);
            Vector2d u = A - P, v = Bp - P;
            double la = u.Length, lb = v.Length;
            if (la < Tol || lb < Tol) return false;
            u /= la; v /= lb;
            double cosT = Math.Max(-1, Math.Min(1, u.DotProduct(v)));
            double theta = Math.Acos(cosT);                 // interior angle at P
            if (theta < 1e-6 || Math.PI - theta < 1e-6) return false;

            double t = chamfer ? r : r / Math.Tan(theta / 2);   // distance from P to each tangent point
            if (t > la + Tol || t > lb + Tol) return false;

            Point2d t1 = P + u * t, t2 = P + v * t;
            double cross = (P - A).X * (Bp - P).Y - (P - A).Y * (Bp - P).X;
            double bulge = chamfer ? 0 : Math.Tan((Math.PI - theta) / 4) * (cross > 0 ? 1 : -1);
            double sw = pl.GetStartWidthAt(i), ew = pl.GetEndWidthAt(i);

            pl.SetPointAt(i, t1);
            pl.SetBulgeAt(i, bulge);
            pl.AddVertexAt(i + 1, t2, 0, sw, ew);
            return true;
        }

        public static int FilletAllPolylineVertices(Polyline pl, double r)
        {
            int done = 0;
            var indices = Enumerable.Range(0, pl.NumberOfVertices).Reverse().ToList();
            foreach (int i in indices) if (FilletPolylineVertex(pl, i, r)) done++;
            return done;
        }

        public static int ChamferAllPolylineVertices(Polyline pl, double d)
        {
            int done = 0;
            var indices = Enumerable.Range(0, pl.NumberOfVertices).Reverse().ToList();
            foreach (int i in indices) if (FilletPolylineVertex(pl, i, d, chamfer: true)) done++;
            return done;
        }

        private static string FilletPolyline(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var pl = Open<Polyline>(tr, Resolve(db, S(req["handle"])), OpenMode.ForWrite);
            double r = D(req["radius"], 0);
            bool chamfer = B(req["chamfer"]);
            if (r <= 0) throw new ArgumentException(chamfer ? "distance must be > 0" : "radius must be > 0");
            var wanted = (req["vertex_indices"] as JArray)?.Select(t => I(t)).OrderByDescending(x => x).ToList();
            int done = 0, skipped = 0;
            if (wanted == null)
                done = chamfer ? ChamferAllPolylineVertices(pl, r) : FilletAllPolylineVertices(pl, r);
            else
                foreach (int i in wanted)
                {
                    if (i < 0 || i >= pl.NumberOfVertices) throw new ArgumentException($"vertex index {i} out of range");
                    if (FilletPolylineVertex(pl, i, r, chamfer)) done++; else skipped++;
                }
            if (done == 0) throw new ArgumentException("no corner could take that radius (corners must join two straight segments long enough for the tangent length)");
            return Ok($"{(chamfer ? "Chamfered" : "Filleted")} {done} corner(s), {skipped} skipped.", new JObject { ["modified"] = new JArray(pl.Handle.ToString()) });
        });

        // ── trim / extend / break / lengthen ──────────────────────────────────────

        private static List<Curve> Split(Curve c, IEnumerable<Point3d> at)
        {
            var pts = new Point3dCollection();
            // Split points must be sorted by parameter or AutoCAD returns garbage.
            var ordered = at.Select(Flat).Distinct()
                            .Select(p => (p, prm: SafeParam(c, p)))
                            .Where(x => x.prm.HasValue)
                            .OrderBy(x => x.prm.Value).Select(x => x.p);
            foreach (var p in ordered) pts.Add(p);
            var pieces = new List<Curve>();
            if (pts.Count == 0) { return pieces; }
            foreach (DBObject o in c.GetSplitCurves(pts)) if (o is Curve pc) pieces.Add(pc);
            return pieces;
        }

        private static double? SafeParam(Curve c, Point3d p)
        {
            try { return c.GetParameterAtPoint(c.GetClosestPointTo(p, false)); } catch { return null; }
        }

        private static double DistanceTo(Curve c, Point3d p)
        {
            try { return c.GetClosestPointTo(Flat(p), false).DistanceTo(Flat(p)); } catch { return double.MaxValue; }
        }

        private static List<Point3d> PickList(JObject req, int count, string field = "pick_points")
        {
            var picks = Points(req[field]);
            if (picks.Count == 1 && count > 1) picks = Enumerable.Repeat(picks[0], count).ToList();
            if (picks.Count != count) throw new ArgumentException($"{field} must have one point per target ({count})");
            return picks;
        }

        /// <summary>Replaces <paramref name="original"/> with <paramref name="pieces"/> (same owner, same properties). Returns the new ids.</summary>
        private static List<ObjectId> Replace(Transaction tr, Curve original, IEnumerable<Curve> pieces)
        {
            var owner = OwnerSpace(tr, original);
            var ids = new List<ObjectId>();
            foreach (var p in pieces) { p.SetPropertiesFrom(original); ids.Add(Append(tr, owner, p)); }
            original.UpgradeOpen();
            original.Erase();
            return ids;
        }

        private static string TrimEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var targets = ResolveAll(db, req["handles"]);
            var cutters = ResolveAll(db, req["cutting_handles"], "cutting_handles");
            var picks = PickList(req, targets.Count);
            var mode = B(req["extend_cutting_edges"]) ? AcIntersect.ExtendArgument : AcIntersect.OnBothOperands;

            var removed = new List<ObjectId>(); var created = new List<ObjectId>(); var report = new JArray();
            for (int i = 0; i < targets.Count; i++)
            {
                var target = OpenCurve(tr, targets[i]);
                var xs = new List<Point3d>();
                foreach (var cid in cutters)
                {
                    if (cid == targets[i]) continue;
                    xs.AddRange(Intersect(target, OpenCurve(tr, cid), mode));
                }
                var pieces = Split(target, xs);
                if (pieces.Count < 2)
                    throw new ArgumentException($"{targets[i].Handle}: cutting edges cross it {xs.Count} time(s); need at least one crossing on an open curve, two on a closed one");

                int drop = 0; double best = double.MaxValue;
                for (int k = 0; k < pieces.Count; k++)
                {
                    double d = DistanceTo(pieces[k], picks[i]);
                    if (d < best) { best = d; drop = k; }
                }
                var keep = pieces.Where((p, k) => k != drop).ToList();
                pieces[drop].Dispose();
                var ids = Replace(tr, target, keep);
                removed.Add(targets[i]); created.AddRange(ids);
                report.Add(new JObject { ["was"] = targets[i].Handle.ToString(), ["now"] = Handles(ids), ["intersections"] = xs.Count });
            }
            return Created($"Trimmed {targets.Count} object(s).", created, new JObject { ["removed"] = Handles(removed), ["details"] = report });
        });

        private static string ExtendEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var targets = ResolveAll(db, req["handles"]);
            var bounds = ResolveAll(db, req["boundary_handles"], "boundary_handles");
            var picks = req["pick_points"] != null ? PickList(req, targets.Count) : null;
            var mode = B(req["extend_boundaries"]) ? AcIntersect.ExtendBoth : AcIntersect.ExtendThis;

            var modified = new List<ObjectId>(); var report = new JArray();
            for (int i = 0; i < targets.Count; i++)
            {
                var target = OpenCurve(tr, targets[i], OpenMode.ForWrite);
                if (target.Closed) throw new ArgumentException($"{targets[i].Handle} is closed and cannot be extended");
                var candidates = new List<Point3d>();
                foreach (var bid in bounds)
                {
                    if (bid == targets[i]) continue;
                    candidates.AddRange(Intersect(target, OpenCurve(tr, bid), mode));
                }
                candidates = candidates.Where(p => DistanceTo(target, p) > 1e-4).ToList();   // only points on the extension

                Point3d s = Flat(target.StartPoint), e = Flat(target.EndPoint);
                bool atStart;
                if (picks != null) atStart = Flat(picks[i]).DistanceTo(s) < Flat(picks[i]).DistanceTo(e);
                else
                {
                    // No pick: extend whichever end has the nearest boundary crossing.
                    double ds = candidates.Count == 0 ? double.MaxValue : candidates.Min(p => p.DistanceTo(s));
                    double de = candidates.Count == 0 ? double.MaxValue : candidates.Min(p => p.DistanceTo(e));
                    atStart = ds < de;
                }
                Point3d end = atStart ? s : e, other = atStart ? e : s;
                var beyond = candidates.Where(p => p.DistanceTo(end) < p.DistanceTo(other)).OrderBy(p => p.DistanceTo(end)).ToList();
                if (beyond.Count == 0) throw new ArgumentException($"{targets[i].Handle}: no boundary lies beyond its {(atStart ? "start" : "end")}");

                Point3d to = beyond[0];
                try { target.Extend(atStart, new Point3d(to.X, to.Y, end.Z)); }
                catch (Autodesk.AutoCAD.Runtime.Exception ex) { throw new ArgumentException($"{targets[i].Handle} cannot be extended to {Arr(to)} ({ex.ErrorStatus})"); }
                modified.Add(targets[i]);
                report.Add(new JObject { ["handle"] = targets[i].Handle.ToString(), ["end"] = atStart ? "start" : "end", ["to"] = Arr(to) });
            }
            return Ok($"Extended {targets.Count} object(s).", new JObject { ["modified"] = Handles(modified), ["details"] = report });
        });

        private static string BreakEntity(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var id = Resolve(db, S(req["handle"]));
            var curve = OpenCurve(tr, id);
            Point3d p1 = curve.GetClosestPointTo(Flat(P3(req["first_point"])), false);
            Point3d? p2raw = P3Opt(req["second_point"]);

            if (p2raw == null)
            {
                if (curve.Closed) throw new ArgumentException("a closed curve needs two break points");
                var pieces = Split(curve, new[] { p1 });
                if (pieces.Count < 2) throw new ArgumentException("first_point is at an end of the curve; nothing to split");
                var ids = Replace(tr, curve, pieces);
                return Created("Broken into two at the point.", ids, new JObject { ["removed"] = new JArray(id.Handle.ToString()) });
            }

            Point3d p2 = curve.GetClosestPointTo(Flat(p2raw.Value), false);
            var parts = Split(curve, new[] { p1, p2 });
            if (parts.Count < 2) throw new ArgumentException("the two points coincide on the curve");

            int drop;
            if (curve.Closed)
                drop = parts.Select((p, k) => (k, d: Flat(p.StartPoint).DistanceTo(p1))).OrderBy(x => x.d).First().k;  // ccw from first to second
            else
            {
                bool Near(Point3d a, Point3d b) => Flat(a).DistanceTo(Flat(b)) < 1e-4;
                drop = -1;
                for (int k = 0; k < parts.Count; k++)
                {
                    var p = parts[k];
                    bool hasBoth = (Near(p.StartPoint, p1) || Near(p.EndPoint, p1)) && (Near(p.StartPoint, p2) || Near(p.EndPoint, p2));
                    if (hasBoth && parts.Count == 3 ? k == 1 : hasBoth) { drop = k; break; }
                }
                if (drop < 0) drop = parts.Count == 3 ? 1 : 0;
            }
            var keepPieces = parts.Where((p, k) => k != drop).ToList();
            parts[drop].Dispose();
            var newIds = Replace(tr, curve, keepPieces);
            return Created("Segment removed between the points.", newIds, new JObject { ["removed"] = new JArray(id.Handle.ToString()) });
        });

        private static string LengthenEntity(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var id = Resolve(db, S(req["handle"]));
            var curve = OpenCurve(tr, id, OpenMode.ForWrite);
            if (curve.Closed) throw new ArgumentException("closed curves cannot be lengthened");
            double length = curve.GetDistanceAtParameter(curve.EndParam) - curve.GetDistanceAtParameter(curve.StartParam);
            string mode = S(req["mode"], "delta").ToLowerInvariant();
            double value = D(req["value"]);

            bool atStart;
            string endField = S(req["end"], "");
            if (endField == "start") atStart = true;
            else if (endField == "end") atStart = false;
            else if (req["near_point"] != null)
            {
                var np = Flat(P3(req["near_point"]));
                atStart = np.DistanceTo(Flat(curve.StartPoint)) < np.DistanceTo(Flat(curve.EndPoint));
            }
            else atStart = false;

            double newLength = mode switch
            {
                "delta"   => length + value,
                "percent" => length * value / 100,
                "total"   => value,
                "to_point" => length + SignedExtensionTo(curve, atStart, Flat(P3(req["point"]))),
                _ => throw new ArgumentException("mode must be delta, percent, total or to_point"),
            };
            if (newLength <= Tol) throw new ArgumentException("resulting length would be zero or negative");
            double delta = newLength - length;
            if (Math.Abs(delta) < Tol) return Ok("Length unchanged.", new JObject { ["modified"] = new JArray(id.Handle.ToString()), ["length"] = R(length) });

            if (curve is Line ln)
            {
                var dir = Dir(ln.StartPoint, ln.EndPoint);
                if (atStart) ln.StartPoint = ln.StartPoint - dir * delta; else ln.EndPoint = ln.EndPoint + dir * delta;
            }
            else if (curve is Arc arc)
            {
                double da = delta / arc.Radius;
                if (atStart) arc.StartAngle = Norm(arc.StartAngle - da); else arc.EndAngle = Norm(arc.EndAngle + da);
            }
            else if (delta > 0)
            {
                Point3d end = atStart ? curve.StartPoint : curve.EndPoint;
                Vector3d t = curve.GetFirstDerivative(end).GetNormal();
                Point3d to = atStart ? end - t * delta : end + t * delta;
                try { curve.Extend(atStart, to); }
                catch (Autodesk.AutoCAD.Runtime.Exception ex) { throw new ArgumentException($"cannot extend this {curve.GetType().Name} ({ex.ErrorStatus}); explode it or use a line/arc"); }
            }
            else
            {
                double d0 = curve.GetDistanceAtParameter(curve.StartParam);
                Point3d cut = curve.GetPointAtDist(atStart ? d0 - delta : d0 + newLength);
                var pieces = Split(curve, new[] { cut });
                if (pieces.Count < 2) throw new ArgumentException("could not shorten the curve at that distance");
                var keep = atStart ? pieces[pieces.Count - 1] : pieces[0];
                foreach (var p in pieces) if (!ReferenceEquals(p, keep)) p.Dispose();
                var ids = Replace(tr, curve, new[] { keep });
                return Created($"Shortened to {R(newLength)} (object replaced).", ids, new JObject { ["removed"] = new JArray(id.Handle.ToString()), ["length"] = R(newLength) });
            }
            return Ok($"Length changed from {R(length)} to {R(newLength)}.", new JObject { ["modified"] = new JArray(id.Handle.ToString()), ["length"] = R(newLength) });
        });

        private static double SignedExtensionTo(Curve curve, bool atStart, Point3d p)
        {
            Point3d end = atStart ? Flat(curve.StartPoint) : Flat(curve.EndPoint);
            Point3d cp = Flat(curve.GetClosestPointTo(p, true));
            Vector3d t = curve.GetFirstDerivative(atStart ? curve.StartPoint : curve.EndPoint).GetNormal();
            double along = (cp - end).DotProduct(t);
            return atStart ? -along : along;
        }

        // ── join / explode / divide ───────────────────────────────────────────────

        public static Polyline ToPolyline(Curve c)
        {
            var pl = new Polyline();
            switch (c)
            {
                case Line ln:
                    pl.AddVertexAt(0, new Point2d(ln.StartPoint.X, ln.StartPoint.Y), 0, 0, 0);
                    pl.AddVertexAt(1, new Point2d(ln.EndPoint.X, ln.EndPoint.Y), 0, 0, 0);
                    break;
                case Arc arc:
                {
                    double sweep = Norm(arc.EndAngle - arc.StartAngle);
                    pl.AddVertexAt(0, new Point2d(arc.StartPoint.X, arc.StartPoint.Y), Math.Tan(sweep / 4), 0, 0);
                    pl.AddVertexAt(1, new Point2d(arc.EndPoint.X, arc.EndPoint.Y), 0, 0, 0);
                    break;
                }
                case Circle ci:
                    pl.AddVertexAt(0, new Point2d(ci.Center.X - ci.Radius, ci.Center.Y), 1, 0, 0);
                    pl.AddVertexAt(1, new Point2d(ci.Center.X + ci.Radius, ci.Center.Y), 1, 0, 0);
                    pl.Closed = true;
                    break;
                case Polyline p:
                    pl.Dispose();
                    return (Polyline)p.Clone();
                case Spline sp:
                    pl.Dispose();
                    return (Polyline)sp.ToPolyline();
                default:
                    throw new ArgumentException($"{c.GetType().Name} cannot be converted to a polyline");
            }
            pl.SetPropertiesFrom(c);
            return pl;
        }

        private static string JoinEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            if (ids.Count < 2) throw new ArgumentException("need at least two objects to join");
            var curves = ids.Select(id => OpenCurve(tr, id, OpenMode.ForWrite)).ToList();
            string mode = S(req["result"], "auto").ToLowerInvariant();

            bool allLines = curves.All(c => c is Line);
            bool allArcs  = curves.All(c => c is Arc);
            Curve target; bool converted = false;
            if (mode == "auto" && (allLines || allArcs || curves[0] is Polyline || curves[0] is Spline))
                target = curves[0];
            else
            {
                target = ToPolyline(curves[0]);
                Append(tr, OwnerSpace(tr, curves[0]), target);
                curves[0].Erase();
                converted = true;
            }

            var others = curves.Skip(1).Cast<AcadEntity>().ToArray();
            IntegerCollection joined;
            try { joined = target.JoinEntities(others); }
            catch (Autodesk.AutoCAD.Runtime.Exception ex)
            {
                if (converted || mode != "auto") throw new ArgumentException($"join failed ({ex.ErrorStatus}); the objects must touch end to end");
                // Retry as a polyline (lines + arcs that are not collinear / concentric).
                var pl = ToPolyline(curves[0]);
                Append(tr, OwnerSpace(tr, curves[0]), pl);
                curves[0].Erase();
                target = pl; converted = true;
                try { joined = target.JoinEntities(others); }
                catch (Autodesk.AutoCAD.Runtime.Exception ex2) { throw new ArgumentException($"join failed ({ex2.ErrorStatus}); the objects must touch end to end"); }
            }

            var consumed = new List<ObjectId>();
            foreach (int k in joined) { others[k].Erase(); consumed.Add(ids[k + 1]); }
            var notJoined = ids.Skip(1).Where(id => !consumed.Contains(id)).ToList();
            var extra = new JObject
            {
                ["result"] = target.Handle.ToString(),
                ["removed"] = Handles(converted ? consumed.Concat(new[] { ids[0] }) : consumed),
                ["not_joined"] = Handles(notJoined),
                ["type"] = target.GetType().Name,
            };
            return converted ? Created($"Joined {consumed.Count} object(s) into a new {target.GetType().Name}.", new[] { target.ObjectId }, extra)
                             : Ok($"Joined {consumed.Count} object(s) into {ids[0].Handle}.", extra);
        });

        private static string ExplodeEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            bool attrsToText = B(req["keep_attributes_as_text"], true);
            var created = new List<ObjectId>();
            foreach (var id in ids)
            {
                var e = Open<AcadEntity>(tr, id, OpenMode.ForWrite);
                var owner = OwnerSpace(tr, e);
                var parts = new DBObjectCollection();
                try { e.Explode(parts); }
                catch (Autodesk.AutoCAD.Runtime.Exception ex) { throw new ArgumentException($"{id.Handle} ({e.GetType().Name}) cannot be exploded ({ex.ErrorStatus})"); }

                if (e is BlockReference br && attrsToText)
                    foreach (ObjectId aid in br.AttributeCollection)
                    {
                        var att = tr.GetObject(aid, OpenMode.ForRead) as AttributeReference;
                        if (att == null || att.Invisible) continue;
                        var txt = new DBText
                        {
                            TextString = att.TextString, Position = att.Position, Height = att.Height, Rotation = att.Rotation,
                            TextStyleId = att.TextStyleId, Layer = att.Layer, WidthFactor = att.WidthFactor,
                            HorizontalMode = att.HorizontalMode, VerticalMode = att.VerticalMode,
                        };
                        if (txt.HorizontalMode != TextHorizontalMode.TextLeft || txt.VerticalMode != TextVerticalMode.TextBase) txt.AlignmentPoint = att.AlignmentPoint;
                        txt.Color = att.Color;
                        created.Add(Append(tr, owner, txt));
                    }

                foreach (DBObject o in parts)
                {
                    if (o is AttributeDefinition) { o.Dispose(); continue; }
                    if (o is AcadEntity pe) created.Add(Append(tr, owner, pe));
                }
                e.Erase();
            }
            return Created($"Exploded {ids.Count} object(s) into {created.Count}.", created, new JObject { ["removed"] = Handles(ids) });
        });

        private static string MarkAlongEntity(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var id = Resolve(db, S(req["handle"]));
            var curve = OpenCurve(tr, id);
            double d0 = curve.GetDistanceAtParameter(curve.StartParam);
            double length = curve.GetDistanceAtParameter(curve.EndParam) - d0;
            var dists = new List<double>();
            int count = I(req["count"], 0);
            double seg = D(req["segment_length"], 0);
            bool fromEnd = S(req["from_end"], "start") == "end";
            bool includeEnds = B(req["include_ends"]);

            if (count >= 2)
            {
                double step = length / count;
                for (int k = 1; k < count; k++) dists.Add(step * k);
                if (curve.Closed) dists.Add(0);
            }
            else if (seg > 0)
            {
                for (double d = seg; d < length - Tol; d += seg) dists.Add(d);
            }
            else throw new ArgumentException("give count (>= 2) or segment_length (> 0)");
            if (includeEnds && !curve.Closed) { dists.Add(0); dists.Add(length); }
            if (fromEnd) dists = dists.Select(d => length - d).ToList();
            dists = dists.Distinct().OrderBy(d => d).ToList();

            string marker = S(req["marker"], "point");
            string layer = S(req["layer"]) ?? curve.Layer;
            bool alignBlock = B(req["align_to_curve"], true);
            double scale = D(req["block_scale"], 1);
            var owner = OwnerSpace(tr, curve);
            var created = new List<ObjectId>(); var pts = new JArray();
            ObjectId blockId = ObjectId.Null;
            if (marker != "point")
            {
                var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                if (!bt.Has(marker)) throw new ArgumentException($"block '{marker}' is not defined in this drawing");
                blockId = bt[marker];
            }
            foreach (double d in dists)
            {
                Point3d p = curve.GetPointAtDist(d0 + d);
                pts.Add(Arr(p));
                if (blockId.IsNull) { created.Add(Append(tr, owner, new DBPoint(p), layer)); continue; }
                var br = new BlockReference(p, blockId) { ScaleFactors = new Scale3d(scale) };
                if (alignBlock) { var t = curve.GetFirstDerivative(p); br.Rotation = Math.Atan2(t.Y, t.X); }
                created.Add(Append(tr, owner, br, layer));
            }
            return Created($"Placed {created.Count} marker(s).", created, new JObject { ["points"] = pts, ["length"] = R(length) });
        });

        // ── polyline / spline editing ─────────────────────────────────────────────

        private static string EditPolyline(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var id = Resolve(db, S(req["handle"]));
            string action = S(req["action"], "").ToLowerInvariant();
            var entity = Open<AcadEntity>(tr, id, OpenMode.ForWrite);

            if (action == "to_polyline")
            {
                if (entity is Polyline) return Ok("Already a polyline.", new JObject { ["modified"] = new JArray(id.Handle.ToString()) });
                var curve = entity as Curve ?? throw new ArgumentException("only curves can be converted");
                var pl = ToPolyline(curve);
                pl.SetPropertiesFrom(curve);
                ObjectId nid = Append(tr, OwnerSpace(tr, curve), pl);
                curve.Erase();
                return Created("Converted to polyline.", new[] { nid }, new JObject { ["removed"] = new JArray(id.Handle.ToString()) });
            }

            var p = entity as Polyline ?? throw new ArgumentException($"{id.Handle} is a {entity.GetType().Name}; use action 'to_polyline' first");
            int idx = I(req["index"], -1);
            void CheckIndex() { if (idx < 0 || idx >= p.NumberOfVertices) throw new ArgumentException($"index must be 0..{p.NumberOfVertices - 1}"); }

            switch (action)
            {
                case "close": p.Closed = true; break;
                case "open":  p.Closed = false; break;
                case "reverse": p.ReverseCurve(); break;
                case "add_vertex":
                {
                    var pt = P2(req["point"]);
                    int at = idx < 0 ? p.NumberOfVertices : idx;
                    double w = p.NumberOfVertices > 0 ? p.GetStartWidthAt(Math.Max(0, at - 1)) : 0;
                    p.AddVertexAt(at, pt, D(req["bulge"]), w, w);
                    break;
                }
                case "remove_vertex":
                    CheckIndex();
                    if (p.NumberOfVertices <= 2) throw new ArgumentException("a polyline needs at least 2 vertices");
                    p.RemoveVertexAt(idx); break;
                case "move_vertex": CheckIndex(); p.SetPointAt(idx, P2(req["point"])); break;
                case "set_bulge":   CheckIndex(); p.SetBulgeAt(idx, D(req["bulge"])); break;
                case "set_width":
                    if (idx >= 0)
                    {
                        CheckIndex();
                        p.SetStartWidthAt(idx, D(req["start_width"], D(req["width"])));
                        p.SetEndWidthAt(idx, D(req["end_width"], D(req["width"])));
                    }
                    else p.ConstantWidth = D(req["width"]);
                    break;
                case "straighten":
                    for (int i = 0; i < p.NumberOfVertices; i++) p.SetBulgeAt(i, 0);
                    break;
                case "set_elevation": p.Elevation = D(req["elevation"]); break;
                case "simplify":
                {
                    double tol = D(req["tolerance"], 1e-6);
                    int removed = 0;
                    for (int i = p.NumberOfVertices - 2; i >= 1; i--)
                    {
                        if (Math.Abs(p.GetBulgeAt(i - 1)) > Tol || Math.Abs(p.GetBulgeAt(i)) > Tol) continue;
                        Point2d a = p.GetPoint2dAt(i - 1), b = p.GetPoint2dAt(i), c = p.GetPoint2dAt(i + 1);
                        double cross = (b.X - a.X) * (c.Y - a.Y) - (b.Y - a.Y) * (c.X - a.X);
                        double dist = Math.Abs(cross) / Math.Max(Tol, a.GetDistanceTo(c));
                        if (dist <= tol && (b - a).DotProduct(c - b) > 0) { p.RemoveVertexAt(i); removed++; }
                    }
                    return Ok($"Removed {removed} collinear vertex(es).", new JObject { ["modified"] = new JArray(id.Handle.ToString()), ["vertices"] = p.NumberOfVertices });
                }
                case "set_start_vertex":
                {
                    // Rotate the vertex list so the closed polyline starts at 'index'.
                    if (!p.Closed) throw new ArgumentException("set_start_vertex applies to closed polylines");
                    CheckIndex();
                    int n = p.NumberOfVertices;
                    var verts = Enumerable.Range(0, n).Select(i => (pt: p.GetPoint2dAt(i), b: p.GetBulgeAt(i), sw: p.GetStartWidthAt(i), ew: p.GetEndWidthAt(i))).ToList();
                    for (int i = n - 1; i >= 0; i--) p.RemoveVertexAt(i);
                    for (int i = 0; i < n; i++) { var v = verts[(idx + i) % n]; p.AddVertexAt(i, v.pt, v.b, v.sw, v.ew); }
                    break;
                }
                default:
                    throw new ArgumentException("action must be close, open, reverse, add_vertex, remove_vertex, move_vertex, set_bulge, set_width, straighten, simplify, set_elevation, set_start_vertex or to_polyline");
            }
            return Ok($"Polyline {action} done.", new JObject { ["modified"] = new JArray(id.Handle.ToString()), ["vertices"] = p.NumberOfVertices, ["length"] = R(p.Length) });
        });

        private static string EditSpline(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var id = Resolve(db, S(req["handle"]));
            var sp = Open<Spline>(tr, id, OpenMode.ForWrite);
            string action = S(req["action"], "").ToLowerInvariant();
            int idx = I(req["index"], -1);
            switch (action)
            {
                case "add_fit_point":
                    if (!sp.HasFitData) throw new ArgumentException("this spline has no fit data (it was built from control points)");
                    sp.InsertFitPointAt(idx < 0 ? sp.NumFitPoints : idx, Flat(P3(req["point"])));
                    break;
                case "remove_fit_point":
                    if (!sp.HasFitData) throw new ArgumentException("this spline has no fit data");
                    sp.RemoveFitPointAt(idx); break;
                case "move_fit_point":
                    if (!sp.HasFitData) throw new ArgumentException("this spline has no fit data");
                    sp.SetFitPointAt(idx, Flat(P3(req["point"]))); break;
                case "move_control_point":
                    sp.SetControlPointAt(idx, Flat(P3(req["point"]))); break;
                case "elevate_degree":
                    sp.ElevateDegree(I(req["degree"], sp.Degree + 1)); break;
                case "reverse": sp.ReverseCurve(); break;
                case "to_polyline":
                {
                    var pl = (Polyline)sp.ToPolyline();
                    pl.SetPropertiesFrom(sp);
                    ObjectId nid = Append(tr, OwnerSpace(tr, sp), pl);
                    sp.Erase();
                    return Created("Spline converted to polyline.", new[] { nid }, new JObject { ["removed"] = new JArray(id.Handle.ToString()) });
                }
                default:
                    throw new ArgumentException("action must be add_fit_point, remove_fit_point, move_fit_point, move_control_point, elevate_degree, reverse or to_polyline");
            }
            return Ok($"Spline {action} done.", new JObject { ["modified"] = new JArray(id.Handle.ToString()), ["fit_points"] = sp.HasFitData ? sp.NumFitPoints : 0, ["control_points"] = sp.NumControlPoints });
        });

        private static string ReverseCurves(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            foreach (var id in ids) OpenCurve(tr, id, OpenMode.ForWrite).ReverseCurve();
            return Ok($"Reversed {ids.Count} curve(s).", new JObject { ["modified"] = Handles(ids) });
        });

        // ── properties / draw order ───────────────────────────────────────────────

        private static string SetProperties(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            int changed = 0;
            foreach (var id in ids)
            {
                var e = Open<AcadEntity>(tr, id, OpenMode.ForWrite);
                if (req["layer"] != null)          e.Layer = EnsureLayer(tr, db, S(req["layer"]));
                if (req["color"] != null)          e.Color = ColorFrom(req["color"]);
                if (req["linetype"] != null)       e.LinetypeId = LinetypeId(tr, db, S(req["linetype"]));
                if (req["lineweight"] != null)     e.LineWeight = LineWeightFrom(req["lineweight"]);
                if (req["linetype_scale"] != null) e.LinetypeScale = D(req["linetype_scale"]);
                if (req["transparency"] != null)
                {
                    int pct = Math.Max(0, Math.Min(90, I(req["transparency"])));
                    e.Transparency = new Transparency((byte)Math.Round(255 * (100 - pct) / 100.0));
                }
                if (req["visible"] != null)        e.Visible = B(req["visible"], true);
                changed++;
            }
            return Ok($"Updated {changed} object(s).", new JObject { ["modified"] = Handles(ids) });
        });

        private static string MatchProperties(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var src = Open<AcadEntity>(tr, Resolve(db, S(req["source"])));
            var targets = ResolveAll(db, req["targets"], "targets");
            var which = (req["properties"] as JArray)?.Select(t => t.ToString().ToLowerInvariant()).ToHashSet()
                        ?? new HashSet<string> { "layer", "color", "linetype", "lineweight", "linetype_scale", "transparency", "text", "dimension", "hatch" };
            foreach (var id in targets)
            {
                var e = Open<AcadEntity>(tr, id, OpenMode.ForWrite);
                if (which.Contains("layer"))          e.Layer = src.Layer;
                if (which.Contains("color"))          e.Color = src.Color;
                if (which.Contains("linetype"))       e.LinetypeId = src.LinetypeId;
                if (which.Contains("lineweight"))     e.LineWeight = src.LineWeight;
                if (which.Contains("linetype_scale")) e.LinetypeScale = src.LinetypeScale;
                if (which.Contains("transparency"))   e.Transparency = src.Transparency;
                if (which.Contains("text"))
                {
                    if (src is DBText st && e is DBText dt) { dt.TextStyleId = st.TextStyleId; dt.Height = st.Height; dt.WidthFactor = st.WidthFactor; }
                    if (src is MText sm && e is MText dm)   { dm.TextStyleId = sm.TextStyleId; dm.TextHeight = sm.TextHeight; }
                    if (src is DBText st2 && e is MText dm2) { dm2.TextStyleId = st2.TextStyleId; dm2.TextHeight = st2.Height; }
                    if (src is MText sm2 && e is DBText dt2) { dt2.TextStyleId = sm2.TextStyleId; dt2.Height = sm2.TextHeight; }
                }
                if (which.Contains("dimension") && src is Dimension sd && e is Dimension dd) dd.DimensionStyle = sd.DimensionStyle;
                if (which.Contains("hatch") && src is Hatch sh && e is Hatch dh)
                {
                    dh.SetHatchPattern(sh.PatternType, sh.PatternName);
                    dh.PatternScale = sh.PatternScale; dh.PatternAngle = sh.PatternAngle;
                    dh.EvaluateHatch(true);
                }
            }
            return Ok($"Matched properties onto {targets.Count} object(s).", new JObject { ["modified"] = Handles(targets) });
        });

        private static string SetDrawOrder(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            string position = S(req["position"], "front").ToLowerInvariant();
            var first = Open<AcadEntity>(tr, ids[0]);
            var owner = (BlockTableRecord)tr.GetObject(first.OwnerId, OpenMode.ForRead);
            var dot = (DrawOrderTable)tr.GetObject(owner.DrawOrderTableId, OpenMode.ForWrite);
            var col = new ObjectIdCollection(ids.ToArray());
            switch (position)
            {
                case "front": dot.MoveToTop(col); break;
                case "back":  dot.MoveToBottom(col); break;
                case "above": dot.MoveAbove(col, Resolve(db, S(req["reference"]))); break;
                case "below": dot.MoveBelow(col, Resolve(db, S(req["reference"]))); break;
                default: throw new ArgumentException("position must be front, back, above or below");
            }
            return Ok($"Draw order: {ids.Count} object(s) moved to {position}.", new JObject { ["modified"] = Handles(ids) });
        });
    }
}
