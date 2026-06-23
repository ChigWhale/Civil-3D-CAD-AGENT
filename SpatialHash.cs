using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Newtonsoft.Json.Linq;

namespace CADAgent
{
    internal sealed class SpatialHash
    {
        // ── Configuration ─────────────────────────────────────────────────────────
        public readonly double CellSize;
        public readonly double Threshold;

        // ── Grid ──────────────────────────────────────────────────────────────────
        private readonly Dictionary<(long, long), HashSet<ObjectId>>  _grid     = new();
        private readonly Dictionary<ObjectId, HashSet<(long, long)>>  _objCells = new();

        // ── Geometry cache ────────────────────────────────────────────────────────
        private readonly Dictionary<ObjectId, Line>     _lines     = new();
        private readonly Dictionary<ObjectId, Arc>      _arcs      = new();
        private readonly Dictionary<ObjectId, Polyline> _polylines = new();

        // ── Output ────────────────────────────────────────────────────────────────
        private readonly List<AdjacencyEdge>             _edges    = new();
        private readonly HashSet<(ObjectId, ObjectId)>   _edgeKeys = new();

        public IReadOnlyList<AdjacencyEdge> Edges => _edges;

        // Returns every occupied cell as world-space bounds (minX, minY, maxX, maxY)
        public IEnumerable<(double x0, double y0, double x1, double y1)> GetOccupiedCells()
        {
            foreach (var (cx, cy) in _grid.Keys)
                yield return (cx * CellSize, cy * CellSize, (cx + 1) * CellSize, (cy + 1) * CellSize);
        }

        public SpatialHash(double cellSize = 50.0, double adjacencyThreshold = 50.0)
        {
            CellSize  = cellSize;
            Threshold = adjacencyThreshold;
        }

        // ── Public API ────────────────────────────────────────────────────────────

        public void Build(IEnumerable<Line> lines, IEnumerable<Arc> arcs, IEnumerable<Polyline> polylines)
        {
            foreach (var ln in lines)     InsertLine(ln);
            foreach (var ar in arcs)      InsertArc(ar);
            foreach (var pl in polylines) InsertPolyline(pl);
            FindAdjacencies();
        }

        public JArray ToJson()
        {
            var arr = new JArray();
            foreach (var e in _edges)
            {
                arr.Add(new JObject
                {
                    ["from"] = e.From.Handle.ToString(),
                    ["to"]   = e.To.Handle.ToString(),
                    ["type"] = "adjacent",
                    ["contact"] = new JObject
                    {
                        ["point"]        = new JArray(Math.Round(e.ContactPoint.X, 4),  Math.Round(e.ContactPoint.Y, 4)),
                        ["from_feature"] = e.FromFeature,
                        ["to_feature"]   = e.ToFeature,
                        ["from_normal"]  = new JArray(Math.Round(e.FromNormal.X, 6),    Math.Round(e.FromNormal.Y, 6)),
                        ["to_normal"]    = new JArray(Math.Round(e.ToNormal.X,   6),    Math.Round(e.ToNormal.Y,   6)),
                        ["distance"]     = Math.Round(e.Distance, 4),
                    }
                });
            }
            return arr;
        }

        // ── Insert pass ───────────────────────────────────────────────────────────

        private void InsertLine(Line ln)
        {
            _lines[ln.Id] = ln;
            var ext = ln.GeometricExtents;
            FillBbox(ln.Id, ext.MinPoint.X, ext.MinPoint.Y, ext.MaxPoint.X, ext.MaxPoint.Y);
        }

        private void InsertArc(Arc ar)
        {
            _arcs[ar.Id] = ar;
            int samples = Math.Max(8, (int)Math.Ceiling(ar.Length / (CellSize * 0.5)));
            for (int i = 0; i <= samples; i++)
            {
                var pt = ar.GetPointAtDist(ar.Length * i / samples);
                AddToCell(ar.Id, CellKey(pt.X, pt.Y));
            }
        }

        private void InsertPolyline(Polyline pl)
        {
            _polylines[pl.Id] = pl;
            int n    = pl.NumberOfVertices;
            int segs = pl.Closed ? n : n - 1;
            for (int s = 0; s < segs; s++)
            {
                if (pl.GetSegmentType(s) == SegmentType.Line)
                {
                    var seg = pl.GetLineSegment2dAt(s);
                    FillBbox(pl.Id,
                        Math.Min(seg.StartPoint.X, seg.EndPoint.X),
                        Math.Min(seg.StartPoint.Y, seg.EndPoint.Y),
                        Math.Max(seg.StartPoint.X, seg.EndPoint.X),
                        Math.Max(seg.StartPoint.Y, seg.EndPoint.Y));
                }
                else
                {
                    // Arc segment — bounding box of chord (broad phase; exact test confirms)
                    var p0 = pl.GetPoint2dAt(s);
                    var p1 = pl.GetPoint2dAt((s + 1) % n);
                    FillBbox(pl.Id,
                        Math.Min(p0.X, p1.X), Math.Min(p0.Y, p1.Y),
                        Math.Max(p0.X, p1.X), Math.Max(p0.Y, p1.Y));
                }
            }
        }

        // ── Narrow phase ──────────────────────────────────────────────────────────

        private void FindAdjacencies()
        {
            var allIds  = _lines.Keys.Concat(_arcs.Keys).Concat(_polylines.Keys).ToList();
            var Checked = new HashSet<(ObjectId, ObjectId)>();

            foreach (var id in allIds)
            {
                foreach (var other in Candidates(id))
                {
                    var key = NormalizeKey(id, other);
                    if (!Checked.Add(key)) continue;

                    var result = ClosestPoints(id, other);
                    if (result != null && result.Value.Distance <= Threshold)
                        AddEdge(id, other, result.Value);
                }
            }
        }

        private HashSet<ObjectId> Candidates(ObjectId id)
        {
            var set = new HashSet<ObjectId>();
            if (!_objCells.TryGetValue(id, out var cells)) return set;

            foreach (var (cx, cy) in cells)
                for (int dx = -1; dx <= 1; dx++)
                    for (int dy = -1; dy <= 1; dy++)
                        if (_grid.TryGetValue((cx + dx, cy + dy), out var bucket))
                            foreach (var oid in bucket)
                                if (oid != id) set.Add(oid);
            return set;
        }

        private ContactResult? ClosestPoints(ObjectId a, ObjectId b)
        {
            Curve ca = GetCurve(a), cb = GetCurve(b);
            if (ca == null || cb == null) return null;

            double best  = double.MaxValue;
            Point3d bestA = default, bestB = default;

            // Sample A, find closest on B
            foreach (var pt in SamplePoints(ca))
            {
                try
                {
                    var closest = cb.GetClosestPointTo(pt, false);
                    double d    = pt.DistanceTo(closest);
                    if (d < best) { best = d; bestA = pt; bestB = closest; }
                }
                catch { }
            }

            // Sample B, find closest on A (catches cases where A samples miss the minimum)
            foreach (var pt in SamplePoints(cb))
            {
                try
                {
                    var closest = ca.GetClosestPointTo(pt, false);
                    double d    = pt.DistanceTo(closest);
                    if (d < best) { best = d; bestA = closest; bestB = pt; }
                }
                catch { }
            }

            if (best > Threshold) return null;

            var contact  = new Point2d((bestA.X + bestB.X) / 2, (bestA.Y + bestB.Y) / 2);
            int featA    = FeatureIndex(ca, bestA);
            int featB    = FeatureIndex(cb, bestB);

            return new ContactResult
            {
                Distance     = best,
                ContactPoint = contact,
                FromFeature  = featA,
                ToFeature    = featB,
                FromNormal   = OutwardNormal(ca, bestA),
                ToNormal     = OutwardNormal(cb, bestB),
            };
        }

        private List<Point3d> SamplePoints(Curve curve)
        {
            var pts = new List<Point3d> { curve.StartPoint };
            try
            {
                double len = curve is Line ln ? ln.Length
                           : curve is Arc  ar ? ar.Length
                           : ((Polyline)curve).Length;

                int n = Math.Max(4, (int)Math.Ceiling(len / (CellSize * 0.4)));
                for (int i = 1; i < n; i++)
                    pts.Add(curve.GetPointAtDist(len * i / n));
            }
            catch { }
            pts.Add(curve.EndPoint);
            return pts;
        }

        private int FeatureIndex(Curve curve, Point3d pt)
        {
            if (curve is Polyline pl)
            {
                try { return (int)Math.Floor(pl.GetParameterAtPoint(pt)); }
                catch { return 0; }
            }
            return 0;
        }

        private Vector2d OutwardNormal(Curve curve, Point3d pt)
        {
            try
            {
                double param  = curve.GetParameterAtPoint(pt);
                var tangent   = curve.GetFirstDerivative(param);
                // Rotate 90° CCW → left of travel direction
                var normal    = new Vector2d(-tangent.Y, tangent.X).GetNormal();

                // For closed polylines flip if winding says left-normal is inward
                if (curve is Polyline pl && pl.Closed && IsWindingCCW(pl))
                    normal = -normal;

                return normal;
            }
            catch { return Vector2d.XAxis; }
        }

        private static bool IsWindingCCW(Polyline pl)
        {
            double area = 0;
            int n = pl.NumberOfVertices;
            for (int i = 0; i < n; i++)
            {
                var p0 = pl.GetPoint2dAt(i);
                var p1 = pl.GetPoint2dAt((i + 1) % n);
                area += p0.X * p1.Y - p1.X * p0.Y;
            }
            return area > 0; // positive signed area = CCW
        }

        // ── Grid helpers ──────────────────────────────────────────────────────────

        private (long, long) CellKey(double x, double y) =>
            ((long)Math.Floor(x / CellSize), (long)Math.Floor(y / CellSize));

        private void AddToCell(ObjectId id, (long, long) key)
        {
            if (!_grid.TryGetValue(key, out var bucket))
                _grid[key] = bucket = new HashSet<ObjectId>();
            bucket.Add(id);

            if (!_objCells.TryGetValue(id, out var cells))
                _objCells[id] = cells = new HashSet<(long, long)>();
            cells.Add(key);
        }

        private void FillBbox(ObjectId id, double x0, double y0, double x1, double y1)
        {
            var (cx0, cy0) = CellKey(x0, y0);
            var (cx1, cy1) = CellKey(x1, y1);
            for (long cx = cx0; cx <= cx1; cx++)
                for (long cy = cy0; cy <= cy1; cy++)
                    AddToCell(id, (cx, cy));
        }

        private Curve GetCurve(ObjectId id)
        {
            if (_lines    .TryGetValue(id, out var ln)) return ln;
            if (_arcs     .TryGetValue(id, out var ar)) return ar;
            if (_polylines.TryGetValue(id, out var pl)) return pl;
            return null;
        }

        private (ObjectId, ObjectId) NormalizeKey(ObjectId a, ObjectId b) =>
            a.Handle.Value <= b.Handle.Value ? (a, b) : (b, a);

        private void AddEdge(ObjectId from, ObjectId to, ContactResult r)
        {
            var key = NormalizeKey(from, to);
            if (!_edgeKeys.Add(key)) return;

            bool swap = from.Handle.Value > to.Handle.Value;
            _edges.Add(new AdjacencyEdge
            {
                From         = swap ? to   : from,
                To           = swap ? from : to,
                ContactPoint = r.ContactPoint,
                FromFeature  = swap ? r.ToFeature   : r.FromFeature,
                ToFeature    = swap ? r.FromFeature : r.ToFeature,
                FromNormal   = swap ? r.ToNormal    : r.FromNormal,
                ToNormal     = swap ? r.FromNormal  : r.ToNormal,
                Distance     = r.Distance,
            });
        }

        // ── Internal types ────────────────────────────────────────────────────────

        private struct ContactResult
        {
            public double   Distance;
            public Point2d  ContactPoint;
            public int      FromFeature, ToFeature;
            public Vector2d FromNormal, ToNormal;
        }
    }

    internal sealed class AdjacencyEdge
    {
        public ObjectId  From, To;
        public Point2d   ContactPoint;
        public int       FromFeature, ToFeature;
        public Vector2d  FromNormal,  ToNormal;
        public double    Distance;
    }
}
