using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.RegularExpressions;
using Newtonsoft.Json.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.Colors;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;

using SysException = System.Exception;
using AcadEntity   = Autodesk.AutoCAD.DatabaseServices.Entity;

namespace CADAgent
{
    /// <summary>
    /// The general-purpose 2D drafting command set behind POST /execute.
    ///
    /// Every intent is a JSON-in / JSON-out function that runs inside ONE transaction:
    /// it either commits everything it did or aborts and reports why. Results always
    /// carry the handles of what was created / modified / removed so the Python layer
    /// can chain commands ("offset @2, then fillet @3 with @4").
    ///
    /// The class is split across files by category:
    ///   Core      registry, JSON helpers, handle resolution, sequence runner
    ///   Draw      points, lines, polylines, circles, arcs, ellipses, splines, regions
    ///   Modify    move/copy/rotate/scale/mirror/stretch/align, offset/array/fillet/chamfer,
    ///             trim/extend/break/lengthen/join/explode/divide, polyline + spline editing
    ///   Annotate  text, hatch, dimensions, leaders, tolerances, tables, styles
    ///   Organize  layers, blocks, attributes, xrefs, images, groups, purge, rename,
    ///             units, layouts, viewports, view, save
    ///   Query     properties, selection filters, key points, closest point, intersections,
    ///             tangents, measurements, duplicates
    ///
    /// Intents that touch Editor / LayoutManager / xref loading are flagged as needing the
    /// AutoCAD application thread; HttpBridge runs those inline instead of on a worker.
    /// </summary>
    public static partial class CadCommands
    {
        public delegate string Handler(JObject request, Document doc);

        private static readonly Dictionary<string, Handler> _handlers =
            new Dictionary<string, Handler>(StringComparer.OrdinalIgnoreCase);
        private static readonly HashSet<string> _appThread =
            new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        public const double Tol = 1e-6;

        static CadCommands()
        {
            Add("sequence", RunSequence);
            RegisterDraw();
            RegisterModify();
            RegisterAnnotate();
            RegisterOrganize();
            RegisterQuery();
            RegisterGrading();
            RegisterSpacing();
        }

        private static void Add(string intent, Handler handler, bool appThread = false)
        {
            _handlers[intent] = handler;
            if (appThread) _appThread.Add(intent);
        }

        public static bool Has(string intent) => intent != null && _handlers.ContainsKey(intent);

        public static IEnumerable<string> Intents => _handlers.Keys.OrderBy(k => k);

        /// <summary>True when the intent (or any step of a sequence) must run on the application thread.</summary>
        public static bool RequiresAppThread(string intent, string json)
        {
            if (intent == null) return false;
            if (_appThread.Contains(intent)) return true;
            if (!intent.Equals("sequence", StringComparison.OrdinalIgnoreCase)) return false;
            try
            {
                var root = JObject.Parse(ModelSpaceBuilder.ExtractJson(json) ?? "{}");
                foreach (JToken step in root["steps"] as JArray ?? new JArray())
                {
                    string si = step["intent"]?.ToString() ?? "";
                    if (_appThread.Contains(si)) return true;
                }
            }
            catch { }
            return false;
        }

        public static string Execute(string intent, string json, Document doc)
        {
            if (!_handlers.TryGetValue(intent ?? "", out Handler handler))
                return Fail($"Unknown intent: {intent}");
            JObject root;
            try
            {
                string extracted = ModelSpaceBuilder.ExtractJson(json);
                root = extracted == null ? new JObject() : JObject.Parse(extracted);
            }
            catch (SysException ex) { return Fail($"Invalid JSON: {ex.Message}"); }

            try   { return handler(root, doc); }
            catch (SysException ex) { return Fail($"{intent} failed: {ex.Message}"); }
        }

        // ── Sequence: several intents in one all-or-nothing outer transaction ─────

        private static readonly Regex _refPattern = new Regex(@"^@(\d+)(?:\[(\d+)\])?$", RegexOptions.Compiled);

        /// <summary>
        /// { "steps": [ { "intent": "draw_line", ... }, { "intent": "offset_entities", "handles": ["@0"] } ] }
        /// "@n" is the first handle created by step n, "@n[i]" the i-th. Any failing step aborts the
        /// outer transaction, so nothing from earlier steps survives either.
        /// </summary>
        private static string RunSequence(JObject req, Document doc)
        {
            var steps = req["steps"] as JArray;
            if (steps == null || steps.Count == 0) return Fail("sequence needs a non-empty 'steps' array.");

            var results = new JArray();
            var created = new List<List<string>>();
            Database db = doc.Database;

            using (Transaction outer = db.TransactionManager.StartTransaction())
            {
                for (int i = 0; i < steps.Count; i++)
                {
                    var step = steps[i] as JObject;
                    if (step == null) { outer.Abort(); return Fail($"step {i} is not an object."); }
                    string intent = step["intent"]?.ToString() ?? "";
                    if (intent.Equals("sequence", StringComparison.OrdinalIgnoreCase) ||
                        !_handlers.TryGetValue(intent, out Handler handler))
                    {
                        outer.Abort();
                        return Fail($"step {i}: unknown intent '{intent}'.");
                    }

                    string result;
                    try
                    {
                        JObject resolved = (JObject)SubstituteRefs(step, created, i);
                        result = handler(resolved, doc);
                    }
                    catch (SysException ex) { result = Fail(ex.Message); }

                    JObject parsed;
                    try { parsed = JObject.Parse(result); }
                    catch { parsed = new JObject { ["ok"] = false, ["message"] = result }; }

                    bool ok = parsed["ok"]?.Value<bool>() ?? false;
                    parsed["step"] = i;
                    parsed["intent"] = intent;
                    results.Add(parsed);
                    if (!ok)
                    {
                        outer.Abort();
                        return new JObject
                        {
                            ["ok"] = false,
                            ["message"] = $"step {i} ({intent}) failed; every step was rolled back.",
                            ["failed_step"] = i,
                            ["results"] = results,
                        }.ToString();
                    }
                    created.Add((parsed["created"] as JArray)?.Select(t => t.ToString()).ToList() ?? new List<string>());
                }
                outer.Commit();
            }

            return new JObject
            {
                ["ok"] = true,
                ["message"] = $"{steps.Count} step(s) applied.",
                ["created"] = new JArray(created.SelectMany(c => c)),
                ["results"] = results,
            }.ToString();
        }

        private static JToken SubstituteRefs(JToken token, List<List<string>> created, int stepIndex)
        {
            switch (token)
            {
                case JObject obj:
                {
                    var copy = new JObject();
                    foreach (var prop in obj.Properties())
                        copy[prop.Name] = SubstituteRefs(prop.Value, created, stepIndex);
                    return copy;
                }
                case JArray arr:
                    return new JArray(arr.Select(t => SubstituteRefs(t, created, stepIndex)));
                case JValue val when val.Type == JTokenType.String:
                {
                    var m = _refPattern.Match(val.ToString());
                    if (!m.Success) return val;
                    int n = int.Parse(m.Groups[1].Value);
                    int k = m.Groups[2].Success ? int.Parse(m.Groups[2].Value) : 0;
                    if (n >= stepIndex) throw new ArgumentException($"'{val}' refers to step {n}, which has not run yet (current step {stepIndex}).");
                    if (k >= created[n].Count) throw new ArgumentException($"'{val}': step {n} created only {created[n].Count} object(s).");
                    return new JValue(created[n][k]);
                }
                default:
                    return token;
            }
        }

        // ── Result helpers ────────────────────────────────────────────────────────

        public static string Ok(string message, JObject extra = null)
        {
            var o = new JObject { ["ok"] = true, ["message"] = message };
            if (extra != null) foreach (var p in extra.Properties()) o[p.Name] = p.Value;
            return o.ToString();
        }

        public static string Fail(string message) =>
            new JObject { ["ok"] = false, ["message"] = message }.ToString();

        public static string Created(string message, params ObjectId[] ids) =>
            Ok(message, new JObject { ["created"] = Handles(ids) });

        public static string Created(string message, IEnumerable<ObjectId> ids, JObject extra = null)
        {
            var o = new JObject { ["created"] = Handles(ids) };
            if (extra != null) foreach (var p in extra.Properties()) o[p.Name] = p.Value;
            return Ok(message, o);
        }

        public static JArray Handles(IEnumerable<ObjectId> ids) =>
            new JArray(ids.Where(id => !id.IsNull).Select(id => id.Handle.ToString()));

        // ── JSON → geometry ───────────────────────────────────────────────────────

        public static double D(JToken t, double fallback = 0) =>
            t == null || t.Type == JTokenType.Null ? fallback : t.Value<double>();

        public static int I(JToken t, int fallback = 0) =>
            t == null || t.Type == JTokenType.Null ? fallback : t.Value<int>();

        public static bool B(JToken t, bool fallback = false) =>
            t == null || t.Type == JTokenType.Null ? fallback : t.Value<bool>();

        public static string S(JToken t, string fallback = null) =>
            t == null || t.Type == JTokenType.Null ? fallback : t.ToString();

        public static double Rad(JToken degrees, double fallbackDeg = 0) =>
            D(degrees, fallbackDeg) * Math.PI / 180.0;

        public static double Deg(double radians) => radians * 180.0 / Math.PI;

        /// <summary>Accepts [x,y], [x,y,z], "x,y" or {"x":..,"y":..}. Z defaults to 0.</summary>
        public static Point3d P3(JToken t)
        {
            if (t == null || t.Type == JTokenType.Null) throw new ArgumentException("missing point");
            if (t is JObject o) return new Point3d(D(o["x"]), D(o["y"]), D(o["z"]));
            if (t.Type == JTokenType.String)
            {
                var parts = t.ToString().Trim().TrimStart('[').TrimEnd(']').Split(',');
                var vals = parts.Select(p => double.Parse(p.Trim(), CultureInfo.InvariantCulture)).ToList();
                while (vals.Count < 3) vals.Add(0);
                return new Point3d(vals[0], vals[1], vals[2]);
            }
            var a = (JArray)t;
            if (a.Count < 2) throw new ArgumentException("point needs at least x and y");
            return new Point3d(D(a[0]), D(a[1]), a.Count > 2 ? D(a[2]) : 0);
        }

        public static Point2d P2(JToken t) { var p = P3(t); return new Point2d(p.X, p.Y); }

        public static Point3d? P3Opt(JToken t) => t == null || t.Type == JTokenType.Null ? (Point3d?)null : P3(t);

        public static List<Point3d> Points(JToken t)
        {
            var list = new List<Point3d>();
            if (t is JArray arr) foreach (var item in arr) list.Add(P3(item));
            return list;
        }

        public static JArray Arr(Point3d p) => new JArray(R(p.X), R(p.Y), R(p.Z));
        public static JArray Arr(Point2d p) => new JArray(R(p.X), R(p.Y));
        public static JArray Arr(Vector3d v) => new JArray(R(v.X), R(v.Y), R(v.Z));
        public static double R(double v) => Math.Round(v, 6);

        public static Point3d Flat(Point3d p) => new Point3d(p.X, p.Y, 0);

        // ── Handle resolution ─────────────────────────────────────────────────────

        public static ObjectId Resolve(Database db, string handle)
        {
            if (string.IsNullOrWhiteSpace(handle)) throw new ArgumentException("empty handle");
            long value;
            try { value = (long)Convert.ToUInt64(handle.Trim(), 16); }
            catch { throw new ArgumentException($"'{handle}' is not a hex handle"); }
            if (!db.TryGetObjectId(new Handle(value), out ObjectId id) || id.IsNull || id.IsErased)
                throw new ArgumentException($"no object with handle {handle}");
            return id;
        }

        public static List<ObjectId> ResolveAll(Database db, JToken handles, string field = "handles")
        {
            var ids = new List<ObjectId>();
            if (handles == null) throw new ArgumentException($"'{field}' is required");
            if (handles.Type == JTokenType.String) { ids.Add(Resolve(db, handles.ToString())); return ids; }
            foreach (JToken h in handles as JArray ?? throw new ArgumentException($"'{field}' must be a list of handles"))
                ids.Add(Resolve(db, h.ToString()));
            if (ids.Count == 0) throw new ArgumentException($"'{field}' is empty");
            return ids;
        }

        public static T Open<T>(Transaction tr, ObjectId id, OpenMode mode = OpenMode.ForRead) where T : class
        {
            var obj = tr.GetObject(id, mode) as T;
            if (obj == null) throw new ArgumentException($"object {id.Handle} is a {tr.GetObject(id, OpenMode.ForRead).GetType().Name}, not a {typeof(T).Name}");
            return obj;
        }

        public static Curve OpenCurve(Transaction tr, ObjectId id, OpenMode mode = OpenMode.ForRead) => Open<Curve>(tr, id, mode);

        // ── Database helpers ──────────────────────────────────────────────────────

        public static BlockTableRecord ModelSpace(Transaction tr, Database db, bool forWrite = true)
        {
            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            return (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace],
                                                  forWrite ? OpenMode.ForWrite : OpenMode.ForRead);
        }

        /// <summary>The block table record an entity lives in (model space, a layout, or a block definition), opened for write.</summary>
        public static BlockTableRecord OwnerSpace(Transaction tr, AcadEntity entity) =>
            (BlockTableRecord)tr.GetObject(entity.OwnerId, OpenMode.ForWrite);

        /// <summary>Returns the layer name, creating the layer if it does not exist yet.</summary>
        public static string EnsureLayer(Transaction tr, Database db, string layer)
        {
            if (string.IsNullOrWhiteSpace(layer)) return "0";
            var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
            if (lt.Has(layer)) return layer;
            lt.UpgradeOpen();
            var ltr = new LayerTableRecord { Name = layer };
            lt.Add(ltr);
            tr.AddNewlyCreatedDBObject(ltr, true);
            return layer;
        }

        /// <summary>Appends an entity to a space, sets its layer and returns its ObjectId.</summary>
        public static ObjectId Append(Transaction tr, BlockTableRecord space, AcadEntity entity, string layer = null)
        {
            ObjectId id = space.AppendEntity(entity);
            tr.AddNewlyCreatedDBObject(entity, true);
            if (layer != null) entity.Layer = EnsureLayer(tr, space.Database, layer);
            return id;
        }

        /// <summary>Runs <paramref name="body"/> inside one transaction: commit on success, abort on exception or ok=false.</summary>
        public static string InTransaction(Document doc, Func<Transaction, Database, string> body)
        {
            Database db = doc.Database;
            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                string result;
                try
                {
                    result = body(tr, db);
                }
                catch
                {
                    tr.Abort();
                    throw;
                }
                bool ok = true;
                try { ok = JObject.Parse(result)["ok"]?.Value<bool>() ?? true; } catch { }
                if (ok) tr.Commit(); else tr.Abort();
                return result;
            }
        }

        public static Color ColorFrom(JToken t)
        {
            if (t == null || t.Type == JTokenType.Null) return null;
            if (t.Type == JTokenType.Integer || t.Type == JTokenType.Float)
            {
                int idx = t.Value<int>();
                if (idx == 256) return Color.FromColorIndex(ColorMethod.ByLayer, 256);
                if (idx == 0)   return Color.FromColorIndex(ColorMethod.ByBlock, 0);
                return Color.FromColorIndex(ColorMethod.ByAci, (short)idx);
            }
            if (t is JArray rgb && rgb.Count == 3)
                return Color.FromRgb((byte)I(rgb[0]), (byte)I(rgb[1]), (byte)I(rgb[2]));
            string s = t.ToString().Trim().ToLowerInvariant();
            if (s == "bylayer") return Color.FromColorIndex(ColorMethod.ByLayer, 256);
            if (s == "byblock") return Color.FromColorIndex(ColorMethod.ByBlock, 0);
            if (int.TryParse(s, out int n)) return Color.FromColorIndex(ColorMethod.ByAci, (short)n);
            throw new ArgumentException($"unrecognised colour '{t}': use an ACI index, [r,g,b], 'bylayer' or 'byblock'");
        }

        public static LineWeight LineWeightFrom(JToken t)
        {
            if (t == null || t.Type == JTokenType.Null) throw new ArgumentException("missing lineweight");
            string s = t.ToString().Trim().ToLowerInvariant();
            if (s == "bylayer") return LineWeight.ByLayer;
            if (s == "byblock") return LineWeight.ByBlock;
            if (s == "default") return LineWeight.ByLineWeightDefault;
            double mm = double.Parse(s, CultureInfo.InvariantCulture);
            int hundredths = (int)Math.Round(mm * 100);
            if (!Enum.IsDefined(typeof(LineWeight), hundredths))
                throw new ArgumentException($"lineweight {mm} mm is not a standard value (0, 0.05, 0.09, 0.13, 0.15, 0.18, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5, 0.53, 0.6, 0.7, 0.8, 0.9, 1, 1.06, 1.2, 1.4, 1.58, 2, 2.11)");
            return (LineWeight)hundredths;
        }

        public static ObjectId LinetypeId(Transaction tr, Database db, string name)
        {
            var ltt = (LinetypeTable)tr.GetObject(db.LinetypeTableId, OpenMode.ForRead);
            if (ltt.Has(name)) return ltt[name];
            // Try loading it from acad.lin before giving up.
            try { db.LoadLineTypeFile(name, "acad.lin"); } catch { }
            ltt = (LinetypeTable)tr.GetObject(db.LinetypeTableId, OpenMode.ForRead);
            if (ltt.Has(name)) return ltt[name];
            throw new ArgumentException($"linetype '{name}' is not loaded and was not found in acad.lin");
        }

        // ── Geometry helpers shared by several files ──────────────────────────────

        public static double Angle(Point3d from, Point3d to) => Math.Atan2(to.Y - from.Y, to.X - from.X);

        public static double Norm(double a)
        {
            a %= 2 * Math.PI;
            if (a < 0) a += 2 * Math.PI;
            return a;
        }

        public static Vector3d Dir(Point3d from, Point3d to)
        {
            var v = to - from;
            if (v.Length < Tol) throw new ArgumentException("two points coincide");
            return v.GetNormal();
        }

        public static Point3d Polar(Point3d from, double angle, double dist) =>
            new Point3d(from.X + dist * Math.Cos(angle), from.Y + dist * Math.Sin(angle), from.Z);

        /// <summary>Signed area of a 2D polygon (positive = counter-clockwise).</summary>
        public static double SignedArea(IList<Point3d> pts)
        {
            double a = 0;
            for (int i = 0; i < pts.Count; i++)
            {
                var p = pts[i]; var q = pts[(i + 1) % pts.Count];
                a += p.X * q.Y - q.X * p.Y;
            }
            return a / 2;
        }

        public static bool PointInPolygon(Point3d p, IList<Point3d> poly)
        {
            bool inside = false;
            for (int i = 0, j = poly.Count - 1; i < poly.Count; j = i++)
            {
                var a = poly[i]; var b = poly[j];
                if ((a.Y > p.Y) != (b.Y > p.Y) &&
                    p.X < (b.X - a.X) * (p.Y - a.Y) / (b.Y - a.Y) + a.X)
                    inside = !inside;
            }
            return inside;
        }

        /// <summary>Intersection points of two curves, optionally on their extensions.</summary>
        public static List<Point3d> Intersect(Curve a, Curve b, Intersect mode = Autodesk.AutoCAD.DatabaseServices.Intersect.OnBothOperands)
        {
            var pts = new Point3dCollection();
            try { a.IntersectWith(b, mode, pts, IntPtr.Zero, IntPtr.Zero); }
            catch { }
            return pts.Cast<Point3d>().Select(Flat).ToList();
        }

        public static JObject BoundsJson(AcadEntity e)
        {
            try
            {
                var ext = e.GeometricExtents;
                return new JObject { ["min"] = Arr(ext.MinPoint), ["max"] = Arr(ext.MaxPoint) };
            }
            catch { return null; }
        }
    }
}
