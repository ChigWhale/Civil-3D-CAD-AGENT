using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;

using AcadEntity = Autodesk.AutoCAD.DatabaseServices.Entity;
using AcIntersect = Autodesk.AutoCAD.DatabaseServices.Intersect;

namespace CADAgent
{
    public static partial class CadCommands
    {
        private static void RegisterQuery()
        {
            Add("list_intents",      (req, doc) => Ok($"{Intents.Count()} intents.", new JObject { ["intents"] = new JArray(Intents) }));
            Add("get_properties",    GetProperties);
            Add("select_entities",   SelectEntities);
            Add("entity_key_points", EntityKeyPoints);
            Add("closest_point_on",  ClosestPointOn);
            Add("intersections",     Intersections);
            Add("tangent_points",    TangentPoints);
            Add("measure_entities",  MeasureEntities);
            Add("angle_between",     AngleBetween);
            Add("point_along",       PointAlong);
            Add("bounding_box",      BoundingBox);
            Add("find_duplicates",   FindDuplicates);
        }

        // ── entity summaries (shared by select / properties / results) ────────────

        public static string TypeName(AcadEntity e) => e switch
        {
            Polyline _ => "Polyline", Polyline2d _ => "Polyline2d", Polyline3d _ => "Polyline3d",
            Line _ => "Line", Arc _ => "Arc", Circle _ => "Circle", Ellipse _ => "Ellipse", Spline _ => "Spline",
            DBPoint _ => "Point", DBText _ => "Text", MText _ => "MText", Hatch _ => "Hatch",
            Dimension _ => "Dimension", MLeader _ => "MLeader", Leader _ => "Leader", FeatureControlFrame _ => "Tolerance",
            Table _ => "Table", BlockReference _ => "BlockReference", Region _ => "Region", Xline _ => "Xline", Ray _ => "Ray",
            Wipeout _ => "Wipeout", RasterImage _ => "Image", Viewport _ => "Viewport",
            Solid _ => "Solid", Face _ => "Face", _ => e.GetType().Name,
        };

        private static bool TypeMatches(AcadEntity e, string wanted)
        {
            string w = wanted.Trim().ToLowerInvariant().Replace("_", "").Replace(" ", "");
            string t = TypeName(e).ToLowerInvariant();
            if (w == t || w == e.GetType().Name.ToLowerInvariant()) return true;
            return w switch
            {
                "block" or "insert" or "blockref" => e is BlockReference,
                "text" => e is DBText || e is MText,
                "dbtext" or "singlelinetext" => e is DBText,
                "curve" => e is Curve,
                "dim" or "dimension" => e is Dimension,
                "leader" => e is MLeader || e is Leader,
                "pline" or "lwpolyline" => e is Polyline,
                "point" or "dbpoint" => e is DBPoint,
                _ => false,
            };
        }

        public static JObject Summarize(Transaction tr, AcadEntity e, bool detail)
        {
            var o = new JObject
            {
                ["handle"] = e.Handle.ToString(),
                ["type"] = TypeName(e),
                ["layer"] = e.Layer,
            };
            if (detail)
            {
                o["color"] = e.Color.ToString();
                o["linetype"] = e.Linetype;
                o["lineweight"] = e.LineWeight.ToString();
                o["linetype_scale"] = R(e.LinetypeScale);
                o["visible"] = e.Visible;
                o["bounds"] = BoundsJson(e);
            }
            try { AddGeometry(tr, e, o, detail); } catch { }
            return o;
        }

        private static void AddGeometry(Transaction tr, AcadEntity e, JObject o, bool detail)
        {
            switch (e)
            {
                case Line ln:
                    o["start"] = Arr(ln.StartPoint); o["end"] = Arr(ln.EndPoint); o["length"] = R(ln.Length); o["angle_deg"] = R(Deg(ln.Angle));
                    break;
                case Arc ar:
                    o["center"] = Arr(ar.Center); o["radius"] = R(ar.Radius);
                    o["start_angle_deg"] = R(Deg(ar.StartAngle)); o["end_angle_deg"] = R(Deg(ar.EndAngle));
                    o["start"] = Arr(ar.StartPoint); o["end"] = Arr(ar.EndPoint); o["length"] = R(ar.Length);
                    break;
                case Circle ci:
                    o["center"] = Arr(ci.Center); o["radius"] = R(ci.Radius); o["area"] = R(ci.Area); o["circumference"] = R(ci.Circumference);
                    break;
                case Ellipse el:
                    o["center"] = Arr(el.Center); o["major_radius"] = R(el.MajorRadius); o["minor_radius"] = R(el.MinorRadius);
                    o["rotation_deg"] = R(Deg(Math.Atan2(el.MajorAxis.Y, el.MajorAxis.X)));
                    o["start_angle_deg"] = R(Deg(el.StartAngle)); o["end_angle_deg"] = R(Deg(el.EndAngle)); o["closed"] = el.Closed;
                    break;
                case Polyline pl:
                {
                    o["closed"] = pl.Closed; o["vertex_count"] = pl.NumberOfVertices; o["length"] = R(pl.Length);
                    if (pl.Closed) o["area"] = R(pl.Area);
                    o["has_arcs"] = Enumerable.Range(0, pl.NumberOfVertices).Any(i => Math.Abs(pl.GetBulgeAt(i)) > Tol);
                    if (detail || pl.NumberOfVertices <= 12)
                    {
                        o["vertices"] = new JArray(Enumerable.Range(0, pl.NumberOfVertices).Select(i => Arr(pl.GetPoint2dAt(i))));
                        o["bulges"] = new JArray(Enumerable.Range(0, pl.NumberOfVertices).Select(i => R(pl.GetBulgeAt(i))));
                    }
                    if (detail) { o["width"] = R(pl.GetStartWidthAt(0)); o["elevation"] = R(pl.Elevation); }
                    break;
                }
                case Spline sp:
                    o["degree"] = sp.Degree; o["closed"] = sp.Closed; o["control_points"] = sp.NumControlPoints;
                    o["fit_points"] = sp.HasFitData ? sp.NumFitPoints : 0; o["start"] = Arr(sp.StartPoint); o["end"] = Arr(sp.EndPoint);
                    o["length"] = R(SafeLength(sp));
                    if (detail && sp.HasFitData) o["fit_point_list"] = new JArray(Enumerable.Range(0, sp.NumFitPoints).Select(i => Arr(sp.GetFitPointAt(i))));
                    break;
                case DBPoint pt:
                    o["position"] = Arr(pt.Position);
                    break;
                case DBText t:
                    o["text"] = t.TextString; o["position"] = Arr(t.Justify == AttachmentPoint.BaseLeft ? t.Position : t.AlignmentPoint);
                    o["height"] = R(t.Height); o["rotation_deg"] = R(Deg(t.Rotation)); o["justification"] = t.Justify.ToString();
                    if (detail) { o["style"] = NameOf(tr, t.TextStyleId); o["width_factor"] = R(t.WidthFactor); }
                    break;
                case MText m:
                    o["text"] = m.Text; o["position"] = Arr(m.Location); o["height"] = R(m.TextHeight); o["width"] = R(m.Width);
                    o["rotation_deg"] = R(Deg(m.Rotation)); o["attachment"] = m.Attachment.ToString();
                    if (detail) { o["style"] = NameOf(tr, m.TextStyleId); o["contents_raw"] = m.Contents; }
                    break;
                case Hatch h:
                    o["pattern"] = h.PatternName; o["scale"] = R(h.PatternScale); o["angle_deg"] = R(Deg(h.PatternAngle));
                    o["loops"] = h.NumberOfLoops; o["associative"] = h.Associative;
                    try { o["area"] = R(h.Area); } catch { }
                    break;
                case Dimension d:
                    o["dimension_type"] = d.GetType().Name.Replace("Dimension", ""); o["measurement"] = R(d.Measurement);
                    o["text_override"] = d.DimensionText; o["style"] = d.DimensionStyleName; o["text_position"] = Arr(d.TextPosition);
                    switch (d)
                    {
                        case RotatedDimension rd: o["point1"] = Arr(rd.XLine1Point); o["point2"] = Arr(rd.XLine2Point); o["dimension_line_point"] = Arr(rd.DimLinePoint); o["rotation_deg"] = R(Deg(rd.Rotation)); break;
                        case AlignedDimension ad: o["point1"] = Arr(ad.XLine1Point); o["point2"] = Arr(ad.XLine2Point); o["dimension_line_point"] = Arr(ad.DimLinePoint); break;
                        case RadialDimension rad: o["center"] = Arr(rad.Center); o["chord_point"] = Arr(rad.ChordPoint); break;
                        case DiametricDimension dd: o["chord_point"] = Arr(dd.ChordPoint); o["far_chord_point"] = Arr(dd.FarChordPoint); break;
                        case Point3AngularDimension p3: o["center"] = Arr(p3.CenterPoint); o["point1"] = Arr(p3.XLine1Point); o["point2"] = Arr(p3.XLine2Point); o["measurement_deg"] = R(Deg(d.Measurement)); break;
                        case LineAngularDimension2 la: o["line1"] = new JArray(Arr(la.XLine1Start), Arr(la.XLine1End)); o["line2"] = new JArray(Arr(la.XLine2Start), Arr(la.XLine2End)); o["measurement_deg"] = R(Deg(d.Measurement)); break;
                        case OrdinateDimension od: o["axis"] = od.UsingXAxis ? "x" : "y"; o["point"] = Arr(od.DefiningPoint); o["leader_end_point"] = Arr(od.LeaderEndPoint); break;
                    }
                    break;
                case MLeader ml:
                    if (ml.ContentType == ContentType.MTextContent) { try { o["text"] = ml.MText.Text; } catch { } }
                    else if (ml.ContentType == ContentType.BlockContent) o["block"] = NameOf(tr, ml.BlockContentId);
                    try
                    {
                        var verts = new JArray();
                        for (int li = 0; li < ml.LeaderLineCount; li++)
                            for (int v = 0; v < ml.VerticesCount(li); v++) verts.Add(Arr(ml.GetVertex(li, v)));
                        o["vertices"] = verts;
                    }
                    catch { }
                    break;
                case Table tb: o["rows"] = tb.Rows.Count; o["columns"] = tb.Columns.Count; o["position"] = Arr(tb.Position); break;
                case BlockReference br:
                {
                    o["name"] = NameOf(tr, br.DynamicBlockTableRecord.IsNull ? br.BlockTableRecord : br.DynamicBlockTableRecord);
                    o["position"] = Arr(br.Position); o["rotation_deg"] = R(Deg(br.Rotation));
                    o["scale"] = new JArray(R(br.ScaleFactors.X), R(br.ScaleFactors.Y));
                    var attrs = new JObject();
                    foreach (ObjectId aid in br.AttributeCollection)
                        if (tr.GetObject(aid, OpenMode.ForRead) is AttributeReference ar) attrs[ar.Tag] = ar.TextString;
                    if (attrs.Count > 0) o["attributes"] = attrs;
                    try { var btr = (BlockTableRecord)tr.GetObject(br.BlockTableRecord, OpenMode.ForRead); o["is_xref"] = btr.IsFromExternalReference; } catch { }
                    break;
                }
                case Region rg:
                    o["area"] = R(rg.Area); o["perimeter"] = R(rg.Perimeter);
                    break;
                case Xline xl: o["base_point"] = Arr(xl.BasePoint); o["direction_deg"] = R(Deg(Math.Atan2(xl.UnitDir.Y, xl.UnitDir.X))); break;
                case Ray ry:   o["base_point"] = Arr(ry.BasePoint); o["direction_deg"] = R(Deg(Math.Atan2(ry.UnitDir.Y, ry.UnitDir.X))); break;
                case Viewport vp: o["center"] = Arr(vp.CenterPoint); o["width"] = R(vp.Width); o["height"] = R(vp.Height); o["scale"] = R(vp.CustomScale); o["on"] = vp.On; break;
                case FeatureControlFrame f: o["text"] = f.Text; o["position"] = Arr(f.Location); break;
            }
        }

        // ── get_properties ────────────────────────────────────────────────────────

        private static string GetProperties(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            var arr = new JArray(ids.Select(id => Summarize(tr, Open<AcadEntity>(tr, id), true)));
            return Ok($"{arr.Count} object(s).", new JObject { ["entities"] = arr });
        });

        // ── select_entities: window / crossing / fence / polygon / radius + filters ──

        private static string SelectEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string mode = S(req["mode"], "all").ToLowerInvariant();
            var pts = Points(req["points"]).Select(Flat).ToList();
            double radius = D(req["radius"], 0);
            int limit = I(req["limit"], 500);
            bool detail = B(req["detail"]);
            var filters = req["filters"] as JObject ?? new JObject();
            var types = (filters["types"] as JArray)?.Select(t => t.ToString()).ToList();
            var layers = (filters["layers"] as JArray)?.Select(t => t.ToString()).ToList();
            var colors = (filters["colors"] as JArray)?.Select(t => I(t)).ToList();
            var linetypes = (filters["linetypes"] as JArray)?.Select(t => t.ToString()).ToList();
            var blockNames = (filters["block_names"] as JArray)?.Select(t => t.ToString()).ToList();
            string textContains = S(filters["text_contains"]);
            double? minLength = filters["min_length"] != null ? D(filters["min_length"]) : (double?)null;
            double? maxLength = filters["max_length"] != null ? D(filters["max_length"]) : (double?)null;

            List<Point3d> poly = null;
            if (mode is "window" or "crossing")
            {
                if (pts.Count == 2)
                {
                    double x0 = Math.Min(pts[0].X, pts[1].X), x1 = Math.Max(pts[0].X, pts[1].X);
                    double y0 = Math.Min(pts[0].Y, pts[1].Y), y1 = Math.Max(pts[0].Y, pts[1].Y);
                    poly = new List<Point3d> { new Point3d(x0, y0, 0), new Point3d(x1, y0, 0), new Point3d(x1, y1, 0), new Point3d(x0, y1, 0) };
                }
                else if (pts.Count >= 3) poly = pts;
                else throw new ArgumentException($"{mode} needs 2 corner points or a polygon of 3+ points");
            }
            else if (mode == "fence" && pts.Count < 2) throw new ArgumentException("fence needs at least 2 points");
            else if (mode == "point_radius" && (pts.Count != 1 || radius <= 0)) throw new ArgumentException("point_radius needs one point and radius > 0");
            else if (mode is not ("all" or "fence" or "point_radius" or "window" or "crossing"))
                throw new ArgumentException("mode must be all, window, crossing, fence or point_radius");

            Polyline fence = null;
            if (mode == "fence" || poly != null)
            {
                var shape = mode == "fence" ? pts : poly;
                fence = new Polyline();
                for (int i = 0; i < shape.Count; i++) fence.AddVertexAt(i, new Point2d(shape[i].X, shape[i].Y), 0, 0, 0);
                fence.Closed = mode != "fence";
            }

            BlockTableRecord space;
            string spaceName = S(req["space"], "model");
            if (spaceName.Equals("model", StringComparison.OrdinalIgnoreCase)) space = ModelSpace(tr, db, false);
            else
            {
                var ld = (DBDictionary)tr.GetObject(db.LayoutDictionaryId, OpenMode.ForRead);
                if (!ld.Contains(spaceName)) throw new ArgumentException($"layout '{spaceName}' does not exist");
                space = (BlockTableRecord)tr.GetObject(((Layout)tr.GetObject(ld.GetAt(spaceName), OpenMode.ForRead)).BlockTableRecordId, OpenMode.ForRead);
            }

            var results = new JArray(); int scanned = 0, matched = 0;
            try
            {
                foreach (ObjectId id in space)
                {
                    AcadEntity e;
                    try { e = tr.GetObject(id, OpenMode.ForRead) as AcadEntity; } catch { continue; }
                    if (e == null) continue;
                    scanned++;
                    if (types != null && !types.Any(t => TypeMatches(e, t))) continue;
                    if (layers != null && !layers.Any(l => LayerMatches(e.Layer, l))) continue;
                    if (colors != null && !colors.Contains(e.Color.ColorIndex)) continue;
                    if (linetypes != null && !linetypes.Any(l => l.Equals(e.Linetype, StringComparison.OrdinalIgnoreCase))) continue;
                    if (blockNames != null)
                    {
                        if (!(e is BlockReference bref)) continue;
                        string bn = NameOf(tr, bref.DynamicBlockTableRecord.IsNull ? bref.BlockTableRecord : bref.DynamicBlockTableRecord);
                        if (!blockNames.Any(n => LayerMatches(bn, n))) continue;
                    }
                    if (textContains != null)
                    {
                        string txt = e is DBText dt ? dt.TextString : e is MText mt ? mt.Text : e is Dimension dm ? dm.DimensionText : e is MLeader ml && ml.ContentType == ContentType.MTextContent ? ml.MText.Text : null;
                        if (txt == null || txt.IndexOf(textContains, StringComparison.OrdinalIgnoreCase) < 0) continue;
                    }
                    if (minLength != null || maxLength != null)
                    {
                        if (!(e is Curve lc)) continue;
                        double len = SafeLength(lc);
                        if (minLength != null && len < minLength) continue;
                        if (maxLength != null && len > maxLength) continue;
                    }
                    if (!Spatially(e, mode, poly, fence, pts, radius)) continue;

                    matched++;
                    if (results.Count < limit) results.Add(Summarize(tr, e, detail));
                }
            }
            finally { fence?.Dispose(); }

            return Ok($"{matched} of {scanned} object(s) match.", new JObject
            {
                ["count"] = matched, ["scanned"] = scanned, ["truncated"] = matched > results.Count,
                ["handles"] = new JArray(results.Select(r => r["handle"])), ["entities"] = results,
            });
        });

        private static bool LayerMatches(string actual, string pattern)
        {
            if (pattern.Contains("*"))
            {
                string rx = "^" + System.Text.RegularExpressions.Regex.Escape(pattern).Replace("\\*", ".*") + "$";
                return System.Text.RegularExpressions.Regex.IsMatch(actual, rx, System.Text.RegularExpressions.RegexOptions.IgnoreCase);
            }
            return actual.Equals(pattern, StringComparison.OrdinalIgnoreCase) || actual.EndsWith("|" + pattern, StringComparison.OrdinalIgnoreCase);
        }

        private static bool Spatially(AcadEntity e, string mode, List<Point3d> poly, Polyline fence, List<Point3d> pts, double radius)
        {
            if (mode == "all") return true;
            Extents3d ext;
            try { ext = e.GeometricExtents; } catch { return false; }
            var corners = new[]
            {
                new Point3d(ext.MinPoint.X, ext.MinPoint.Y, 0), new Point3d(ext.MaxPoint.X, ext.MinPoint.Y, 0),
                new Point3d(ext.MaxPoint.X, ext.MaxPoint.Y, 0), new Point3d(ext.MinPoint.X, ext.MaxPoint.Y, 0),
            };
            switch (mode)
            {
                case "window":
                    return corners.All(c => PointInPolygon(c, poly));
                case "crossing":
                {
                    if (corners.Any(c => PointInPolygon(c, poly))) return true;
                    if (poly.Any(p => p.X >= ext.MinPoint.X && p.X <= ext.MaxPoint.X && p.Y >= ext.MinPoint.Y && p.Y <= ext.MaxPoint.Y)) return true;
                    return CrossesFence(e, fence, corners);
                }
                case "fence":
                    return CrossesFence(e, fence, corners);
                case "point_radius":
                {
                    Point3d p = pts[0];
                    if (e is Curve c) { try { return c.GetClosestPointTo(p, false).DistanceTo(p) <= radius; } catch { } }
                    var centre = new Point3d((ext.MinPoint.X + ext.MaxPoint.X) / 2, (ext.MinPoint.Y + ext.MaxPoint.Y) / 2, 0);
                    double dx = Math.Max(ext.MinPoint.X - p.X, Math.Max(0, p.X - ext.MaxPoint.X));
                    double dy = Math.Max(ext.MinPoint.Y - p.Y, Math.Max(0, p.Y - ext.MaxPoint.Y));
                    return Math.Sqrt(dx * dx + dy * dy) <= radius;
                }
            }
            return false;
        }

        private static bool CrossesFence(AcadEntity e, Polyline fence, Point3d[] corners)
        {
            if (e is Curve c)
            {
                var pts = new Point3dCollection();
                try { c.IntersectWith(fence, AcIntersect.OnBothOperands, pts, IntPtr.Zero, IntPtr.Zero); return pts.Count > 0; }
                catch { }
            }
            // Non-curves: test the bounding box edges against the fence.
            var box = new Polyline();
            try
            {
                for (int i = 0; i < 4; i++) box.AddVertexAt(i, new Point2d(corners[i].X, corners[i].Y), 0, 0, 0);
                box.Closed = true;
                var pts = new Point3dCollection();
                box.IntersectWith(fence, AcIntersect.OnBothOperands, pts, IntPtr.Zero, IntPtr.Zero);
                return pts.Count > 0;
            }
            catch { return false; }
            finally { box.Dispose(); }
        }

        // ── snap-style queries ────────────────────────────────────────────────────

        private static string EntityKeyPoints(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"] ?? req["handle"]);
            var arr = new JArray();
            foreach (var id in ids)
            {
                var e = Open<AcadEntity>(tr, id);
                var o = new JObject { ["handle"] = id.Handle.ToString(), ["type"] = TypeName(e) };
                switch (e)
                {
                    case Line ln:
                        o["start"] = Arr(ln.StartPoint); o["end"] = Arr(ln.EndPoint); o["mid"] = Arr(ln.StartPoint + (ln.EndPoint - ln.StartPoint) / 2);
                        break;
                    case Arc ar:
                        o["start"] = Arr(ar.StartPoint); o["end"] = Arr(ar.EndPoint); o["center"] = Arr(ar.Center);
                        o["mid"] = Arr(Polar(ar.Center, ar.StartAngle + Norm(ar.EndAngle - ar.StartAngle) / 2, ar.Radius));
                        break;
                    case Circle ci:
                        o["center"] = Arr(ci.Center);
                        o["quadrants"] = new JArray(new[] { 0.0, 90, 180, 270 }.Select(a => Arr(Polar(ci.Center, a * Math.PI / 180, ci.Radius))));
                        break;
                    case Ellipse el:
                        o["center"] = Arr(el.Center); o["start"] = Arr(el.StartPoint); o["end"] = Arr(el.EndPoint);
                        o["quadrants"] = new JArray(Arr(el.Center + el.MajorAxis), Arr(el.Center + el.MinorAxis), Arr(el.Center - el.MajorAxis), Arr(el.Center - el.MinorAxis));
                        break;
                    case Polyline pl:
                    {
                        var verts = new JArray(); var mids = new JArray();
                        int n = pl.NumberOfVertices;
                        for (int i = 0; i < n; i++)
                        {
                            verts.Add(Arr(pl.GetPoint2dAt(i)));
                            if (i < n - 1 || pl.Closed)
                            {
                                try
                                {
                                    double d0 = pl.GetDistanceAtParameter(i), d1 = pl.GetDistanceAtParameter(pl.Closed && i == n - 1 ? n : i + 1);
                                    mids.Add(Arr(pl.GetPointAtDist((d0 + d1) / 2)));
                                }
                                catch { }
                            }
                        }
                        o["vertices"] = verts; o["segment_midpoints"] = mids; o["start"] = Arr(pl.StartPoint); o["end"] = Arr(pl.EndPoint);
                        break;
                    }
                    case Spline sp:
                        o["start"] = Arr(sp.StartPoint); o["end"] = Arr(sp.EndPoint);
                        if (sp.HasFitData) o["fit_points"] = new JArray(Enumerable.Range(0, sp.NumFitPoints).Select(i => Arr(sp.GetFitPointAt(i))));
                        o["control_points"] = new JArray(Enumerable.Range(0, sp.NumControlPoints).Select(i => Arr(sp.GetControlPointAt(i))));
                        break;
                    case DBPoint pt: o["node"] = Arr(pt.Position); break;
                    case BlockReference br: o["insertion"] = Arr(br.Position); break;
                    case DBText t: o["insertion"] = Arr(t.Position); if (t.Justify != AttachmentPoint.BaseLeft) o["alignment"] = Arr(t.AlignmentPoint); break;
                    case MText m: o["insertion"] = Arr(m.Location); break;
                    default:
                    {
                        var col = new Point3dCollection();
                        try { e.GetGripPoints(col, new IntegerCollection(), new IntegerCollection()); o["grips"] = new JArray(col.Cast<Point3d>().Select(Arr)); } catch { }
                        break;
                    }
                }
                var bb = BoundsJson(e);
                if (bb != null)
                {
                    var mn = (JArray)bb["min"]; var mx = (JArray)bb["max"];
                    o["bounds"] = bb;
                    o["bounds_center"] = new JArray(R((D(mn[0]) + D(mx[0])) / 2), R((D(mn[1]) + D(mx[1])) / 2), 0);
                }
                arr.Add(o);
            }
            return Ok($"Key points for {arr.Count} object(s).", new JObject { ["entities"] = arr });
        });

        private static string ClosestPointOn(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"] ?? req["handle"]);
            Point3d p = Flat(P3(req["point"]));
            bool extend = B(req["extend"]);
            var arr = new JArray();
            foreach (var id in ids)
            {
                var c = OpenCurve(tr, id);
                Point3d cp = c.GetClosestPointTo(p, extend);
                var o = new JObject { ["handle"] = id.Handle.ToString(), ["point"] = Arr(cp), ["distance"] = R(cp.DistanceTo(p)) };
                try
                {
                    Point3d onCurve = c.GetClosestPointTo(p, false);
                    o["on_curve"] = onCurve.DistanceTo(cp) < 1e-6;
                    o["distance_along"] = R(c.GetDistAtPoint(onCurve));
                    var t = c.GetFirstDerivative(onCurve);
                    o["tangent_angle_deg"] = R(Deg(Math.Atan2(t.Y, t.X)));
                    o["perpendicular_foot"] = o["on_curve"];
                }
                catch { }
                arr.Add(o);
            }
            var best = arr.OrderBy(x => D(x["distance"])).FirstOrDefault();
            return Ok("Closest points computed.", new JObject { ["results"] = arr, ["nearest"] = best });
        });

        private static string Intersections(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            AcIntersect mode = S(req["extend"], "none").ToLowerInvariant() switch
            {
                "a" or "this" or "first" => AcIntersect.ExtendThis,
                "b" or "other" or "second" => AcIntersect.ExtendArgument,
                "both" => AcIntersect.ExtendBoth,
                _ => AcIntersect.OnBothOperands,
            };
            var pairs = new List<(ObjectId, ObjectId)>();
            if (req["handle_a"] != null && req["handle_b"] != null)
                pairs.Add((Resolve(db, S(req["handle_a"])), Resolve(db, S(req["handle_b"]))));
            else if (req["handles"] != null)
            {
                var ids = ResolveAll(db, req["handles"]);
                if (req["against"] != null)
                {
                    var others = ResolveAll(db, req["against"], "against");
                    foreach (var a in ids) foreach (var b in others) if (a != b) pairs.Add((a, b));
                }
                else
                    for (int i = 0; i < ids.Count; i++) for (int j = i + 1; j < ids.Count; j++) pairs.Add((ids[i], ids[j]));
            }
            else throw new ArgumentException("give handle_a + handle_b, or handles (optionally with against)");

            var arr = new JArray(); var all = new JArray();
            foreach (var (a, b) in pairs)
            {
                var pts = Intersect(OpenCurve(tr, a), OpenCurve(tr, b), mode);
                if (pts.Count == 0) continue;
                arr.Add(new JObject { ["a"] = a.Handle.ToString(), ["b"] = b.Handle.ToString(), ["points"] = new JArray(pts.Select(Arr)) });
                foreach (var p in pts) all.Add(Arr(p));
            }
            return Ok($"{all.Count} intersection point(s) across {arr.Count} pair(s).", new JObject { ["pairs"] = arr, ["points"] = all });
        });

        private static string TangentPoints(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var c = OpenCurve(tr, Resolve(db, S(req["handle"])));
            Point3d p = Flat(P3(req["from_point"]));
            Point3d center; double r; Arc arc = c as Arc;
            switch (c)
            {
                case Circle ci: center = Flat(ci.Center); r = ci.Radius; break;
                case Arc a: center = Flat(a.Center); r = a.Radius; break;
                default: throw new ArgumentException("tangent points need a circle or an arc");
            }
            double d = center.DistanceTo(p);
            if (d <= r + Tol) throw new ArgumentException("the point is inside (or on) the circle; no tangent exists");
            double half = Math.Acos(r / d), baseAng = Angle(center, p);
            var pts = new JArray();
            foreach (double a in new[] { baseAng + half, baseAng - half })
            {
                Point3d t = Polar(center, a, r);
                var o = new JObject { ["point"] = Arr(t), ["angle_deg"] = R(Deg(Norm(a))), ["tangent_length"] = R(t.DistanceTo(p)) };
                if (arc != null) o["on_arc"] = Norm(a - arc.StartAngle) <= Norm(arc.EndAngle - arc.StartAngle);
                pts.Add(o);
            }
            return Ok("Tangent points computed.", new JObject { ["points"] = pts, ["center"] = Arr(center), ["radius"] = R(r) });
        });

        // ── measurements ──────────────────────────────────────────────────────────

        private static string MeasureEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            double totalLen = 0, totalArea = 0; var arr = new JArray();
            foreach (var id in ids)
            {
                var e = Open<AcadEntity>(tr, id);
                var o = new JObject { ["handle"] = id.Handle.ToString(), ["type"] = TypeName(e) };
                double len = 0, area = 0;
                switch (e)
                {
                    case Curve c:
                        len = SafeLength(c);
                        try { if (c.Closed || c is Circle) area = c.Area; } catch { }
                        if (c is Circle ci) { o["radius"] = R(ci.Radius); o["diameter"] = R(2 * ci.Radius); }
                        if (c is Arc ar) { o["radius"] = R(ar.Radius); o["included_angle_deg"] = R(Deg(Norm(ar.EndAngle - ar.StartAngle))); o["chord"] = R(ar.StartPoint.DistanceTo(ar.EndPoint)); }
                        break;
                    case Region rg: area = rg.Area; len = rg.Perimeter; break;
                    case Hatch h: try { area = h.Area; } catch { } break;
                }
                o["length"] = R(len); o["area"] = R(area); o["bounds"] = BoundsJson(e);
                totalLen += len; totalArea += area;
                arr.Add(o);
            }
            return Ok($"Measured {ids.Count} object(s).", new JObject { ["entities"] = arr, ["total_length"] = R(totalLen), ["total_area"] = R(totalArea) });
        });

        private static string AngleBetween(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var a = OpenCurve(tr, Resolve(db, S(req["handle_a"]))); var b = OpenCurve(tr, Resolve(db, S(req["handle_b"])));
            var xs = Intersect(a, b, AcIntersect.ExtendBoth);
            Vector3d ta, tb;
            if (xs.Count > 0) { ta = a.GetFirstDerivative(a.GetClosestPointTo(xs[0], true)).GetNormal(); tb = b.GetFirstDerivative(b.GetClosestPointTo(xs[0], true)).GetNormal(); }
            else { ta = a.GetFirstDerivative(a.StartPoint).GetNormal(); tb = b.GetFirstDerivative(b.StartPoint).GetNormal(); }
            double ang = Math.Acos(Math.Max(-1, Math.Min(1, ta.DotProduct(tb))));
            double acute = Math.Min(ang, Math.PI - ang);
            return Ok("Angle measured.", new JObject
            {
                ["angle_deg"] = R(Deg(ang)), ["acute_angle_deg"] = R(Deg(acute)), ["parallel"] = acute < 1e-6,
                ["intersection"] = xs.Count > 0 ? Arr(xs[0]) : null,
                ["direction_a_deg"] = R(Deg(Math.Atan2(ta.Y, ta.X))), ["direction_b_deg"] = R(Deg(Math.Atan2(tb.Y, tb.X))),
            });
        });

        private static string PointAlong(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var c = OpenCurve(tr, Resolve(db, S(req["handle"])));
            double d0 = c.GetDistanceAtParameter(c.StartParam);
            double len = c.GetDistanceAtParameter(c.EndParam) - d0;
            var dists = new List<double>();
            if (req["distances"] is JArray da) dists.AddRange(da.Select(t => D(t)));
            else if (req["distance"] != null) dists.Add(D(req["distance"]));
            else if (req["fraction"] != null) dists.Add(D(req["fraction"]) * len);
            else throw new ArgumentException("give distance, distances or fraction");
            bool fromEnd = S(req["from"], "start") == "end";
            var arr = new JArray();
            foreach (double raw in dists)
            {
                double d = fromEnd ? len - raw : raw;
                if (d < -Tol || d > len + Tol) throw new ArgumentException($"distance {R(raw)} is outside the curve length {R(len)}");
                d = Math.Max(0, Math.Min(len, d));
                Point3d p = c.GetPointAtDist(d0 + d);
                var t = c.GetFirstDerivative(p);
                double ang = Math.Atan2(t.Y, t.X);
                arr.Add(new JObject
                {
                    ["point"] = Arr(p), ["distance_from_start"] = R(d), ["tangent_angle_deg"] = R(Deg(ang)),
                    ["left_normal_deg"] = R(Deg(Norm(ang + Math.PI / 2))), ["right_normal_deg"] = R(Deg(Norm(ang - Math.PI / 2))),
                });
            }
            return Ok($"{arr.Count} point(s) along {c.Handle}.", new JObject { ["points"] = arr, ["length"] = R(len) });
        });

        private static string BoundingBox(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var ids = ResolveAll(db, req["handles"]);
            Extents3d? all = null; var arr = new JArray();
            foreach (var id in ids)
            {
                try
                {
                    var ext = Open<AcadEntity>(tr, id).GeometricExtents;
                    arr.Add(new JObject { ["handle"] = id.Handle.ToString(), ["min"] = Arr(ext.MinPoint), ["max"] = Arr(ext.MaxPoint) });
                    if (all == null) all = ext; else { var x = all.Value; x.AddExtents(ext); all = x; }
                }
                catch { }
            }
            if (all == null) throw new ArgumentException("none of the objects has extents");
            var mn = all.Value.MinPoint; var mx = all.Value.MaxPoint;
            return Ok("Bounding box computed.", new JObject
            {
                ["min"] = Arr(mn), ["max"] = Arr(mx), ["center"] = new JArray(R((mn.X + mx.X) / 2), R((mn.Y + mx.Y) / 2), 0),
                ["width"] = R(mx.X - mn.X), ["height"] = R(mx.Y - mn.Y), ["entities"] = arr,
            });
        });

        // ── duplicates (OVERKILL, detect only) ────────────────────────────────────

        private static string FindDuplicates(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            double tol = D(req["tolerance"], 1e-4);
            var layers = (req["layers"] as JArray)?.Select(t => t.ToString()).ToList();
            var types = (req["types"] as JArray)?.Select(t => t.ToString()).ToList();
            string Key(double v) => Math.Round(v / tol).ToString();
            string PKey(Point3d p) => Key(p.X) + "," + Key(p.Y);

            var groups = new Dictionary<string, List<AcadEntity>>();
            foreach (ObjectId id in ModelSpace(tr, db, false))
            {
                AcadEntity e;
                try { e = tr.GetObject(id, OpenMode.ForRead) as AcadEntity; } catch { continue; }
                if (e == null) continue;
                if (layers != null && !layers.Any(l => LayerMatches(e.Layer, l))) continue;
                if (types != null && !types.Any(t => TypeMatches(e, t))) continue;
                string sig = null;
                switch (e)
                {
                    case Line ln:
                    {
                        var ends = new[] { PKey(ln.StartPoint), PKey(ln.EndPoint) }.OrderBy(s => s);
                        sig = "L|" + string.Join("|", ends); break;
                    }
                    case Arc ar: sig = $"A|{PKey(ar.Center)}|{Key(ar.Radius)}|{Key(ar.StartAngle)}|{Key(ar.EndAngle)}"; break;
                    case Circle ci: sig = $"C|{PKey(ci.Center)}|{Key(ci.Radius)}"; break;
                    case Polyline pl:
                    {
                        var vs = Enumerable.Range(0, pl.NumberOfVertices).Select(i => PKey(new Point3d(pl.GetPoint2dAt(i).X, pl.GetPoint2dAt(i).Y, 0)) + ":" + Key(pl.GetBulgeAt(i))).ToList();
                        var rev = Enumerable.Range(0, pl.NumberOfVertices).Reverse().Select(i => PKey(new Point3d(pl.GetPoint2dAt(i).X, pl.GetPoint2dAt(i).Y, 0)) + ":" + Key(-pl.GetBulgeAt(Math.Max(0, i - 1)))).ToList();
                        string f = string.Join("|", vs), r = string.Join("|", rev);
                        sig = "P|" + (pl.Closed ? "c" : "o") + "|" + (string.CompareOrdinal(f, r) <= 0 ? f : r); break;
                    }
                    case DBText t: sig = $"T|{PKey(t.Position)}|{t.TextString}|{Key(t.Height)}|{Key(t.Rotation)}"; break;
                    case MText m: sig = $"M|{PKey(m.Location)}|{m.Contents}|{Key(m.TextHeight)}"; break;
                    case DBPoint pt: sig = $"N|{PKey(pt.Position)}"; break;
                    case BlockReference br: sig = $"B|{br.Name}|{PKey(br.Position)}|{Key(br.Rotation)}|{Key(br.ScaleFactors.X)}"; break;
                }
                if (sig == null) continue;
                if (B(req["same_layer_only"], true)) sig += "|" + e.Layer.ToLowerInvariant();
                if (!groups.TryGetValue(sig, out var list)) groups[sig] = list = new List<AcadEntity>();
                list.Add(e);
            }
            var dupes = new JArray();
            int extra = 0;
            foreach (var g in groups.Values.Where(v => v.Count > 1))
            {
                dupes.Add(new JObject { ["type"] = TypeName(g[0]), ["layer"] = g[0].Layer, ["keep"] = g[0].Handle.ToString(), ["duplicates"] = new JArray(g.Skip(1).Select(x => x.Handle.ToString())) });
                extra += g.Count - 1;
            }
            return Ok($"{dupes.Count} group(s) with {extra} duplicate object(s). Nothing was deleted.", new JObject { ["groups"] = dupes, ["duplicate_count"] = extra });
        });
    }
}
