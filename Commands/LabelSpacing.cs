using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.Civil.DatabaseServices;

using SysException  = System.Exception;
using AcadEntity    = Autodesk.AutoCAD.DatabaseServices.Entity;
using AcadDBObject  = Autodesk.AutoCAD.DatabaseServices.DBObject;
using CivilLabel    = Autodesk.Civil.DatabaseServices.Label;
using CivilSurface  = Autodesk.Civil.DatabaseServices.Surface;

namespace CADAgent
{
    /// <summary>
    /// Puts grading labels where they belong. Two treatments, chosen per label:
    ///
    ///   position  finished-grade elevation labels (lot line / right of way, lot line / setback,
    ///             swale meeting points, swale high points, lot corners) are dragged clear of
    ///             linework and of each other;
    ///   align     top-of-curb labels (curb arc/straight transitions, driveway at curb) are
    ///             rotated to follow the back of curb at their anchor and otherwise left alone.
    ///
    /// Which is which comes from the kind tag label_grading writes on every label it creates;
    /// an untagged elevation label is aligned when its anchor sits on the back of curb and
    /// positioned otherwise. Pad (FP) labels and slope labels are outside this spec and are
    /// skipped unless asked for.
    ///
    /// Positioning method: greedy candidate placement with local-search repair, scored on a
    /// raster occupancy grid (Christensen/Marks/Shieber-style candidates, Mapnik-style grid).
    ///
    ///   1. Linework within reach of the labels (model space and xrefs, every layer unless
    ///      told otherwise) is rasterised into a grid of cells about half a text height
    ///      across, dilated by a clearance. Existing text and the labels that are NOT being
    ///      moved are stamped into a second "label" grid.
    ///   2. Every label to be placed gets candidates: left where it is (undragged, at the
    ///      anchor) and dragged in each of 8 directions at a few leader lengths. Each
    ///      candidate is measured for real -- the offset is applied and the label's extents
    ///      read back -- so the text box is exactly what Civil 3D would draw.
    ///   3. Cost = linework under the text + other labels under the text + linework under
    ///      the leader + leader length + direction preference, minus a bonus for staying put.
    ///   4. Labels are placed greedily, most constrained first, each taking its cheapest
    ///      candidate given what is already on the grid. Then repair passes revisit every
    ///      label that still costs something and move it if a cheaper spot opened up.
    ///
    /// Compute evaluates and rolls everything back; Draw applies the chosen offsets in one
    /// transaction. Labels that cannot be made clean are reported with their remaining
    /// overlaps rather than hidden. Deterministic: same drawing, same answer.
    /// </summary>
    public sealed class LabelSpacingHandler : PipeInvertSetter.ICadObjectHandler
    {
        public string Name => "label_spacing";

        public string GetContext(Document doc, string requestJson) => LabelSpacing.Run(doc, Parse(requestJson), apply: false);
        public string Compute(Document doc, string requestJson)    => LabelSpacing.Run(doc, Parse(requestJson), apply: false);
        public string Draw(Document doc, string requestJson)       => LabelSpacing.Run(doc, Parse(requestJson), apply: true);

        private static JObject Parse(string json)
        {
            if (string.IsNullOrWhiteSpace(json)) return new JObject();
            try { return JObject.Parse(json); }
            catch (SysException ex) { throw new ArgumentException($"Invalid JSON: {ex.Message}"); }
        }
    }

    public static partial class CadCommands
    {
        private static void RegisterSpacing()
        {
            // Civil 3D label evaluation stays on the application thread.
            Add("space_labels", (req, doc) => LabelSpacing.Run(doc, req, apply: B(req["apply"], true)), appThread: true);
        }
    }

    public static class LabelSpacing
    {
        // ── Request ───────────────────────────────────────────────────────────────

        private sealed class Options
        {
            public List<string> Handles = new List<string>();
            public string Layer;                    // suffix; null = any layer
            public string Types = "surface";        // elevation | slope | surface | all
            public Point2d? WinMin, WinMax;
            public bool IncludeDragged = false;     // move labels somebody already dragged?
            public bool IncludePinned = false;
            public string Mode = "auto";            // auto | position | align
            public HashSet<string> Kinds;           // optional filter on the label_grading kind tag
            public bool IncludeOther = false;       // also position pad_fp / slope labels
            public string CurbLayer = "c-road-back";
            public double CurbTolerance = 1.0;      // ft: an untagged anchor this close to the curb is a curb label
            public List<string> ObstacleLayers = new List<string>();   // suffixes; empty = every layer
            public List<string> IgnoreLayers = new List<string>();
            public double CellSize = 0;             // 0 = half the median text height
            public double Clearance = 0.5;          // ft of dilation around linework
            public List<double> Directions = new List<double> { 45, 135, 315, 225, 0, 180, 90, 270 }; // preference order
            public List<double> LeaderLengths = new List<double>();    // empty = 1, 2, 3 text heights
            public bool AllowUndragged = true;
            public double WLine = 10, WLabel = 25, WLeader = 1, WLength = 1, WDirection = 0.5, WStay = 2;
            public int MaxPasses = 10;
            public int MaxCells = 6_000_000;
        }

        private static Options ReadOptions(JObject req)
        {
            var o = new Options();
            if (req["handles"] is JArray hs) o.Handles = hs.Select(h => h.ToString()).ToList();
            o.Layer = CadCommands.S(req["layer"]);
            o.Types = (CadCommands.S(req["types"], o.Types) ?? o.Types).Trim().ToLowerInvariant();
            if (!new[] { "elevation", "slope", "surface", "all" }.Contains(o.Types))
                throw new ArgumentException("'types' must be elevation, slope, surface or all");
            if (req["window"] is JArray win && win.Count == 2)
            {
                var a = CadCommands.P2(win[0]); var b = CadCommands.P2(win[1]);
                o.WinMin = new Point2d(Math.Min(a.X, b.X), Math.Min(a.Y, b.Y));
                o.WinMax = new Point2d(Math.Max(a.X, b.X), Math.Max(a.Y, b.Y));
            }
            o.IncludeDragged = CadCommands.B(req["include_dragged"], o.IncludeDragged);
            o.IncludePinned  = CadCommands.B(req["include_pinned"], o.IncludePinned);
            o.Mode = (CadCommands.S(req["mode"], o.Mode) ?? o.Mode).Trim().ToLowerInvariant();
            if (!new[] { "auto", "position", "align" }.Contains(o.Mode))
                throw new ArgumentException("'mode' must be auto, position or align");
            if (req["kinds"] is JArray ks && ks.Count > 0)
                o.Kinds = new HashSet<string>(ks.Select(k => k.ToString().Trim().ToLowerInvariant()), StringComparer.OrdinalIgnoreCase);
            o.IncludeOther = CadCommands.B(req["include_other"], o.IncludeOther);
            o.CurbLayer = CadCommands.S(req["curb_layer"], o.CurbLayer);
            o.CurbTolerance = CadCommands.D(req["curb_tolerance"], o.CurbTolerance);
            if (req["obstacle_layers"] is JArray ol) o.ObstacleLayers = ol.Select(t => t.ToString()).Where(t => t.Trim() != "").ToList();
            if (req["ignore_layers"] is JArray il) o.IgnoreLayers = il.Select(t => t.ToString()).Where(t => t.Trim() != "").ToList();
            o.CellSize  = CadCommands.D(req["cell_size"], o.CellSize);
            o.Clearance = CadCommands.D(req["clearance"], o.Clearance);
            if (req["directions"] is JArray ds && ds.Count > 0) o.Directions = ds.Select(d => CadCommands.D(d)).ToList();
            if (req["leader_lengths"] is JArray ls && ls.Count > 0) o.LeaderLengths = ls.Select(d => CadCommands.D(d)).Where(d => d > 0).ToList();
            o.AllowUndragged = CadCommands.B(req["allow_undragged"], o.AllowUndragged);
            if (req["weights"] is JObject w)
            {
                o.WLine = CadCommands.D(w["line"], o.WLine);
                o.WLabel = CadCommands.D(w["label"], o.WLabel);
                o.WLeader = CadCommands.D(w["leader"], o.WLeader);
                o.WLength = CadCommands.D(w["length"], o.WLength);
                o.WDirection = CadCommands.D(w["direction"], o.WDirection);
                o.WStay = CadCommands.D(w["stay"], o.WStay);
            }
            o.MaxPasses = CadCommands.I(req["max_passes"], o.MaxPasses);
            return o;
        }

        // ── Occupancy grid ────────────────────────────────────────────────────────

        private sealed class Grid
        {
            public double Cell, X0, Y0;
            public int W, H;
            public byte[] Lines, Labels;

            public Grid(double x0, double y0, double x1, double y1, double cell)
            {
                Cell = cell; X0 = x0; Y0 = y0;
                W = Math.Max(1, (int)Math.Ceiling((x1 - x0) / cell));
                H = Math.Max(1, (int)Math.Ceiling((y1 - y0) / cell));
                Lines = new byte[W * H]; Labels = new byte[W * H];
            }

            public int Col(double x) => (int)Math.Floor((x - X0) / Cell);
            public int Row(double y) => (int)Math.Floor((y - Y0) / Cell);

            public void Stamp(byte[] layer, double x, double y, int radius)
            {
                int c = Col(x), r = Row(y);
                for (int dr = -radius; dr <= radius; dr++)
                    for (int dc = -radius; dc <= radius; dc++)
                    {
                        int cc = c + dc, rr = r + dr;
                        if (cc < 0 || rr < 0 || cc >= W || rr >= H) continue;
                        int i = rr * W + cc;
                        if (layer[i] < 255) layer[i]++;
                    }
            }

            /// <summary>Adds (or removes) a box from a layer: +1 / -1 per covered cell.</summary>
            public void Box(byte[] layer, Box b, int delta)
            {
                int c0 = Math.Max(0, Col(b.MinX)), c1 = Math.Min(W - 1, Col(b.MaxX));
                int r0 = Math.Max(0, Row(b.MinY)), r1 = Math.Min(H - 1, Row(b.MaxY));
                for (int r = r0; r <= r1; r++)
                    for (int c = c0; c <= c1; c++)
                    {
                        int i = r * W + c;
                        int v = layer[i] + delta;
                        layer[i] = (byte)Math.Max(0, Math.Min(255, v));
                    }
            }

            /// <summary>Occupied cells (any count) of a layer under a box.</summary>
            public int Count(byte[] layer, Box b)
            {
                int c0 = Math.Max(0, Col(b.MinX)), c1 = Math.Min(W - 1, Col(b.MaxX));
                int r0 = Math.Max(0, Row(b.MinY)), r1 = Math.Min(H - 1, Row(b.MaxY));
                int n = 0;
                for (int r = r0; r <= r1; r++)
                    for (int c = c0; c <= c1; c++)
                        if (layer[r * W + c] > 0) n++;
                return n;
            }

            /// <summary>Occupied line cells along a segment, skipping the first <paramref name="skip"/> feet
            /// (the anchor usually sits ON linework: that is why the label exists) and the box itself.</summary>
            public int Along(Point3d a, Point3d b, double skip, Box exclude)
            {
                double len = a.DistanceTo(b);
                if (len < Cell) return 0;
                int n = 0; int lastC = int.MinValue, lastR = int.MinValue;
                for (double d = skip; d <= len; d += Cell / 2)
                {
                    double t = d / len;
                    double x = a.X + (b.X - a.X) * t, y = a.Y + (b.Y - a.Y) * t;
                    if (exclude.Contains(x, y)) continue;
                    int c = Col(x), r = Row(y);
                    if (c == lastC && r == lastR) continue;
                    lastC = c; lastR = r;
                    if (c < 0 || r < 0 || c >= W || r >= H) continue;
                    if (Lines[r * W + c] > 0) n++;
                }
                return n;
            }
        }

        private struct Box
        {
            public double MinX, MinY, MaxX, MaxY;
            public Box(double x0, double y0, double x1, double y1) { MinX = x0; MinY = y0; MaxX = x1; MaxY = y1; }
            public static Box Of(Extents3d e) => new Box(e.MinPoint.X, e.MinPoint.Y, e.MaxPoint.X, e.MaxPoint.Y);
            public double Width => MaxX - MinX;
            public double Height => MaxY - MinY;
            public bool Contains(double x, double y) => x >= MinX && x <= MaxX && y >= MinY && y <= MaxY;
            public Point3d Nearest(Point3d p) => new Point3d(Math.Max(MinX, Math.Min(MaxX, p.X)), Math.Max(MinY, Math.Min(MaxY, p.Y)), 0);
            public JObject Json() => new JObject { ["min"] = new JArray(CadCommands.R(MinX), CadCommands.R(MinY)), ["max"] = new JArray(CadCommands.R(MaxX), CadCommands.R(MaxY)) };
        }

        // ── Labels and candidates ─────────────────────────────────────────────────

        private sealed class Candidate
        {
            public bool Undragged;
            public Vector3d Offset;
            public Box Text;            // the text block
            public Box Full;            // text + leader + marker, what Civil 3D reports as extents
            public int LineHits, LeaderHits;
            public double Length;       // leader length, ft
            public int DirectionRank;   // position in the preference list
            public bool IsCurrent;
            public double BaseCost;     // everything except label-on-label, which depends on the grid state
            public JObject Json(int labelHits, double cost) => new JObject
            {
                ["undragged"] = Undragged,
                ["offset"] = Undragged ? null : new JArray(CadCommands.R(Offset.X), CadCommands.R(Offset.Y)),
                ["text_box"] = Text.Json(),
                ["line_hits"] = LineHits, ["label_hits"] = labelHits, ["leader_hits"] = LeaderHits,
                ["leader_length"] = CadCommands.R(Length), ["cost"] = CadCommands.R(cost),
            };
        }

        private sealed class Job
        {
            public ObjectId Id;
            public string Handle, Type;
            public Point3d Anchor;
            public bool WasDragged;
            public Vector3d WasOffset;
            public double TextHeight;
            public List<Candidate> Candidates = new List<Candidate>();
            public Candidate Chosen;
            public double ChosenCost;
            public int ChosenLabelHits;
            public string Problem;
            public string Mode;         // position | align | skip
            public string Kind, Pad;    // from the label_grading tag, when present
            public string SkipReason;
            public double? RotationDeg, WasRotationDeg, CurbDistance;
        }

        public static readonly HashSet<string> PositionKinds = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            { "lot_row", "lot_setback", "swale_meet", "swale_high", "lot_corner" };
        public static readonly HashSet<string> AlignKinds = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            { "curb_transition", "drive_curb" };

        /// <summary>Distance from a point to the nearest of the curves, and that curve.</summary>
        private static double Nearest(Point3d p, List<Curve> curves, out Curve nearest)
        {
            double best = double.MaxValue; nearest = null;
            foreach (var c in curves)
            {
                try
                {
                    var q = c.GetClosestPointTo(p, false);
                    double d = CadCommands.Flat(p).DistanceTo(CadCommands.Flat(q));
                    if (d < best) { best = d; nearest = c; }
                }
                catch { }
            }
            return best;
        }

        /// <summary>Angle of the curve at the point nearest p, folded so text reads left to right.</summary>
        private static double TangentAngle(Curve c, Point3d p)
        {
            var q = c.GetClosestPointTo(p, false);
            Vector3d t;
            try { t = c.GetFirstDerivative(c.GetParameterAtPoint(q)); }
            catch { t = c.EndPoint - c.StartPoint; }
            double a = Math.Atan2(t.Y, t.X);
            while (a > Math.PI / 2) a -= Math.PI;
            while (a <= -Math.PI / 2) a += Math.PI;
            return a;
        }

        private static void Classify(Job job, CivilLabel lbl, Options o, List<Curve> curb)
        {
            if (o.Mode == "position") { job.Mode = "position"; return; }
            if (o.Mode == "align")    { job.Mode = "align"; return; }

            if (job.Kind != null)
            {
                if (PositionKinds.Contains(job.Kind)) job.Mode = "position";
                else if (AlignKinds.Contains(job.Kind)) job.Mode = "align";
                else if (o.IncludeOther) job.Mode = "position";
                else { job.Mode = "skip"; job.SkipReason = $"kind '{job.Kind}' is outside the spec (include_other to position it)"; }
                return;
            }
            // Untagged: a spot elevation on the back of curb is a curb label; any other spot
            // elevation gets positioned; slope labels wait for include_other.
            if (lbl is SurfaceSlopeLabel && !o.IncludeOther) { job.Mode = "skip"; job.SkipReason = "slope label (include_other to position it)"; return; }
            if (curb.Count > 0 && Nearest(job.Anchor, curb, out _) <= o.CurbTolerance) { job.Mode = "align"; return; }
            job.Mode = "position";
        }

        private static bool WantsType(Options o, CivilLabel lbl) => o.Types switch
        {
            "elevation" => lbl is SurfaceElevationLabel,
            "slope"     => lbl is SurfaceSlopeLabel,
            "surface"   => lbl is SurfaceElevationLabel || lbl is SurfaceSlopeLabel,
            _           => true,
        };

        private static bool EndsWithAny(string layer, List<string> suffixes) =>
            suffixes.Any(s => (layer ?? "").ToLowerInvariant().EndsWith(s.Trim().ToLowerInvariant()));

        private static bool InWindow(Options o, Point3d p) =>
            o.WinMin == null || (p.X >= o.WinMin.Value.X && p.X <= o.WinMax.Value.X && p.Y >= o.WinMin.Value.Y && p.Y <= o.WinMax.Value.Y);

        // ── Obstacles ─────────────────────────────────────────────────────────────

        private static bool LayerVisible(Transaction tr, Database db, string layer)
        {
            try
            {
                var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
                if (!lt.Has(layer)) return true;
                var rec = (LayerTableRecord)tr.GetObject(lt[layer], OpenMode.ForRead);
                return !rec.IsFrozen && !rec.IsOff;
            }
            catch { return true; }
        }

        private static bool ObstacleLayer(Options o, Transaction tr, Database db, string layer)
        {
            if (o.IgnoreLayers.Count > 0 && EndsWithAny(layer, o.IgnoreLayers)) return false;
            if (o.ObstacleLayers.Count > 0 && !EndsWithAny(layer, o.ObstacleLayers)) return false;
            return LayerVisible(tr, db, layer);
        }

        private static bool Heavy(AcadEntity e) =>
            e is CivilSurface || e is Corridor || e is ProfileView || e is SectionView || e is Hatch || e is Wipeout ||
            e is Autodesk.AutoCAD.DatabaseServices.Surface || e is Solid3d || e is Region || e is Viewport;

        /// <summary>Walks model space and every block reference, rasterising linework into the grid's
        /// line layer and text-like things into its label layer. Only what overlaps the region is touched.</summary>
        private static void Rasterise(BlockTableRecord space, Transaction tr, Database db, Matrix3d xform, int depth,
                                      Options o, Grid grid, Box region, int radius, HashSet<ObjectId> movable,
                                      double textW, double textH,
                                      ref int curves, ref int texts, ref int fixedLabels)
        {
            if (depth > 8) return;
            foreach (ObjectId id in space)
            {
                AcadEntity entity;
                try { entity = tr.GetObject(id, OpenMode.ForRead) as AcadEntity; }
                catch { continue; }
                if (entity == null) continue;

                if (entity is BlockReference br)
                {
                    try
                    {
                        var ext = br.GeometricExtents; var b = Box.Of(ext);
                        if (b.MaxX < region.MinX || b.MinX > region.MaxX || b.MaxY < region.MinY || b.MinY > region.MaxY) continue;
                    }
                    catch { }
                    BlockTableRecord def;
                    try { def = (BlockTableRecord)tr.GetObject(br.BlockTableRecord, OpenMode.ForRead); }
                    catch { continue; }
                    Rasterise(def, tr, db, xform * br.BlockTransform, depth + 1, o, grid, region, radius, movable,
                              textW, textH, ref curves, ref texts, ref fixedLabels);
                    continue;
                }

                if (movable.Contains(id)) continue;             // the ones being placed are not obstacles to themselves
                if (Heavy(entity)) continue;
                if (!ObstacleLayer(o, tr, db, entity.Layer)) continue;

                Extents3d extents;
                try { extents = entity.GeometricExtents; } catch { continue; }
                var box = Box.Of(extents);
                if (depth > 0)
                {
                    // Extents are in the block's own space; a transformed copy is the honest way to get world ones.
                    var lo = extents.MinPoint.TransformBy(xform); var hi = extents.MaxPoint.TransformBy(xform);
                    box = new Box(Math.Min(lo.X, hi.X), Math.Min(lo.Y, hi.Y), Math.Max(lo.X, hi.X), Math.Max(lo.Y, hi.Y));
                }
                if (box.MaxX < region.MinX || box.MinX > region.MaxX || box.MaxY < region.MinY || box.MinY > region.MaxY) continue;

                // Text, mtext, leaders and labels that are not being moved are obstacles. For a label the
                // obstacle is its TEXT, not its whole extents: a two-point slope label's extents run the
                // length of its line, and a dragged label's include the leader. Blocking all of that
                // pushed positioned labels off the free ground and onto linework instead.
                if (entity is CivilLabel || entity is DBText || entity is MText || entity is MLeader)
                {
                    if (entity is CivilLabel fixedLbl && depth == 0 && textW > 0)
                        box = FixedTextBox(fixedLbl, box, textW, textH);
                    grid.Box(grid.Labels, box, +1);
                    if (entity is CivilLabel) fixedLabels++; else texts++;
                    continue;
                }

                foreach (Curve c in CurvesOf(entity, xform))
                {
                    try { RasteriseCurve(grid, c, radius); curves++; }
                    catch { }
                    finally { if (!ReferenceEquals(c, entity)) c.Dispose(); }
                }
            }
        }

        private static IEnumerable<Curve> CurvesOf(AcadEntity entity, Matrix3d xform)
        {
            if (entity is Curve curve)
            {
                if (xform.IsEqualTo(Matrix3d.Identity)) { yield return curve; yield break; }
                var copy = (Curve)curve.Clone();
                try { copy.TransformBy(xform); } catch { copy.Dispose(); yield break; }
                yield return copy;
                yield break;
            }
            var pieces = new DBObjectCollection();
            try { entity.Explode(pieces); } catch { yield break; }
            foreach (AcadDBObject piece in pieces)
            {
                if (piece is Curve c)
                {
                    try { if (!xform.IsEqualTo(Matrix3d.Identity)) c.TransformBy(xform); } catch { }
                    yield return c;            // disposed by the caller
                }
                else piece.Dispose();
            }
        }

        private static void RasteriseCurve(Grid grid, Curve c, int radius)
        {
            double len;
            try { len = c.GetDistanceAtParameter(c.EndParam) - c.GetDistanceAtParameter(c.StartParam); } catch { return; }
            if (double.IsNaN(len) || len <= 0) return;
            double step = grid.Cell / 2;
            int steps = (int)Math.Ceiling(len / step);
            if (steps > 2_000_000) return;                       // something absurd; skip rather than hang
            for (int i = 0; i <= steps; i++)
            {
                Point3d p;
                try { p = c.GetPointAtDist(Math.Min(len, i * step)); } catch { continue; }
                grid.Stamp(grid.Lines, p.X, p.Y, radius);
            }
        }

        // ── Measuring a label ─────────────────────────────────────────────────────

        private static Box Extents(CivilLabel lbl) => Box.Of(lbl.GeometricExtents);

        /// <summary>The text block of a label that is staying put, estimated from the typical text size.</summary>
        private static Box FixedTextBox(CivilLabel lbl, Box full, double w, double h)
        {
            try
            {
                if (lbl.Dragged) return TextBoxFrom(full, w, h, lbl.DraggedOffset);
                if (full.Width <= w * 1.5 && full.Height <= h * 1.5) return full;   // already just text (+ marker)
                // A two-point slope label, or anything else with a long line in its extents: the text
                // sits at the middle of the extents. Keep it at least the text size so it still blocks.
                double cx = (full.MinX + full.MaxX) / 2, cy = (full.MinY + full.MaxY) / 2;
                return new Box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2);
            }
            catch { return full; }
        }

        /// <summary>Text box of a dragged label: the text block is a W×H rectangle flush against the far
        /// side of the label's extents in the drag direction (the leader runs from the anchor to it).</summary>
        private static Box TextBoxFrom(Box full, double w, double h, Vector3d dir)
        {
            double minX = Math.Abs(dir.X) < 0.3 * dir.Length ? (full.MinX + full.MaxX) / 2 - w / 2
                        : dir.X > 0 ? full.MaxX - w : full.MinX;
            double minY = Math.Abs(dir.Y) < 0.3 * dir.Length ? (full.MinY + full.MaxY) / 2 - h / 2
                        : dir.Y > 0 ? full.MaxY - h : full.MinY;
            minX = Math.Max(full.MinX, minX); minY = Math.Max(full.MinY, minY);
            return new Box(minX, minY, Math.Min(full.MaxX, minX + w), Math.Min(full.MaxY, minY + h));
        }

        /// <summary>Text width/height, measured by dragging far along each axis: the extents then are
        /// leader + text in one direction and just the text in the other.</summary>
        private static (double w, double h) Calibrate(CivilLabel lbl)
        {
            const double far = 200.0;
            lbl.DraggedOffset = new Vector3d(0, far, 0);
            double w = Extents(lbl).Width;
            lbl.DraggedOffset = new Vector3d(far, 0, 0);
            double h = Extents(lbl).Height;
            return (w, h);
        }

        private static void Restore(CivilLabel lbl, Job job)
        {
            try
            {
                if (job.WasDragged) lbl.DraggedOffset = job.WasOffset;
                else lbl.ResetLocation();
            }
            catch { }
        }

        private static void Evaluate(CivilLabel lbl, Job job, Options o, Grid grid, List<double> leaders)
        {
            double w, h;
            try { (w, h) = Calibrate(lbl); }
            catch (SysException ex) { job.Problem = $"could not measure: {ex.Message}"; Restore(lbl, job); return; }
            if (w <= 0 || h <= 0) { job.Problem = "label has no extents"; Restore(lbl, job); return; }
            job.TextHeight = h;

            // Undragged: where the style puts it with no leader.
            if (o.AllowUndragged)
            {
                try
                {
                    lbl.ResetLocation();
                    var full = Extents(lbl);
                    var text = full.Width > w * 1.5 || full.Height > h * 1.5 ? TextBoxFrom(full, w, h, new Vector3d(1, 1, 0)) : full;
                    job.Candidates.Add(Score(o, grid, job, new Candidate
                    {
                        Undragged = true, Offset = new Vector3d(0, 0, 0), Text = text, Full = full,
                        Length = 0, DirectionRank = 0, IsCurrent = !job.WasDragged,
                    }));
                }
                catch { }
            }

            for (int di = 0; di < o.Directions.Count; di++)
            {
                double ang = o.Directions[di] * Math.PI / 180.0;
                foreach (double len in leaders)
                {
                    var off = new Vector3d(len * Math.Cos(ang), len * Math.Sin(ang), 0);
                    try
                    {
                        lbl.DraggedOffset = off;
                        var full = Extents(lbl);
                        var text = TextBoxFrom(full, w, h, off);
                        bool current = job.WasDragged && (job.WasOffset - off).Length < grid.Cell;
                        job.Candidates.Add(Score(o, grid, job, new Candidate
                        {
                            Offset = off, Text = text, Full = full, Length = len, DirectionRank = di, IsCurrent = current,
                        }));
                    }
                    catch { }
                }
            }

            // The spot it is in now, when that is a dragged position not in the candidate set.
            if (job.WasDragged && o.IncludeDragged && !job.Candidates.Any(c => c.IsCurrent))
            {
                try
                {
                    lbl.DraggedOffset = job.WasOffset;
                    var full = Extents(lbl);
                    job.Candidates.Add(Score(o, grid, job, new Candidate
                    {
                        Offset = job.WasOffset, Text = TextBoxFrom(full, w, h, job.WasOffset), Full = full,
                        Length = job.WasOffset.Length, DirectionRank = o.Directions.Count, IsCurrent = true,
                    }));
                }
                catch { }
            }

            Restore(lbl, job);
            if (job.Candidates.Count == 0) job.Problem = "no candidate could be evaluated";
        }

        private static Candidate Score(Options o, Grid grid, Job job, Candidate c)
        {
            c.LineHits = grid.Count(grid.Lines, c.Text);
            c.LeaderHits = c.Undragged ? 0 : grid.Along(job.Anchor, c.Text.Nearest(job.Anchor), o.Clearance + grid.Cell, c.Text);
            c.BaseCost = o.WLine * c.LineHits + o.WLeader * c.LeaderHits
                       + o.WLength * (job.TextHeight > 0 ? c.Length / job.TextHeight : c.Length)
                       + o.WDirection * c.DirectionRank
                       - (c.IsCurrent ? o.WStay : 0);
            return c;
        }

        private static double Cost(Options o, Grid grid, Candidate c, out int labelHits)
        {
            labelHits = grid.Count(grid.Labels, c.Text);
            return c.BaseCost + o.WLabel * labelHits;
        }

        private static Candidate Best(Options o, Grid grid, Job job, out double cost, out int hits)
        {
            Candidate best = null; cost = double.MaxValue; hits = 0;
            foreach (var c in job.Candidates)
            {
                double k = Cost(o, grid, c, out int lh);
                if (k < cost - 1e-9) { cost = k; best = c; hits = lh; }
            }
            return best;
        }

        // ── Entry point ───────────────────────────────────────────────────────────

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
                var warnings = new List<string>();
                var jobs = new List<Job>();
                var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);

                // ── Which labels ──
                IEnumerable<ObjectId> pool;
                try
                {
                    pool = o.Handles.Count > 0 ? o.Handles.Select(h => CadCommands.Resolve(db, h)).ToList() : ms.Cast<ObjectId>();
                }
                catch (SysException ex) { tr.Abort(); return Report(stage, false, ex.Message); }

                List<Curve> curb;
                try { curb = GradingLabels.CollectLayer(tr, db, o.CurbLayer); }
                catch { curb = new List<Curve>(); }

                int skippedDragged = 0, skippedPinned = 0;
                foreach (ObjectId id in pool)
                {
                    CivilLabel lbl;
                    try { lbl = tr.GetObject(id, OpenMode.ForRead, false) as CivilLabel; } catch { continue; }
                    if (lbl == null)
                    {
                        if (o.Handles.Count > 0) warnings.Add($"{id.Handle} is not a Civil 3D label; skipped.");
                        continue;
                    }
                    if (o.Handles.Count == 0)
                    {
                        if (!WantsType(o, lbl)) continue;
                        if (o.Layer != null && !EndsWithAny(lbl.Layer, new List<string> { o.Layer })) continue;
                        if (!InWindow(o, lbl.LabelLocation)) continue;
                    }
                    bool pinned = false, dragged = false;
                    try { pinned = lbl.Pinned; } catch { }
                    try { dragged = lbl.Dragged; } catch { }
                    if (pinned && !o.IncludePinned) { skippedPinned++; continue; }
                    if (dragged && !o.IncludeDragged) { skippedDragged++; continue; }
                    var job = new Job { Id = id, Handle = id.Handle.ToString(), Type = lbl.GetType().Name, Anchor = CadCommands.Flat(lbl.LabelLocation), WasDragged = dragged };
                    if (dragged) { try { job.WasOffset = lbl.DraggedOffset; } catch { } }
                    (job.Kind, job.Pad) = GradingLabels.ReadTag(tr, lbl);
                    if (o.Kinds != null && (job.Kind == null || !o.Kinds.Contains(job.Kind))) continue;
                    try { job.WasRotationDeg = CadCommands.R(CadCommands.Deg(lbl.RotationAngle)); } catch { }
                    Classify(job, lbl, o, curb);
                    jobs.Add(job);
                }
                var skipped = jobs.Where(j => j.Mode == "skip").ToList();
                var alignJobs = jobs.Where(j => j.Mode == "align").ToList();
                jobs = jobs.Where(j => j.Mode == "position").ToList();

                // ── Align: rotate curb labels to the back of curb at their anchor ──
                // Done before the grid is built so their rotated boxes are what the positioned
                // labels have to keep clear of.
                int aligned = 0;
                foreach (var job in alignJobs)
                {
                    try
                    {
                        if (curb.Count == 0) { job.Problem = $"no back of curb linework on a layer ending in '{o.CurbLayer}'"; continue; }
                        double d = Nearest(job.Anchor, curb, out Curve near);
                        job.CurbDistance = CadCommands.R(d);
                        double curbReach = job.Kind != null ? Math.Max(o.CurbTolerance, 5.0) : o.CurbTolerance;
                        if (d > curbReach) { job.Problem = $"anchor is {d:0.##} ft from the back of curb (more than {curbReach} ft)"; continue; }
                        double angle = TangentAngle(near, job.Anchor);
                        var lbl = (CivilLabel)tr.GetObject(job.Id, OpenMode.ForWrite);
                        lbl.RotationAngle = angle;
                        job.RotationDeg = CadCommands.R(CadCommands.Deg(angle));
                        aligned++;
                    }
                    catch (SysException ex) { job.Problem = $"could not rotate: {ex.Message}"; }
                }

                if (jobs.Count == 0 && alignJobs.Count == 0)
                {
                    tr.Abort();
                    return Report(stage, true, "No labels to place." +
                        (skipped.Count > 0 ? $" {skipped.Count} skipped (outside the spec)." : "") +
                        (skippedDragged > 0 ? $" {skippedDragged} already dragged (include_dragged to move them)." : "") +
                        (skippedPinned > 0 ? $" {skippedPinned} pinned." : ""), warnings: warnings);
                }
                if (jobs.Count == 0)
                {
                    // Only curb labels: nothing to grid. Commit the rotations if drawing.
                    if (apply) tr.Commit(); else tr.Abort();
                    return Report(stage, true, $"{(apply ? "Aligned" : "Would align")} {aligned} curb label(s); nothing to position.", new JObject
                    {
                        ["applied"] = apply, ["aligned"] = aligned, ["placed"] = 0,
                        ["labels"] = LabelsJson(alignJobs.Concat(skipped)),
                    }, warnings);
                }
                // ── Text size and grid ──
                // One calibration up front sets the cell size and the leader lengths; the per-candidate
                // measurements come later, once the grid exists to score against.
                var heights = new List<double>(); var widths = new List<double>();
                foreach (var job in jobs.Take(25))
                {
                    try
                    {
                        var lbl = (CivilLabel)tr.GetObject(job.Id, OpenMode.ForWrite);
                        var (w, h) = Calibrate(lbl);
                        Restore(lbl, job);
                        if (h > 0) heights.Add(h);
                        if (w > 0) widths.Add(w);
                    }
                    catch { }
                }
                double textHeight = heights.Count > 0 ? heights.OrderBy(v => v).ElementAt(heights.Count / 2) : 2.0;
                double textWidth  = widths.Count > 0 ? widths.OrderBy(v => v).ElementAt(widths.Count / 2) : 4 * textHeight;
                if (heights.Count == 0) warnings.Add("could not measure any label; assuming 2 ft text.");
                double cell = o.CellSize > 0 ? o.CellSize : Math.Max(0.25, textHeight / 2);
                var leaders = o.LeaderLengths.Count > 0 ? o.LeaderLengths : new List<double> { textHeight, 2 * textHeight, 3 * textHeight, 4.5 * textHeight };
                double reach = leaders.Max() + 4 * textHeight + o.Clearance;   // widest a candidate can get from its anchor

                var region = new Box(jobs.Min(j => j.Anchor.X) - reach, jobs.Min(j => j.Anchor.Y) - reach,
                                     jobs.Max(j => j.Anchor.X) + reach, jobs.Max(j => j.Anchor.Y) + reach);
                double cells = (region.Width / cell) * (region.Height / cell);
                if (cells > o.MaxCells)
                {
                    double factor = Math.Sqrt(cells / o.MaxCells);
                    cell *= factor;
                    warnings.Add($"region is {region.Width:0}×{region.Height:0} ft; cell size coarsened to {cell:0.00} ft to stay under {o.MaxCells} cells. Pass a window to work one sheet at a time.");
                }
                var grid = new Grid(region.MinX, region.MinY, region.MaxX, region.MaxY, cell);
                int radius = (int)Math.Ceiling(o.Clearance / cell);

                int curves = 0, texts = 0, fixedLabels = 0;
                var movable = new HashSet<ObjectId>(jobs.Select(j => j.Id));
                Rasterise(ms, tr, db, Matrix3d.Identity, 0, o, grid, region, radius, movable, textWidth, textHeight, ref curves, ref texts, ref fixedLabels);
                if (curves == 0) warnings.Add("no linework found within reach of the labels; check obstacle_layers / ignore_layers.");

                // ── Candidates ──
                foreach (var job in jobs)
                {
                    try
                    {
                        var lbl = (CivilLabel)tr.GetObject(job.Id, OpenMode.ForWrite);
                        Evaluate(lbl, job, o, grid, leaders);
                    }
                    catch (SysException ex) { job.Problem = ex.Message; }
                }
                var live = jobs.Where(j => j.Problem == null && j.Candidates.Count > 0).ToList();

                // ── Greedy: most constrained first ──
                // A label with few clean options goes first so it is not boxed out by easy ones.
                var order = live
                    .OrderBy(j => j.Candidates.Count(c => c.LineHits == 0))
                    .ThenByDescending(j => j.Candidates.Min(c => c.BaseCost))
                    .ToList();
                foreach (var job in order)
                {
                    job.Chosen = Best(o, grid, job, out job.ChosenCost, out job.ChosenLabelHits);
                    grid.Box(grid.Labels, job.Chosen.Text, +1);
                }

                // ── Repair passes ──
                int passes = 0, moves = 0;
                for (; passes < o.MaxPasses; passes++)
                {
                    bool improved = false;
                    foreach (var job in order.Where(j => j.ChosenLabelHits > 0 || j.Chosen.LineHits > 0).OrderByDescending(j => j.ChosenCost).ToList())
                    {
                        grid.Box(grid.Labels, job.Chosen.Text, -1);
                        var best = Best(o, grid, job, out double cost, out int hits);
                        if (cost < job.ChosenCost - 1e-6)
                        {
                            job.Chosen = best; job.ChosenCost = cost; job.ChosenLabelHits = hits;
                            improved = true; moves++;
                        }
                        grid.Box(grid.Labels, job.Chosen.Text, +1);
                    }
                    if (!improved) break;
                }

                // ── Apply ──
                int applied = 0;
                if (apply)
                {
                    try
                    {
                        foreach (var job in live)
                        {
                            var lbl = (CivilLabel)tr.GetObject(job.Id, OpenMode.ForWrite);
                            if (job.Chosen.Undragged) { if (job.WasDragged) lbl.ResetLocation(); }
                            else lbl.DraggedOffset = job.Chosen.Offset;
                            applied++;
                        }
                    }
                    catch (SysException ex)
                    {
                        tr.Abort();
                        foreach (var c in curb) c.Dispose();
                        return Report(stage, false, $"could not move a label, nothing was changed: {ex.Message}", warnings: warnings);
                    }
                    tr.Commit();
                }
                else tr.Abort();   // every temporary offset used for measuring goes away with the transaction

                // ── Report ──
                int clean = live.Count(j => j.Chosen.LineHits == 0 && j.ChosenLabelHits == 0);
                int moved = live.Count(j => !j.Chosen.IsCurrent);
                foreach (var c in curb) c.Dispose();

                string msg = apply
                    ? $"Placed {applied} label(s): {clean} clear of linework and other labels, {live.Count - clean} still overlapping something; aligned {aligned} curb label(s)."
                    : $"{live.Count} label(s) evaluated: {clean} can be made clear, {live.Count - clean} cannot with these candidates; {aligned} curb label(s) would be aligned.";
                return Report(stage, true, msg, new JObject
                {
                    ["applied"] = apply,
                    ["labels_considered"] = jobs.Count + alignJobs.Count + skipped.Count,
                    ["aligned"] = aligned,
                    ["skipped"] = skipped.Count,
                    ["placed"] = live.Count,
                    ["clean"] = clean,
                    ["overlapping"] = live.Count - clean,
                    ["moved"] = moved,
                    ["unchanged"] = live.Count - moved,
                    ["problems"] = jobs.Count(j => j.Problem != null),
                    ["skipped_dragged"] = skippedDragged,
                    ["skipped_pinned"] = skippedPinned,
                    ["repair_passes"] = passes,
                    ["repair_moves"] = moves,
                    ["grid"] = new JObject
                    {
                        ["cell"] = CadCommands.R(cell), ["columns"] = grid.W, ["rows"] = grid.H,
                        ["region"] = region.Json(), ["clearance"] = o.Clearance,
                        ["curves"] = curves, ["texts"] = texts, ["fixed_labels"] = fixedLabels,
                    },
                    ["text_height"] = CadCommands.R(textHeight),
                    ["leader_lengths"] = new JArray(leaders.Select(CadCommands.R)),
                    ["directions"] = new JArray(o.Directions),
                    ["labels"] = LabelsJson(jobs.Concat(alignJobs).Concat(skipped)),
                    // The geometry this stage puts on the sheet, in the one shape every
                    // handler reports it: each positioned label's text box, and its leader
                    // from the anchor to the box. Aligned labels only rotate in place and
                    // add nothing. The spatial gate on the Python side checks these
                    // against the drawing before the draw stage is allowed to run.
                    ["proposed"] = new JObject
                    {
                        ["boxes"] = new JArray(live.Select(j => j.Chosen.Text.Json())),
                        ["segments"] = new JArray(live.Where(j => !j.Chosen.Undragged).Select(j =>
                        {
                            var near = j.Chosen.Text.Nearest(j.Anchor);
                            return new JArray(
                                new JArray(CadCommands.R(j.Anchor.X), CadCommands.R(j.Anchor.Y)),
                                new JArray(CadCommands.R(near.X), CadCommands.R(near.Y)));
                        })),
                    },
                }, warnings);
            }
        }

        private static JArray LabelsJson(IEnumerable<Job> jobs) => new JArray(jobs.Select(j =>
        {
            var jo = new JObject
            {
                ["handle"] = j.Handle, ["type"] = j.Type, ["mode"] = j.Mode,
                ["anchor"] = new JArray(CadCommands.R(j.Anchor.X), CadCommands.R(j.Anchor.Y)),
            };
            if (j.Kind != null) jo["kind"] = j.Kind;
            if (j.Pad != null) jo["pad"] = j.Pad;
            if (j.Mode == "skip") { jo["why"] = j.SkipReason; return jo; }
            if (j.Problem != null) { jo["problem"] = j.Problem; return jo; }
            if (j.Mode == "align")
            {
                jo["rotation_deg"] = j.RotationDeg; jo["was_rotation_deg"] = j.WasRotationDeg;
                jo["curb_distance"] = j.CurbDistance;
                jo["dragged"] = j.WasDragged;
                return jo;
            }
            jo["was"] = new JObject { ["dragged"] = j.WasDragged, ["offset"] = j.WasDragged ? new JArray(CadCommands.R(j.WasOffset.X), CadCommands.R(j.WasOffset.Y)) : null };
            if (j.Chosen == null) { jo["problem"] = "not placed"; return jo; }
            jo["placed"] = j.Chosen.Json(j.ChosenLabelHits, j.ChosenCost);
            jo["moved"] = !j.Chosen.IsCurrent;
            jo["clean"] = j.Chosen.LineHits == 0 && j.ChosenLabelHits == 0;
            return jo;
        }));

        private static string Report(string stage, bool ok, string message, JObject extra = null, List<string> warnings = null)
        {
            var o = new JObject { ["ok"] = ok, ["stage"] = stage, ["message"] = message };
            if (!ok) o["errors"] = new JArray(message);
            if (extra != null) foreach (var p in extra.Properties()) o[p.Name] = p.Value;
            o["warnings"] = new JArray(warnings ?? new List<string>());
            return o.ToString();
        }
    }
}
