using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;

using AcadEntity = Autodesk.AutoCAD.DatabaseServices.Entity;

namespace CADAgent
{
    public static partial class CadCommands
    {
        private static void RegisterDraw()
        {
            Add("draw_point",          DrawPoint);
            Add("draw_construction_line", DrawConstructionLine);
            Add("draw_polyline",       DrawPolyline);
            Add("draw_rectangle",      DrawRectangle);
            Add("draw_polygon",        DrawPolygon);
            Add("draw_circle",         DrawCircle);
            Add("draw_arc_by",         DrawArcBy);
            Add("draw_ellipse",        DrawEllipse);
            Add("draw_spline",         DrawSpline);
            Add("draw_donut",          DrawDonut);
            Add("draw_revision_cloud", DrawRevisionCloud);
            Add("draw_wipeout",        DrawWipeout);
            Add("create_region",       CreateRegion);
            Add("region_boolean",      RegionBoolean);
            Add("trace_boundary",      TraceBoundary, appThread: true);
        }

        // ── point ─────────────────────────────────────────────────────────────────

        private static string DrawPoint(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var pts = req["points"] is JArray ? Points(req["points"]) : new List<Point3d> { P3(req["point"]) };
            var ms = ModelSpace(tr, db);
            var ids = pts.Select(p => Append(tr, ms, new DBPoint(p), S(req["layer"], "0"))).ToList();
            if (req["display_mode"] != null) db.Pdmode = I(req["display_mode"]);
            if (req["display_size"] != null) db.Pdsize = D(req["display_size"]);
            return Created($"{ids.Count} point(s) created.", ids);
        });

        // ── xline / ray ───────────────────────────────────────────────────────────

        private static string DrawConstructionLine(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            Point3d basePt = P3(req["base_point"]);
            Vector3d dir;
            if (req["second_point"] != null) dir = Dir(basePt, P3(req["second_point"]));
            else if (req["angle_deg"] != null) { double a = Rad(req["angle_deg"]); dir = new Vector3d(Math.Cos(a), Math.Sin(a), 0); }
            else throw new ArgumentException("give second_point or angle_deg");

            string kind = S(req["kind"], "xline").ToLowerInvariant();
            AcadEntity e;
            if (kind == "ray") e = new Ray { BasePoint = basePt, UnitDir = dir };
            else               e = new Xline { BasePoint = basePt, UnitDir = dir };
            ObjectId id = Append(tr, ModelSpace(tr, db), e, S(req["layer"], "0"));
            return Created($"{kind} created.", id);
        });

        // ── polyline (with bulges / widths) ───────────────────────────────────────

        private static string DrawPolyline(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var verts = Points(req["vertices"]);
            if (verts.Count < 2) throw new ArgumentException("need at least 2 vertices");
            var bulges = (req["bulges"] as JArray)?.Select(t => D(t)).ToList();
            double width = D(req["width"], 0);

            var pl = new Polyline();
            for (int i = 0; i < verts.Count; i++)
            {
                double b = bulges != null && i < bulges.Count ? bulges[i] : 0;
                pl.AddVertexAt(i, new Point2d(verts[i].X, verts[i].Y), b, width, width);
            }
            pl.Closed = B(req["closed"], false);
            if (req["elevation"] != null) pl.Elevation = D(req["elevation"]);
            ObjectId id = Append(tr, ModelSpace(tr, db), pl, S(req["layer"], "0"));
            return Created("Polyline created.", new[] { id }, new JObject { ["length"] = R(pl.Length), ["area"] = pl.Closed ? R(pl.Area) : 0 });
        });

        // ── rectangle ─────────────────────────────────────────────────────────────

        private static string DrawRectangle(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            Point3d c1 = P3(req["corner1"]);
            Point3d c2;
            if (req["corner2"] != null) c2 = P3(req["corner2"]);
            else if (req["width"] != null && req["height"] != null)
                c2 = new Point3d(c1.X + D(req["width"]), c1.Y + D(req["height"]), 0);
            else throw new ArgumentException("give corner2, or width and height");

            double rot = Rad(req["rotation_deg"]);
            var pts = new[]
            {
                new Point3d(c1.X, c1.Y, 0), new Point3d(c2.X, c1.Y, 0),
                new Point3d(c2.X, c2.Y, 0), new Point3d(c1.X, c2.Y, 0),
            };
            if (Math.Abs(rot) > Tol)
            {
                var m = Matrix3d.Rotation(rot, Vector3d.ZAxis, c1);
                pts = pts.Select(p => p.TransformBy(m)).ToArray();
            }

            var pl = new Polyline();
            for (int i = 0; i < 4; i++) pl.AddVertexAt(i, new Point2d(pts[i].X, pts[i].Y), 0, 0, 0);
            pl.Closed = true;

            double fillet = D(req["fillet_radius"], 0);
            if (fillet > Tol) FilletAllPolylineVertices(pl, fillet);
            double chamfer = D(req["chamfer_distance"], 0);
            if (chamfer > Tol) ChamferAllPolylineVertices(pl, chamfer);

            ObjectId id = Append(tr, ModelSpace(tr, db), pl, S(req["layer"], "0"));
            return Created("Rectangle created.", new[] { id }, new JObject { ["area"] = R(pl.Area) });
        });

        // ── regular polygon ───────────────────────────────────────────────────────

        private static string DrawPolygon(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            int sides = I(req["sides"], 0);
            if (sides < 3) throw new ArgumentException("sides must be >= 3");
            var pts = new List<Point3d>();

            if (req["edge_start"] != null && req["edge_end"] != null)
            {
                // POLYGON Edge option: the edge runs counter-clockwise from start to end.
                Point3d a = P3(req["edge_start"]), b = P3(req["edge_end"]);
                double side = a.DistanceTo(b);
                double interior = Math.PI - 2 * Math.PI / sides;
                Point3d cur = a; double heading = Angle(a, b);
                for (int i = 0; i < sides; i++)
                {
                    pts.Add(cur);
                    cur = Polar(cur, heading, side);
                    heading += Math.PI - interior;
                }
            }
            else
            {
                Point3d center = P3(req["center"]);
                double radius = D(req["radius"]);
                if (radius <= 0) throw new ArgumentException("radius must be > 0");
                bool circumscribed = S(req["mode"], "inscribed").ToLowerInvariant().StartsWith("circum");
                double r = circumscribed ? radius / Math.Cos(Math.PI / sides) : radius;
                double start = Rad(req["rotation_deg"]) + (circumscribed ? Math.PI / sides : 0);
                for (int i = 0; i < sides; i++)
                    pts.Add(Polar(center, start + 2 * Math.PI * i / sides, r));
            }

            var pl = new Polyline();
            for (int i = 0; i < pts.Count; i++) pl.AddVertexAt(i, new Point2d(pts[i].X, pts[i].Y), 0, 0, 0);
            pl.Closed = true;
            ObjectId id = Append(tr, ModelSpace(tr, db), pl, S(req["layer"], "0"));
            return Created($"{sides}-sided polygon created.", new[] { id }, new JObject { ["area"] = R(pl.Area), ["vertices"] = new JArray(pts.Select(Arr)) });
        });

        // ── circle ────────────────────────────────────────────────────────────────

        private static string DrawCircle(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string method = S(req["method"], "center_radius").ToLowerInvariant();
            Point3d center; double radius;
            var pts = Points(req["points"]);

            switch (method)
            {
                case "center_radius":
                    center = P3(req["center"]); radius = D(req["radius"]); break;
                case "center_diameter":
                    center = P3(req["center"]); radius = D(req["diameter"]) / 2; break;
                case "two_points":
                    if (pts.Count < 2) throw new ArgumentException("two_points needs 2 points");
                    center = new Point3d((pts[0].X + pts[1].X) / 2, (pts[0].Y + pts[1].Y) / 2, 0);
                    radius = pts[0].DistanceTo(pts[1]) / 2; break;
                case "three_points":
                {
                    if (pts.Count < 3) throw new ArgumentException("three_points needs 3 points");
                    var ca = new CircularArc3d(Flat(pts[0]), Flat(pts[1]), Flat(pts[2]));
                    center = ca.Center; radius = ca.Radius; break;
                }
                case "two_points_radius":
                {
                    if (pts.Count < 2) throw new ArgumentException("two_points_radius needs 2 points");
                    radius = D(req["radius"]);
                    double d = pts[0].DistanceTo(pts[1]);
                    if (d > 2 * radius + Tol) throw new ArgumentException($"points are {R(d)} apart; radius must be at least {R(d / 2)}");
                    double h = Math.Sqrt(Math.Max(0, radius * radius - d * d / 4));
                    var mid = new Point3d((pts[0].X + pts[1].X) / 2, (pts[0].Y + pts[1].Y) / 2, 0);
                    var n = Dir(pts[0], pts[1]).RotateBy(Math.PI / 2, Vector3d.ZAxis);
                    // Of the two candidates take the one on the side of 'side_point' (default: left of p1→p2).
                    Point3d c1 = mid + n * h, c2 = mid - n * h;
                    Point3d? side = P3Opt(req["side_point"]);
                    center = side == null ? c1 : (c1.DistanceTo(side.Value) <= c2.DistanceTo(side.Value) ? c1 : c2);
                    break;
                }
                case "tangent_tangent_radius":
                case "ttr":
                {
                    radius = D(req["radius"]);
                    var handles = ResolveAll(db, req["tangent_handles"], "tangent_handles");
                    if (handles.Count != 2) throw new ArgumentException("tangent_tangent_radius needs exactly 2 tangent_handles");
                    var tpts = Points(req["tangent_points"]);
                    Curve a = OpenCurve(tr, handles[0]), b = OpenCurve(tr, handles[1]);
                    Point3d pa = tpts.Count > 0 ? tpts[0] : a.StartPoint;
                    Point3d pb = tpts.Count > 1 ? tpts[1] : b.StartPoint;
                    var candidates = FilletCenters(a, b, radius);
                    if (candidates.Count == 0) throw new ArgumentException("no circle of that radius is tangent to both objects");
                    center = candidates.OrderBy(c => TangentScore(a, b, c, pa, pb)).First();
                    break;
                }
                default:
                    throw new ArgumentException("method must be center_radius, center_diameter, two_points, three_points, two_points_radius or tangent_tangent_radius");
            }
            if (radius <= 0) throw new ArgumentException("radius must be > 0");

            var circle = new Circle(Flat(center), Vector3d.ZAxis, radius);
            ObjectId id = Append(tr, ModelSpace(tr, db), circle, S(req["layer"], "0"));
            return Created("Circle created.", new[] { id }, new JObject { ["center"] = Arr(center), ["radius"] = R(radius) });
        });

        // ── arc by construction method ────────────────────────────────────────────

        private static string DrawArcBy(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string method = S(req["method"], "three_points").ToLowerInvariant();
            var pts = Points(req["points"]);
            Point3d? start = P3Opt(req["start"]), end = P3Opt(req["end"]), center = P3Opt(req["center"]);
            Arc arc;

            switch (method)
            {
                case "three_points":
                {
                    if (pts.Count < 3) throw new ArgumentException("three_points needs points [p1, p2, p3]");
                    arc = ArcThrough(pts[0], pts[1], pts[2]);
                    break;
                }
                case "start_center_end":
                {
                    Point3d s = start ?? pts[0], c = center ?? pts[1], e = end ?? pts[2];
                    double r = s.DistanceTo(c);
                    arc = MakeArc(c, r, Angle(c, s), Angle(c, e), !B(req["clockwise"]));
                    break;
                }
                case "start_center_angle":
                {
                    Point3d s = start ?? pts[0], c = center ?? pts[1];
                    double inc = Rad(req["angle_deg"]);
                    double a0 = Angle(c, s);
                    arc = MakeArc(c, s.DistanceTo(c), a0, a0 + inc, inc >= 0);
                    break;
                }
                case "start_center_length":
                {
                    Point3d s = start ?? pts[0], c = center ?? pts[1];
                    double r = s.DistanceTo(c);
                    double chord = D(req["chord_length"]);
                    if (Math.Abs(chord) > 2 * r) throw new ArgumentException("chord longer than the diameter");
                    double inc = 2 * Math.Asin(Math.Abs(chord) / (2 * r));
                    if (chord < 0) inc = 2 * Math.PI - inc;
                    double a0 = Angle(c, s);
                    arc = MakeArc(c, r, a0, a0 + inc, true);
                    break;
                }
                case "start_end_radius":
                {
                    Point3d s = start ?? pts[0], e = end ?? pts[1];
                    double r = D(req["radius"]);
                    double d = s.DistanceTo(e);
                    if (Math.Abs(r) * 2 < d - Tol) throw new ArgumentException($"radius must be at least half the chord ({R(d / 2)})");
                    double h = Math.Sqrt(Math.Max(0, r * r - d * d / 4));
                    var mid = new Point3d((s.X + e.X) / 2, (s.Y + e.Y) / 2, 0);
                    var n = Dir(s, e).RotateBy(Math.PI / 2, Vector3d.ZAxis);
                    // AutoCAD: positive radius = minor arc counter-clockwise from start to end.
                    bool major = r < 0;
                    Point3d c = major ? mid - n * h : mid + n * h;
                    arc = MakeArc(c, Math.Abs(r), Angle(c, s), Angle(c, e), true);
                    break;
                }
                case "start_end_angle":
                {
                    Point3d s = start ?? pts[0], e = end ?? pts[1];
                    double inc = Rad(req["angle_deg"]);
                    if (Math.Abs(inc) < Tol || Math.Abs(inc) >= 2 * Math.PI) throw new ArgumentException("angle_deg must be in (0, 360)");
                    double d = s.DistanceTo(e);
                    double r = d / (2 * Math.Sin(Math.Abs(inc) / 2));
                    double h = r * Math.Cos(Math.Abs(inc) / 2);
                    var mid = new Point3d((s.X + e.X) / 2, (s.Y + e.Y) / 2, 0);
                    var n = Dir(s, e).RotateBy(Math.PI / 2, Vector3d.ZAxis);
                    // Counter-clockwise from start to end: centre is left of the chord for a minor arc.
                    bool ccw = inc > 0;
                    bool minor = Math.Abs(inc) <= Math.PI;
                    Point3d c = (ccw == minor) ? mid + n * h : mid - n * h;
                    arc = MakeArc(c, r, Angle(c, s), Angle(c, e), ccw);
                    break;
                }
                case "start_end_direction":
                {
                    Point3d s = start ?? pts[0], e = end ?? pts[1];
                    double dirAng = Rad(req["direction_deg"]);
                    var t = new Vector3d(Math.Cos(dirAng), Math.Sin(dirAng), 0);
                    var n = t.RotateBy(Math.PI / 2, Vector3d.ZAxis);
                    var d = e - s;
                    double dn = d.DotProduct(n);
                    if (Math.Abs(dn) < Tol) throw new ArgumentException("direction is collinear with the chord");
                    double r = d.LengthSqrd / (2 * dn);          // signed: positive = centre on the left
                    Point3d c = s + n * r;
                    arc = MakeArc(c, Math.Abs(r), Angle(c, s), Angle(c, e), r > 0);
                    break;
                }
                default:
                    throw new ArgumentException("method must be three_points, start_center_end, start_center_angle, start_center_length, start_end_radius, start_end_angle or start_end_direction");
            }

            ObjectId id = Append(tr, ModelSpace(tr, db), arc, S(req["layer"], "0"));
            return Created("Arc created.", new[] { id }, new JObject
            {
                ["center"] = Arr(arc.Center), ["radius"] = R(arc.Radius),
                ["start_angle_deg"] = R(Deg(arc.StartAngle)), ["end_angle_deg"] = R(Deg(arc.EndAngle)),
                ["start_point"] = Arr(arc.StartPoint), ["end_point"] = Arr(arc.EndPoint), ["length"] = R(arc.Length),
            });
        });

        /// <summary>Arc through three points, running from p1 via p2 to p3 (either direction).</summary>
        public static Arc ArcThrough(Point3d p1, Point3d p2, Point3d p3)
        {
            var ca = new CircularArc3d(Flat(p1), Flat(p2), Flat(p3));
            Point3d c = ca.Center;
            bool ccw = SignedArea(new[] { Flat(p1), Flat(p2), Flat(p3) }) > 0;
            return MakeArc(c, ca.Radius, Angle(c, p1), Angle(c, p3), ccw);
        }

        /// <summary>AutoCAD arcs always run counter-clockwise from StartAngle to EndAngle; swap when the caller wants clockwise.</summary>
        public static Arc MakeArc(Point3d center, double radius, double a0, double a1, bool ccw)
        {
            if (radius <= Tol) throw new ArgumentException("arc radius must be > 0");
            return ccw ? new Arc(Flat(center), Vector3d.ZAxis, radius, Norm(a0), Norm(a1))
                       : new Arc(Flat(center), Vector3d.ZAxis, radius, Norm(a1), Norm(a0));
        }

        // ── ellipse ───────────────────────────────────────────────────────────────

        private static string DrawEllipse(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            Point3d center = Flat(P3(req["center"]));
            Vector3d major;
            double ratio;
            if (req["major_axis_end"] != null)
                major = Flat(P3(req["major_axis_end"])) - center;
            else if (req["major_radius"] != null)
            {
                double a = Rad(req["rotation_deg"]);
                major = new Vector3d(Math.Cos(a), Math.Sin(a), 0) * D(req["major_radius"]);
            }
            else throw new ArgumentException("give major_axis_end or major_radius");

            if (req["ratio"] != null) ratio = D(req["ratio"]);
            else if (req["minor_radius"] != null) ratio = D(req["minor_radius"]) / major.Length;
            else if (req["minor_axis_end"] != null) ratio = (Flat(P3(req["minor_axis_end"])) - center).Length / major.Length;
            else throw new ArgumentException("give ratio, minor_radius or minor_axis_end");

            if (ratio > 1)
            {
                // AutoCAD wants the longer axis as the major one; rotate the frame 90° and invert the ratio.
                major = major.RotateBy(Math.PI / 2, Vector3d.ZAxis) * ratio;
                ratio = 1 / ratio;
            }
            if (ratio <= 0 || ratio > 1) throw new ArgumentException("ratio must be in (0, 1]");

            double startAng = req["start_angle_deg"] != null ? Rad(req["start_angle_deg"]) : 0;
            double endAng   = req["end_angle_deg"]   != null ? Rad(req["end_angle_deg"])   : 2 * Math.PI;
            var el = new Ellipse(center, Vector3d.ZAxis, major, ratio, startAng, endAng);
            ObjectId id = Append(tr, ModelSpace(tr, db), el, S(req["layer"], "0"));
            return Created("Ellipse created.", new[] { id }, new JObject { ["major_radius"] = R(el.MajorRadius), ["minor_radius"] = R(el.MinorRadius) });
        });

        // ── spline ────────────────────────────────────────────────────────────────

        private static string DrawSpline(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var pts = Points(req["points"]);
            if (pts.Count < 2) throw new ArgumentException("need at least 2 points");
            int degree = I(req["degree"], 3);
            bool closed = B(req["closed"]);
            string mode = S(req["mode"], "fit").ToLowerInvariant();
            Spline sp;

            if (mode == "control")
            {
                if (closed && pts[0].DistanceTo(pts[pts.Count - 1]) > Tol) pts.Add(pts[0]);
                int n = pts.Count;
                if (n <= degree) throw new ArgumentException($"need more than {degree} control points for degree {degree}");
                var cps = new Point3dCollection(pts.Select(Flat).ToArray());
                var knots = new DoubleCollection();
                int m = n + degree + 1;
                for (int i = 0; i < m; i++)
                {
                    if (i <= degree) knots.Add(0);
                    else if (i >= n) knots.Add(1);
                    else knots.Add((double)(i - degree) / (n - degree));
                }
                var weights = new DoubleCollection();
                sp = new Spline(degree, false, closed, closed, cps, knots, weights, 0, 0);
            }
            else
            {
                if (closed && pts[0].DistanceTo(pts[pts.Count - 1]) > Tol) pts.Add(pts[0]);
                var fit = new Point3dCollection(pts.Select(Flat).ToArray());
                if (req["start_tangent"] != null || req["end_tangent"] != null)
                {
                    Vector3d st = req["start_tangent"] != null ? P3(req["start_tangent"]).GetAsVector().GetNormal() : Vector3d.XAxis;
                    Vector3d et = req["end_tangent"]   != null ? P3(req["end_tangent"]).GetAsVector().GetNormal()   : Vector3d.XAxis;
                    sp = new Spline(fit, st, et, degree + 1, D(req["fit_tolerance"], 0));
                }
                else
                    sp = new Spline(fit, degree + 1, D(req["fit_tolerance"], 0));
            }

            ObjectId id = Append(tr, ModelSpace(tr, db), sp, S(req["layer"], "0"));
            return Created("Spline created.", new[] { id }, new JObject { ["length"] = R(SafeLength(sp)) });
        });

        private static double SafeLength(Curve c)
        {
            try { return c.GetDistanceAtParameter(c.EndParam) - c.GetDistanceAtParameter(c.StartParam); }
            catch { return 0; }
        }

        // ── donut ─────────────────────────────────────────────────────────────────

        private static string DrawDonut(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            Point3d c = Flat(P3(req["center"]));
            double inner = D(req["inner_diameter"]), outer = D(req["outer_diameter"]);
            if (outer <= 0 || inner < 0 || inner >= outer) throw new ArgumentException("need 0 <= inner_diameter < outer_diameter");
            double rMid = (inner + outer) / 4, width = (outer - inner) / 2;
            var pl = new Polyline();
            pl.AddVertexAt(0, new Point2d(c.X - rMid, c.Y), 1, width, width);
            pl.AddVertexAt(1, new Point2d(c.X + rMid, c.Y), 1, width, width);
            pl.Closed = true;
            ObjectId id = Append(tr, ModelSpace(tr, db), pl, S(req["layer"], "0"));
            return Created("Donut created.", id);
        });

        // ── revision cloud ────────────────────────────────────────────────────────

        private static string DrawRevisionCloud(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var verts = Points(req["vertices"]).Select(Flat).ToList();
            if (verts.Count < 3) throw new ArgumentException("need at least 3 vertices");
            double arcLen = D(req["arc_length"], 0);
            if (arcLen <= 0)
            {
                double perim = 0;
                for (int i = 0; i < verts.Count; i++) perim += verts[i].DistanceTo(verts[(i + 1) % verts.Count]);
                arcLen = perim / 40;
            }
            bool ccw = SignedArea(verts) > 0;
            // Cloud arcs bulge outward: for a counter-clockwise outline the outside is to the right of travel (clockwise arcs).
            double bulge = ccw ? -0.55 : 0.55;

            var pl = new Polyline();
            int idx = 0;
            for (int i = 0; i < verts.Count; i++)
            {
                Point3d a = verts[i], b = verts[(i + 1) % verts.Count];
                double len = a.DistanceTo(b);
                int n = Math.Max(1, (int)Math.Round(len / arcLen));
                for (int k = 0; k < n; k++)
                {
                    Point3d p = a + (b - a) * ((double)k / n);
                    pl.AddVertexAt(idx++, new Point2d(p.X, p.Y), bulge, 0, 0);
                }
            }
            pl.Closed = true;
            ObjectId id = Append(tr, ModelSpace(tr, db), pl, S(req["layer"], "0"));
            return Created("Revision cloud created.", id);
        });

        // ── wipeout ───────────────────────────────────────────────────────────────

        private static string DrawWipeout(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var verts = Points(req["vertices"]);
            if (verts.Count < 3) throw new ArgumentException("need at least 3 vertices");
            var col = new Point2dCollection(verts.Select(p => new Point2d(p.X, p.Y)).ToArray());
            if (verts[0].DistanceTo(verts[verts.Count - 1]) > Tol) col.Add(new Point2d(verts[0].X, verts[0].Y));
            var wo = new Wipeout();
            wo.SetDatabaseDefaults(db);
            wo.SetFrom(col, Vector3d.ZAxis);
            ObjectId id = Append(tr, ModelSpace(tr, db), wo, S(req["layer"], "0"));
            return Created("Wipeout created.", id);
        });

        // ── regions ───────────────────────────────────────────────────────────────

        private static string CreateRegion(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            var curves = new DBObjectCollection();
            foreach (var id in ids) curves.Add(tr.GetObject(id, OpenMode.ForRead));
            DBObjectCollection regions;
            try { regions = Region.CreateFromCurves(curves); }
            catch (Autodesk.AutoCAD.Runtime.Exception ex) { throw new ArgumentException($"the curves do not form closed loops ({ex.ErrorStatus})"); }

            var ms = ModelSpace(tr, db);
            string layer = S(req["layer"]) ?? Open<AcadEntity>(tr, ids[0]).Layer;
            var created = new List<ObjectId>();
            foreach (DBObject r in regions) created.Add(Append(tr, ms, (AcadEntity)r, layer));

            if (B(req["delete_sources"]))
                foreach (var id in ids) Open<AcadEntity>(tr, id, OpenMode.ForWrite).Erase();

            return Created($"{created.Count} region(s) created.", created, new JObject
            {
                ["areas"] = new JArray(created.Select(id => R(Open<Region>(tr, id).Area))),
            });
        });

        private static string RegionBoolean(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string op = S(req["operation"], "union").ToLowerInvariant();
            BooleanOperationType type = op switch
            {
                "union"     => BooleanOperationType.BoolUnite,
                "subtract"  => BooleanOperationType.BoolSubtract,
                "intersect" => BooleanOperationType.BoolIntersect,
                _ => throw new ArgumentException("operation must be union, subtract or intersect"),
            };
            var target = Open<Region>(tr, Resolve(db, S(req["target"])), OpenMode.ForWrite);
            var tools = ResolveAll(db, req["tools"], "tools");
            foreach (var id in tools)
            {
                var tool = Open<Region>(tr, id, OpenMode.ForWrite);
                target.BooleanOperation(type, tool);
                tool.Erase();   // the operand is consumed, exactly as the REGION booleans do
            }
            return Ok($"{op} applied; {tools.Count} operand region(s) consumed.", new JObject
            {
                ["modified"] = new JArray(target.Handle.ToString()),
                ["removed"]  = Handles(tools),
                ["area"] = R(target.Area),
            });
        });

        // ── boundary trace (needs Editor → application thread) ───────────────────

        private static string TraceBoundary(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            Point3d seed = Flat(P3(req["point"]));
            bool asRegion = S(req["kind"], "polyline").ToLowerInvariant() == "region";
            DBObjectCollection found;
            try { found = doc.Editor.TraceBoundary(seed, asRegion); }
            catch (Autodesk.AutoCAD.Runtime.Exception ex) { throw new ArgumentException($"no closed boundary encloses that point ({ex.ErrorStatus})"); }
            if (found.Count == 0) throw new ArgumentException("no closed boundary encloses that point");

            var ms = ModelSpace(tr, db);
            var created = new List<ObjectId>();
            foreach (DBObject o in found) created.Add(Append(tr, ms, (AcadEntity)o, S(req["layer"], "0")));
            return Created($"{created.Count} boundary object(s) created.", created);
        });
    }
}
