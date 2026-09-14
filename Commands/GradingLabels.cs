using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.Civil.ApplicationServices;
using Autodesk.Civil.DatabaseServices;
using Autodesk.Civil.DatabaseServices.Styles;
using Autodesk.Civil.Settings;

using SysException  = System.Exception;
using AcadEntity    = Autodesk.AutoCAD.DatabaseServices.Entity;
using AcadDBObject  = Autodesk.AutoCAD.DatabaseServices.DBObject;
using CivilSurface  = Autodesk.Civil.DatabaseServices.Surface;

namespace CADAgent
{
    /// <summary>
    /// Grading-sheet labels: surface elevation labels (SEL) and surface slope labels (SSL)
    /// on the finished grade surface, placed from ordinary 2D linework.
    ///
    /// Everything is worked out from linework that may live in model space OR inside an
    /// xref (pads, lot lines, curb, driveways and swales normally arrive through one), so
    /// the collector walks block references and brings every piece into world coordinates
    /// before any geometry is done.
    ///
    /// Label kinds (one label per target point; "elevation" / "tc" / "fp" / "slope" are the
    /// label styles the request names):
    ///
    ///   pad_fp           FP     centre of each pad
    ///   lot_row          elev   lot line  ∩ right-of-way line
    ///   lot_setback      elev   lot line  ∩ setback line
    ///   swale_meet       elev   swale     ∩ lot line (where the legs of neighbouring swales meet)
    ///   swale_high       elev   highest FG elevation along each pad's swale linework
    ///   lot_corner       elev   lot line  ∩ lot line away from the right of way (rear corners)
    ///   curb_transition  TC     back of curb: arc ↔ straight changes
    ///   drive_curb       TC     where the driveway's flare arc lands on the back of curb (right side
    ///                           looking from the pad to the road; drive_curb_side = left | right | both)
    ///   drive_slope      slope  driveway: middle of its pad edge → straight out to the right of way
    ///   swale_slope      slope  each swale bend → straight back to the lot line behind it
    ///
    /// Styles are optional. With none given, every label takes the drawing's default surface
    /// spot elevation / slope label style (Surface feature settings). A requested style that
    /// does not exist is reported as a warning and the default is used instead of failing.
    ///
    /// Compute never writes; Draw creates the labels in one transaction. Points the surface
    /// does not cover, and points that already carry a label, are reported and skipped
    /// rather than guessed at. Where a label's text ends up (its dragged state) is not this
    /// command's concern.
    /// </summary>
    public sealed class GradingLabelHandler : PipeInvertSetter.ICadObjectHandler
    {
        public string Name => "grading_labels";

        public string GetContext(Document doc, string requestJson) => GradingLabels.Context(doc, Parse(requestJson));
        public string Compute(Document doc, string requestJson)    => GradingLabels.Run(doc, Parse(requestJson), apply: false);
        public string Draw(Document doc, string requestJson)       => GradingLabels.Run(doc, Parse(requestJson), apply: true);

        private static JObject Parse(string json)
        {
            if (string.IsNullOrWhiteSpace(json)) return new JObject();
            try { return JObject.Parse(json); }
            catch (SysException ex) { throw new ArgumentException($"Invalid JSON: {ex.Message}"); }
        }
    }

    public static partial class CadCommands
    {
        /// <summary>POST /execute entry so dashboards can run this like any other block.</summary>
        private static void RegisterGrading()
        {
            // Civil 3D label creation and xref walking stay on the application thread.
            Add("label_grading",             (req, doc) => GradingLabels.Run(doc, req, apply: B(req["apply"], true)), appThread: true);
            Add("list_surface_label_styles", (req, doc) => GradingLabels.Context(doc, req), appThread: true);
        }
    }

    public static class GradingLabels
    {
        // ── Defaults ──────────────────────────────────────────────────────────────

        public static readonly string[] PadLayers = { "c-lots-pad", "c-lots-pad-30", "c-lots-pad-40", "c-lots-pad-50" };
        public const string CurbLayer       = "c-road-back";
        public const string LotLineLayer    = "c-lots-line";
        public const string SetbackLayer    = "c-lots-stbk";
        public const string RowLayer        = "c-rway-line";
        public const string CentrelineLayer = "c-rway-center";
        public const string SwaleLayer      = "c-lots-fl";
        public const string DrivewayLayer   = "c-driv";

        public static readonly string[] AllKinds =
        {
            "pad_fp", "lot_row", "lot_setback", "swale_meet", "swale_high", "lot_corner",
            "curb_transition", "drive_curb", "drive_slope", "swale_slope",
        };

        /// <summary>Two pieces "touch" when their ends are this close (drawn snapped, so tiny).</summary>
        private const double TouchTol = 0.05;
        /// <summary>Two targets this close are the same target.</summary>
        private const double DedupeTolDefault = 0.5;
        /// <summary>An existing label this close to a target means it is already labelled.</summary>
        private const double ExistingTolDefault = 1.0;
        /// <summary>Swale / driveway linework further than this from every pad belongs to no pad.</summary>
        private const double PadReachDefault = 60.0;
        /// <summary>A curb "transition" this close to the centreline is the closing chord of a closed curb polyline.</summary>
        private const double CentrelineClearance = 5.0;
        /// <summary>A junction of swale pieces this close to a lot line is a leg landing, not a bend.</summary>
        private const double LotLineClearance = 0.5;
        /// <summary>Direction change below this at a junction is a continuation, not a bend.</summary>
        private const double BendDegrees = 10.0;
        /// <summary>Sampling step for the swale high point search.</summary>
        private const double SampleStep = 1.0;
        /// <summary>How far a ray is cast when looking for the right of way / curb / lot line.</summary>
        private const double RayLength = 1000.0;
        /// <summary>A driveway piece end this close to the curb is sitting on it (fillets are drawn tangent).</summary>
        private const double DriveCurbTol = 0.25;
        /// <summary>Driveway piece ends further than this from the driveway centreline belong to another driveway.</summary>
        private const double DriveHalfWidth = 40.0;
        private const int MaxDepth = 8;

        // ── Request ───────────────────────────────────────────────────────────────

        private sealed class Options
        {
            public string Surface;
            public string FpStyle, ElevationStyle, TcStyle, SlopeStyle;   // null = the drawing's default style
            public string MarkerStyle;                                    // null = the drawing's default marker
            public string Layer = "0";
            public HashSet<string> Kinds;
            public Point2d? WinMin, WinMax;
            public double DedupeTol = DedupeTolDefault, ExistingTol = ExistingTolDefault, PadReach = PadReachDefault;
            public List<string> Pad = PadLayers.ToList();
            public string Curb = CurbLayer, LotLine = LotLineLayer, Setback = SetbackLayer, Row = RowLayer,
                          Centreline = CentrelineLayer, Swale = SwaleLayer, Driveway = DrivewayLayer;
            public string DriveCurbSide = "right";  // left | right | both, looking from the pad toward the road
        }

        private static Options ReadOptions(JObject req)
        {
            var o = new Options { Surface = CadCommands.S(req["surface"]) };
            if (req["styles"] is JObject st)
            {
                o.FpStyle        = CadCommands.S(st["fp"], o.FpStyle);
                o.ElevationStyle = CadCommands.S(st["elevation"], o.ElevationStyle);
                o.TcStyle        = CadCommands.S(st["tc"], o.TcStyle);
                o.SlopeStyle     = CadCommands.S(st["slope"], o.SlopeStyle);
            }
            o.FpStyle        = CadCommands.S(req["fp_style"], o.FpStyle);
            o.ElevationStyle = CadCommands.S(req["elevation_style"], o.ElevationStyle);
            o.TcStyle        = CadCommands.S(req["tc_style"], o.TcStyle);
            o.SlopeStyle     = CadCommands.S(req["slope_style"], o.SlopeStyle);
            o.MarkerStyle    = CadCommands.S(req["marker_style"]);
            o.Layer          = CadCommands.S(req["layer"], "0");
            o.DedupeTol      = CadCommands.D(req["dedupe_tolerance"], o.DedupeTol);
            o.ExistingTol    = CadCommands.D(req["existing_tolerance"], o.ExistingTol);
            o.PadReach       = CadCommands.D(req["pad_reach"], o.PadReach);
            o.DriveCurbSide  = (CadCommands.S(req["drive_curb_side"], o.DriveCurbSide) ?? o.DriveCurbSide).Trim().ToLowerInvariant();
            if (!new[] { "left", "right", "both" }.Contains(o.DriveCurbSide))
                throw new ArgumentException("'drive_curb_side' must be left, right or both");

            if (req["kinds"] is JArray kinds && kinds.Count > 0)
            {
                o.Kinds = new HashSet<string>(kinds.Select(k => k.ToString().Trim().ToLowerInvariant()), StringComparer.OrdinalIgnoreCase);
                var unknown = o.Kinds.Where(k => !AllKinds.Contains(k)).ToList();
                if (unknown.Count > 0)
                    throw new ArgumentException($"unknown label kind(s) {string.Join(", ", unknown)}; known: {string.Join(", ", AllKinds)}");
            }
            if (req["window"] is JArray win && win.Count == 2)
            {
                var a = CadCommands.P2(win[0]); var b = CadCommands.P2(win[1]);
                o.WinMin = new Point2d(Math.Min(a.X, b.X), Math.Min(a.Y, b.Y));
                o.WinMax = new Point2d(Math.Max(a.X, b.X), Math.Max(a.Y, b.Y));
            }
            if (req["layers"] is JObject ly)
            {
                if (ly["pad"] is JArray pads && pads.Count > 0) o.Pad = pads.Select(p => p.ToString()).ToList();
                else if (ly["pad"]?.Type == JTokenType.String) o.Pad = new List<string> { ly["pad"].ToString() };
                o.Curb       = CadCommands.S(ly["curb"], o.Curb);
                o.LotLine    = CadCommands.S(ly["lot_line"], o.LotLine);
                o.Setback    = CadCommands.S(ly["setback"], o.Setback);
                o.Row        = CadCommands.S(ly["right_of_way"], o.Row);
                o.Centreline = CadCommands.S(ly["centreline"], o.Centreline);
                o.Swale      = CadCommands.S(ly["swale"], o.Swale);
                o.Driveway   = CadCommands.S(ly["driveway"], o.Driveway);
            }
            return o;
        }

        private static bool Wants(Options o, string kind) => o.Kinds == null || o.Kinds.Contains(kind);

        // ── Linework in world coordinates ─────────────────────────────────────────

        /// <summary>One straight or arc piece of linework, as a non-resident AutoCAD curve in world space.</summary>
        private sealed class Piece : IDisposable
        {
            public Curve Curve;          // Line or Arc
            public string Layer, Handle; // where it came from (the parent's, for xref/exploded pieces)
            public int Group;            // pieces of the same source entity share a group
            public bool IsArc => Curve is Arc;
            public Point3d A => Curve.StartPoint;
            public Point3d B => Curve.EndPoint;
            public Extents3d Box;
            public void Dispose() => Curve?.Dispose();
        }

        private sealed class Pad : IDisposable
        {
            public Polyline Poly;        // closed, world space
            public string Handle, Layer;
            public List<Point3d> Verts = new List<Point3d>();
            public Point3d Centroid;
            public Vector3d Major;       // unit direction of the longest edge
            public Extents3d Box;
            public void Dispose() => Poly?.Dispose();
        }

        private sealed class Linework : IDisposable
        {
            public List<Pad> Pads = new List<Pad>();
            public List<Piece> Curb = new List<Piece>(), LotLine = new List<Piece>(), Setback = new List<Piece>(),
                               Row = new List<Piece>(), Centreline = new List<Piece>(), Swale = new List<Piece>(),
                               Driveway = new List<Piece>();
            public int Groups;
            public void Dispose()
            {
                foreach (var p in Pads) p.Dispose();
                foreach (var l in new[] { Curb, LotLine, Setback, Row, Centreline, Swale, Driveway })
                    foreach (var p in l) p.Dispose();
            }
        }

        private static bool EndsWith(string layer, string suffix) =>
            !string.IsNullOrEmpty(suffix) && (layer ?? "").ToLowerInvariant().EndsWith(suffix.Trim().ToLowerInvariant());

        private static Linework CollectLinework(Transaction tr, Database db, Options o)
        {
            var lw = new Linework();
            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
            Walk(ms, tr, Matrix3d.Identity, 0, o, lw);
            return lw;
        }

        private static void Walk(BlockTableRecord space, Transaction tr, Matrix3d xform, int depth, Options o, Linework lw)
        {
            if (depth > MaxDepth) return;
            foreach (ObjectId id in space)
            {
                AcadEntity entity;
                try { entity = tr.GetObject(id, OpenMode.ForRead) as AcadEntity; }
                catch { continue; }
                if (entity == null) continue;

                if (entity is BlockReference br)
                {
                    BlockTableRecord def;
                    try { def = (BlockTableRecord)tr.GetObject(br.BlockTableRecord, OpenMode.ForRead); }
                    catch { continue; }
                    Walk(def, tr, xform * br.BlockTransform, depth + 1, o, lw);
                    continue;
                }

                string layer = entity.Layer;
                List<Piece> target = null;
                bool isPad = o.Pad.Any(s => EndsWith(layer, s));
                if (!isPad)
                {
                    if      (EndsWith(layer, o.Curb))       target = lw.Curb;
                    else if (EndsWith(layer, o.LotLine))    target = lw.LotLine;
                    else if (EndsWith(layer, o.Setback))    target = lw.Setback;
                    else if (EndsWith(layer, o.Row))        target = lw.Row;
                    else if (EndsWith(layer, o.Centreline)) target = lw.Centreline;
                    else if (EndsWith(layer, o.Swale))      target = lw.Swale;
                    else if (EndsWith(layer, o.Driveway))   target = lw.Driveway;
                    else continue;
                }

                string handle = entity.Handle.ToString();
                int group = lw.Groups++;
                foreach (AcadEntity prim in Primitives(entity))
                {
                    try
                    {
                        if (isPad)
                        {
                            if (prim is Polyline pl && pl.NumberOfVertices >= 3)
                            {
                                var copy = (Polyline)pl.Clone();
                                copy.TransformBy(xform);
                                lw.Pads.Add(MakePad(copy, handle, layer));
                            }
                            continue;
                        }
                        foreach (Curve c in Explode(prim, xform))
                        {
                            var piece = new Piece { Curve = c, Layer = layer, Handle = handle, Group = group };
                            try { piece.Box = c.GeometricExtents; } catch { piece.Box = new Extents3d(c.StartPoint, c.StartPoint); }
                            target.Add(piece);
                        }
                    }
                    catch { }
                }
            }
        }

        /// <summary>The entity itself when it is a line, arc or polyline, otherwise what it explodes into
        /// (a parcel segment or feature line gives lines and arcs). Exploded pieces are disposed after use.</summary>
        private static IEnumerable<AcadEntity> Primitives(AcadEntity entity)
        {
            if (entity is Line || entity is Arc || entity is Polyline || entity is Polyline2d || entity is Polyline3d)
            {
                yield return entity;
                yield break;
            }
            var pieces = new DBObjectCollection();
            try { entity.Explode(pieces); } catch { yield break; }
            try
            {
                foreach (AcadDBObject piece in pieces)
                    if (piece is AcadEntity e) yield return e;
            }
            finally { foreach (AcadDBObject piece in pieces) piece.Dispose(); }
        }

        /// <summary>Straight and arc pieces of one primitive, cloned and transformed into world space.</summary>
        private static IEnumerable<Curve> Explode(AcadEntity prim, Matrix3d xform)
        {
            var outPieces = new List<Curve>();
            if (prim is Line || prim is Arc)
            {
                var c = (Curve)prim.Clone();
                c.TransformBy(xform);
                outPieces.Add(c);
            }
            else if (prim is Polyline pl)
            {
                int n = pl.NumberOfVertices;
                int count = pl.Closed && n > 2 ? n : n - 1;
                for (int i = 0; i < count; i++)
                {
                    int j = (i + 1) % n;
                    double bulge = pl.GetBulgeAt(i);
                    Point3d a = pl.GetPoint3dAt(i), b = pl.GetPoint3dAt(j);
                    if (a.DistanceTo(b) < CadCommands.Tol) continue;
                    Curve c;
                    if (Math.Abs(bulge) < 1e-9)
                        c = new Line(a, b);
                    else
                        c = ArcThrough(CadCommands.Flat(a), CadCommands.Flat(b), bulge);
                    c.TransformBy(xform);
                    outPieces.Add(c);
                }
            }
            else if (prim is Polyline2d || prim is Polyline3d)
            {
                var pieces = new DBObjectCollection();
                try { prim.Explode(pieces); } catch { return outPieces; }
                foreach (AcadDBObject piece in pieces)
                {
                    if (piece is Line || piece is Arc)
                    {
                        var c = (Curve)piece;
                        c.TransformBy(xform);
                        outPieces.Add(c);
                    }
                    else piece.Dispose();
                }
            }
            return outPieces;
        }

        /// <summary>The arc a polyline bulge describes, as a plain Arc entity (always counter-clockwise).</summary>
        private static Arc ArcThrough(Point3d a, Point3d b, double bulge)
        {
            double chord = a.DistanceTo(b);
            double sweep = 4.0 * Math.Atan(bulge);
            double radius = chord / (2.0 * Math.Sin(Math.Abs(sweep) / 2.0));
            var mid = new Point3d((a.X + b.X) / 2, (a.Y + b.Y) / 2, 0);
            var u = (b - a).GetNormal();
            var left = new Vector3d(-u.Y, u.X, 0);
            double offset = radius * Math.Cos(sweep / 2.0) * (bulge > 0 ? 1.0 : -1.0);
            Point3d centre = mid + left * offset;
            Point3d start = bulge > 0 ? a : b, end = bulge > 0 ? b : a;
            double a0 = Math.Atan2(start.Y - centre.Y, start.X - centre.X);
            double a1 = Math.Atan2(end.Y - centre.Y, end.X - centre.X);
            while (a1 <= a0) a1 += 2 * Math.PI;
            return new Arc(centre, Vector3d.ZAxis, radius, a0, a1);
        }

        private static Pad MakePad(Polyline poly, string handle, string layer)
        {
            var pad = new Pad { Poly = poly, Handle = handle, Layer = layer };
            poly.Closed = true;
            for (int i = 0; i < poly.NumberOfVertices; i++) pad.Verts.Add(CadCommands.Flat(poly.GetPoint3dAt(i)));
            // Area centroid of the polygon (vertex average drifts on uneven vertex spacing).
            double area = 0, cx = 0, cy = 0;
            for (int i = 0; i < pad.Verts.Count; i++)
            {
                var p = pad.Verts[i]; var q = pad.Verts[(i + 1) % pad.Verts.Count];
                double cross = p.X * q.Y - q.X * p.Y;
                area += cross; cx += (p.X + q.X) * cross; cy += (p.Y + q.Y) * cross;
            }
            if (Math.Abs(area) > CadCommands.Tol)
                pad.Centroid = new Point3d(cx / (3 * area), cy / (3 * area), 0);
            else
                pad.Centroid = new Point3d(pad.Verts.Average(v => v.X), pad.Verts.Average(v => v.Y), 0);

            Point3d la = pad.Verts[0], lb = pad.Verts[1 % pad.Verts.Count];
            for (int i = 0; i < pad.Verts.Count; i++)
            {
                var p = pad.Verts[i]; var q = pad.Verts[(i + 1) % pad.Verts.Count];
                if (p.DistanceTo(q) > la.DistanceTo(lb)) { la = p; lb = q; }
            }
            pad.Major = la.DistanceTo(lb) > CadCommands.Tol ? (lb - la).GetNormal() : Vector3d.XAxis;
            try { pad.Box = poly.GeometricExtents; } catch { pad.Box = new Extents3d(pad.Centroid, pad.Centroid); }
            return pad;
        }

        // ── Small geometry helpers ────────────────────────────────────────────────

        private static bool Overlaps(Extents3d a, Extents3d b, double pad = TouchTol) =>
            a.MinPoint.X <= b.MaxPoint.X + pad && b.MinPoint.X <= a.MaxPoint.X + pad &&
            a.MinPoint.Y <= b.MaxPoint.Y + pad && b.MinPoint.Y <= a.MaxPoint.Y + pad;

        private static bool Near(Point3d a, Point3d b, double tol) =>
            Math.Abs(a.X - b.X) <= tol && Math.Abs(a.Y - b.Y) <= tol && CadCommands.Flat(a).DistanceTo(CadCommands.Flat(b)) <= tol;

        private static double DistanceTo(Point3d p, Curve c)
        {
            try { return CadCommands.Flat(p).DistanceTo(CadCommands.Flat(c.GetClosestPointTo(CadCommands.Flat(p), false))); }
            catch { return double.MaxValue; }
        }

        /// <summary>Distance from a point to the nearest piece in a list (box-prefiltered).</summary>
        private static double DistanceTo(Point3d p, List<Piece> pieces, double within, out Piece nearest)
        {
            double best = double.MaxValue; nearest = null;
            foreach (var pc in pieces)
            {
                if (p.X < pc.Box.MinPoint.X - within || p.X > pc.Box.MaxPoint.X + within ||
                    p.Y < pc.Box.MinPoint.Y - within || p.Y > pc.Box.MaxPoint.Y + within) continue;
                double d = DistanceTo(p, pc.Curve);
                if (d < best) { best = d; nearest = pc; }
            }
            return best;
        }

        /// <summary>Points where two pieces cross or touch. Ends within TouchTol of the other piece count
        /// as well, because a lot line that STOPS on another lot line rarely registers as a true crossing.</summary>
        private static List<Point3d> Meet(Piece a, Piece b)
        {
            var pts = new List<Point3d>();
            if (!Overlaps(a.Box, b.Box)) return pts;
            foreach (var p in CadCommands.Intersect(a.Curve, b.Curve)) pts.Add(p);
            foreach (var e in new[] { a.A, a.B })
                if (DistanceTo(e, b.Curve) <= TouchTol) pts.Add(CadCommands.Flat(e));
            foreach (var e in new[] { b.A, b.B })
                if (DistanceTo(e, a.Curve) <= TouchTol) pts.Add(CadCommands.Flat(e));
            return pts;
        }

        /// <summary>First point along a ray where it meets any of the pieces, or null.</summary>
        private static Point3d? Cast(Point3d origin, Vector3d dir, List<Piece> pieces, out Piece hitPiece)
        {
            hitPiece = null;
            if (dir.Length < CadCommands.Tol) return null;
            dir = dir.GetNormal();
            Point3d o = CadCommands.Flat(origin);
            using (var ray = new Line(o, o + dir * RayLength))
            {
                var rayBox = new Extents3d(
                    new Point3d(Math.Min(o.X, ray.EndPoint.X), Math.Min(o.Y, ray.EndPoint.Y), 0),
                    new Point3d(Math.Max(o.X, ray.EndPoint.X), Math.Max(o.Y, ray.EndPoint.Y), 0));
                double best = double.MaxValue; Point3d? hit = null;
                foreach (var pc in pieces)
                {
                    if (!Overlaps(rayBox, pc.Box)) continue;
                    foreach (var p in CadCommands.Intersect(ray, pc.Curve))
                    {
                        double t = (p - o).DotProduct(dir);
                        if (t > TouchTol && t < best) { best = t; hit = p; hitPiece = pc; }
                    }
                }
                return hit;
            }
        }

        private static double AngleBetween(Vector3d a, Vector3d b)
        {
            double c = a.GetNormal().DotProduct(b.GetNormal());
            return Math.Acos(Math.Max(-1, Math.Min(1, c))) * 180.0 / Math.PI;
        }

        private static bool InWindow(Options o, Point3d p) =>
            o.WinMin == null || (p.X >= o.WinMin.Value.X && p.X <= o.WinMax.Value.X && p.Y >= o.WinMin.Value.Y && p.Y <= o.WinMax.Value.Y);

        // ── Targets ───────────────────────────────────────────────────────────────

        private sealed class Target
        {
            public string Kind, Style;   // style = "fp" | "elevation" | "tc" | "slope"
            public Point3d Point;        // SEL location, or SSL first point
            public Point3d? End;         // SSL second point
            public string Pad;           // pad handle where the target is per pad
            public string Note;
            public bool IsSlope => End != null;
        }

        private sealed class Plan
        {
            public List<Target> Targets = new List<Target>();
            public List<JObject> Skipped = new List<JObject>();
            public List<string> Warnings = new List<string>();
            public Dictionary<string, int> Deduped = new Dictionary<string, int>();
        }

        private static void AddTarget(Plan plan, Options o, Target t)
        {
            if (!InWindow(o, t.Point)) return;
            // Same kind of target within the dedupe distance is the same target.
            foreach (var existing in plan.Targets)
            {
                if (existing.IsSlope != t.IsSlope) continue;
                if (!Near(existing.Point, t.Point, o.DedupeTol)) continue;
                if (t.IsSlope && !Near(existing.End.Value, t.End.Value, o.DedupeTol)) continue;
                plan.Deduped[t.Kind] = plan.Deduped.TryGetValue(t.Kind, out int n) ? n + 1 : 1;
                return;
            }
            plan.Targets.Add(t);
        }

        private static void Skip(Plan plan, string kind, string why, string pad = null, Point3d? at = null)
        {
            var o = new JObject { ["kind"] = kind, ["why"] = why };
            if (pad != null) o["pad"] = pad;
            if (at != null) o["point"] = CadCommands.Arr(CadCommands.Flat(at.Value));
            plan.Skipped.Add(o);
        }

        private static void PlanIntersections(Plan plan, Options o, string kind, string style, List<Piece> a, List<Piece> b)
        {
            foreach (var pa in a)
                foreach (var pb in b)
                {
                    if (ReferenceEquals(pa, pb) || pa.Group == pb.Group) continue;
                    foreach (var p in Meet(pa, pb))
                        AddTarget(plan, o, new Target { Kind = kind, Style = style, Point = p });
                }
        }

        /// <summary>Lot line ∩ lot line away from the right of way: the rear and side corners of each lot.
        /// Polyline corners (a direction change between consecutive segments of one entity) count too.</summary>
        private static void PlanLotCorners(Plan plan, Options o, Linework lw)
        {
            var candidates = new List<Point3d>();
            var lots = lw.LotLine;
            for (int i = 0; i < lots.Count; i++)
            {
                for (int j = i + 1; j < lots.Count; j++)
                {
                    if (lots[i].Group == lots[j].Group)
                    {
                        // Consecutive pieces of one polyline: a shared end with a direction change is a corner.
                        foreach (var e in new[] { lots[i].A, lots[i].B })
                            foreach (var f in new[] { lots[j].A, lots[j].B })
                            {
                                if (!Near(e, f, TouchTol)) continue;
                                double angle = AngleBetween(Tangent(lots[i], e), Tangent(lots[j], f));  // 180 = straight through
                                if (angle > BendDegrees && angle < 180 - BendDegrees) candidates.Add(CadCommands.Flat(e));
                            }
                        continue;
                    }
                    candidates.AddRange(Meet(lots[i], lots[j]));
                }
            }

            foreach (var p in candidates)
            {
                // Corners on the right of way are the lot_row labels; corners far from every pad are
                // not lot corners at all (plat boundary, easements drawn on the lot line layer).
                if (DistanceTo(p, lw.Row, 1.0, out _) <= 1.0) continue;
                if (NearestPad(p, lw.Pads, o.PadReach) == null) continue;
                AddTarget(plan, o, new Target { Kind = "lot_corner", Style = "elevation", Point = p });
            }
        }

        /// <summary>Unit tangent of a piece at one of its ends, pointing away from the end into the piece.</summary>
        private static Vector3d Tangent(Piece pc, Point3d atEnd)
        {
            try
            {
                double param = pc.Curve.GetParameterAtPoint(pc.Curve.GetClosestPointTo(atEnd, false));
                Vector3d t = pc.Curve.GetFirstDerivative(param);
                if (t.Length < CadCommands.Tol) t = pc.B - pc.A;
                t = new Vector3d(t.X, t.Y, 0).GetNormal();
                return Near(atEnd, pc.A, TouchTol) ? t : -t;
            }
            catch
            {
                var v = new Vector3d(pc.B.X - pc.A.X, pc.B.Y - pc.A.Y, 0).GetNormal();
                return Near(atEnd, pc.A, TouchTol) ? v : -v;
            }
        }

        /// <summary>Back of curb: every end of an arc piece that coincides with the end of a straight piece.</summary>
        private static void PlanCurbTransitions(Plan plan, Options o, Linework lw)
        {
            var straightEnds = new List<Point3d>();
            foreach (var pc in lw.Curb)
                if (!pc.IsArc) { straightEnds.Add(CadCommands.Flat(pc.A)); straightEnds.Add(CadCommands.Flat(pc.B)); }

            foreach (var pc in lw.Curb)
            {
                if (!pc.IsArc) continue;
                foreach (var e in new[] { pc.A, pc.B })
                {
                    Point3d p = CadCommands.Flat(e);
                    if (!straightEnds.Any(s => Near(s, p, TouchTol))) continue;
                    // The closing chord of a closed curb polyline runs down the crown; a "transition"
                    // there is not curb. Same guard the driveway planner uses.
                    if (lw.Centreline.Count > 0 && DistanceTo(p, lw.Centreline, CentrelineClearance, out _) < CentrelineClearance) continue;
                    AddTarget(plan, o, new Target { Kind = "curb_transition", Style = "tc", Point = p });
                }
            }
        }

        private static Pad NearestPad(Point3d p, List<Pad> pads, double within)
        {
            Pad best = null; double bestD = within;
            foreach (var pad in pads)
            {
                if (p.X < pad.Box.MinPoint.X - within || p.X > pad.Box.MaxPoint.X + within ||
                    p.Y < pad.Box.MinPoint.Y - within || p.Y > pad.Box.MaxPoint.Y + within) continue;
                double d = DistanceTo(p, pad.Poly);
                if (d < bestD) { bestD = d; best = pad; }
            }
            return best;
        }

        /// <summary>Swale pieces grouped by the pad each one sits behind (nearest pad within reach).</summary>
        private static Dictionary<Pad, List<Piece>> SwalesByPad(Linework lw, Options o, Plan plan)
        {
            var byPad = new Dictionary<Pad, List<Piece>>();
            int orphans = 0;
            foreach (var pc in lw.Swale)
            {
                Point3d mid;
                try { mid = pc.Curve.GetPointAtDist(pc.Curve.GetDistanceAtParameter(pc.Curve.EndParam) / 2); }
                catch { mid = new Point3d((pc.A.X + pc.B.X) / 2, (pc.A.Y + pc.B.Y) / 2, 0); }
                var pad = NearestPad(mid, lw.Pads, o.PadReach);
                if (pad == null) { orphans++; continue; }
                if (!byPad.TryGetValue(pad, out var list)) byPad[pad] = list = new List<Piece>();
                list.Add(pc);
            }
            if (orphans > 0) plan.Warnings.Add($"{orphans} swale piece(s) on '{o.Swale}' sit further than {o.PadReach} ft from every pad and were ignored.");
            return byPad;
        }

        /// <summary>Highest finished-grade elevation along each pad's swale linework.</summary>
        private static void PlanSwaleHighPoints(Plan plan, Options o, CivilSurface surface, Dictionary<Pad, List<Piece>> byPad)
        {
            foreach (var kv in byPad)
            {
                double bestZ = double.MinValue; Point3d? best = null; int outside = 0;
                foreach (var pc in kv.Value)
                {
                    double length;
                    try { length = pc.Curve.GetDistanceAtParameter(pc.Curve.EndParam); } catch { continue; }
                    int steps = Math.Max(1, (int)Math.Ceiling(length / SampleStep));
                    for (int i = 0; i <= steps; i++)
                    {
                        Point3d p;
                        try { p = CadCommands.Flat(pc.Curve.GetPointAtDist(Math.Min(length, i * SampleStep))); } catch { continue; }
                        double z;
                        try { z = surface.FindElevationAtXY(p.X, p.Y); } catch { outside++; continue; }
                        if (z > bestZ + 1e-9) { bestZ = z; best = p; }
                    }
                }
                if (best == null)
                {
                    Skip(plan, "swale_high", outside > 0 ? "the surface does not cover the swale" : "no swale linework could be sampled", kv.Key.Handle);
                    continue;
                }
                AddTarget(plan, o, new Target
                {
                    Kind = "swale_high", Style = "elevation", Point = best.Value, Pad = kv.Key.Handle,
                    Note = $"FG {bestZ:0.00}",
                });
            }
        }

        /// <summary>Each swale bend (a junction of two swale pieces with a direction change, off the lot
        /// lines) projected straight back, away from the road, onto the lot line behind it.</summary>
        private static void PlanSwaleSlopes(Plan plan, Options o, Linework lw, Dictionary<Pad, List<Piece>> byPad)
        {
            foreach (var kv in byPad)
            {
                Pad pad = kv.Key; var pieces = kv.Value;
                int bends = 0;
                for (int i = 0; i < pieces.Count; i++)
                    for (int j = i + 1; j < pieces.Count; j++)
                        foreach (var e in new[] { pieces[i].A, pieces[i].B })
                            foreach (var f in new[] { pieces[j].A, pieces[j].B })
                            {
                                if (!Near(e, f, TouchTol)) continue;
                                Vector3d ti = Tangent(pieces[i], e), tj = Tangent(pieces[j], f);
                                double angle = AngleBetween(ti, tj);            // 180 = straight through
                                if (angle > 180 - BendDegrees) continue;        // continuation, not a bend
                                Point3d bend = CadCommands.Flat(e);
                                if (DistanceTo(bend, lw.LotLine, LotLineClearance, out _) <= LotLineClearance) continue; // a leg landing

                                // Which piece is the leg: the one whose far end lands on a lot line; failing
                                // that, the one less parallel to the pad's rear edge (which runs across the major axis).
                                Piece leg, run;
                                bool iLands = LandsOnLotLine(pieces[i], e, lw), jLands = LandsOnLotLine(pieces[j], f, lw);
                                if (iLands != jLands) { leg = iLands ? pieces[i] : pieces[j]; run = iLands ? pieces[j] : pieces[i]; }
                                else
                                {
                                    double ci = Math.Abs(ti.DotProduct(pad.Major)), cj = Math.Abs(tj.DotProduct(pad.Major));
                                    // The run is perpendicular to the major axis (|dot| small); the leg leans along it.
                                    leg = ci > cj ? pieces[i] : pieces[j]; run = ci > cj ? pieces[j] : pieces[i];
                                }
                                Vector3d runDir = ReferenceEquals(run, pieces[i]) ? ti : tj;
                                Vector3d legDir = ReferenceEquals(leg, pieces[i]) ? ti : tj;
                                Vector3d normal = new Vector3d(-runDir.Y, runDir.X, 0);
                                if (normal.DotProduct(legDir) > 0) normal = -normal;   // legs run roadward; project the other way

                                Point3d? landing = Cast(bend, normal, lw.LotLine, out _);
                                if (landing == null)
                                {
                                    Skip(plan, "swale_slope", $"no '{o.LotLine}' line behind the bend", pad.Handle, bend);
                                    continue;
                                }
                                bends++;
                                AddTarget(plan, o, new Target
                                {
                                    Kind = "swale_slope", Style = "slope", Point = bend, End = landing, Pad = pad.Handle,
                                });
                            }
                if (bends == 0) Skip(plan, "swale_slope", "no bend found in this pad's swale linework", pad.Handle);
                else if (bends > 2) plan.Warnings.Add($"pad {pad.Handle}: {bends} swale bends found (expected 2).");
            }
        }

        private static bool LandsOnLotLine(Piece pc, Point3d thisEnd, Linework lw)
        {
            Point3d far = Near(thisEnd, pc.A, TouchTol) ? pc.B : pc.A;
            return DistanceTo(far, lw.LotLine, LotLineClearance, out _) <= LotLineClearance;
        }

        /// <summary>Driveway edges start on the pad. The middle of those starts is the first slope point;
        /// a straight line out from it meets the right of way (second slope point) and, further on,
        /// the back of curb (the driveway/curb elevation label).</summary>
        private static void PlanDriveways(Plan plan, Options o, Linework lw)
        {
            bool wantSlope = Wants(o, "drive_slope"), wantCurb = Wants(o, "drive_curb");
            foreach (var pad in lw.Pads)
            {
                var starts = new List<Point3d>(); var dirs = new List<Vector3d>();
                foreach (var pc in lw.Driveway)
                {
                    if (pc.IsArc || !Overlaps(pad.Box, pc.Box, 1.0)) continue;
                    foreach (var e in new[] { pc.A, pc.B })
                    {
                        if (DistanceTo(e, pad.Poly) > 1.0) continue;
                        Point3d far = Near(e, pc.A, TouchTol) ? pc.B : pc.A;
                        if (far.DistanceTo(e) < CadCommands.Tol) continue;
                        starts.Add(CadCommands.Flat(e));
                        dirs.Add(new Vector3d(far.X - e.X, far.Y - e.Y, 0).GetNormal());
                        break;
                    }
                }
                if (starts.Count == 0) { Skip(plan, "driveway", $"no '{o.Driveway}' line starts on this pad", pad.Handle); continue; }
                if (starts.Count > 2) plan.Warnings.Add($"pad {pad.Handle}: {starts.Count} driveway lines start on it (expected 2); using their average.");

                var p1 = new Point3d(starts.Average(s => s.X), starts.Average(s => s.Y), 0);
                Vector3d dir = new Vector3d(0, 0, 0);
                foreach (var d in dirs) dir += d;
                if (dir.Length < CadCommands.Tol) { Skip(plan, "driveway", "driveway lines point opposite ways", pad.Handle); continue; }
                dir = dir.GetNormal();

                if (wantSlope)
                {
                    Point3d? row = Cast(p1, dir, lw.Row, out _);
                    if (row == null) Skip(plan, "drive_slope", $"the driveway line never meets '{o.Row}'", pad.Handle, p1);
                    else AddTarget(plan, o, new Target { Kind = "drive_slope", Style = "slope", Point = p1, End = row, Pad = pad.Handle });
                }
                if (wantCurb)
                {
                    // The label goes where the driveway's flare arc lands on the back of curb: every
                    // driveway piece end that sits on the curb, ahead of the pad and within a driveway's
                    // reach of the centreline. Left/right is judged looking from the pad toward the road;
                    // the outermost point on each side is the flare's tangent point.
                    Point3d? axis = Cast(p1, dir, lw.Curb, out _);
                    double run = axis == null ? RayLength : p1.DistanceTo(axis.Value) + 20;
                    var lands = new List<(Point3d p, double across)>();
                    foreach (var pc in lw.Driveway)
                        foreach (var e in new[] { pc.A, pc.B })
                        {
                            var v = CadCommands.Flat(e) - p1;
                            double ahead = v.DotProduct(dir);
                            if (ahead <= 0 || ahead > run) continue;
                            double across = dir.X * v.Y - dir.Y * v.X;     // +left, -right of the centreline
                            if (Math.Abs(across) > DriveHalfWidth) continue;
                            if (DistanceTo(e, lw.Curb, DriveCurbTol, out _) > DriveCurbTol) continue;
                            lands.Add((CadCommands.Flat(e), across));
                        }
                    var picks = new List<Point3d>();
                    if (o.DriveCurbSide != "left" && lands.Any(l => l.across < 0))
                        picks.Add(lands.Where(l => l.across < 0).OrderBy(l => l.across).First().p);
                    if (o.DriveCurbSide != "right" && lands.Any(l => l.across >= 0))
                        picks.Add(lands.Where(l => l.across >= 0).OrderByDescending(l => l.across).First().p);

                    if (picks.Count > 0)
                        foreach (var p in picks)
                            AddTarget(plan, o, new Target { Kind = "drive_curb", Style = "tc", Point = p, Pad = pad.Handle });
                    else if (axis == null)
                        Skip(plan, "drive_curb", $"the driveway never meets '{o.Curb}'", pad.Handle, p1);
                    else
                    {
                        // No flare lands on the curb (driveway drawn without fillets, or not on the curb layer):
                        // the centreline is the only honest point left, and worth saying so.
                        plan.Warnings.Add($"pad {pad.Handle}: no driveway flare meets the back of curb on the {o.DriveCurbSide}; the centreline point was labelled instead.");
                        AddTarget(plan, o, new Target { Kind = "drive_curb", Style = "tc", Point = axis.Value, Pad = pad.Handle, Note = "centreline fallback" });
                    }
                }
            }
        }

        // ── Civil 3D lookups ──────────────────────────────────────────────────────

        /// <summary>
        /// Every surface in the drawing, data-shortcut references included: a referenced surface is
        /// a real Surface object in the host drawing (IsReferenceObject), so GetSurfaceIds returns it
        /// and FindElevationAtXY / label creation work on it like any other. The flags say whether the
        /// reference still resolves, so a stale or broken one can be seen before labels go on it.
        /// </summary>
        private static List<(string name, CivilSurface s)> Surfaces(Transaction tr, CivilDocument civil)
        {
            var found = new List<(string name, CivilSurface s)>();
            foreach (ObjectId id in civil.GetSurfaceIds())
            {
                try { if (tr.GetObject(id, OpenMode.ForRead, false) is CivilSurface s) found.Add((s.Name, s)); } catch { }
            }
            return found;
        }

        public static JObject SurfaceJson(CivilSurface s)
        {
            var o = new JObject { ["name"] = s.Name, ["handle"] = s.Handle.ToString(), ["type"] = s.GetType().Name };
            try { o["description"] = s.Description; } catch { }
            bool isRef = false;
            try { isRef = s.IsReferenceObject; } catch { }
            o["is_reference"] = isRef;
            if (isRef)
            {
                try { o["reference_valid"] = s.IsReferenceValid; } catch { }
                try { o["reference_stale"] = s.IsReferenceStale; } catch { }
                try { o["source_exists"] = s.IsReferencedSourceExisting; } catch { }
            }
            try { o["out_of_date"] = s.IsOutOfDate; } catch { }
            return o;
        }

        /// <summary>"FG (data reference, stale)" style tag for messages.</summary>
        private static string Describe(CivilSurface s)
        {
            var tags = new List<string>();
            try
            {
                if (s.IsReferenceObject)
                {
                    tags.Add("data reference");
                    try { if (s.IsReferenceStale) tags.Add("stale"); } catch { }
                    try { if (!s.IsReferenceValid) tags.Add("broken"); } catch { }
                }
            }
            catch { }
            return tags.Count == 0 ? s.Name : $"{s.Name} ({string.Join(", ", tags)})";
        }

        /// <summary>
        /// The surface to label. Exact name first, then a unique case-insensitive substring; with no
        /// name given the key is "fg". Nothing is guessed between several matches -- the message lists
        /// them (with their reference state) so the caller can pass the exact one.
        /// </summary>
        private static CivilSurface ResolveSurface(Transaction tr, CivilDocument civil, string wanted, out List<string> names)
        {
            var found = Surfaces(tr, civil);
            names = found.Select(f => Describe(f.s)).ToList();
            if (found.Count == 0) throw new ArgumentException("the drawing has no Civil 3D surfaces (none drawn here and none data-referenced in).");

            string key = string.IsNullOrWhiteSpace(wanted) ? "fg" : wanted.Trim();
            var exact = found.Where(f => f.name.Equals(key, StringComparison.OrdinalIgnoreCase)).ToList();
            if (exact.Count == 1) return exact[0].s;
            var partial = found.Where(f => f.name.IndexOf(key, StringComparison.OrdinalIgnoreCase) >= 0).ToList();
            if (partial.Count == 1) return partial[0].s;
            if (partial.Count == 0)
                throw new ArgumentException($"no surface named like '{key}'. Surfaces in the drawing: {string.Join(", ", names)}. Pass 'surface'.");
            throw new ArgumentException($"'{key}' matches several surfaces ({string.Join(", ", partial.Select(p => Describe(p.s)))}); pass the exact 'surface' name.");
        }

        private static List<(string name, ObjectId id)> StyleNames(Transaction tr, IEnumerable<ObjectId> ids)
        {
            var list = new List<(string, ObjectId)>();
            foreach (ObjectId id in ids)
            {
                try { if (tr.GetObject(id, OpenMode.ForRead, false) is StyleBase sb) list.Add((sb.Name, id)); } catch { }
            }
            return list;
        }

        /// <summary>Exact match first, then a unique case-insensitive substring match ("TC" finds "Top Curb (TC)").</summary>
        private static ObjectId ResolveStyle(List<(string name, ObjectId id)> styles, string wanted, string what)
        {
            if (styles.Count == 0) throw new ArgumentException($"the drawing has no {what} styles.");
            if (string.IsNullOrWhiteSpace(wanted)) throw new ArgumentException($"no {what} style name was given.");
            string key = wanted.Trim();
            var exact = styles.Where(s => s.name.Equals(key, StringComparison.OrdinalIgnoreCase)).ToList();
            if (exact.Count == 1) return exact[0].id;
            var partial = styles.Where(s => s.name.IndexOf(key, StringComparison.OrdinalIgnoreCase) >= 0).ToList();
            if (partial.Count == 1) return partial[0].id;
            string available = string.Join(", ", styles.Select(s => s.name));
            if (partial.Count == 0) throw new ArgumentException($"no {what} style named like '{key}'. Available: {available}.");
            throw new ArgumentException($"'{key}' matches several {what} styles ({string.Join(", ", partial.Select(p => p.name))}); give the exact name.");
        }

        /// <summary>A style choice: what was asked for, what will be used, and whether that is the drawing default.</summary>
        private sealed class StylePick
        {
            public string Requested, Name;
            public ObjectId Id;          // Null = let Civil 3D apply its own default at creation
            public bool IsDefault = true;
            public JObject Json() => new JObject { ["requested"] = Requested, ["used"] = Name, ["default"] = IsDefault };
        }

        /// <summary>The drawing's default surface label / marker styles, from the Surface feature settings.</summary>
        private sealed class Defaults
        {
            public ObjectId Elevation = ObjectId.Null, Slope = ObjectId.Null, Marker = ObjectId.Null;
            public string ElevationName, SlopeName, MarkerName;

            public static Defaults Read(CivilDocument civil)
            {
                var d = new Defaults();
                try
                {
                    var styles = civil.Settings.GetSettings<SettingsSurface>().Styles;
                    try { d.Elevation = styles.SurfaceSpotElevationLabelStyleId.Value; } catch { }
                    try { d.ElevationName = styles.SurfaceSpotElevationLabel.Value; } catch { }
                    try { d.Slope = styles.SurfaceSlopeLabelStyleId.Value; } catch { }
                    try { d.SlopeName = styles.SurfaceSlopeLabel.Value; } catch { }
                    try { d.Marker = styles.MarkerStyleId.Value; } catch { }
                    try { d.MarkerName = styles.Marker.Value; } catch { }
                }
                catch { }
                return d;
            }
        }

        /// <summary>Nothing requested → the default. Requested but missing → the default plus a warning.
        /// Never throws: a style name is a preference, not a precondition.</summary>
        private static StylePick PickStyle(List<(string name, ObjectId id)> styles, string wanted, string what,
                                           ObjectId defaultId, string defaultName, List<string> warnings)
        {
            var pick = new StylePick
            {
                Requested = string.IsNullOrWhiteSpace(wanted) ? null : wanted.Trim(),
                Id = defaultId,
                Name = string.IsNullOrWhiteSpace(defaultName) ? "(drawing default)" : defaultName,
            };
            if (pick.Requested == null) return pick;
            try
            {
                ObjectId id = ResolveStyle(styles, pick.Requested, what);
                pick.Id = id;
                pick.Name = styles.First(s => s.id == id).name;
                pick.IsDefault = false;
            }
            catch (ArgumentException ex)
            {
                warnings.Add($"{what} style '{pick.Requested}' not used: {ex.Message} Using the drawing default '{pick.Name}'.");
            }
            return pick;
        }

        /// <summary>Marker for spot elevation labels placed with an explicit style: requested, else the
        /// settings default, else the drawing's "none" marker, else the first one there is.</summary>
        private static StylePick PickMarker(List<(string name, ObjectId id)> markers, string wanted, Defaults defaults, List<string> warnings)
        {
            var pick = PickStyle(markers, wanted, "marker", defaults.Marker, defaults.MarkerName, warnings);
            if (!pick.Id.IsNull || markers.Count == 0) return pick;
            var fallback = markers.FirstOrDefault(m =>
                m.name.IndexOf("none", StringComparison.OrdinalIgnoreCase) >= 0 ||
                m.name.IndexOf("no marker", StringComparison.OrdinalIgnoreCase) >= 0);
            if (fallback.id.IsNull) fallback = markers[0];
            pick.Id = fallback.id; pick.Name = fallback.name;
            return pick;
        }

        private sealed class Existing
        {
            public List<Point3d> Elevation = new List<Point3d>();
            public List<Point3d> Slope = new List<Point3d>();
        }

        private static Existing ExistingLabels(Transaction tr, Database db)
        {
            var ex = new Existing();
            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
            foreach (ObjectId id in ms)
            {
                try
                {
                    var obj = tr.GetObject(id, OpenMode.ForRead, false);
                    if (obj is SurfaceElevationLabel sel) ex.Elevation.Add(CadCommands.Flat(sel.LabelLocation));
                    else if (obj is SurfaceSlopeLabel ssl) ex.Slope.Add(CadCommands.Flat(ssl.LabelLocation));
                }
                catch { }
            }
            return ex;
        }

        // ── Kind tag ──────────────────────────────────────────────────────────────
        //
        // Each label this command creates carries an Xrecord naming its kind and pad, so a
        // later command (space_labels) can treat a curb label differently from a lot corner
        // without guessing from geometry.

        public const string TagKey = "CADAGENT_GRADING";

        public static void WriteTag(Transaction tr, AcadEntity ent, string kind, string pad)
        {
            try
            {
                if (ent.ExtensionDictionary.IsNull) ent.CreateExtensionDictionary();
                var dict = (DBDictionary)tr.GetObject(ent.ExtensionDictionary, OpenMode.ForWrite);
                var xr = new Xrecord
                {
                    Data = new ResultBuffer(new TypedValue((int)DxfCode.Text, kind ?? ""),
                                            new TypedValue((int)DxfCode.Text, pad ?? "")),
                };
                dict.SetAt(TagKey, xr);
                tr.AddNewlyCreatedDBObject(xr, true);
            }
            catch { }   // a label without its tag is still a label; the spacer falls back to geometry
        }

        /// <summary>(kind, pad) written by label_grading, or (null, null) for any other label.</summary>
        public static (string kind, string pad) ReadTag(Transaction tr, AcadDBObject obj)
        {
            try
            {
                if (obj.ExtensionDictionary.IsNull) return (null, null);
                var dict = (DBDictionary)tr.GetObject(obj.ExtensionDictionary, OpenMode.ForRead);
                if (!dict.Contains(TagKey)) return (null, null);
                var xr = (Xrecord)tr.GetObject(dict.GetAt(TagKey), OpenMode.ForRead);
                var vals = xr.Data?.AsArray() ?? new TypedValue[0];
                string kind = vals.Length > 0 ? vals[0].Value?.ToString() : null;
                string pad = vals.Length > 1 ? vals[1].Value?.ToString() : null;
                return (string.IsNullOrEmpty(kind) ? null : kind, string.IsNullOrEmpty(pad) ? null : pad);
            }
            catch { return (null, null); }
        }

        /// <summary>World-space curves of every line/arc/polyline on a layer suffix, model space and
        /// xrefs alike. The caller owns (and disposes) the curves.</summary>
        public static List<Curve> CollectLayer(Transaction tr, Database db, string layerSuffix)
        {
            const string never = "\u0001never";
            var o = new Options
            {
                Pad = new List<string> { never }, Curb = layerSuffix, LotLine = never, Setback = never, Row = never,
                Centreline = never, Swale = never, Driveway = never,
            };
            var lw = CollectLinework(tr, db, o);
            var curves = lw.Curb.Select(p => p.Curve).ToList();
            lw.Curb.Clear();            // ownership moves to the caller
            lw.Dispose();
            return curves;
        }

        /// <summary>World-space closed polylines on the pad layer suffixes, with the source entity's
        /// handle (the one the CADAGENT_GRADING tag carries) and area centroid. Model space and
        /// xrefs alike. The caller owns (and disposes) the polylines.</summary>
        public static List<(Polyline poly, string handle, Point3d centroid)> CollectPads(Transaction tr, Database db, IEnumerable<string> padSuffixes)
        {
            const string never = "never";
            var o = new Options
            {
                Pad = padSuffixes.Where(s => !string.IsNullOrWhiteSpace(s)).ToList(),
                Curb = never, LotLine = never, Setback = never, Row = never, Centreline = never, Swale = never, Driveway = never,
            };
            if (o.Pad.Count == 0) return new List<(Polyline, string, Point3d)>();
            var lw = CollectLinework(tr, db, o);
            var pads = lw.Pads.Select(p => (p.Poly, p.Handle, p.Centroid)).ToList();
            lw.Pads.Clear();            // ownership moves to the caller
            lw.Dispose();
            return pads;
        }

        // ── Entry points ──────────────────────────────────────────────────────────

        /// <summary>Read-only: surfaces, label/marker styles and how much linework each layer contributes.</summary>
        public static string Context(Document doc, JObject req)
        {
            Options o;
            try { o = ReadOptions(req); }
            catch (SysException ex) { return CadCommands.Fail(ex.Message); }

            using (doc.LockDocument())
            using (Transaction tr = doc.Database.TransactionManager.StartTransaction())
            {
                CivilDocument civil = CivilDocument.GetCivilDocument(doc.Database);
                var surfaces = new JArray(Surfaces(tr, civil).Select(f => SurfaceJson(f.s)));
                // What label_grading would pick with this request's 'surface' (or "fg" when blank).
                JToken pick;
                try { pick = SurfaceJson(ResolveSurface(tr, civil, o.Surface, out _)); }
                catch (ArgumentException ex) { pick = new JObject { ["error"] = ex.Message }; }

                var elev  = StyleNames(tr, civil.Styles.LabelStyles.SurfaceLabelStyles.SpotElevationLabelStyles.Cast<ObjectId>());
                var slope = StyleNames(tr, civil.Styles.LabelStyles.SurfaceLabelStyles.SlopeLabelStyles.Cast<ObjectId>());
                var marks = StyleNames(tr, civil.Styles.MarkerStyles.Cast<ObjectId>());
                var defaults = Defaults.Read(civil);

                var layers = new JObject();
                using (var lw = CollectLinework(tr, doc.Database, o))
                {
                    layers[o.Curb] = lw.Curb.Count; layers[o.LotLine] = lw.LotLine.Count; layers[o.Setback] = lw.Setback.Count;
                    layers[o.Row] = lw.Row.Count; layers[o.Centreline] = lw.Centreline.Count; layers[o.Swale] = lw.Swale.Count;
                    layers[o.Driveway] = lw.Driveway.Count; layers["pads"] = lw.Pads.Count;
                }
                var existing = ExistingLabels(tr, doc.Database);

                tr.Commit();
                return CadCommands.Ok("Grading label context.", new JObject
                {
                    ["surfaces"] = surfaces,
                    ["surface_pick"] = pick,
                    ["elevation_label_styles"] = new JArray(elev.Select(s => s.name)),
                    ["slope_label_styles"] = new JArray(slope.Select(s => s.name)),
                    ["marker_styles"] = new JArray(marks.Select(s => s.name)),
                    ["default_styles"] = new JObject
                    {
                        ["elevation"] = defaults.ElevationName, ["slope"] = defaults.SlopeName, ["marker"] = defaults.MarkerName,
                    },
                    ["linework_pieces"] = layers,
                    ["existing_labels"] = new JObject { ["elevation"] = existing.Elevation.Count, ["slope"] = existing.Slope.Count },
                    ["kinds"] = new JArray(AllKinds),
                });
            }
        }

        /// <summary>Compute (apply=false) or draw (apply=true) every grading label.</summary>
        public static string Run(Document doc, JObject req, bool apply)
        {
            string stage = apply ? "draw" : "compute";
            Options o;
            try { o = ReadOptions(req); }
            catch (SysException ex) { return Report(stage, false, ex.Message); }

            Database db = doc.Database;
            using (doc.LockDocument())
            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                CivilDocument civil = CivilDocument.GetCivilDocument(db);
                CivilSurface surface; List<string> surfaceNames;
                var plan = new Plan();
                StylePick fpStyle, elevStyle, tcStyle, slopeStyle, marker;
                try
                {
                    surface = ResolveSurface(tr, civil, o.Surface, out surfaceNames);
                    try
                    {
                        if (surface.IsReferenceObject)
                        {
                            if (!surface.IsReferenceValid || !surface.IsReferencedSourceExisting)
                                plan.Warnings.Add($"surface '{surface.Name}' is a data reference whose source cannot be found; elevations are whatever it last synchronised.");
                            else if (surface.IsReferenceStale)
                                plan.Warnings.Add($"surface '{surface.Name}' is a data reference that is out of date; synchronise it before trusting the elevations.");
                        }
                    }
                    catch { }
                    var elev  = StyleNames(tr, civil.Styles.LabelStyles.SurfaceLabelStyles.SpotElevationLabelStyles.Cast<ObjectId>());
                    var slope = StyleNames(tr, civil.Styles.LabelStyles.SurfaceLabelStyles.SlopeLabelStyles.Cast<ObjectId>());
                    var marks = StyleNames(tr, civil.Styles.MarkerStyles.Cast<ObjectId>());
                    var defaults = Defaults.Read(civil);
                    // A missing style is a warning, never a failure: the label still goes in with the base style.
                    fpStyle    = PickStyle(elev, o.FpStyle, "surface elevation label (FP)", defaults.Elevation, defaults.ElevationName, plan.Warnings);
                    elevStyle  = PickStyle(elev, o.ElevationStyle, "surface elevation label", defaults.Elevation, defaults.ElevationName, plan.Warnings);
                    tcStyle    = PickStyle(elev, o.TcStyle, "surface elevation label (TC)", defaults.Elevation, defaults.ElevationName, plan.Warnings);
                    slopeStyle = PickStyle(slope, o.SlopeStyle, "surface slope label", defaults.Slope, defaults.SlopeName, plan.Warnings);
                    marker     = PickMarker(marks, o.MarkerStyle, defaults, plan.Warnings);
                }
                catch (SysException ex) { tr.Abort(); return Report(stage, false, ex.Message); }

                using (var lw = CollectLinework(tr, db, o))
                {
                    if (lw.Pads.Count == 0)
                        plan.Warnings.Add($"no pads found on layers ending in {string.Join(", ", o.Pad)} (model space or xrefs).");
                    foreach (var (name, list) in new[] { (o.Curb, lw.Curb), (o.LotLine, lw.LotLine), (o.Setback, lw.Setback),
                                                          (o.Row, lw.Row), (o.Swale, lw.Swale), (o.Driveway, lw.Driveway) })
                        if (list.Count == 0) plan.Warnings.Add($"no linework on a layer ending in '{name}'.");

                    if (Wants(o, "pad_fp"))
                        foreach (var pad in lw.Pads)
                            AddTarget(plan, o, new Target { Kind = "pad_fp", Style = "fp", Point = pad.Centroid, Pad = pad.Handle });
                    if (Wants(o, "lot_row"))     PlanIntersections(plan, o, "lot_row", "elevation", lw.LotLine, lw.Row);
                    if (Wants(o, "lot_setback")) PlanIntersections(plan, o, "lot_setback", "elevation", lw.LotLine, lw.Setback);
                    if (Wants(o, "swale_meet"))  PlanIntersections(plan, o, "swale_meet", "elevation", lw.Swale, lw.LotLine);
                    if (Wants(o, "lot_corner"))  PlanLotCorners(plan, o, lw);
                    if (Wants(o, "curb_transition")) PlanCurbTransitions(plan, o, lw);

                    if (Wants(o, "swale_high") || Wants(o, "swale_slope"))
                    {
                        var byPad = SwalesByPad(lw, o, plan);
                        if (Wants(o, "swale_high"))  PlanSwaleHighPoints(plan, o, surface, byPad);
                        if (Wants(o, "swale_slope")) PlanSwaleSlopes(plan, o, lw, byPad);
                    }
                    if (Wants(o, "drive_slope") || Wants(o, "drive_curb")) PlanDriveways(plan, o, lw);
                }

                // Existing labels and surface coverage decide what is actually placed.
                var existing = ExistingLabels(tr, db);
                var toPlace = new List<Target>();
                int alreadyLabelled = 0, outside = 0;
                foreach (var t in plan.Targets)
                {
                    var pool = t.IsSlope ? existing.Slope : existing.Elevation;
                    if (pool.Any(p => Near(p, t.Point, o.ExistingTol)) ||
                        (t.IsSlope && pool.Any(p => Near(p, Mid(t.Point, t.End.Value), o.ExistingTol))))
                    {
                        alreadyLabelled++;
                        Skip(plan, t.Kind, "already labelled", t.Pad, t.Point);
                        continue;
                    }
                    bool covered = true;
                    foreach (var p in t.IsSlope ? new[] { t.Point, t.End.Value } : new[] { t.Point })
                    {
                        try { double z = surface.FindElevationAtXY(p.X, p.Y); if (double.IsNaN(z)) covered = false; }
                        catch { covered = false; }
                    }
                    if (!covered)
                    {
                        outside++;
                        Skip(plan, t.Kind, $"surface '{surface.Name}' has no elevation there", t.Pad, t.Point);
                        continue;
                    }
                    toPlace.Add(t);
                }

                var created = new List<ObjectId>();
                if (apply)
                {
                    string layer = CadCommands.EnsureLayer(tr, db, o.Layer);
                    ObjectId surfaceId = surface.ObjectId;
                    try
                    {
                        foreach (var t in toPlace)
                        {
                            StylePick pick = t.Style == "fp" ? fpStyle : t.Style == "tc" ? tcStyle : t.Style == "slope" ? slopeStyle : elevStyle;
                            // No resolvable style id → the overload without one, which applies Civil 3D's
                            // own defaults (the same thing the settings dialog would do).
                            ObjectId id;
                            if (t.IsSlope)
                                id = pick.Id.IsNull
                                    ? SurfaceSlopeLabel.Create(surfaceId, P2(t.Point), P2(t.End.Value))
                                    : SurfaceSlopeLabel.Create(surfaceId, P2(t.Point), P2(t.End.Value), pick.Id);
                            else
                                id = pick.Id.IsNull || marker.Id.IsNull
                                    ? SurfaceElevationLabel.Create(surfaceId, P2(t.Point))
                                    : SurfaceElevationLabel.Create(surfaceId, P2(t.Point), pick.Id, marker.Id);
                            if (id.IsNull) throw new InvalidOperationException($"Civil 3D returned no label for {t.Kind} at {t.Point.X:0.##},{t.Point.Y:0.##}");
                            if (tr.GetObject(id, OpenMode.ForWrite) is AcadEntity ent)
                            {
                                ent.Layer = layer;
                                WriteTag(tr, ent, t.Kind, t.Pad);
                            }
                            created.Add(id);
                        }
                    }
                    catch (SysException ex)
                    {
                        tr.Abort();
                        return Report(stage, false, $"label creation failed, nothing was written: {ex.Message}", plan, toPlace, surface.Name);
                    }
                }

                var extra = new JObject
                {
                    ["applied"] = apply,
                    ["surface"] = surface.Name,
                    ["surface_info"] = SurfaceJson(surface),
                    ["styles"] = new JObject
                    {
                        ["fp"] = fpStyle.Json(), ["elevation"] = elevStyle.Json(), ["tc"] = tcStyle.Json(),
                        ["slope"] = slopeStyle.Json(), ["marker"] = marker.Json(),
                    },
                    ["layer"] = o.Layer,
                    ["planned"] = toPlace.Count,
                    ["already_labelled"] = alreadyLabelled,
                    ["outside_surface"] = outside,
                    ["created"] = CadCommands.Handles(created),
                };
                tr.Commit();
                string msg = apply
                    ? $"Placed {created.Count} label(s) on '{surface.Name}'."
                    : $"{toPlace.Count} label(s) would be placed on '{surface.Name}'.";
                return Report(stage, true, msg, plan, toPlace, surface.Name, extra);
            }
        }

        private static Point3d Mid(Point3d a, Point3d b) => new Point3d((a.X + b.X) / 2, (a.Y + b.Y) / 2, 0);
        private static Point2d P2(Point3d p) => new Point2d(p.X, p.Y);

        private static string Report(string stage, bool ok, string message, Plan plan = null, List<Target> placed = null,
                                     string surface = null, JObject extra = null)
        {
            var o = new JObject { ["ok"] = ok, ["stage"] = stage, ["message"] = message };
            if (!ok) o["errors"] = new JArray(message);
            if (extra != null) foreach (var p in extra.Properties()) o[p.Name] = p.Value;
            if (plan != null)
            {
                var counts = new JObject();
                var kinds = AllKinds.Concat(plan.Skipped.Select(j => j["kind"]?.ToString() ?? "")).Where(k => k != "").Distinct();
                foreach (var kind in kinds)
                {
                    int n = placed?.Count(t => t.Kind == kind) ?? 0;
                    int s = plan.Skipped.Count(j => j["kind"]?.ToString() == kind);
                    if (n > 0 || s > 0) counts[kind] = new JObject { ["placed"] = n, ["skipped"] = s };
                }
                o["by_kind"] = counts;
                o["labels"] = new JArray((placed ?? new List<Target>()).Select(t =>
                {
                    var j = new JObject { ["kind"] = t.Kind, ["style"] = t.Style, ["point"] = CadCommands.Arr(t.Point) };
                    if (t.End != null) j["end"] = CadCommands.Arr(t.End.Value);
                    if (t.Pad != null) j["pad"] = t.Pad;
                    if (t.Note != null) j["note"] = t.Note;
                    return j;
                }));
                o["skipped"] = new JArray(plan.Skipped);
                o["warnings"] = new JArray(plan.Warnings);
                if (plan.Deduped.Count > 0) o["deduped"] = JObject.FromObject(plan.Deduped);

                // The geometry this stage creates (or created), in the one shape every
                // handler reports it: points for spot labels, segments for the two-point
                // slope labels. The spatial gate on the Python side checks these against
                // the drawing before the draw stage is allowed to run.
                var targets = placed ?? new List<Target>();
                o["proposed"] = new JObject
                {
                    ["points"] = new JArray(targets.Where(t => !t.IsSlope)
                        .Select(t => new JArray(CadCommands.R(t.Point.X), CadCommands.R(t.Point.Y)))),
                    ["segments"] = new JArray(targets.Where(t => t.IsSlope)
                        .Select(t => new JArray(
                            new JArray(CadCommands.R(t.Point.X), CadCommands.R(t.Point.Y)),
                            new JArray(CadCommands.R(t.End.Value.X), CadCommands.R(t.End.Value.Y))))),
                };
            }
            return o.ToString();
        }
    }
}
