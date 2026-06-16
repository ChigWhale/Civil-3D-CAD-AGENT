using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;
using Autodesk.Civil.ApplicationServices;
using Autodesk.Civil.DatabaseServices;

// Disambiguate types that exist in both AutoCAD and Civil3D namespaces
using SysException  = System.Exception;
using AcadDBObject  = Autodesk.AutoCAD.DatabaseServices.DBObject;
using AcadEntity    = Autodesk.AutoCAD.DatabaseServices.Entity;

namespace CADAgent
{
    /// <summary>
    /// Reconstructs Civil 3D / AutoCAD objects in model space from agent plan steps.
    /// </summary>
    public class ModelSpaceBuilder
    {
        // ── Programmatic entry point (called by the palette pipeline) ─────────────

        /// <summary>
        /// Returns true if <paramref name="response"/> looks like a build-instruction
        /// JSON object (action == "add", or contains at least one known Civil 3D key).
        /// </summary>
        /// <summary>
        /// Extracts the first balanced JSON object from a response string by counting
        /// braces, so trailing prose or extra JSON objects don't corrupt the result.
        /// Returns null if no complete JSON object is found.
        /// </summary>
        public static string ExtractJson(string response)
        {
            if (string.IsNullOrWhiteSpace(response)) return null;
            int start = response.IndexOf('{');
            if (start < 0) return null;
            int depth = 0;
            bool inString = false;
            bool escape = false;
            for (int i = start; i < response.Length; i++)
            {
                char c = response[i];
                if (escape) { escape = false; continue; }
                if (c == '\\' && inString) { escape = true; continue; }
                if (c == '"') { inString = !inString; continue; }
                if (inString) continue;
                if (c == '{') depth++;
                else if (c == '}') { depth--; if (depth == 0) return response.Substring(start, i - start + 1); }
            }
            return null;
        }

        /// <summary>
        /// Parses the agent's JSON array output into a list of step objects.
        /// Falls back to a single-object list if the response is not an array.
        /// </summary>
        public static List<JObject> ParseSteps(string response)
        {
            if (string.IsNullOrWhiteSpace(response)) return new List<JObject>();
            try
            {
                string trimmed = response.Trim();
                if (trimmed.StartsWith("["))
                {
                    var arr = Newtonsoft.Json.Linq.JArray.Parse(trimmed);
                    return arr.OfType<JObject>().ToList();
                }
                string extracted = ExtractJson(response);
                if (extracted == null) return new List<JObject>();
                var obj = JObject.Parse(extracted);
                return new List<JObject> { obj };
            }
            catch { return new List<JObject>(); }
        }

        /// <summary>
        /// Dispatches a single plan step to the appropriate handler method.
        /// </summary>
        public static string ExecuteStep(string intent, string json, Document doc)
        {
            switch (intent)
            {
                case "draw_line":         return DrawLine(json, doc);
                case "draw_shape":        return DrawShape(json, doc);
                case "create_layer":      return CreateLayer(json, doc);
                case "add_to_layer":      return AddToLayer(json, doc);
                case "edit_layer":        return EditLayer(json, doc);
                case "remove_from_layer": return RemoveFromLayer(json, doc);
                case "delete":            return Delete(json, doc);
                default:                  return $"Unknown intent: {intent}";
            }
        }

        /// <summary>
        /// Returns the "intent" field from the LLM's JSON envelope, or "chat" if unparseable.
        /// </summary>
        public static string GetIntent(string response)
        {
            try
            {
                string json = ExtractJson(response);
                if (json == null) return "chat";
                var obj = JObject.Parse(json);
                return obj["intent"]?.ToString()?.ToLower() ?? "chat";
            }
            catch { return "chat"; }
        }

        /// <summary>
        /// Returns the "text" field from the LLM's JSON envelope, or the raw response if unparseable.
        /// </summary>
        public static string GetText(string response)
        {
            try
            {
                string json = ExtractJson(response);
                if (json == null) return response;
                var obj = JObject.Parse(json);
                return obj["text"]?.ToString() ?? response;
            }
            catch { return response; }
        }

        public static bool IsBuildInstruction(string response)
        {
            if (string.IsNullOrWhiteSpace(response)) return false;
            try
            {
                string extracted = ExtractJson(response);
                if (extracted == null) return false;
                var obj = JObject.Parse(extracted);
                if (obj["action"]?.ToString() == "add") return true;
                return obj.ContainsKey("lines")         ||
                       obj.ContainsKey("arcs")          ||
                       obj.ContainsKey("alignments")    ||
                       obj.ContainsKey("feature_lines") ||
                       obj.ContainsKey("surfaces")      ||
                       obj.ContainsKey("corridors")     ||
                       obj.ContainsKey("pipe_networks");
            }
            catch { return false; }
        }

        /// <summary>
        /// Returns true if <paramref name="response"/> is a delete-instruction JSON object
        /// (action == "delete" with an object_handle).
        /// </summary>
        public static bool IsDeleteInstruction(string response)
        {
            if (string.IsNullOrWhiteSpace(response)) return false;
            if (!response.Contains("\"delete\"")) return false;
            try
            {
                string json = ExtractJson(response);
                if (json == null) return false;
                var obj = JObject.Parse(json);
                return obj["action"]?.ToString() == "delete" &&
                       obj["objects"] is JArray;
            }
            catch { return false; }
        }

        /// <summary>
        /// Erases all objects listed in the delete-instruction JSON.
        /// Called by the palette when the agent returns a delete-instruction JSON.
        /// </summary>
        public static string Delete(string json, Document doc)
        {
            JObject root;
            string rootJson = ExtractJson(json);
            if (rootJson == null) return "Invalid JSON: no JSON object found.";
            try   { root = JObject.Parse(rootJson); }
            catch (SysException ex) { return $"Invalid JSON: {ex.Message}"; }

            var objects = root["objects"] as JArray
                         ?? (root["objects"]?.Type == JTokenType.String
                             ? JArray.Parse(root["objects"].ToString().Replace("'", "\"")) : null);
            if (objects == null || objects.Count == 0)
                return "Delete failed: no objects listed in response.";

            Database db = doc.Database;
            int deleted = 0, skipped = 0;

            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                foreach (JToken item in objects)
                {
                    string handle     = item["object_handle"]?.ToString();
                    string objectType = item["object_type"]?.ToString() ?? "object";

                    if (string.IsNullOrWhiteSpace(handle)) { skipped++; continue; }

                    try
                    {
                        long handleLong = (long)Convert.ToUInt64(handle, 16);
                        ObjectId objId;
                        if (!db.TryGetObjectId(new Handle(handleLong), out objId)) { skipped++; continue; }

                        AcadDBObject obj = tr.GetObject(objId, OpenMode.ForWrite) as AcadDBObject;
                        if (obj == null) { skipped++; continue; }
                        obj.Erase();
                        deleted++;
                    }
                    catch { skipped++; }
                }
                tr.Commit();
            }

            string summary = $"Deleted {deleted} object(s), {skipped} skipped.";
            return summary;
        }

        // ── draw_line ─────────────────────────────────────────────────────────────

        public static string DrawLine(string json, Document doc)
        {
            try
            {
                var root = JObject.Parse(ExtractJson(json));
                var sp   = ResolvePointArray(root["start_point"]);
                var ep   = ResolvePointArray(root["end_point"]);
                string layer = root["layer"]?.ToString() ?? "0";

                Database db = doc.Database;
                using (Transaction tr = db.TransactionManager.StartTransaction())
                {
                    BlockTable       bt = tr.GetObject(db.BlockTableId,                OpenMode.ForRead)  as BlockTable;
                    BlockTableRecord ms = tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForWrite) as BlockTableRecord;
                    var line = new Line(Pt3(sp), Pt3(ep));
                    ms.AppendEntity(line);
                    tr.AddNewlyCreatedDBObject(line, true);
                    LayerTable lt = tr.GetObject(db.LayerTableId, OpenMode.ForRead) as LayerTable;
                    line.Layer = (lt != null && lt.Has(layer)) ? layer : "0";
                    tr.Commit();
                }
                return "Line created.";
            }
            catch (SysException ex) { return $"draw_line failed: {ex.Message}"; }
        }

        // ── draw_shape ────────────────────────────────────────────────────────────

        public static string DrawShape(string json, Document doc)
        {
            try
            {
                var root     = JObject.Parse(ExtractJson(json));
                var vertices = root["vertices"] as JArray
                              ?? (root["vertices"]?.Type == JTokenType.String
                                  ? JArray.Parse(root["vertices"].ToString()) : null);
                string layer = root["layer"]?.ToString() ?? "0";
                bool closed  = root["closed"]?.Value<bool>() ?? true;

                if (vertices == null || vertices.Count < 2)
                    return "draw_shape failed: need at least 2 vertices.";

                Database db = doc.Database;
                using (Transaction tr = db.TransactionManager.StartTransaction())
                {
                    BlockTable       bt = tr.GetObject(db.BlockTableId,                OpenMode.ForRead)  as BlockTable;
                    BlockTableRecord ms = tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForWrite) as BlockTableRecord;
                    var pl = new Polyline { Closed = closed };
                    for (int i = 0; i < vertices.Count; i++)
                        pl.AddVertexAt(i, Pt2(vertices[i] as JArray), 0, 0, 0);
                    ms.AppendEntity(pl);
                    tr.AddNewlyCreatedDBObject(pl, true);
                    LayerTable lt = tr.GetObject(db.LayerTableId, OpenMode.ForRead) as LayerTable;
                    pl.Layer = (lt != null && lt.Has(layer)) ? layer : "0";
                    tr.Commit();
                }
                return "Shape created.";
            }
            catch (SysException ex) { return $"draw_shape failed: {ex.Message}"; }
        }

        // ── create_layer ──────────────────────────────────────────────────────────

        public static string CreateLayer(string json, Document doc)
        {
            try
            {
                var root     = JObject.Parse(ExtractJson(json));
                string name  = root["name"]?.ToString();
                short  color    = root["color"] != null ? root["color"].Value<short>() : (short)7;
                string linetype = root["linetype"]?.ToString() ?? "Continuous";

                if (string.IsNullOrWhiteSpace(name)) return "create_layer failed: no name provided.";

                Database db = doc.Database;
                using (Transaction tr = db.TransactionManager.StartTransaction())
                {
                    LayerTable lt2 = tr.GetObject(db.LayerTableId, OpenMode.ForWrite) as LayerTable;
                    if (lt2.Has(name)) { tr.Abort(); return $"Layer '{name}' already exists."; }
                    var ltr = new LayerTableRecord
                    {
                        Name  = name,
                        Color = Autodesk.AutoCAD.Colors.Color.FromColorIndex(
                                    Autodesk.AutoCAD.Colors.ColorMethod.ByAci, color),
                    };
                    lt2.Add(ltr);
                    tr.AddNewlyCreatedDBObject(ltr, true);
                    LinetypeTable ltt = tr.GetObject(db.LinetypeTableId, OpenMode.ForRead) as LinetypeTable;
                    if (ltt != null && ltt.Has(linetype))
                        ltr.LinetypeObjectId = ltt[linetype];
                    tr.Commit();
                }
                return $"Layer '{name}' created.";
            }
            catch (SysException ex) { return $"create_layer failed: {ex.Message}"; }
        }

        // ── add_to_layer ──────────────────────────────────────────────────────────

        public static string AddToLayer(string json, Document doc)
        {
            try
            {
                var root      = JObject.Parse(ExtractJson(json));
                string layer  = root["layer"]?.ToString();
                var handles   = root["handles"] as JArray;

                if (string.IsNullOrWhiteSpace(layer)) return "add_to_layer failed: no layer specified.";
                if (handles == null || handles.Count == 0) return "add_to_layer failed: no handles provided.";

                Database db = doc.Database;
                int moved = 0, skipped = 0;
                using (Transaction tr = db.TransactionManager.StartTransaction())
                {
                    LayerTable lt = tr.GetObject(db.LayerTableId, OpenMode.ForWrite) as LayerTable;
                    if (!lt.Has(layer))
                    {
                        var ltr = new LayerTableRecord { Name = layer };
                        lt.Add(ltr);
                        tr.AddNewlyCreatedDBObject(ltr, true);
                    }

                    foreach (JToken h in handles)
                    {
                        try
                        {
                            long handleLong = (long)Convert.ToUInt64(h.ToString(), 16);
                            ObjectId objId;
                            if (!db.TryGetObjectId(new Handle(handleLong), out objId)) { skipped++; continue; }
                            AcadEntity ent = tr.GetObject(objId, OpenMode.ForWrite) as AcadEntity;
                            if (ent == null) { skipped++; continue; }
                            ent.Layer = layer;
                            moved++;
                        }
                        catch { skipped++; }
                    }
                    tr.Commit();
                }
                return $"Moved {moved} object(s) to layer '{layer}', {skipped} skipped.";
            }
            catch (SysException ex) { return $"add_to_layer failed: {ex.Message}"; }
        }

        // ── edit_layer ────────────────────────────────────────────────────────────

        public static string EditLayer(string json, Document doc)
        {
            try
            {
                var root    = JObject.Parse(ExtractJson(json));
                string name = root["name"]?.ToString();

                if (string.IsNullOrWhiteSpace(name)) return "edit_layer failed: no name provided.";

                Database db = doc.Database;
                using (Transaction tr = db.TransactionManager.StartTransaction())
                {
                    LayerTable lt = tr.GetObject(db.LayerTableId, OpenMode.ForRead) as LayerTable;
                    if (!lt.Has(name)) { tr.Abort(); return $"edit_layer failed: layer '{name}' not found."; }

                    LayerTableRecord ltr = tr.GetObject(lt[name], OpenMode.ForWrite) as LayerTableRecord;

                    if (root["color"] != null)
                        ltr.Color = Autodesk.AutoCAD.Colors.Color.FromColorIndex(
                                        Autodesk.AutoCAD.Colors.ColorMethod.ByAci,
                                        root["color"].Value<short>());

                    if (root["visible"] != null)
                        ltr.IsOff = !root["visible"].Value<bool>();

                    tr.Commit();
                }
                return $"Layer '{name}' updated.";
            }
            catch (SysException ex) { return $"edit_layer failed: {ex.Message}"; }
        }

        // ── remove_from_layer ─────────────────────────────────────────────────────

        public static string RemoveFromLayer(string json, Document doc)
        {
            try
            {
                var root    = JObject.Parse(ExtractJson(json));
                var handles = root["handles"] as JArray;

                if (handles == null || handles.Count == 0) return "remove_from_layer failed: no handles provided.";

                Database db = doc.Database;
                int moved = 0, skipped = 0;
                using (Transaction tr = db.TransactionManager.StartTransaction())
                {
                    foreach (JToken h in handles)
                    {
                        try
                        {
                            long handleLong = (long)Convert.ToUInt64(h.ToString(), 16);
                            ObjectId objId;
                            if (!db.TryGetObjectId(new Handle(handleLong), out objId)) { skipped++; continue; }
                            AcadEntity ent = tr.GetObject(objId, OpenMode.ForWrite) as AcadEntity;
                            if (ent == null) { skipped++; continue; }
                            ent.Layer = "0";
                            moved++;
                        }
                        catch { skipped++; }
                    }
                    tr.Commit();
                }
                return $"Moved {moved} object(s) to layer '0', {skipped} skipped.";
            }
            catch (SysException ex) { return $"remove_from_layer failed: {ex.Message}"; }
        }

        /// <summary>
        /// Builds Civil 3D objects from a JSON string directly into <paramref name="doc"/>.
        /// Called by the palette when the agent returns a build-instruction JSON.
        /// </summary>
        public static string Build(string json, Document doc)
        {
            JObject root;
            try   { root = JObject.Parse(ExtractJson(json)); }
            catch (SysException ex) { return $"Invalid JSON: {ex.Message}"; }
            return Build(root, doc);
        }

        /// <summary>Core build logic shared by the command and the pipeline.</summary>
        public static string Build(JObject root, Document doc)
        {
            Editor        ed       = doc.Editor;
            Database      db       = doc.Database;
            CivilDocument civilDoc = CivilDocument.GetCivilDocument(doc.Database);

            int created = 0, skipped = 0;

            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                BlockTable       bt = tr.GetObject(db.BlockTableId,                OpenMode.ForRead)  as BlockTable;
                BlockTableRecord ms = tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForWrite) as BlockTableRecord;

                BuildLines    (root, ms, tr, ref created, ref skipped);
                BuildPolylines(root, ms, tr, ref created, ref skipped);
                BuildArcs     (root, ms, tr, ref created, ref skipped);
                BuildAlignmentsAndProfiles(root, db, civilDoc, tr, ed, ref created, ref skipped);
                BuildFeatureLines(root, civilDoc, tr, ed, ref created, ref skipped);

                WarnSkipped(root, "pipe_networks", "Pipe network", "requires part-catalog references not stored in the JSON", ed, ref skipped);
                WarnSkipped(root, "surfaces",      "Surface",      "full point data requires LandXML import",                ed, ref skipped);
                WarnSkipped(root, "corridors",     "Corridor",     "requires existing alignment, profile, and assembly refs", ed, ref skipped);

                tr.Commit();
            }

            string summary = $"Build complete — {created} object(s) created, {skipped} skipped.";
            ed.WriteMessage($"\n  {summary}\n");
            return summary;
        }

        // ── Lines ─────────────────────────────────────────────────────────────────

        private static void BuildLines(
            JObject root, BlockTableRecord ms, Transaction tr,
            ref int created, ref int skipped)
        {
            foreach (JToken t in root["lines"] ?? new JArray())
            {
                try
                {
                    var sp  = ResolvePointArray(t["start_point"]);
                    var ep  = ResolvePointArray(t["end_point"]);
                    var line = new Line(Pt3(sp), Pt3(ep))
                    {
                        Layer = t["layer"]?.ToString() ?? "0"
                    };
                    ms.AppendEntity(line);
                    tr.AddNewlyCreatedDBObject(line, true);
                    created++;
                }
                catch { skipped++; }
            }
        }

        // ── Polylines ─────────────────────────────────────────────────────────────

        private static void BuildPolylines(
            JObject root, BlockTableRecord ms, Transaction tr,
            ref int created, ref int skipped)
        {
            foreach (JToken t in root["polylines"] ?? new JArray())
            {
                try
                {
                    var vertices = t["vertices"] as JArray;
                    if (vertices == null || vertices.Count < 2) { skipped++; continue; }

                    var pl = new Polyline();

                    for (int i = 0; i < vertices.Count; i++)
                        pl.AddVertexAt(i, Pt2(vertices[i] as JArray), 0, 0, 0);

                    pl.Closed = t["closed"]?.Value<bool>() ?? false;

                    ms.AppendEntity(pl);
                    tr.AddNewlyCreatedDBObject(pl, true);
                    pl.Layer = t["layer"]?.ToString() ?? "0";
                    created++;
                }
                catch { skipped++; }
            }
        }

        // ── Arcs ──────────────────────────────────────────────────────────────────

        private static void BuildArcs(
            JObject root, BlockTableRecord ms, Transaction tr,
            ref int created, ref int skipped)
        {
            foreach (JToken t in root["arcs"] ?? new JArray())
            {
                try
                {
                    var arc = new Arc(
                        Pt3(t["center"] as JArray),
                        Vector3d.ZAxis,
                        (double)t["radius"],
                        (double)t["start_angle"],
                        (double)t["end_angle"])
                    {
                        Layer = t["layer"]?.ToString() ?? "0"
                    };
                    ms.AppendEntity(arc);
                    tr.AddNewlyCreatedDBObject(arc, true);
                    created++;
                }
                catch { skipped++; }
            }
        }

        // ── Alignments (+ nested profiles) ───────────────────────────────────────

        private static void BuildAlignmentsAndProfiles(
            JObject root, Database db, CivilDocument civilDoc, Transaction tr,
            Editor ed, ref int created, ref int skipped)
        {
            ObjectId alignStyleId    = FirstId(civilDoc.Styles.AlignmentStyles);
            ObjectId alignLabelSetId = FirstId(civilDoc.Styles.LabelSetStyles.AlignmentLabelSetStyles);
            ObjectId profStyleId     = FirstId(civilDoc.Styles.ProfileStyles);
            ObjectId profLabelSetId  = FirstId(civilDoc.Styles.LabelSetStyles.ProfileLabelSetStyles);

            // Resolve the "0" layer ObjectId for alignment creation
            LayerTable lt      = tr.GetObject(db.LayerTableId, OpenMode.ForRead) as LayerTable;
            ObjectId layer0Id  = (lt != null && lt.Has("0")) ? lt["0"] : ObjectId.Null;

            foreach (JToken alignToken in root["alignments"] ?? new JArray())
            {
                string originalName = alignToken["name"]?.ToString() ?? "Alignment";
                string safeName     = UniqueAlignmentName(originalName, civilDoc, tr);

                ObjectId alignId;
                try
                {
                    alignId = Alignment.Create(civilDoc, safeName, ObjectId.Null, layer0Id,
                                               alignStyleId, alignLabelSetId);
                    created++;
                }
                catch (SysException ex)
                {
                    ed.WriteMessage($"\n  [Skip alignment '{originalName}'] {ex.Message}");
                    skipped++;
                    continue;
                }

                // Alignment entities (tangents + arcs)
                try
                {
                    Alignment align = tr.GetObject(alignId, OpenMode.ForWrite) as Alignment;
                    if (align == null) { skipped++; created--; continue; }
                    foreach (JToken entToken in alignToken["entities"] ?? new JArray())
                    {
                        try
                        {
                            string type = entToken["type"]?.ToString();
                            if (type == "Tangent")
                            {
                                // Use reflection — AddFixedLine signature varies across Civil 3D versions
                                var m = align.Entities.GetType()
                                    .GetMethod("AddFixedLine",
                                        new[] { typeof(Point2d), typeof(Point2d) });
                                if (m == null)
                                    ed.WriteMessage("\n  [Skip entity] AddFixedLine not found on this Civil 3D version.");
                                else
                                    m.Invoke(align.Entities, new object[]
                                    {
                                        Pt2(entToken["start"] as JArray),
                                        Pt2(entToken["end"]   as JArray)
                                    });
                            }
                            else if (type == "Arc")
                            {
                                bool cw = entToken["clockwise"]?.Value<bool>() ?? false;
                                // Use reflection — AddFixedCurve signature varies across Civil 3D versions
                                var m = align.Entities.GetType()
                                    .GetMethod("AddFixedCurve",
                                        new[] { typeof(Point2d), typeof(Point2d), typeof(Point2d), typeof(bool) });
                                if (m == null)
                                    ed.WriteMessage("\n  [Skip entity] AddFixedCurve not found on this Civil 3D version.");
                                else
                                    m.Invoke(align.Entities, new object[]
                                    {
                                        Pt2(entToken["center"] as JArray),
                                        Pt2(entToken["start"]  as JArray),
                                        Pt2(entToken["end"]    as JArray),
                                        cw
                                    });
                            }
                            // Spirals and compound types are not reconstructable from summary JSON
                        }
                        catch { /* skip individual entity */ }
                    }
                }
                catch { /* entity population failed — alignment still created */ }

                // Profiles nested under this alignment
                foreach (JToken profToken in alignToken["profiles"] ?? new JArray())
                {
                    try
                    {
                        string profName = profToken["name"]?.ToString() ?? "Profile";
                        ObjectId profileId = Profile.CreateByLayout(
                            profName, alignId, ObjectId.Null,
                            profStyleId, profLabelSetId);

                        Profile profile = tr.GetObject(profileId, OpenMode.ForWrite) as Profile;
                        if (profile == null) { skipped++; continue; }
                        foreach (JToken pviToken in profToken["pvis"] ?? new JArray())
                        {
                            try
                            {
                                profile.PVIs.AddPVI(
                                    (double)pviToken["station"],
                                    (double)pviToken["elevation"]);
                            }
                            catch { }
                        }
                        created++;
                    }
                    catch (SysException ex)
                    {
                        ed.WriteMessage($"\n  [Skip profile under '{safeName}'] {ex.Message}");
                        skipped++;
                    }
                }
            }
        }

        // ── Feature lines ─────────────────────────────────────────────────────────

        private static void BuildFeatureLines(
            JObject root, CivilDocument civilDoc, Transaction tr,
            Editor ed, ref int created, ref int skipped)
        {
            ObjectId flStyleId = FirstId(civilDoc.Styles.FeatureLineStyles);

            foreach (JToken flToken in root["feature_lines"] ?? new JArray())
            {
                string name = flToken["name"]?.ToString() ?? "FeatureLine";
                try
                {
                    // FeatureLine.Create and point-insertion API varies across Civil 3D versions —
                    // use reflection so this compiles and degrades gracefully on mismatches.
                    var createMethod = typeof(FeatureLine).GetMethod("Create",
                        new[] { typeof(Transaction), typeof(string), typeof(ObjectId), typeof(ObjectId) });
                    FeatureLine fl = createMethod?.Invoke(null,
                        new object[] { tr, name, ObjectId.Null, flStyleId }) as FeatureLine;

                    if (fl != null)
                    {
                        var insertMethod = fl.GetType().GetMethod("InsertPI",
                            new[] { typeof(Point3d) })
                            ?? fl.GetType().GetMethod("AddVertex",
                            new[] { typeof(Point3d) });

                        foreach (JToken ptToken in flToken["points"] ?? new JArray())
                        {
                            try
                            {
                                insertMethod?.Invoke(fl, new object[]
                                {
                                    new Point3d(
                                        (double)ptToken[0],
                                        (double)ptToken[1],
                                        (double)ptToken[2])
                                });
                            }
                            catch { }
                        }
                        created++;
                    }
                    else skipped++;
                }
                catch (SysException ex)
                {
                    ed.WriteMessage($"\n  [Skip feature line '{name}'] {ex.Message}");
                    skipped++;
                }
            }
        }

        // ── Helpers ───────────────────────────────────────────────────────────────

        /// <summary>Returns the first ObjectId from a Civil 3D style collection, or ObjectId.Null.</summary>
        private static ObjectId FirstId(System.Collections.IEnumerable collection)
        {
            foreach (object item in collection)
                if (item is ObjectId id) return id;
            return ObjectId.Null;
        }

        private static Point3d Pt3(JArray a) =>
            new Point3d((double)a[0], (double)a[1], (double)a[2]);

        private static Point2d Pt2(JArray a) =>
            new Point2d((double)a[0], (double)a[1]);

        /// <summary>
        /// Resolves a point token that may be a JArray [x,y,z] or a string "[x, y, z]".
        /// Z defaults to 0 if only two values are present.
        /// </summary>
        private static JArray ResolvePointArray(JToken token)
        {
            if (token is JArray arr) return arr;
            if (token?.Type == JTokenType.String)
            {
                string s = token.ToString().Trim();
                // Strip brackets and split on comma
                s = s.TrimStart('[').TrimEnd(']');
                var parts = s.Split(',');
                var result = new JArray();
                foreach (var p in parts)
                    result.Add(double.Parse(p.Trim(),
                        System.Globalization.CultureInfo.InvariantCulture));
                // Pad to 3 values with 0
                while (result.Count < 3) result.Add(0.0);
                return result;
            }
            return null;
        }

        /// <summary>
        /// Returns <paramref name="name"/> if no alignment with that name exists in the drawing;
        /// otherwise appends _1, _2, … until a unique name is found.
        /// </summary>
        private static string UniqueAlignmentName(
            string name, CivilDocument civilDoc, Transaction tr)
        {
            var taken = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (ObjectId id in civilDoc.GetAlignmentIds())
            {
                try
                {
                    if (tr.GetObject(id, OpenMode.ForRead) is Alignment a)
                        taken.Add(a.Name);
                }
                catch { }
            }

            if (!taken.Contains(name)) return name;

            int i = 1;
            while (taken.Contains($"{name}_{i}")) i++;
            return $"{name}_{i}";
        }

        /// <summary>Logs a skip notice for each item in an unsupported JSON array.</summary>
        private static void WarnSkipped(
            JObject root, string key, string label, string reason,
            Editor ed, ref int skipped)
        {
            foreach (JToken t in root[key] ?? new JArray())
            {
                string name = t["name"]?.ToString() ?? "(unnamed)";
                ed.WriteMessage($"\n  [Skip {label} '{name}'] {reason}.");
                skipped++;
            }
        }
    }
}
