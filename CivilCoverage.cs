using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.Civil.ApplicationServices;
using Autodesk.Civil.DatabaseServices;

using AcadEntity = Autodesk.AutoCAD.DatabaseServices.Entity;
using AcadCurve  = Autodesk.AutoCAD.DatabaseServices.Curve;
using CivilSurface = Autodesk.Civil.DatabaseServices.Surface;
using AcadTable = Autodesk.AutoCAD.DatabaseServices.Table;

namespace CADAgent
{
    /// <summary>
    /// The rest of what a plan sheet is drawn from, read into the model-space
    /// snapshot so the spatial index can put it in cells: pressure networks
    /// (water), parcel boundaries, surface borders, corridor feature lines, COGO
    /// points and point groups, sample lines, catchments, gradings,
    /// intersections, survey figures, view frames, and the paper-space layouts
    /// with their viewports' model-space windows.
    ///
    /// Everything is read-only and nothing is ever created in the drawing --
    /// surface borders come from the triangles, not from ExtractBorder, which
    /// would add entities and move the revision counter on every read. Optional
    /// API members are read by name so a member that differs between Civil 3D
    /// releases degrades to a missing field, never a failed snapshot.
    /// </summary>
    internal static class CivilCoverage
    {
        private const int MaxTrianglesForBorder = 400_000;   // above this the border is skipped
        private const int MaxPointsPerLoop      = 4_000;     // loops are decimated beyond this
        private const int MaxCogoPoints         = 30_000;
        private const double CurveSampleStep    = 5.0;       // ft, when a curve has no vertex API

        public static void Append(JObject root, Transaction tr, Database db, CivilDocument civilDoc,
                                  List<AcadEntity> sampleLines, List<AcadEntity> catchments,
                                  List<AcadEntity> gradings, List<AcadEntity> intersections,
                                  List<AcadEntity> surveyFigures, List<AcadEntity> viewFrames,
                                  List<AcadEntity> profileViews, List<Parcel> parcels,
                                  List<CivilSurface> surfaces, List<Corridor> corridors)
        {
            Safe(() => root["pressure_networks"] = PressureNetworks(tr, civilDoc));
            Safe(() => root["parcels"]           = Parcels(tr, parcels));
            Safe(() => root["surfaces"]          = Surfaces(tr, surfaces, root["surfaces"] as JArray));
            Safe(() => root["corridor_lines"]    = CorridorLines(tr, corridors));
            Safe(() => root["cogo_points"]       = CogoPoints(tr, civilDoc));
            Safe(() => root["point_groups"]      = PointGroups(tr, civilDoc));
            Safe(() => root["sample_lines"]      = Curves(tr, sampleLines, "sample_line"));
            Safe(() => root["catchments"]        = Regions(tr, catchments, "catchment"));
            Safe(() => root["gradings"]          = Regions(tr, gradings, "grading"));
            Safe(() => root["intersections"]     = Intersections(tr, intersections));
            Safe(() => root["survey_figures"]    = Curves(tr, surveyFigures, "survey_figure"));
            Safe(() => root["view_frames"]       = Regions(tr, viewFrames, "view_frame"));
            Safe(() => root["profile_views"]     = ProfileViews(tr, profileViews));
            Safe(() => root["layouts"]           = Layouts(tr, db));
        }

        // ── Pressure networks ─────────────────────────────────────────────────────

        private static JArray PressureNetworks(Transaction tr, CivilDocument civilDoc)
        {
            var nets = new JArray();
            ObjectIdCollection ids = civilDoc.GetPressurePipeNetworkIds();
            foreach (ObjectId nid in ids)
            {
                try
                {
                    var net = tr.GetObject(nid, OpenMode.ForRead) as PressurePipeNetwork;
                    if (net == null) continue;
                    var pipes = new JArray();
                    foreach (ObjectId pid in net.GetPipeIds())
                    {
                        try
                        {
                            var p = tr.GetObject(pid, OpenMode.ForRead) as PressurePipe;
                            if (p == null) continue;
                            pipes.Add(new JObject
                            {
                                ["handle"]         = p.Handle.ToString(),
                                ["name"]           = p.Name,
                                ["description"]    = Str(p, "Description", "PartDescription"),
                                ["layer"]          = p.Layer,
                                ["network"]        = net.Name,
                                ["start_point"]    = XYZ(p.StartPoint),
                                ["end_point"]      = XYZ(p.EndPoint),
                                ["inner_diameter"] = Num(p, "InnerDiameter"),
                                ["outer_diameter"] = Num(p, "OuterDiameter"),
                                ["nominal_diameter"] = Num(p, "NominalDiameter"),
                                ["length"]         = Num(p, "Length2D", "Length3D", "Length"),
                                ["part_size"]      = Str(p, "PartSizeName"),
                                ["material"]       = Str(p, "Material"),
                                ["start_part"]     = PartName(tr, Get(p, "StartPartId")),
                                ["end_part"]       = PartName(tr, Get(p, "EndPartId")),
                            });
                        }
                        catch { }
                    }
                    var fittings = PressurePoints(tr, net.GetFittingIds(), net.Name, "fitting");
                    var appurtenances = PressurePoints(tr, net.GetAppurtenanceIds(), net.Name, "appurtenance");
                    nets.Add(new JObject
                    {
                        ["name"]          = net.Name,
                        ["handle"]        = net.Handle.ToString(),
                        ["pipes"]         = pipes,
                        ["fittings"]      = fittings,
                        ["appurtenances"] = appurtenances,
                    });
                }
                catch { }
            }
            return nets;
        }

        private static JArray PressurePoints(Transaction tr, ObjectIdCollection ids, string netName, string kind)
        {
            var arr = new JArray();
            foreach (ObjectId id in ids)
            {
                try
                {
                    var part = tr.GetObject(id, OpenMode.ForRead) as AcadEntity;
                    if (part == null) continue;
                    object pos = Get(part, "Position") ?? Get(part, "Location");
                    var j = new JObject
                    {
                        ["handle"]      = part.Handle.ToString(),
                        ["kind"]        = kind,
                        ["name"]        = Str(part, "Name"),
                        ["description"] = Str(part, "Description", "PartDescription"),
                        ["layer"]       = part.Layer,
                        ["network"]     = netName,
                        ["part_size"]   = Str(part, "PartSizeName"),
                        ["part_type"]   = Str(part, "PartType", "PartFamilyName"),
                        ["position"]    = pos != null ? XYZ(pos) : Centre(part),
                    };
                    arr.Add(j);
                }
                catch { }
            }
            return arr;
        }

        private static string PartName(Transaction tr, object idObj)
        {
            try
            {
                if (idObj is ObjectId id && !id.IsNull)
                    return (tr.GetObject(id, OpenMode.ForRead) as AcadEntity) is AcadEntity e ? Str(e, "Name") : null;
            }
            catch { }
            return null;
        }

        // ── Parcels ───────────────────────────────────────────────────────────────

        private static JArray Parcels(Transaction tr, List<Parcel> parcels)
        {
            var arr = new JArray();
            foreach (var p in parcels)
            {
                try
                {
                    var j = new JObject
                    {
                        ["name"]      = p.Name,
                        ["handle"]    = p.Handle.ToString(),
                        ["layer"]     = p.Layer,
                        ["number"]    = Num(p, "Number"),
                        ["area"]      = p.Area,
                        ["perimeter"] = Num(p, "Perimeter"),
                        ["site"]      = Str(p, "SiteName"),
                        ["style"]     = Str(p, "StyleName"),
                    };
                    var loops = new JArray();
                    object loopsObj = Get(p, "ParcelLoops");
                    if (loopsObj is IEnumerable loopsEnum)
                    {
                        foreach (object loop in loopsEnum)
                        {
                            var segs = new JArray();
                            if (loop is IEnumerable segEnum)
                                foreach (object seg in segEnum)
                                {
                                    var s = SegmentJson(seg);
                                    if (s != null) segs.Add(s);
                                }
                            if (segs.Count > 0) loops.Add(segs);
                        }
                    }
                    if (loops.Count == 0)
                    {
                        // No loop API on this release: the parcel's extents at least.
                        var ring = ExtentsRing(p);
                        if (ring != null) j["extents"] = ring;
                    }
                    j["loops"] = loops;
                    arr.Add(j);
                }
                catch { }
            }
            return arr;
        }

        private static JObject SegmentJson(object seg)
        {
            object sp = Get(seg, "StartPoint"), ep = Get(seg, "EndPoint");
            if (sp == null || ep == null) return null;
            var j = new JObject { ["start"] = XY(sp), ["end"] = XY(ep) };
            double? radius = Num(seg, "Radius");
            object centre = Get(seg, "CenterPoint");
            string kind = Str(seg, "SegmentType") ?? seg.GetType().Name;
            if (radius.HasValue && radius.Value > 0 && centre != null && kind.IndexOf("Arc", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                j["type"] = "arc";
                j["center"] = XY(centre);
                j["radius"] = radius.Value;
                object cw = Get(seg, "IsClockwise") ?? Get(seg, "Clockwise");
                if (cw is bool b) j["clockwise"] = b;
            }
            else j["type"] = "segment";
            return j;
        }

        // ── Surfaces: border from the triangles ───────────────────────────────────

        private static JArray Surfaces(Transaction tr, List<CivilSurface> surfaces, JArray existing)
        {
            // Keep what the collector already wrote (name, extents, reference
            // status) and add the border loops per surface.
            var byHandle = new Dictionary<string, JObject>();
            if (existing != null)
                foreach (var t in existing.OfType<JObject>())
                    byHandle[t["handle"]?.ToString() ?? ""] = t;

            foreach (var s in surfaces)
            {
                JObject j;
                if (!byHandle.TryGetValue(s.Handle.ToString(), out j))
                {
                    j = new JObject { ["name"] = s.Name, ["handle"] = s.Handle.ToString() };
                    existing = existing ?? new JArray();
                    existing.Add(j);
                }
                try
                {
                    j["layer"] = s.Layer;
                    if (s is TinSurface tin)
                    {
                        var tris = tin.GetTriangles(false);
                        int count = tris.Count;
                        j["triangles"] = count;
                        if (count > MaxTrianglesForBorder)
                        {
                            j["boundary_skipped"] = $"{count} triangles is above the {MaxTrianglesForBorder} limit; extents only";
                            j["boundary"] = new JArray();
                        }
                        else j["boundary"] = BorderLoops(tris);
                        try { tris.Dispose(); } catch { }
                    }
                    else
                    {
                        j["boundary"] = new JArray();
                        var ring = ExtentsRing(s);
                        if (ring != null) j["extents_ring"] = ring;
                    }
                }
                catch (Exception ex) { j["boundary_error"] = ex.Message; }
            }
            return existing ?? new JArray();
        }

        /// <summary>Edges that belong to exactly one triangle, chained into loops,
        /// largest first. Coordinates are rounded to a millimetre for matching.</summary>
        private static JArray BorderLoops(TinSurfaceTriangleCollection tris)
        {
            var edgeCount = new Dictionary<(long, long, long, long), int>();
            var pointOf   = new Dictionary<(long, long), Point2d>();

            (long, long) K(Point3d p)
            {
                var k = ((long)Math.Round(p.X * 1000), (long)Math.Round(p.Y * 1000));
                if (!pointOf.ContainsKey(k)) pointOf[k] = new Point2d(p.X, p.Y);
                return k;
            }
            void AddEdge((long, long) a, (long, long) b)
            {
                var key = a.CompareTo(b) <= 0 ? (a.Item1, a.Item2, b.Item1, b.Item2) : (b.Item1, b.Item2, a.Item1, a.Item2);
                edgeCount[key] = edgeCount.TryGetValue(key, out int n) ? n + 1 : 1;
            }

            foreach (TinSurfaceTriangle t in tris)
            {
                try
                {
                    var a = K(t.Vertex1.Location); var b = K(t.Vertex2.Location); var c = K(t.Vertex3.Location);
                    AddEdge(a, b); AddEdge(b, c); AddEdge(c, a);
                }
                catch { }
            }

            var adj = new Dictionary<(long, long), List<(long, long)>>();
            foreach (var kv in edgeCount)
            {
                if (kv.Value != 1) continue;
                var a = (kv.Key.Item1, kv.Key.Item2); var b = (kv.Key.Item3, kv.Key.Item4);
                if (!adj.TryGetValue(a, out var la)) adj[a] = la = new List<(long, long)>();
                if (!adj.TryGetValue(b, out var lb)) adj[b] = lb = new List<(long, long)>();
                la.Add(b); lb.Add(a);
            }

            var loops = new List<List<Point2d>>();
            var used = new HashSet<((long, long), (long, long))>();
            foreach (var start in adj.Keys.ToList())
            {
                foreach (var first in adj[start])
                {
                    if (used.Contains((start, first)) || used.Contains((first, start))) continue;
                    var loop = new List<Point2d> { pointOf[start] };
                    var prev = start; var cur = first;
                    used.Add((prev, cur));
                    int guard = 0;
                    while (cur != start && guard++ < 2_000_000)
                    {
                        loop.Add(pointOf[cur]);
                        var next = adj[cur].FirstOrDefault(n => n != prev && !used.Contains((cur, n)) && !used.Contains((n, cur)));
                        if (next == default((long, long))) break;
                        used.Add((cur, next));
                        prev = cur; cur = next;
                    }
                    if (loop.Count >= 3) loops.Add(loop);
                }
            }

            loops.Sort((x, y) => Math.Abs(SignedArea(y)).CompareTo(Math.Abs(SignedArea(x))));
            var arr = new JArray();
            foreach (var loop in loops.Take(12))
                arr.Add(new JObject
                {
                    ["area"]   = Math.Round(Math.Abs(SignedArea(loop)), 2),
                    ["points"] = Decimate(loop),
                });
            return arr;
        }

        private static double SignedArea(List<Point2d> ring)
        {
            double a = 0;
            for (int i = 0; i < ring.Count; i++)
            {
                var p = ring[i]; var q = ring[(i + 1) % ring.Count];
                a += p.X * q.Y - q.X * p.Y;
            }
            return a / 2;
        }

        private static JArray Decimate(List<Point2d> pts)
        {
            int step = Math.Max(1, (int)Math.Ceiling(pts.Count / (double)MaxPointsPerLoop));
            var arr = new JArray();
            for (int i = 0; i < pts.Count; i += step)
                arr.Add(new JArray(Math.Round(pts[i].X, 4), Math.Round(pts[i].Y, 4)));
            return arr;
        }

        // ── Corridor feature lines ────────────────────────────────────────────────

        private static JArray CorridorLines(Transaction tr, List<Corridor> corridors)
        {
            var arr = new JArray();
            foreach (var corridor in corridors)
            {
                try
                {
                    foreach (Baseline bl in corridor.Baselines)
                    {
                        string blName = Str(bl, "Name") ?? "";
                        object main = Get(bl, "MainBaselineFeatureLines");
                        object map = main != null ? Get(main, "FeatureLineCollectionMap") : null;
                        if (!(map is IEnumerable collections)) continue;
                        foreach (object coll in collections)
                        {
                            string code = Str(coll, "CodeName") ?? "";
                            if (!(coll is IEnumerable lines)) continue;
                            foreach (object fl in lines)
                            {
                                try
                                {
                                    var pts = new JArray();
                                    if (Get(fl, "FeatureLinePoints") is IEnumerable fpts)
                                        foreach (object p in fpts) pts.Add(XYZ(p));
                                    if (pts.Count < 2) continue;
                                    arr.Add(new JObject
                                    {
                                        ["corridor"] = corridor.Name,
                                        ["handle"]   = corridor.Handle.ToString(),
                                        ["baseline"] = blName,
                                        ["code"]     = Str(fl, "CodeName") ?? code,
                                        ["layer"]    = corridor.Layer,
                                        ["points"]   = pts.Count > MaxPointsPerLoop ? DecimateArray(pts) : pts,
                                    });
                                }
                                catch { }
                            }
                        }
                    }
                }
                catch { }
            }
            return arr;
        }

        private static JArray DecimateArray(JArray pts)
        {
            int step = Math.Max(1, (int)Math.Ceiling(pts.Count / (double)MaxPointsPerLoop));
            var arr = new JArray();
            for (int i = 0; i < pts.Count; i += step) arr.Add(pts[i]);
            if (pts.Count > 0 && (pts.Count - 1) % step != 0) arr.Add(pts[pts.Count - 1]);
            return arr;
        }

        // ── COGO points and groups ────────────────────────────────────────────────

        private static JArray CogoPoints(Transaction tr, CivilDocument civilDoc)
        {
            var arr = new JArray();
            int n = 0;
            foreach (ObjectId id in civilDoc.CogoPoints)
            {
                if (n++ >= MaxCogoPoints) break;
                try
                {
                    var cp = tr.GetObject(id, OpenMode.ForRead) as CogoPoint;
                    if (cp == null) continue;
                    arr.Add(new JObject
                    {
                        ["handle"]      = cp.Handle.ToString(),
                        ["number"]      = (long)cp.PointNumber,
                        ["name"]        = Str(cp, "PointName"),
                        ["layer"]       = cp.Layer,
                        ["position"]    = XYZ(cp.Location),
                        ["elevation"]   = Num(cp, "Elevation"),
                        ["description"] = Str(cp, "RawDescription"),
                        ["full_description"] = Str(cp, "FullDescription"),
                    });
                }
                catch { }
            }
            return arr;
        }

        private static JArray PointGroups(Transaction tr, CivilDocument civilDoc)
        {
            var arr = new JArray();
            foreach (ObjectId id in civilDoc.PointGroups)
            {
                try
                {
                    var g = tr.GetObject(id, OpenMode.ForRead) as PointGroup;
                    if (g == null) continue;
                    var nums = new JArray();
                    try { foreach (uint pn in g.GetPointNumbers()) nums.Add((long)pn); } catch { }
                    arr.Add(new JObject
                    {
                        ["name"]   = g.Name,
                        ["handle"] = g.Handle.ToString(),
                        ["count"]  = nums.Count,
                        ["point_numbers"] = nums.Count > 5000 ? DecimateArray(nums) : nums,
                    });
                }
                catch { }
            }
            return arr;
        }

        // ── Curve-like and region-like Civil entities ─────────────────────────────

        private static JArray Curves(Transaction tr, List<AcadEntity> entities, string kind)
        {
            var arr = new JArray();
            foreach (var e in entities)
            {
                try
                {
                    var j = new JObject
                    {
                        ["handle"] = e.Handle.ToString(),
                        ["kind"]   = kind,
                        ["name"]   = Str(e, "Name"),
                        ["layer"]  = e.Layer,
                        ["station"] = Num(e, "Station"),
                    };
                    var pts = VertexPoints(e);
                    if (pts == null && e is AcadCurve c) pts = SampleCurve(c);
                    if (pts == null)
                    {
                        var ring = ExtentsRing(e);
                        if (ring != null) j["extents"] = ring;
                    }
                    else j["points"] = pts.Count > MaxPointsPerLoop ? DecimateArray(pts) : pts;
                    arr.Add(j);
                }
                catch { }
            }
            return arr;
        }

        private static JArray Regions(Transaction tr, List<AcadEntity> entities, string kind)
        {
            var arr = new JArray();
            foreach (var e in entities)
            {
                try
                {
                    var j = new JObject
                    {
                        ["handle"] = e.Handle.ToString(),
                        ["kind"]   = kind,
                        ["name"]   = Str(e, "Name"),
                        ["layer"]  = e.Layer,
                        ["area"]   = Num(e, "Area"),
                        ["perimeter"] = Num(e, "Perimeter2D", "Perimeter"),
                        ["group"]  = Str(e, "GradingGroupName", "GroupName"),
                    };
                    JArray ring = VertexPoints(e) ?? BoundaryPoints(e);
                    if (ring == null && e is AcadCurve c) ring = SampleCurve(c);
                    j["boundary"] = ring != null ? (ring.Count > MaxPointsPerLoop ? DecimateArray(ring) : ring) : new JArray();
                    var ext = ExtentsRing(e);
                    if (ext != null) j["extents"] = ext;
                    arr.Add(j);
                }
                catch { }
            }
            return arr;
        }

        private static JArray Intersections(Transaction tr, List<AcadEntity> entities)
        {
            var arr = new JArray();
            foreach (var e in entities)
            {
                try
                {
                    object loc = Get(e, "Location");
                    arr.Add(new JObject
                    {
                        ["handle"]   = e.Handle.ToString(),
                        ["name"]     = Str(e, "Name"),
                        ["layer"]    = e.Layer,
                        ["position"] = loc != null ? XYZ(loc) : Centre(e),
                        ["primary_alignment"]   = AlignmentName(tr, Get(e, "PrimaryAlignmentId")),
                        ["secondary_alignment"] = AlignmentName(tr, Get(e, "SecondaryAlignmentId")),
                    });
                }
                catch { }
            }
            return arr;
        }

        private static string AlignmentName(Transaction tr, object idObj)
        {
            try
            {
                if (idObj is ObjectId id && !id.IsNull)
                    return (tr.GetObject(id, OpenMode.ForRead) as Alignment)?.Name;
            }
            catch { }
            return null;
        }

        private static JArray ProfileViews(Transaction tr, List<AcadEntity> views)
        {
            var arr = new JArray();
            foreach (var e in views)
            {
                try
                {
                    var pv = e as ProfileView;
                    var j = new JObject
                    {
                        ["name"]   = Str(e, "Name"),
                        ["handle"] = e.Handle.ToString(),
                        ["layer"]  = e.Layer,
                        ["alignment"] = AlignmentName(tr, Get(e, "AlignmentId")),
                        ["station_start"] = Num(e, "StationStart"),
                        ["station_end"]   = Num(e, "StationEnd"),
                        ["elevation_min"] = Num(e, "ElevationMin"),
                        ["elevation_max"] = Num(e, "ElevationMax"),
                    };
                    object loc = Get(e, "Location");
                    if (loc != null) j["location"] = XY(loc);
                    var ext = ExtentsRing(e);
                    if (ext != null) j["extents"] = ext;
                    var profiles = new JArray();
                    try
                    {
                        if (Get(e, "GetProfileIds") is IEnumerable pids)
                            foreach (object pid in pids)
                                if (pid is ObjectId oid)
                                    profiles.Add((tr.GetObject(oid, OpenMode.ForRead) as Profile)?.Name);
                    }
                    catch { }
                    j["profiles"] = profiles;
                    arr.Add(j);
                }
                catch { }
            }
            return arr;
        }

        // ── Layouts and viewports ─────────────────────────────────────────────────

        /// <summary>Every paper-space layout with its page setup, its viewports (each
        /// with the model-space window it shows, twist applied, and the layers frozen
        /// in it), and what sits on the paper: text, every block reference with its
        /// position (title block, north arrow, notes, warnings) and tables.</summary>
        private static JArray Layouts(Transaction tr, Database db)
        {
            var arr = new JArray();
            var dict = tr.GetObject(db.LayoutDictionaryId, OpenMode.ForRead) as DBDictionary;
            if (dict == null) return arr;
            foreach (DBDictionaryEntry entry in dict)
            {
                try
                {
                    var layout = tr.GetObject(entry.Value, OpenMode.ForRead) as Layout;
                    if (layout == null || layout.ModelType) continue;
                    var btr = tr.GetObject(layout.BlockTableRecordId, OpenMode.ForRead) as BlockTableRecord;
                    var viewports = new JArray();
                    var texts = new JArray();
                    var blocks = new JArray();
                    var tables = new JArray();
                    int entities = 0;
                    if (btr != null)
                        foreach (ObjectId oid in btr)
                        {
                            try
                            {
                                var obj = tr.GetObject(oid, OpenMode.ForRead);
                                if (obj is Viewport vp)
                                {
                                    if (vp.Number == 1) continue;   // the paper itself
                                    var j = CadCommands.ViewportWindow(vp);
                                    j["handle"]  = vp.Handle.ToString();
                                    j["number"]  = vp.Number;
                                    j["layer"]   = vp.Layer;
                                    j["on"]      = vp.On;
                                    j["locked"]  = vp.Locked;
                                    j["paper_center"] = new JArray(Math.Round(vp.CenterPoint.X, 4), Math.Round(vp.CenterPoint.Y, 4));
                                    j["paper_size"]   = new JArray(Math.Round(vp.Width, 4), Math.Round(vp.Height, 4));
                                    j["annotation_scale"] = Try(() => vp.AnnotationScale?.Name);
                                    j["frozen_layers"] = FrozenLayerNames(tr, vp);
                                    viewports.Add(j);
                                }
                                else if (obj is DBText t) texts.Add(new JObject { ["handle"] = t.Handle.ToString(), ["text"] = t.TextString, ["layer"] = t.Layer, ["position"] = new JArray(Math.Round(t.Position.X, 2), Math.Round(t.Position.Y, 2)) });
                                else if (obj is MText m) texts.Add(new JObject { ["handle"] = m.Handle.ToString(), ["text"] = m.Text, ["layer"] = m.Layer, ["position"] = new JArray(Math.Round(m.Location.X, 2), Math.Round(m.Location.Y, 2)) });
                                else if (obj is AcadTable tb)   // before BlockReference: a table is one
                                {
                                    string title = null;
                                    try { if (tb.Rows.Count > 0 && tb.Columns.Count > 0) title = tb.Cells[0, 0].TextString; } catch { }
                                    tables.Add(new JObject
                                    {
                                        ["handle"] = tb.Handle.ToString(), ["layer"] = tb.Layer,
                                        ["position"] = new JArray(Math.Round(tb.Position.X, 2), Math.Round(tb.Position.Y, 2)),
                                        ["rows"] = tb.Rows.Count, ["columns"] = tb.Columns.Count, ["title"] = title,
                                    });
                                }
                                else if (obj is BlockReference br)
                                {
                                    var attribs = new JObject();
                                    try
                                    {
                                        foreach (ObjectId aid in br.AttributeCollection)
                                            if (tr.GetObject(aid, OpenMode.ForRead) is AttributeReference ar) attribs[ar.Tag] = ar.TextString;
                                    }
                                    catch { }
                                    blocks.Add(new JObject
                                    {
                                        ["handle"] = br.Handle.ToString(), ["name"] = br.Name, ["layer"] = br.Layer,
                                        ["position"] = new JArray(Math.Round(br.Position.X, 2), Math.Round(br.Position.Y, 2)),
                                        ["rotation_deg"] = Math.Round(br.Rotation * 180.0 / Math.PI, 2),
                                        ["scale"] = Math.Round(br.ScaleFactors.X, 4),
                                        ["attributes"] = attribs,
                                    });
                                }
                                entities++;
                            }
                            catch { }
                        }
                    arr.Add(new JObject
                    {
                        ["name"]      = layout.LayoutName,
                        ["tab_order"] = layout.TabOrder,
                        ["handle"]    = layout.Handle.ToString(),
                        ["paper_size"] = Str(layout, "CanonicalMediaName"),
                        ["plot_style"] = Str(layout, "StyleSheet", "CurrentStyleSheet"),
                        ["plot_device"] = Str(layout, "PlotConfigurationName"),
                        ["plot_paper_size"] = Try(() => new JArray(Math.Round(layout.PlotPaperSize.X, 2), Math.Round(layout.PlotPaperSize.Y, 2))),
                        ["plot_paper_units"] = Try(() => layout.PlotPaperUnits.ToString()),
                        ["plot_origin"] = Try(() => new JArray(Math.Round(layout.PlotOrigin.X, 2), Math.Round(layout.PlotOrigin.Y, 2))),
                        ["plot_rotation"] = Try(() => layout.PlotRotation.ToString()),
                        ["entities"]  = entities,
                        ["viewports"] = viewports,
                        ["texts"]     = texts.Count > 400 ? DecimateArray(texts) : texts,
                        ["blocks"]    = blocks.Count > 400 ? DecimateArray(blocks) : blocks,
                        ["tables"]    = tables,
                    });
                }
                catch { }
            }
            return arr;
        }

        private static JArray FrozenLayerNames(Transaction tr, Viewport vp)
        {
            var names = new JArray();
            try
            {
                foreach (ObjectId lid in vp.GetFrozenLayers())
                    if (tr.GetObject(lid, OpenMode.ForRead) is LayerTableRecord ltr) names.Add(ltr.Name);
            }
            catch { }
            return names;
        }

        private static JToken Try(Func<JToken> f) { try { return f(); } catch { return null; } }
        private static JToken Try(Func<string> f) { try { return f(); } catch { return null; } }

        // ── Geometry helpers ──────────────────────────────────────────────────────

        private static JArray VertexPoints(object e)
        {
            foreach (string name in new[] { "GetVertices", "Vertices", "GetPoints", "Points", "GetBoundaryPoints" })
            {
                try
                {
                    object v = Get(e, name);
                    if (v is IEnumerable en)
                    {
                        var arr = new JArray();
                        foreach (object item in en)
                        {
                            object p = item is Point3d || item is Point2d ? item : (Get(item, "Location") ?? Get(item, "Position") ?? item);
                            var xy = TryXY(p);
                            if (xy != null) arr.Add(xy);
                        }
                        if (arr.Count >= 2) return arr;
                    }
                }
                catch { }
            }
            return null;
        }

        private static JArray BoundaryPoints(object e)
        {
            foreach (string name in new[] { "BoundaryPolyline3d", "Boundary", "GetBoundary" })
            {
                try
                {
                    object b = Get(e, name);
                    if (b == null) continue;
                    if (b is AcadCurve c) return SampleCurve(c);
                    if (b is IEnumerable en)
                    {
                        var arr = new JArray();
                        foreach (object item in en) { var xy = TryXY(item); if (xy != null) arr.Add(xy); }
                        if (arr.Count >= 3) return arr;
                    }
                }
                catch { }
            }
            return null;
        }

        private static JArray SampleCurve(AcadCurve c)
        {
            try
            {
                double len = c.GetDistanceAtParameter(c.EndParam) - c.GetDistanceAtParameter(c.StartParam);
                if (len <= 0) return null;
                int n = Math.Max(2, Math.Min(MaxPointsPerLoop, (int)Math.Ceiling(len / CurveSampleStep)));
                var arr = new JArray();
                for (int i = 0; i <= n; i++)
                {
                    var p = c.GetPointAtDist(len * i / n);
                    arr.Add(new JArray(Math.Round(p.X, 4), Math.Round(p.Y, 4)));
                }
                return arr;
            }
            catch { return null; }
        }

        private static JArray ExtentsRing(AcadEntity e)
        {
            try
            {
                var ext = e.GeometricExtents;
                return new JArray(
                    new JArray(Math.Round(ext.MinPoint.X, 4), Math.Round(ext.MinPoint.Y, 4)),
                    new JArray(Math.Round(ext.MaxPoint.X, 4), Math.Round(ext.MinPoint.Y, 4)),
                    new JArray(Math.Round(ext.MaxPoint.X, 4), Math.Round(ext.MaxPoint.Y, 4)),
                    new JArray(Math.Round(ext.MinPoint.X, 4), Math.Round(ext.MaxPoint.Y, 4)));
            }
            catch { return null; }
        }

        private static JArray Centre(AcadEntity e)
        {
            try
            {
                var ext = e.GeometricExtents;
                return new JArray(Math.Round((ext.MinPoint.X + ext.MaxPoint.X) / 2, 4),
                                  Math.Round((ext.MinPoint.Y + ext.MaxPoint.Y) / 2, 4), 0.0);
            }
            catch { return null; }
        }

        private static JArray XYZ(object p)
        {
            if (p is Point3d p3) return new JArray(Math.Round(p3.X, 4), Math.Round(p3.Y, 4), Math.Round(p3.Z, 4));
            if (p is Point2d p2) return new JArray(Math.Round(p2.X, 4), Math.Round(p2.Y, 4), 0.0);
            var xy = TryXY(p);
            if (xy != null) { xy.Add(0.0); return xy; }
            return null;
        }

        private static JArray XY(object p)
        {
            if (p is Point3d p3) return new JArray(Math.Round(p3.X, 4), Math.Round(p3.Y, 4));
            if (p is Point2d p2) return new JArray(Math.Round(p2.X, 4), Math.Round(p2.Y, 4));
            return TryXY(p);
        }

        private static JArray TryXY(object p)
        {
            if (p == null) return null;
            if (p is Point3d p3) return new JArray(Math.Round(p3.X, 4), Math.Round(p3.Y, 4));
            if (p is Point2d p2) return new JArray(Math.Round(p2.X, 4), Math.Round(p2.Y, 4));
            double? x = Num(p, "X"), y = Num(p, "Y");
            return x.HasValue && y.HasValue ? new JArray(Math.Round(x.Value, 4), Math.Round(y.Value, 4)) : null;
        }

        // ── Reflection helpers: optional members by name ──────────────────────────

        private static object Get(object o, string name)
        {
            if (o == null) return null;
            try
            {
                var t = o.GetType();
                var prop = t.GetProperty(name);
                if (prop != null) return prop.GetValue(o);
                var method = t.GetMethod(name, Type.EmptyTypes);
                if (method != null) return method.Invoke(o, null);
            }
            catch { }
            return null;
        }

        private static string Str(object o, params string[] names)
        {
            foreach (var n in names)
            {
                object v = Get(o, n);
                if (v != null) return v.ToString();
            }
            return null;
        }

        private static double? Num(object o, params string[] names)
        {
            foreach (var n in names)
            {
                object v = Get(o, n);
                if (v == null) continue;
                try { return Convert.ToDouble(v); } catch { }
            }
            return null;
        }

        private static int SafeCount(Func<int> f) { try { return f(); } catch { return 0; } }

        private static void Safe(Action a) { try { a(); } catch { } }
    }
}
