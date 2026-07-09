using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Shapes;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;
using Autodesk.Civil.ApplicationServices;
using Autodesk.Civil.DatabaseServices;

// Disambiguate types shared between AutoCAD/Civil and WPF namespaces
using AcadApp            = Autodesk.AutoCAD.ApplicationServices.Application;
using CivilLabel         = Autodesk.Civil.DatabaseServices.Label;
using AcadEntity         = Autodesk.AutoCAD.DatabaseServices.Entity;
using AcadLine           = Autodesk.AutoCAD.DatabaseServices.Line;
using AcadPolyline       = Autodesk.AutoCAD.DatabaseServices.Polyline;
using WpfDoubleCollection = System.Windows.Media.DoubleCollection;
using WpfEllipse         = System.Windows.Shapes.Ellipse;
using WpfLine            = System.Windows.Shapes.Line;
using WpfPoint           = System.Windows.Point;
using WpfTextAlignment   = System.Windows.TextAlignment;

[assembly: CommandClass(typeof(CADAgent.ObjectGraphCommand))]

namespace CADAgent
{
    // ── Data transfer objects ─────────────────────────────────────────────────

    internal sealed class GraphNodeData
    {
        public readonly string Handle, Type, Layer, Label;
        public readonly double WorldX, WorldY;
        public GraphNodeData(string handle, string type, string layer, string label, double x, double y)
        { Handle = handle; Type = type; Layer = layer; Label = label; WorldX = x; WorldY = y; }
    }

    internal sealed class GraphEdgeData
    {
        public readonly string From, To, EdgeType;
        public GraphEdgeData(string from, string to, string edgeType)
        { From = from; To = to; EdgeType = edgeType; }
    }

    // ── AutoCAD command ───────────────────────────────────────────────────────

    public sealed class ObjectGraphCommand
    {
        private const double HalfSpan = 25.0; // 50×50 ft box → ±25 ft from center

        [CommandMethod("GRAPHOBJECT")]
        public void GraphObject()
        {
            var doc = AcadApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            var ed = doc.Editor;
            var db = doc.Database;

            var peo = new PromptEntityOptions("\nSelect the focal object: ");
            peo.AllowNone = false;
            var per = ed.GetEntity(peo);
            if (per.Status != PromptStatus.OK) return;

            var nodes     = new List<GraphNodeData>();
            var edges     = new List<GraphEdgeData>();
            string seedHandle;

            using (doc.LockDocument())
            using (var tr = db.TransactionManager.StartTransaction())
            {
                var seed = tr.GetObject(per.ObjectId, OpenMode.ForRead) as AcadEntity;
                if (seed == null) { tr.Abort(); return; }

                seedHandle = seed.Handle.ToString();
                var center = CenterOf(seed);

                double x0 = center.X - HalfSpan, x1 = center.X + HalfSpan;
                double y0 = center.Y - HalfSpan, y1 = center.Y + HalfSpan;

                // ── Collect primitives ────────────────────────────────────────
                var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);

                var lines     = new List<AcadLine>();
                var arcs      = new List<Arc>();
                var polylines = new List<AcadPolyline>();

                foreach (ObjectId id in ms)
                {
                    try
                    {
                        var obj = tr.GetObject(id, OpenMode.ForRead) as AcadEntity;
                        if (obj == null) continue;
                        // Civil 3D types are handled separately via their own API
                        if (obj is Alignment || obj is Pipe || obj is Structure) continue;
                        if (!BboxIntersects(obj, x0, y0, x1, y1)) continue;

                        string h = obj.Handle.ToString();
                        if (obj is AcadLine ln)
                        {
                            lines.Add(ln);
                            var c = Mid2d(ln.StartPoint, ln.EndPoint);
                            nodes.Add(new GraphNodeData(h, "Line", ln.Layer, $"Line\n{h}", c.X, c.Y));
                        }
                        else if (obj is Arc ar)
                        {
                            arcs.Add(ar);
                            nodes.Add(new GraphNodeData(h, "Arc", ar.Layer, $"Arc\n{h}", ar.Center.X, ar.Center.Y));
                        }
                        else if (obj is AcadPolyline pl)
                        {
                            polylines.Add(pl);
                            var c = PlCenter(pl);
                            nodes.Add(new GraphNodeData(h, "Polyline", pl.Layer, $"PL\n{pl.Layer}", c.X, c.Y));
                        }
                        else if (obj is BlockReference br)
                        {
                            nodes.Add(new GraphNodeData(h, "Block", br.Layer,
                                $"Blk\n{br.Name}", br.Position.X, br.Position.Y));
                        }
                        else if (obj is CivilLabel lbl)
                        {
                            // LabelLocation is the anchor — the point on the civil feature
                            // the label connects to, NOT where the text box ends up.
                            Point3d anchor;
                            try   { anchor = lbl.LabelLocation; }
                            catch { var ext = lbl.GeometricExtents;
                                    anchor = new Point3d(
                                        (ext.MinPoint.X + ext.MaxPoint.X) / 2,
                                        (ext.MinPoint.Y + ext.MaxPoint.Y) / 2, 0); }
                            string shortType = lbl.GetType().Name.Replace("Label", "");
                            nodes.Add(new GraphNodeData(h, "Label", lbl.Layer,
                                $"Lbl\n{shortType}", anchor.X, anchor.Y));
                        }
                    }
                    catch { }
                }

                // Spatial adjacency for primitives (1 ft threshold for touching/near-touching objects)
                var hash = new SpatialHash(cellSize: 10.0, adjacencyThreshold: 1.0);
                hash.Build(lines, arcs, polylines);
                foreach (var e in hash.Edges)
                    edges.Add(new GraphEdgeData(e.From.Handle.ToString(), e.To.Handle.ToString(), "adjacent"));

                // ── Civil 3D objects ──────────────────────────────────────────
                try
                {
                    var civil       = CivilApplication.ActiveDocument;
                    var seenHandles = new HashSet<string>();

                    // Alignments
                    foreach (ObjectId aid in civil.GetAlignmentIds())
                    {
                        try
                        {
                            var al = tr.GetObject(aid, OpenMode.ForRead) as Alignment;
                            if (al == null || !BboxIntersects(al, x0, y0, x1, y1)) continue;
                            var ext = al.GeometricExtents;
                            nodes.Add(new GraphNodeData(al.Handle.ToString(), "Alignment", al.Layer,
                                $"Align\n{al.Name}",
                                (ext.MinPoint.X + ext.MaxPoint.X) / 2,
                                (ext.MinPoint.Y + ext.MaxPoint.Y) / 2));
                        }
                        catch { }
                    }

                    // Pipe networks
                    foreach (ObjectId netId in civil.GetPipeNetworkIds())
                    {
                        try
                        {
                            var net = tr.GetObject(netId, OpenMode.ForRead) as Network;
                            if (net == null) continue;

                            foreach (ObjectId sid in net.GetStructureIds())
                            {
                                try
                                {
                                    var s = tr.GetObject(sid, OpenMode.ForRead) as Structure;
                                    if (s == null) continue;
                                    var ip = s.Location;
                                    if (!Pt2dInBbox(ip.X, ip.Y, x0, y0, x1, y1)) continue;
                                    string sh = s.Handle.ToString();
                                    if (seenHandles.Add(sh))
                                        nodes.Add(new GraphNodeData(sh, "Structure", s.Layer,
                                            $"Struct\n{s.Name}", ip.X, ip.Y));
                                }
                                catch { }
                            }

                            foreach (ObjectId pid in net.GetPipeIds())
                            {
                                try
                                {
                                    var p  = tr.GetObject(pid, OpenMode.ForRead) as Pipe;
                                    if (p == null) continue;
                                    var sp = p.StartPoint;
                                    var ep = p.EndPoint;
                                    double pcx = (sp.X + ep.X) / 2;
                                    double pcy = (sp.Y + ep.Y) / 2;
                                    // Include if any endpoint or midpoint falls inside the box
                                    if (!Pt2dInBbox(sp.X, sp.Y, x0, y0, x1, y1) &&
                                        !Pt2dInBbox(ep.X, ep.Y, x0, y0, x1, y1) &&
                                        !Pt2dInBbox(pcx,  pcy,  x0, y0, x1, y1)) continue;

                                    string ph = p.Handle.ToString();
                                    if (seenHandles.Add(ph))
                                        nodes.Add(new GraphNodeData(ph, "Pipe", p.Layer,
                                            $"Pipe\n{p.Name}", pcx, pcy));

                                    if (p.StartStructureId != ObjectId.Null)
                                    {
                                        try
                                        {
                                            var ss = tr.GetObject(p.StartStructureId, OpenMode.ForRead) as Structure;
                                            if (ss != null)
                                                edges.Add(new GraphEdgeData(ph, ss.Handle.ToString(), "pipe_connection"));
                                        }
                                        catch { }
                                    }
                                    if (p.EndStructureId != ObjectId.Null)
                                    {
                                        try
                                        {
                                            var es = tr.GetObject(p.EndStructureId, OpenMode.ForRead) as Structure;
                                            if (es != null)
                                                edges.Add(new GraphEdgeData(ph, es.Handle.ToString(), "pipe_connection"));
                                        }
                                        catch { }
                                    }
                                }
                                catch { }
                            }
                        }
                        catch { }
                    }
                }
                catch { }

                tr.Commit();
            }

            ed.WriteMessage($"\n  Graph: {nodes.Count} nodes, {edges.Count} edges in 50×50 ft region.\n");

            // Launch the WPF window on a dedicated STA thread so it doesn't block the command
            var capturedNodes = nodes;
            var capturedEdges = edges;
            var capturedSeed  = seedHandle;
            var thread = new Thread(() =>
            {
                var win = new ObjectGraphWindow(capturedNodes, capturedEdges, capturedSeed);
                win.ShowDialog();
            });
            thread.SetApartmentState(ApartmentState.STA);
            thread.IsBackground = true;
            thread.Start();
        }

        // ── Geometry helpers ──────────────────────────────────────────────────

        private static Point2d CenterOf(AcadEntity e) => e switch
        {
            AcadLine ln       => Mid2d(ln.StartPoint, ln.EndPoint),
            Arc ar            => new Point2d(ar.Center.X, ar.Center.Y),
            AcadPolyline pl   => PlCenter(pl),
            BlockReference br => new Point2d(br.Position.X, br.Position.Y),
            Structure s       => new Point2d(s.Location.X, s.Location.Y),
            Pipe p            => Mid2d(p.StartPoint, p.EndPoint),
            Alignment al      => ExtCenter(al.GeometricExtents),
            CivilLabel lbl         => LabelAnchor(lbl),
            _                 => ExtCenter(e.GeometricExtents),
        };

        private static Point2d LabelAnchor(CivilLabel lbl)
        {
            try   { var a = lbl.LabelLocation; return new Point2d(a.X, a.Y); }
            catch { return ExtCenter(lbl.GeometricExtents); }
        }

        private static Point2d Mid2d(Point3d a, Point3d b) => new((a.X + b.X) / 2, (a.Y + b.Y) / 2);

        private static Point2d ExtCenter(Extents3d ext) =>
            new((ext.MinPoint.X + ext.MaxPoint.X) / 2, (ext.MinPoint.Y + ext.MaxPoint.Y) / 2);

        private static Point2d PlCenter(AcadPolyline pl)
        {
            int n = pl.NumberOfVertices;
            if (n == 0) return Point2d.Origin;
            double sx = 0, sy = 0;
            for (int i = 0; i < n; i++) { var v = pl.GetPoint2dAt(i); sx += v.X; sy += v.Y; }
            return new Point2d(sx / n, sy / n);
        }

        private static bool BboxIntersects(AcadEntity e, double x0, double y0, double x1, double y1)
        {
            try
            {
                var ext = e.GeometricExtents;
                return ext.MinPoint.X <= x1 && ext.MaxPoint.X >= x0 &&
                       ext.MinPoint.Y <= y1 && ext.MaxPoint.Y >= y0;
            }
            catch { return false; }
        }

        private static bool Pt2dInBbox(double x, double y, double x0, double y0, double x1, double y1) =>
            x >= x0 && x <= x1 && y >= y0 && y <= y1;
    }

    // ── WPF interactive graph window ──────────────────────────────────────────

    internal sealed class ObjectGraphWindow : Window
    {
        private const double NodeR = 18;
        private const double Pad   = 60;
        private const double CW    = 1000;
        private const double CH    = 700;

        private readonly Canvas _canvas = new() { Width = CW, Height = CH, ClipToBounds = true };
        private readonly Dictionary<string, NodeView> _views    = new();
        private readonly List<(string from, string to, WpfLine line)> _edgeLines = new();

        private NodeView? _dragging;
        private WpfPoint  _anchor;
        private double    _startCx, _startCy;

        private static readonly Dictionary<string, Color> _colors = new()
        {
            ["Line"]      = Colors.DodgerBlue,
            ["Arc"]       = Colors.OrangeRed,
            ["Polyline"]  = Colors.ForestGreen,
            ["Alignment"] = Colors.MediumPurple,
            ["Pipe"]      = Colors.DarkCyan,
            ["Structure"] = Colors.Crimson,
            ["Block"]     = Colors.SlateGray,
            ["Label"]     = Colors.Yellow,
        };

        public ObjectGraphWindow(List<GraphNodeData> nodes, List<GraphEdgeData> edges, string seedHandle)
        {
            Title      = "Object Graph — 50 × 50 ft";
            Width      = CW + 32;
            Height     = CH + 80;
            Background = new SolidColorBrush(Color.FromRgb(28, 28, 28));

            _canvas.Background = new SolidColorBrush(Color.FromRgb(38, 38, 38));
            _canvas.MouseMove           += (_, e) => OnDragMove(e.GetPosition(_canvas));
            _canvas.MouseLeftButtonUp   += (_, _) => EndDrag();

            Content = new ScrollViewer
            {
                Content = _canvas,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
                VerticalScrollBarVisibility   = ScrollBarVisibility.Auto,
                Margin = new Thickness(4),
            };

            if (nodes.Count == 0)
            {
                _canvas.Children.Add(new TextBlock
                {
                    Text = "No objects found in the 50 × 50 ft region.",
                    Foreground = Brushes.White, FontSize = 14,
                    Margin = new Thickness(20),
                });
                return;
            }

            // ── World → canvas coordinate mapping ────────────────────────────
            double wxMin = nodes.Min(n => n.WorldX), wxMax = nodes.Max(n => n.WorldX);
            double wyMin = nodes.Min(n => n.WorldY), wyMax = nodes.Max(n => n.WorldY);
            double wW    = Math.Max(wxMax - wxMin, 1);
            double wH    = Math.Max(wyMax - wyMin, 1);
            double scale = Math.Min((CW - 2 * Pad) / wW, (CH - 2 * Pad) / wH);

            // Single-node: place at canvas center; multi-node: map by world position
            bool single = nodes.Count == 1;
            double Cx(double wx) => single ? CW / 2 : (wx - wxMin) * scale + Pad;
            double Cy(double wy) => single ? CH / 2 : CH - ((wy - wyMin) * scale + Pad); // flip Y

            // ── Draw edges first (rendered behind nodes) ──────────────────────
            var byHandle = nodes.ToDictionary(n => n.Handle);
            foreach (var e in edges)
            {
                if (!byHandle.TryGetValue(e.From, out var fn) ||
                    !byHandle.TryGetValue(e.To,   out var tn)) continue;

                bool structural = e.EdgeType == "pipe_connection";
                var line = new WpfLine
                {
                    X1 = Cx(fn.WorldX), Y1 = Cy(fn.WorldY),
                    X2 = Cx(tn.WorldX), Y2 = Cy(tn.WorldY),
                    Stroke = new SolidColorBrush(structural
                        ? Color.FromRgb(210, 170, 80)
                        : Color.FromRgb(110, 110, 110)),
                    StrokeThickness = structural ? 2.0 : 1.0,
                };
                if (!structural)
                    line.StrokeDashArray = new WpfDoubleCollection { 4, 2 };
                _canvas.Children.Add(line);
                _edgeLines.Add((e.From, e.To, line));
            }

            // ── Draw nodes ────────────────────────────────────────────────────
            foreach (var n in nodes)
            {
                bool isSeed = n.Handle == seedHandle;
                double r    = isSeed ? NodeR * 1.35 : NodeR;
                double cx   = Cx(n.WorldX);
                double cy   = Cy(n.WorldY);

                var fill   = new SolidColorBrush(_colors.TryGetValue(n.Type, out var col) ? col : Colors.Gray);
                var stroke = new SolidColorBrush(isSeed ? Colors.Gold : Color.FromRgb(200, 200, 200));

                var ellipse = new WpfEllipse
                {
                    Width  = r * 2,  Height = r * 2,
                    Fill   = fill,   Stroke = stroke,
                    StrokeThickness = isSeed ? 3.0 : 1.5,
                    Cursor  = Cursors.Hand,
                    ToolTip = $"{n.Type}  [{n.Handle}]\nLayer: {n.Layer}",
                };

                var parts = n.Label.Split('\n');
                var lbl = new TextBlock
                {
                    Text          = parts.Length > 1 ? parts[1] : parts[0],
                    Foreground    = Brushes.White,
                    FontSize      = 9,
                    MaxWidth      = 90,
                    TextWrapping  = TextWrapping.Wrap,
                    TextAlignment = WpfTextAlignment.Center,
                };

                var stack = new StackPanel { Orientation = Orientation.Vertical };
                stack.Children.Add(ellipse);
                stack.Children.Add(lbl);

                Canvas.SetLeft(stack, cx - r);
                Canvas.SetTop(stack,  cy - r);
                _canvas.Children.Add(stack);

                var view = new NodeView(n.Handle, stack, cx, cy, r);
                _views[n.Handle] = view;

                ellipse.Tag = view;
                ellipse.MouseLeftButtonDown += BeginDrag;
            }

            // ── Legend ────────────────────────────────────────────────────────
            var legend = new StackPanel
            {
                Orientation = Orientation.Vertical,
                Background  = new SolidColorBrush(Color.FromArgb(200, 20, 20, 20)),
            };
            foreach (var (type, color) in _colors)
            {
                var row = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(6, 3, 6, 3) };
                row.Children.Add(new WpfEllipse
                {
                    Width  = 12, Height = 12,
                    Fill   = new SolidColorBrush(color),
                    Margin = new Thickness(0, 0, 6, 0),
                });
                row.Children.Add(new TextBlock { Text = type, Foreground = Brushes.White, FontSize = 10 });
                legend.Children.Add(row);
            }
            var legendBorder = new Border
            {
                Child           = legend,
                BorderBrush     = new SolidColorBrush(Color.FromRgb(90, 90, 90)),
                BorderThickness = new Thickness(1),
            };
            Canvas.SetLeft(legendBorder, 10);
            Canvas.SetTop(legendBorder,  10);
            _canvas.Children.Add(legendBorder);
        }

        // ── Drag ─────────────────────────────────────────────────────────────

        private void BeginDrag(object sender, MouseButtonEventArgs e)
        {
            if (sender is not WpfEllipse el || el.Tag is not NodeView v) return;
            _dragging = v;
            _anchor   = e.GetPosition(_canvas);
            _startCx  = v.CenterX;
            _startCy  = v.CenterY;
            Mouse.Capture(_canvas); // capture on canvas so fast mouse moves don't miss events
            e.Handled = true;
        }

        private void OnDragMove(WpfPoint pos)
        {
            if (_dragging == null) return;
            double cx = _startCx + (pos.X - _anchor.X);
            double cy = _startCy + (pos.Y - _anchor.Y);
            _dragging.CenterX = cx;
            _dragging.CenterY = cy;
            Canvas.SetLeft(_dragging.Container, cx - _dragging.Radius);
            Canvas.SetTop (_dragging.Container, cy - _dragging.Radius);

            // Update every edge endpoint that touches this node
            foreach (var (from, to, line) in _edgeLines)
            {
                if (from == _dragging.Handle) { line.X1 = cx; line.Y1 = cy; }
                if (to   == _dragging.Handle) { line.X2 = cx; line.Y2 = cy; }
            }
        }

        private void EndDrag()
        {
            Mouse.Capture(null);
            _dragging = null;
        }

        private sealed class NodeView
        {
            public readonly string           Handle;
            public readonly FrameworkElement Container;
            public readonly double           Radius;
            public double CenterX, CenterY;

            public NodeView(string handle, FrameworkElement container, double cx, double cy, double r)
            { Handle = handle; Container = container; CenterX = cx; CenterY = cy; Radius = r; }
        }
    }
}
