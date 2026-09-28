using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Newtonsoft.Json.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;

using AcadEntity = Autodesk.AutoCAD.DatabaseServices.Entity;
using SysException = System.Exception;

namespace CADAgent
{
    public static partial class CadCommands
    {
        private static void RegisterOrganize()
        {
            Add("list_layers",            ListLayers);
            Add("layer_state",            LayerState);
            Add("isolate_layers",         IsolateLayers);
            Add("set_current_layer",      SetCurrentLayer);
            Add("purge_unused",           PurgeUnused);
            Add("rename_named_object",    RenameNamedObject);
            Add("get_drawing_settings",   GetDrawingSettings);
            Add("set_drawing_settings",   SetDrawingSettings);
            Add("define_block",           DefineBlock);
            Add("insert_block",           InsertBlock);
            Add("list_block_definitions", ListBlockDefinitions);
            Add("set_block_attributes",   SetBlockAttributes);
            Add("define_attribute",       DefineAttribute);
            Add("sync_block_attributes",  SyncBlockAttributes);
            Add("export_block",           ExportBlock, appThread: true);
            Add("attach_xref",            AttachXref, appThread: true);
            Add("attach_image",           AttachImage);
            Add("attach_pdf",             AttachPdf);
            Add("group_entities",         GroupEntities);
            Add("manage_layouts",         ManageLayouts, appThread: true);
            Add("create_viewport",        CreateViewport, appThread: true);   // LayoutManager
            Add("set_viewport",           SetViewport,    appThread: true);
            Add("zoom_view",              ZoomView, appThread: true);
            Add("save_drawing",           SaveDrawing, appThread: true);
        }

        // ── layers ────────────────────────────────────────────────────────────────

        private static string ListLayers(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var counts = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
            if (B(req["count_entities"], true))
                foreach (ObjectId id in ModelSpace(tr, db, false))
                {
                    try
                    {
                        if (tr.GetObject(id, OpenMode.ForRead) is AcadEntity e)
                            counts[e.Layer] = counts.TryGetValue(e.Layer, out int c) ? c + 1 : 1;
                    }
                    catch { }
                }

            var arr = new JArray();
            var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
            foreach (ObjectId id in lt)
            {
                var r = (LayerTableRecord)tr.GetObject(id, OpenMode.ForRead);
                string ltName = "";
                try { ltName = ((LinetypeTableRecord)tr.GetObject(r.LinetypeObjectId, OpenMode.ForRead)).Name; } catch { }
                arr.Add(new JObject
                {
                    ["name"] = r.Name, ["color"] = r.Color.ColorIndex, ["linetype"] = ltName,
                    ["lineweight"] = r.LineWeight.ToString(), ["on"] = !r.IsOff, ["frozen"] = r.IsFrozen,
                    ["locked"] = r.IsLocked, ["plottable"] = r.IsPlottable, ["description"] = r.Description,
                    ["current"] = id == db.Clayer, ["entities"] = counts.TryGetValue(r.Name, out int n) ? n : 0,
                });
            }
            return Ok($"{arr.Count} layer(s).", new JObject { ["layers"] = arr });
        });

        private static IEnumerable<LayerTableRecord> LayerRecords(Transaction tr, Database db, JToken which, JToken except, OpenMode mode)
        {
            var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
            var skip = new HashSet<string>((except as JArray)?.Select(t => t.ToString()) ?? Enumerable.Empty<string>(), StringComparer.OrdinalIgnoreCase);
            if (which == null || which.Type == JTokenType.Null || (which.Type == JTokenType.String && which.ToString() == "all"))
            {
                foreach (ObjectId id in lt)
                {
                    var r = (LayerTableRecord)tr.GetObject(id, mode);
                    if (!skip.Contains(r.Name)) yield return r;
                }
                yield break;
            }
            var names = which is JArray a ? a.Select(t => t.ToString()).ToList() : new List<string> { which.ToString() };
            foreach (string name in names)
            {
                if (!lt.Has(name)) throw new ArgumentException($"layer '{name}' does not exist");
                if (!skip.Contains(name)) yield return (LayerTableRecord)tr.GetObject(lt[name], mode);
            }
        }

        private static string LayerState(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string action = S(req["action"], "").ToLowerInvariant();
            int n = 0; var skipped = new JArray();
            foreach (var r in LayerRecords(tr, db, req["layers"], req["except"], OpenMode.ForWrite))
            {
                bool isCurrent = r.ObjectId == db.Clayer;
                switch (action)
                {
                    case "on":  r.IsOff = false; break;
                    case "off": r.IsOff = true; break;
                    case "freeze":
                        if (isCurrent) { skipped.Add(r.Name + " (current layer cannot be frozen)"); continue; }
                        r.IsFrozen = true; break;
                    case "thaw":   r.IsFrozen = false; break;
                    case "lock":   r.IsLocked = true; break;
                    case "unlock": r.IsLocked = false; break;
                    case "plot":   r.IsPlottable = true; break;
                    case "noplot": r.IsPlottable = false; break;
                    default: throw new ArgumentException("action must be on, off, freeze, thaw, lock, unlock, plot or noplot");
                }
                n++;
            }
            return Ok($"{action}: {n} layer(s).", new JObject { ["count"] = n, ["skipped"] = skipped });
        });

        private static string IsolateLayers(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var keep = new HashSet<string>(ResolveLayerNames(req["layers"]), StringComparer.OrdinalIgnoreCase);
            if (keep.Count == 0) throw new ArgumentException("layers is required");
            bool freeze = S(req["mode"], "off") == "freeze";
            int hidden = 0;
            var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
            foreach (string name in keep) if (!lt.Has(name)) throw new ArgumentException($"layer '{name}' does not exist");
            foreach (ObjectId id in lt)
            {
                var r = (LayerTableRecord)tr.GetObject(id, OpenMode.ForWrite);
                bool wanted = keep.Contains(r.Name);
                if (wanted) { r.IsOff = false; r.IsFrozen = false; continue; }
                if (freeze && id != db.Clayer) r.IsFrozen = true; else r.IsOff = true;
                hidden++;
            }
            return Ok($"Isolated {keep.Count} layer(s); {hidden} hidden. Use layer_state action 'on'/'thaw' with layers 'all' to restore.", new JObject { ["hidden"] = hidden });
        });

        private static List<string> ResolveLayerNames(JToken t) =>
            t == null ? new List<string>() : t is JArray a ? a.Select(x => x.ToString()).ToList() : new List<string> { t.ToString() };

        private static string SetCurrentLayer(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string name = S(req["name"]) ?? throw new ArgumentException("name is required");
            EnsureLayer(tr, db, name);
            var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
            var r = (LayerTableRecord)tr.GetObject(lt[name], OpenMode.ForWrite);
            if (r.IsFrozen) r.IsFrozen = false;
            db.Clayer = lt[name];
            return Ok($"Current layer is '{name}'.");
        });

        // ── purge / rename ────────────────────────────────────────────────────────

        private static string PurgeUnused(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var kinds = new HashSet<string>((req["kinds"] as JArray)?.Select(t => t.ToString().ToLowerInvariant()) ?? new[] { "all" });
            bool All(string k) => kinds.Contains("all") || kinds.Contains(k);
            var purged = new JObject();
            int total = 0;

            for (int pass = 0; pass < 6; pass++)
            {
                var candidates = new ObjectIdCollection();
                var names = new Dictionary<ObjectId, (string kind, string name)>();
                void Collect(SymbolTable table, string kind)
                {
                    foreach (ObjectId id in table)
                    {
                        var r = (SymbolTableRecord)tr.GetObject(id, OpenMode.ForRead);
                        if (r is BlockTableRecord b && (b.IsLayout || b.IsFromExternalReference || b.IsDependent)) continue;
                        if (r is LayerTableRecord && (r.Name == "0" || r.Name.Equals("Defpoints", StringComparison.OrdinalIgnoreCase))) continue;
                        if (r is LinetypeTableRecord && (r.Name.Equals("Continuous", StringComparison.OrdinalIgnoreCase) || r.Name.StartsWith("By", StringComparison.OrdinalIgnoreCase))) continue;
                        if (r is TextStyleTableRecord && r.Name.Equals("Standard", StringComparison.OrdinalIgnoreCase)) continue;
                        if (r is DimStyleTableRecord && r.Name.Equals("Standard", StringComparison.OrdinalIgnoreCase)) continue;
                        candidates.Add(id); names[id] = (kind, r.Name);
                    }
                }
                if (All("blocks"))      Collect((SymbolTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead), "blocks");
                if (All("layers"))      Collect((SymbolTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead), "layers");
                if (All("linetypes"))   Collect((SymbolTable)tr.GetObject(db.LinetypeTableId, OpenMode.ForRead), "linetypes");
                if (All("text_styles")) Collect((SymbolTable)tr.GetObject(db.TextStyleTableId, OpenMode.ForRead), "text_styles");
                if (All("dim_styles"))  Collect((SymbolTable)tr.GetObject(db.DimStyleTableId, OpenMode.ForRead), "dim_styles");
                if (candidates.Count == 0) break;

                db.Purge(candidates);   // leaves only the ids that are safe to remove
                if (candidates.Count == 0) break;
                foreach (ObjectId id in candidates)
                {
                    var (kind, name) = names[id];
                    try
                    {
                        tr.GetObject(id, OpenMode.ForWrite).Erase();
                        if (purged[kind] == null) purged[kind] = new JArray();
                        ((JArray)purged[kind]).Add(name);
                        total++;
                    }
                    catch { }
                }
            }
            return Ok($"Purged {total} unused named object(s).", new JObject { ["purged"] = purged });
        });

        private static string RenameNamedObject(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string kind = S(req["kind"], "").ToLowerInvariant();
            string oldName = S(req["old_name"]) ?? throw new ArgumentException("old_name is required");
            string newName = S(req["new_name"]) ?? throw new ArgumentException("new_name is required");
            SymbolTable table = kind switch
            {
                "layer" => (SymbolTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead),
                "block" => (SymbolTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead),
                "text_style" => (SymbolTable)tr.GetObject(db.TextStyleTableId, OpenMode.ForRead),
                "dim_style" => (SymbolTable)tr.GetObject(db.DimStyleTableId, OpenMode.ForRead),
                "linetype" => (SymbolTable)tr.GetObject(db.LinetypeTableId, OpenMode.ForRead),
                "group" => null,
                _ => throw new ArgumentException("kind must be layer, block, text_style, dim_style, linetype or group (layouts: manage_layouts rename)"),
            };
            if (table == null)
            {
                var gd = (DBDictionary)tr.GetObject(db.GroupDictionaryId, OpenMode.ForRead);
                if (!gd.Contains(oldName)) throw new ArgumentException($"group '{oldName}' does not exist");
                var g = (Group)tr.GetObject(gd.GetAt(oldName), OpenMode.ForWrite);
                g.Name = newName;
                return Ok($"Group renamed to '{newName}'.");
            }
            if (!table.Has(oldName)) throw new ArgumentException($"{kind} '{oldName}' does not exist");
            if (table.Has(newName)) throw new ArgumentException($"{kind} '{newName}' already exists");
            var rec = (SymbolTableRecord)tr.GetObject(table[oldName], OpenMode.ForWrite);
            rec.Name = newName;
            return Ok($"{kind} '{oldName}' renamed to '{newName}'.");
        });

        // ── drawing settings (UNITS etc.) ─────────────────────────────────────────

        private static string NameOf(Transaction tr, ObjectId id)
        {
            try { return (tr.GetObject(id, OpenMode.ForRead) as SymbolTableRecord)?.Name ?? ""; } catch { return ""; }
        }

        private static string GetDrawingSettings(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string[] lunits = { "", "scientific", "decimal", "engineering", "architectural", "fractional" };
            string[] aunits = { "decimal_degrees", "deg_min_sec", "grads", "radians", "surveyor" };
            var layouts = new JArray();
            try
            {
                var ld = (DBDictionary)tr.GetObject(db.LayoutDictionaryId, OpenMode.ForRead);
                foreach (DBDictionaryEntry e in ld) layouts.Add(e.Key);
            }
            catch { }
            return Ok("Settings read.", new JObject
            {
                ["file"] = db.Filename,
                ["insertion_units"] = db.Insunits.ToString(),
                ["linear_units"] = db.Lunits >= 1 && db.Lunits <= 5 ? lunits[db.Lunits] : db.Lunits.ToString(),
                ["linear_precision"] = db.Luprec,
                ["angular_units"] = db.Aunits >= 0 && db.Aunits <= 4 ? aunits[db.Aunits] : db.Aunits.ToString(),
                ["angular_precision"] = db.Auprec,
                ["angle_base_deg"] = R(Deg(db.Angbase)),
                ["angles_clockwise"] = db.Angdir,
                ["linetype_scale"] = db.Ltscale,
                ["current_layer"] = NameOf(tr, db.Clayer),
                ["current_text_style"] = NameOf(tr, db.Textstyle),
                ["current_dim_style"] = NameOf(tr, db.Dimstyle),
                ["current_linetype"] = NameOf(tr, db.Celtype),
                ["current_color"] = db.Cecolor.ToString(),
                ["current_lineweight"] = db.Celweight.ToString(),
                ["point_display_mode"] = db.Pdmode,
                ["point_display_size"] = db.Pdsize,
                ["limits"] = new JObject { ["min"] = Arr(db.Limmin), ["max"] = Arr(db.Limmax) },
                ["extents"] = new JObject { ["min"] = Arr(db.Extmin), ["max"] = Arr(db.Extmax) },
                ["layouts"] = layouts,
                ["available_intents"] = new JArray(Intents),
            });
        });

        private static string SetDrawingSettings(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var changed = new JArray();
            if (req["insertion_units"] != null)
            {
                string u = S(req["insertion_units"]);
                if (!Enum.TryParse(u, true, out UnitsValue uv)) throw new ArgumentException($"unknown unit '{u}' (Inches, Feet, Millimeters, Centimeters, Meters, Kilometers, USSurveyFeet, ...)");
                db.Insunits = uv; changed.Add("insertion_units");
            }
            if (req["linear_units"] != null)
            {
                var map = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase) { ["scientific"] = 1, ["decimal"] = 2, ["engineering"] = 3, ["architectural"] = 4, ["fractional"] = 5 };
                string s = S(req["linear_units"]);
                db.Lunits = map.TryGetValue(s, out int v) ? v : int.Parse(s); changed.Add("linear_units");
            }
            if (req["linear_precision"] != null) { db.Luprec = I(req["linear_precision"]); changed.Add("linear_precision"); }
            if (req["angular_units"] != null)
            {
                var map = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase) { ["decimal_degrees"] = 0, ["deg_min_sec"] = 1, ["grads"] = 2, ["radians"] = 3, ["surveyor"] = 4 };
                string s = S(req["angular_units"]);
                db.Aunits = map.TryGetValue(s, out int v) ? v : int.Parse(s); changed.Add("angular_units");
            }
            if (req["angular_precision"] != null) { db.Auprec = I(req["angular_precision"]); changed.Add("angular_precision"); }
            if (req["angle_base_deg"] != null) { db.Angbase = Rad(req["angle_base_deg"]); changed.Add("angle_base_deg"); }
            if (req["angles_clockwise"] != null) { db.Angdir = B(req["angles_clockwise"]); changed.Add("angles_clockwise"); }
            if (req["linetype_scale"] != null) { db.Ltscale = D(req["linetype_scale"]); changed.Add("linetype_scale"); }
            if (req["current_layer"] != null)
            {
                string name = S(req["current_layer"]); EnsureLayer(tr, db, name);
                var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
                db.Clayer = lt[name]; changed.Add("current_layer");
            }
            if (req["current_text_style"] != null) { db.Textstyle = TextStyleId(tr, db, S(req["current_text_style"])); changed.Add("current_text_style"); }
            if (req["current_dim_style"] != null) { db.Dimstyle = DimStyleId(tr, db, S(req["current_dim_style"])); changed.Add("current_dim_style"); }
            if (req["current_linetype"] != null) { db.Celtype = LinetypeId(tr, db, S(req["current_linetype"])); changed.Add("current_linetype"); }
            if (req["current_color"] != null) { db.Cecolor = ColorFrom(req["current_color"]); changed.Add("current_color"); }
            if (req["current_lineweight"] != null) { db.Celweight = LineWeightFrom(req["current_lineweight"]); changed.Add("current_lineweight"); }
            if (req["point_display_mode"] != null) { db.Pdmode = I(req["point_display_mode"]); changed.Add("point_display_mode"); }
            if (req["point_display_size"] != null) { db.Pdsize = D(req["point_display_size"]); changed.Add("point_display_size"); }
            if (req["limits"] != null)
            {
                var pts = Points(req["limits"]);
                if (pts.Count != 2) throw new ArgumentException("limits needs [[minx,miny],[maxx,maxy]]");
                db.Limmin = new Point2d(pts[0].X, pts[0].Y); db.Limmax = new Point2d(pts[1].X, pts[1].Y); changed.Add("limits");
            }
            return Ok($"Changed {changed.Count} setting(s).", new JObject { ["changed"] = changed });
        });

        // ── blocks ────────────────────────────────────────────────────────────────

        private static string DefineBlock(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string name = S(req["name"]) ?? throw new ArgumentException("name is required");
            var ids = ResolveAll(db, req["handles"]);
            Point3d basePt = Flat(P3(req["base_point"]));
            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForWrite);
            if (bt.Has(name)) throw new ArgumentException($"block '{name}' already exists (rename_named_object or pick another name)");

            var btr = new BlockTableRecord { Name = name, Origin = Point3d.Origin };
            if (req["description"] != null) btr.Comments = S(req["description"]);
            ObjectId btrId = bt.Add(btr);
            tr.AddNewlyCreatedDBObject(btr, true);

            var shift = Matrix3d.Displacement(Point3d.Origin - basePt);
            int attrs = 0;
            foreach (var id in ids)
            {
                var src = Open<AcadEntity>(tr, id);
                var clone = (AcadEntity)src.Clone();
                clone.TransformBy(shift);
                if (clone is AttributeDefinition) attrs++;
                btr.AppendEntity(clone);
                tr.AddNewlyCreatedDBObject(clone, true);
            }

            var extra = new JObject { ["block"] = name, ["entities"] = ids.Count, ["attributes"] = attrs };
            if (B(req["replace_originals"]))
            {
                var owner = OwnerSpace(tr, Open<AcadEntity>(tr, ids[0]));
                string layer = S(req["layer"]) ?? Open<AcadEntity>(tr, ids[0]).Layer;
                foreach (var id in ids) Open<AcadEntity>(tr, id, OpenMode.ForWrite).Erase();
                var br = new BlockReference(basePt, btrId);
                ObjectId brId = Append(tr, owner, br, layer);
                AddAttributeReferences(tr, br, btr, null);
                extra["removed"] = Handles(ids);
                return Created($"Block '{name}' defined and the originals replaced by one reference.", new[] { brId }, extra);
            }
            return Ok($"Block '{name}' defined from {ids.Count} object(s); originals untouched.", extra);
        });

        private static int AddAttributeReferences(Transaction tr, BlockReference br, BlockTableRecord btr, JObject values)
        {
            int n = 0;
            foreach (ObjectId id in btr)
            {
                if (!(tr.GetObject(id, OpenMode.ForRead) is AttributeDefinition ad) || ad.Constant) continue;
                var ar = new AttributeReference();
                ar.SetAttributeFromBlock(ad, br.BlockTransform);
                ar.TextString = ad.TextString;
                if (values != null)
                    foreach (var p in values.Properties())
                        if (p.Name.Equals(ad.Tag, StringComparison.OrdinalIgnoreCase)) ar.TextString = p.Value.ToString();
                br.AttributeCollection.AppendAttribute(ar);
                tr.AddNewlyCreatedDBObject(ar, true);
                n++;
            }
            return n;
        }

        /// <summary>The block definition called `name`: already in the drawing, else imported
        /// from `sourceFile` (the whole drawing as the block, or `sourceBlock` out of it).</summary>
        internal static ObjectId ResolveBlockDefinition(Transaction tr, Database db, string name, string sourceFile, string sourceBlock)
        {
            if (string.IsNullOrWhiteSpace(name)) throw new ArgumentException("block name is required");
            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            if (bt.Has(name)) return bt[name];
            // a block library drawing may carry it under its own name too
            if (!string.IsNullOrWhiteSpace(sourceBlock) && bt.Has(sourceBlock)) return bt[sourceBlock];
            string source = sourceFile ?? throw new ArgumentException($"block '{name}' is not defined here; pass source_file (a .dwg) to import it");
            if (!File.Exists(source)) throw new FileNotFoundException($"source_file not found: {source}");
            ObjectId btrId;
            using (var src = new Database(false, true))
            {
                src.ReadDwgFile(source, FileOpenMode.OpenForReadAndAllShare, true, null);
                src.CloseInput(true);
                // a library drawing that defines the block: take that definition; else the
                // whole drawing becomes the block
                string pick = sourceBlock;
                if (pick == null)
                {
                    using (var str = src.TransactionManager.StartTransaction())
                    {
                        var srcBt = (BlockTable)str.GetObject(src.BlockTableId, OpenMode.ForRead);
                        if (srcBt.Has(name)) pick = name;
                        str.Commit();
                    }
                }
                if (pick == null)
                    btrId = db.Insert(name, src, true);
                else
                {
                    using (var str = src.TransactionManager.StartTransaction())
                    {
                        var srcBt = (BlockTable)str.GetObject(src.BlockTableId, OpenMode.ForRead);
                        if (!srcBt.Has(pick)) throw new ArgumentException($"'{source}' has no block named '{pick}'");
                        var ids = new ObjectIdCollection { srcBt[pick] };
                        var map = new IdMapping();
                        src.WblockCloneObjects(ids, db.BlockTableId, map, DuplicateRecordCloning.Ignore, false);
                        str.Commit();
                    }
                    bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                    if (!bt.Has(pick)) throw new ArgumentException($"importing '{pick}' failed");
                    btrId = bt[pick];
                    if (!pick.Equals(name, StringComparison.OrdinalIgnoreCase))
                        ((BlockTableRecord)tr.GetObject(btrId, OpenMode.ForWrite)).Name = name;
                }
            }
            return btrId;
        }

        /// <summary>The X extent of a block definition's geometry relative to its base point
        /// (minX..maxX); false when it has none. A dimension-line block's arrow tips are at
        /// these extents, not at the base point, so a stretch must be measured from them.</summary>
        internal static bool BlockDefinitionSpanX(Transaction tr, ObjectId btrId, out double minX, out double maxX)
        {
            minX = maxX = 0;
            try
            {
                var btr = (BlockTableRecord)tr.GetObject(btrId, OpenMode.ForRead);
                bool any = false; var ext = new Extents3d();
                foreach (ObjectId id in btr)
                {
                    try
                    {
                        var e = tr.GetObject(id, OpenMode.ForRead) as AcadEntity;
                        if (e == null) continue;
                        var ge = e.GeometricExtents;
                        if (!any) { ext = ge; any = true; } else ext.AddExtents(ge);
                    }
                    catch { }
                }
                if (!any) return false;
                minX = ext.MinPoint.X; maxX = ext.MaxPoint.X;
                return maxX - minX > 1e-6;
            }
            catch { return false; }
        }

        internal static double BlockDefinitionWidth(Transaction tr, ObjectId btrId) =>
            BlockDefinitionSpanX(tr, btrId, out double a, out double b) ? b - a : 0.0;

        private static string InsertBlock(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string name = S(req["name"]) ?? throw new ArgumentException("name is required");
            ObjectId btrId = ResolveBlockDefinition(tr, db, name, S(req["source_file"]), S(req["source_block"]));

            var btr = (BlockTableRecord)tr.GetObject(btrId, OpenMode.ForRead);
            var br = new BlockReference(Flat(P3(req["position"])), btrId);
            if (req["scale"] is JArray sc && sc.Count >= 2) br.ScaleFactors = new Scale3d(D(sc[0]), D(sc[1]), sc.Count > 2 ? D(sc[2]) : 1);
            else br.ScaleFactors = new Scale3d(D(req["scale"], 1));
            br.Rotation = Rad(req["rotation_deg"]);
            ObjectId id = Append(tr, TargetSpace(tr, db, req), br, S(req["layer"], "0"));
            int attrs = AddAttributeReferences(tr, br, btr, req["attributes"] as JObject);
            return Created($"Block '{name}' inserted.", new[] { id }, new JObject { ["attributes_set"] = attrs, ["bounds"] = BoundsJson(br) });
        });

        private static string ListBlockDefinitions(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var arr = new JArray();
            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            bool includeAnonymous = B(req["include_anonymous"]);
            foreach (ObjectId id in bt)
            {
                var b = (BlockTableRecord)tr.GetObject(id, OpenMode.ForRead);
                if (b.IsLayout || (b.IsAnonymous && !includeAnonymous)) continue;
                var tags = new JArray(); int count = 0;
                foreach (ObjectId eid in b)
                {
                    count++;
                    if (tr.GetObject(eid, OpenMode.ForRead) is AttributeDefinition ad) tags.Add(ad.Tag);
                }
                int refs = 0;
                try { refs = b.GetBlockReferenceIds(true, false).Count; } catch { }
                arr.Add(new JObject
                {
                    ["name"] = b.Name, ["entities"] = count, ["references"] = refs, ["attribute_tags"] = tags,
                    ["is_xref"] = b.IsFromExternalReference, ["path"] = b.IsFromExternalReference ? b.PathName : null,
                    ["description"] = b.Comments, ["origin"] = Arr(b.Origin),
                });
            }
            return Ok($"{arr.Count} block definition(s).", new JObject { ["blocks"] = arr });
        });

        private static string SetBlockAttributes(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var id = Resolve(db, S(req["handle"]));
            var br = Open<BlockReference>(tr, id, OpenMode.ForWrite);
            var values = req["attributes"] as JObject ?? throw new ArgumentException("attributes {TAG: value} is required");
            var set = new JArray(); var present = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (ObjectId aid in br.AttributeCollection)
            {
                var ar = (AttributeReference)tr.GetObject(aid, OpenMode.ForWrite);
                present.Add(ar.Tag);
                foreach (var p in values.Properties())
                    if (p.Name.Equals(ar.Tag, StringComparison.OrdinalIgnoreCase)) { ar.TextString = p.Value.ToString(); set.Add(ar.Tag); }
            }
            var missing = new JArray(values.Properties().Where(p => !present.Contains(p.Name)).Select(p => p.Name));
            return Ok($"Set {set.Count} attribute(s).", new JObject { ["modified"] = new JArray(id.Handle.ToString()), ["set"] = set, ["missing_tags"] = missing });
        });

        private static string DefineAttribute(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string block = S(req["block_name"]) ?? throw new ArgumentException("block_name is required");
            string tag = S(req["tag"]) ?? throw new ArgumentException("tag is required");
            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            if (!bt.Has(block)) throw new ArgumentException($"block '{block}' does not exist");
            var btr = (BlockTableRecord)tr.GetObject(bt[block], OpenMode.ForWrite);
            var ad = new AttributeDefinition();
            ad.SetDatabaseDefaults(db);
            ad.Tag = tag.Replace(" ", "_");
            ad.Prompt = S(req["prompt"], tag);
            ad.TextString = S(req["default"], "");
            ad.Position = Flat(P3(req["position"]));
            ad.Height = D(req["height"], db.Textsize > 0 ? db.Textsize : 2.5);
            ad.Rotation = Rad(req["rotation_deg"]);
            ad.Invisible = B(req["invisible"]);
            ad.Constant = B(req["constant"]);
            ad.TextStyleId = TextStyleId(tr, db, S(req["text_style"]));
            ad.Layer = EnsureLayer(tr, db, S(req["layer"], "0"));
            var j = Attachment(S(req["justification"]), AttachmentPoint.BaseLeft);
            if (j != AttachmentPoint.BaseLeft) { ad.Justify = j; ad.AlignmentPoint = ad.Position; }
            btr.AppendEntity(ad);
            tr.AddNewlyCreatedDBObject(ad, true);
            int synced = B(req["sync"], true) ? SyncAttributes(tr, btr) : 0;
            return Ok($"Attribute '{ad.Tag}' added to block '{block}'.", new JObject { ["references_updated"] = synced });
        });

        private static int SyncAttributes(Transaction tr, BlockTableRecord btr)
        {
            var defs = new List<AttributeDefinition>();
            foreach (ObjectId id in btr)
                if (tr.GetObject(id, OpenMode.ForRead) is AttributeDefinition ad && !ad.Constant) defs.Add(ad);
            int n = 0;
            foreach (ObjectId rid in btr.GetBlockReferenceIds(true, false))
            {
                var br = (BlockReference)tr.GetObject(rid, OpenMode.ForWrite);
                var existing = new Dictionary<string, AttributeReference>(StringComparer.OrdinalIgnoreCase);
                foreach (ObjectId aid in br.AttributeCollection)
                {
                    var ar = (AttributeReference)tr.GetObject(aid, OpenMode.ForWrite);
                    existing[ar.Tag] = ar;
                }
                foreach (var ad in defs)
                {
                    if (existing.TryGetValue(ad.Tag, out var keep)) continue;
                    var ar = new AttributeReference();
                    ar.SetAttributeFromBlock(ad, br.BlockTransform);
                    ar.TextString = ad.TextString;
                    br.AttributeCollection.AppendAttribute(ar);
                    tr.AddNewlyCreatedDBObject(ar, true);
                }
                foreach (var kv in existing)
                    if (!defs.Any(d => d.Tag.Equals(kv.Key, StringComparison.OrdinalIgnoreCase))) kv.Value.Erase();
                n++;
            }
            return n;
        }

        private static string SyncBlockAttributes(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string block = S(req["block_name"]) ?? throw new ArgumentException("block_name is required");
            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            if (!bt.Has(block)) throw new ArgumentException($"block '{block}' does not exist");
            int n = SyncAttributes(tr, (BlockTableRecord)tr.GetObject(bt[block], OpenMode.ForRead));
            return Ok($"Synchronised attributes on {n} reference(s) of '{block}'.", new JObject { ["references_updated"] = n });
        });

        private static string ExportBlock(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string path = S(req["path"]) ?? throw new ArgumentException("path (.dwg) is required");
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path)) ?? ".");
            Database outDb = null;
            try
            {
                if (req["name"] != null)
                {
                    var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                    string name = S(req["name"]);
                    if (!bt.Has(name)) throw new ArgumentException($"block '{name}' does not exist");
                    outDb = db.Wblock(bt[name]);
                }
                else
                {
                    var ids = ResolveAll(db, req["handles"]);
                    Point3d basePt = req["base_point"] != null ? Flat(P3(req["base_point"])) : Point3d.Origin;
                    outDb = new Database(true, false);
                    db.Wblock(outDb, new ObjectIdCollection(ids.ToArray()), basePt, DuplicateRecordCloning.Ignore);
                }
                outDb.SaveAs(path, DwgVersion.Current);
            }
            finally { outDb?.Dispose(); }
            return Ok($"Written {path}.", new JObject { ["path"] = Path.GetFullPath(path) });
        });

        // ── external references / images / pdf ────────────────────────────────────

        private static string AttachXref(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string path = S(req["path"]) ?? throw new ArgumentException("path is required");
            if (!File.Exists(path)) throw new FileNotFoundException($"drawing not found: {path}");
            string name = S(req["name"]) ?? Path.GetFileNameWithoutExtension(path);
            ObjectId xid = B(req["overlay"]) ? db.OverlayXref(path, name) : db.AttachXref(path, name);
            if (xid.IsNull) throw new ArgumentException("AutoCAD refused the reference (circular or unreadable)");
            var br = new BlockReference(Flat(P3(req["position"])), xid)
            {
                ScaleFactors = new Scale3d(D(req["scale"], 1)),
                Rotation = Rad(req["rotation_deg"]),
            };
            // "layout": the xref goes on that sheet (the border / title block xref)
            ObjectId id = Append(tr, TargetSpace(tr, db, req), br, S(req["layer"], "0"));
            return Created($"Xref '{name}' attached.", new[] { id }, new JObject { ["name"] = name });
        });

        private static string AttachImage(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string path = S(req["path"]) ?? throw new ArgumentException("path is required");
            if (!File.Exists(path)) throw new FileNotFoundException($"image not found: {path}");
            string name = S(req["name"]) ?? Path.GetFileNameWithoutExtension(path);
            ObjectId dictId = RasterImageDef.GetImageDictionary(db);
            if (dictId.IsNull) dictId = RasterImageDef.CreateImageDictionary(db);
            var dict = (DBDictionary)tr.GetObject(dictId, OpenMode.ForWrite);

            RasterImageDef def; ObjectId defId;
            if (dict.Contains(name)) { defId = dict.GetAt(name); def = (RasterImageDef)tr.GetObject(defId, OpenMode.ForWrite); }
            else
            {
                def = new RasterImageDef { SourceFileName = path };
                def.Load();
                defId = dict.SetAt(name, def);
                tr.AddNewlyCreatedDBObject(def, true);
            }

            double width = D(req["width"], 0);
            double aspect = def.Size.X > 0 ? def.Size.Y / def.Size.X : 1;
            if (width <= 0) width = def.Size.X > 0 ? def.Size.X / 100.0 : 10;
            double height = D(req["height"], width * aspect);
            double a = Rad(req["rotation_deg"]);
            var u = new Vector3d(Math.Cos(a), Math.Sin(a), 0) * width;
            var v = new Vector3d(-Math.Sin(a), Math.Cos(a), 0) * height;
            var img = new RasterImage { ImageDefId = defId, Orientation = new CoordinateSystem3d(Flat(P3(req["position"])), u, v) };
            ObjectId id = Append(tr, ModelSpace(tr, db), img, S(req["layer"], "0"));
            img.AssociateRasterDef(def);
            return Created($"Image '{name}' attached.", new[] { id }, new JObject { ["width"] = R(width), ["height"] = R(height), ["pixels"] = new JArray(def.Size.X, def.Size.Y) });
        });

        private static string AttachPdf(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string path = S(req["path"]) ?? throw new ArgumentException("path is required");
            if (!File.Exists(path)) throw new FileNotFoundException($"pdf not found: {path}");
            string page = S(req["page"], "1");
            string name = S(req["name"]) ?? $"{Path.GetFileNameWithoutExtension(path)} - {page}";
            string key = UnderlayDefinition.GetDictionaryKey(typeof(PdfDefinition));
            var nod = (DBDictionary)tr.GetObject(db.NamedObjectsDictionaryId, OpenMode.ForWrite);
            DBDictionary dict;
            if (nod.Contains(key)) dict = (DBDictionary)tr.GetObject(nod.GetAt(key), OpenMode.ForWrite);
            else { dict = new DBDictionary(); nod.SetAt(key, dict); tr.AddNewlyCreatedDBObject(dict, true); }

            ObjectId defId;
            if (dict.Contains(name)) defId = dict.GetAt(name);
            else
            {
                var def = new PdfDefinition { SourceFileName = path, ItemName = page };
                defId = dict.SetAt(name, def);
                tr.AddNewlyCreatedDBObject(def, true);
            }
            var pr = new PdfReference
            {
                DefinitionId = defId,
                Position = Flat(P3(req["position"])),
                ScaleFactors = new Scale3d(D(req["scale"], 1)),
                Rotation = Rad(req["rotation_deg"]),
            };
            ObjectId id = Append(tr, ModelSpace(tr, db), pr, S(req["layer"], "0"));
            return Created($"PDF page {page} attached.", new[] { id }, new JObject { ["name"] = name });
        });

        // ── groups ────────────────────────────────────────────────────────────────

        private static string GroupEntities(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string action = S(req["action"], "create").ToLowerInvariant();
            var gd = (DBDictionary)tr.GetObject(db.GroupDictionaryId, OpenMode.ForWrite);
            if (action == "list")
            {
                var arr = new JArray();
                foreach (DBDictionaryEntry e in gd)
                {
                    var g = (Group)tr.GetObject(e.Value, OpenMode.ForRead);
                    arr.Add(new JObject { ["name"] = g.Name, ["description"] = g.Description, ["members"] = Handles(g.GetAllEntityIds()), ["selectable"] = g.Selectable });
                }
                return Ok($"{arr.Count} group(s).", new JObject { ["groups"] = arr });
            }
            string name = S(req["name"]) ?? throw new ArgumentException("name is required");
            switch (action)
            {
                case "create":
                {
                    if (gd.Contains(name)) throw new ArgumentException($"group '{name}' already exists");
                    var ids = ResolveAll(db, req["handles"]);
                    var g = new Group(S(req["description"], ""), true);
                    gd.SetAt(name, g);
                    tr.AddNewlyCreatedDBObject(g, true);
                    g.Append(new ObjectIdCollection(ids.ToArray()));
                    return Ok($"Group '{name}' created with {ids.Count} member(s).", new JObject { ["members"] = Handles(ids) });
                }
                case "add":
                case "remove":
                {
                    if (!gd.Contains(name)) throw new ArgumentException($"group '{name}' does not exist");
                    var g = (Group)tr.GetObject(gd.GetAt(name), OpenMode.ForWrite);
                    var ids = ResolveAll(db, req["handles"]);
                    if (action == "add") g.Append(new ObjectIdCollection(ids.ToArray())); else g.Remove(new ObjectIdCollection(ids.ToArray()));
                    return Ok($"Group '{name}': {action} {ids.Count} member(s).", new JObject { ["members"] = Handles(g.GetAllEntityIds()) });
                }
                case "dissolve":
                {
                    if (!gd.Contains(name)) throw new ArgumentException($"group '{name}' does not exist");
                    var g = (Group)tr.GetObject(gd.GetAt(name), OpenMode.ForWrite);
                    var members = Handles(g.GetAllEntityIds());
                    g.Erase();   // the container only; the entities stay
                    return Ok($"Group '{name}' dissolved; its objects remain.", new JObject { ["members"] = members });
                }
                default:
                    throw new ArgumentException("action must be create, add, remove, dissolve or list");
            }
        });

        // ── layouts / viewports / view / save (application thread) ────────────────

        /// <summary>The layouts of a template file (the choices the "From template" dialog
        /// offers): name, paper size in inches and mm, plotter, media, entities on the sheet.</summary>
        private static string ListTemplateLayouts(string path)
        {
            if (!File.Exists(path)) throw new FileNotFoundException($"template not found: {path}");
            var arr = new JArray();
            using (var src = new Database(false, true))
            {
                src.ReadDwgFile(path, FileOpenMode.OpenForReadAndAllShare, true, null);
                src.CloseInput(true);
                using (Transaction str = src.TransactionManager.StartTransaction())
                {
                    var ld = (DBDictionary)str.GetObject(src.LayoutDictionaryId, OpenMode.ForRead);
                    foreach (DBDictionaryEntry e in ld)
                    {
                        Layout lay;
                        try { lay = (Layout)str.GetObject(e.Value, OpenMode.ForRead); } catch { continue; }
                        if (lay.ModelType) continue;
                        int ents = 0, vps = 0;
                        try
                        {
                            var btr = (BlockTableRecord)str.GetObject(lay.BlockTableRecordId, OpenMode.ForRead);
                            foreach (ObjectId id in btr)
                            {
                                try { if (str.GetObject(id, OpenMode.ForRead) is Viewport) vps++; else ents++; } catch { }
                            }
                        }
                        catch { }
                        bool landscape = lay.PlotRotation == PlotRotation.Degrees090 || lay.PlotRotation == PlotRotation.Degrees270;
                        double w = lay.PlotPaperSize.X, h = lay.PlotPaperSize.Y;
                        if (landscape) { double t = w; w = h; h = t; }
                        arr.Add(new JObject
                        {
                            ["name"] = lay.LayoutName, ["tab_order"] = lay.TabOrder,
                            ["paper_size_in"] = new JArray(Math.Round(w / 25.4, 2), Math.Round(h / 25.4, 2)),
                            ["paper_size_mm"] = new JArray(R(w), R(h)),
                            ["plotter"] = SafeStr(() => lay.PlotConfigurationName), ["media"] = SafeStr(() => lay.CanonicalMediaName),
                            ["rotation"] = lay.PlotRotation.ToString(),
                            ["entities"] = ents, ["viewports"] = vps,
                        });
                    }
                    str.Commit();
                }
            }
            return Ok($"{arr.Count} layout(s) in {Path.GetFileName(path)}.", new JObject { ["file"] = path, ["layouts"] = arr });
        }

        private static string ManageLayouts(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string action = S(req["action"], "list").ToLowerInvariant();
            var lm = LayoutManager.Current;
            switch (action)
            {
                case "list":
                {
                    if (!string.IsNullOrWhiteSpace(S(req["source_file"]))) return ListTemplateLayouts(S(req["source_file"]));
                    var arr = new JArray();
                    var ld = (DBDictionary)tr.GetObject(db.LayoutDictionaryId, OpenMode.ForRead);
                    foreach (DBDictionaryEntry e in ld)
                    {
                        var lay = (Layout)tr.GetObject(e.Value, OpenMode.ForRead);
                        int vps = 0;
                        try { foreach (ObjectId _ in lay.GetViewports()) vps++; } catch { }
                        arr.Add(new JObject
                        {
                            ["name"] = lay.LayoutName, ["tab_order"] = lay.TabOrder, ["model"] = lay.ModelType,
                            ["current"] = lay.LayoutName.Equals(lm.CurrentLayout, StringComparison.OrdinalIgnoreCase),
                            ["paper_size"] = new JArray(R(lay.PlotPaperSize.X), R(lay.PlotPaperSize.Y)), ["viewports"] = vps,
                        });
                    }
                    return Ok($"{arr.Count} layout(s).", new JObject { ["layouts"] = arr, ["current"] = lm.CurrentLayout });
                }
                case "create":
                {
                    string name = S(req["name"]) ?? throw new ArgumentException("name is required");
                    if (lm.LayoutExists(name)) throw new ArgumentException($"layout '{name}' already exists");
                    ObjectId id = lm.CreateLayout(name);
                    if (B(req["set_current"])) lm.CurrentLayout = name;
                    return Ok($"Layout '{name}' created.", new JObject { ["name"] = name });
                }
                case "set_current":
                {
                    string name = S(req["name"]) ?? throw new ArgumentException("name is required");
                    if (!lm.LayoutExists(name)) throw new ArgumentException($"layout '{name}' does not exist");
                    lm.CurrentLayout = name;
                    return Ok($"Current layout is '{name}'.");
                }
                case "rename":
                {
                    string old = S(req["name"]) ?? throw new ArgumentException("name is required");
                    string nw = S(req["new_name"]) ?? throw new ArgumentException("new_name is required");
                    if (!lm.LayoutExists(old)) throw new ArgumentException($"layout '{old}' does not exist");
                    lm.RenameLayout(old, nw);
                    return Ok($"Layout '{old}' renamed to '{nw}'.");
                }
                case "clone_from_template":
                {
                    // A sheet from the template: the page setup and everything drawn on
                    // the template layout (title block, borders, notes) come across; the
                    // template's viewports do not, the caller cuts its own.
                    string name = S(req["name"]) ?? throw new ArgumentException("name is required");
                    string source = S(req["source_file"]) ?? throw new ArgumentException("source_file (the .dwg/.dwt holding the template layout) is required");
                    string sourceLayout = S(req["source_layout"]) ?? throw new ArgumentException("source_layout (the layout's name inside source_file) is required");
                    if (lm.LayoutExists(name)) throw new ArgumentException($"layout '{name}' already exists");
                    if (!File.Exists(source)) throw new FileNotFoundException($"source_file not found: {source}");

                    ObjectId newId = lm.CreateLayout(name);
                    var layout = (Layout)tr.GetObject(newId, OpenMode.ForWrite);
                    var created = new List<ObjectId>();
                    var blocks = new JArray();
                    string titleBlock = null;
                    bool titleHasAttrs = false;
                    using (var src = new Database(false, true))
                    {
                        src.ReadDwgFile(source, FileOpenMode.OpenForReadAndAllShare, true, null);
                        src.CloseInput(true);
                        using (var str = src.TransactionManager.StartTransaction())
                        {
                            var srcDict = (DBDictionary)str.GetObject(src.LayoutDictionaryId, OpenMode.ForRead);
                            if (!srcDict.Contains(sourceLayout)) throw new ArgumentException($"'{source}' has no layout named '{sourceLayout}'");
                            var srcLayout = (Layout)str.GetObject(srcDict.GetAt(sourceLayout), OpenMode.ForRead);
                            layout.CopyFrom(srcLayout);          // page setup: paper, device, style sheet, origin, rotation
                            if (!layout.LayoutName.Equals(name, StringComparison.OrdinalIgnoreCase)) layout.LayoutName = name;

                            var srcBtr = (BlockTableRecord)str.GetObject(srcLayout.BlockTableRecordId, OpenMode.ForRead);
                            var ids = new ObjectIdCollection();
                            foreach (ObjectId oid in srcBtr)
                                if (!(str.GetObject(oid, OpenMode.ForRead) is Viewport)) ids.Add(oid);
                            if (ids.Count > 0)
                            {
                                var map = new IdMapping();
                                src.WblockCloneObjects(ids, layout.BlockTableRecordId, map, DuplicateRecordCloning.Ignore, false);
                                foreach (IdPair pair in map)
                                {
                                    if (!pair.IsPrimary || !pair.IsCloned || pair.Value.IsNull) continue;
                                    created.Add(pair.Value);
                                    if (tr.GetObject(pair.Value, OpenMode.ForRead) is BlockReference br)
                                    {
                                        int attrs = 0;
                                        try { attrs = br.AttributeCollection.Count; } catch { }
                                        blocks.Add(new JObject { ["handle"] = br.Handle.ToString(), ["name"] = br.Name, ["attributes"] = attrs });
                                        // the title block: the first block with attributes, else the first block
                                        if (titleBlock == null || (attrs > 0 && !titleHasAttrs)) { titleBlock = br.Handle.ToString(); titleHasAttrs = attrs > 0; }
                                    }
                                }
                            }
                            str.Commit();
                        }
                    }
                    if (titleBlock != null)
                    {
                        // "@0" in a sequence is the first handle this step created: make it the title block
                        int at = created.FindIndex(i => i.Handle.ToString() == titleBlock);
                        if (at > 0) { var tb = created[at]; created.RemoveAt(at); created.Insert(0, tb); }
                    }
                    if (B(req["set_current"])) lm.CurrentLayout = name;
                    return Created($"Layout '{name}' cloned from '{sourceLayout}' in {Path.GetFileName(source)} ({created.Count} entities).",
                                   created, new JObject { ["name"] = name, ["title_block"] = titleBlock, ["blocks"] = blocks,
                                                          ["paper_size"] = new JArray(R(layout.PlotPaperSize.X), R(layout.PlotPaperSize.Y)) });
                }
                case "delete":
                {
                    string name = S(req["name"]) ?? throw new ArgumentException("name is required");
                    if (!lm.LayoutExists(name)) throw new ArgumentException($"layout '{name}' does not exist");
                    if (name.Equals("Model", StringComparison.OrdinalIgnoreCase)) throw new ArgumentException("Model cannot be deleted");
                    if (name.Equals(lm.CurrentLayout, StringComparison.OrdinalIgnoreCase)) throw new ArgumentException($"'{name}' is the current layout; set another current first");
                    lm.DeleteLayout(name);
                    return Ok($"Layout '{name}' deleted.", new JObject { ["deleted"] = name });
                }
                default:
                    throw new ArgumentException("action must be list, create, set_current, rename, clone_from_template or delete");
            }
        });

        private static string CreateViewport(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string layoutName = S(req["layout"]) ?? throw new ArgumentException("layout is required (a paper space layout name)");
            var ld = (DBDictionary)tr.GetObject(db.LayoutDictionaryId, OpenMode.ForRead);
            if (!ld.Contains(layoutName)) throw new ArgumentException($"layout '{layoutName}' does not exist");
            var layout = (Layout)tr.GetObject(ld.GetAt(layoutName), OpenMode.ForRead);
            if (layout.ModelType) throw new ArgumentException("viewports go in a paper space layout, not Model");
            // The layout must be current AND initialised before a Viewport entity is added to
            // it: a freshly created / cloned layout has no paper-space view yet, and when
            // AutoCAD initialises it (on becoming current) it adopts the first Viewport it
            // finds as that view -- seen live 2026-09-16: the sheet viewport became the
            // layout's own view at scale 1, locked, with the frozen layers. LayoutManager:
            // application thread. Switching the viewport on needs the current layout too
            // (eNotInPaperspace otherwise).
            var lm = LayoutManager.Current;
            if (!string.Equals(lm.CurrentLayout, layoutName, StringComparison.OrdinalIgnoreCase))
                lm.CurrentLayout = layoutName;
            layout = (Layout)tr.GetObject(ld.GetAt(layoutName), OpenMode.ForRead);
            int paperViews = 0;
            try { paperViews = layout.GetViewports().Count; } catch { }
            if (paperViews == 0)
            {
                try { layout.UpgradeOpen(); layout.Initialize(); } catch (SysException ex) { throw new InvalidOperationException($"layout '{layoutName}' could not be initialised: {ex.Message}"); }
            }
            var space = (BlockTableRecord)tr.GetObject(layout.BlockTableRecordId, OpenMode.ForWrite);

            Point3d center; double width, height;
            string fit = S(req["fit"], "").Trim().ToLowerInvariant();
            if (fit == "layout" && LayoutContentExtents(tr, space, out Extents3d ext))
            {
                // one viewport the size of the sheet: whatever the template layout carries
                // (border xref, title block) is the sheet; the viewport covers exactly that
                center = new Point3d((ext.MinPoint.X + ext.MaxPoint.X) / 2, (ext.MinPoint.Y + ext.MaxPoint.Y) / 2, 0);
                width = ext.MaxPoint.X - ext.MinPoint.X; height = ext.MaxPoint.Y - ext.MinPoint.Y;
            }
            else if (fit == "layout" || fit == "paper")
            {
                // an empty layout: the plot paper, mm in the API, in paper units here
                double f = layout.PlotPaperUnits == PlotPaperUnit.Inches ? 1.0 / 25.4 : layout.PlotPaperUnits == PlotPaperUnit.Millimeters ? 1.0 : 1.0 / 25.4;
                width = layout.PlotPaperSize.X * f; height = layout.PlotPaperSize.Y * f;
                if (layout.PlotRotation == PlotRotation.Degrees090 || layout.PlotRotation == PlotRotation.Degrees270) { double t = width; width = height; height = t; }
                center = new Point3d(width / 2, height / 2, 0);
                if (req["center"] != null) center = Flat(P3(req["center"]));
            }
            else
            {
                center = Flat(P3(req["center"]));
                width = D(req["width"]); height = D(req["height"]);
            }
            if (width <= 0 || height <= 0) throw new ArgumentException("width and height must be > 0 (paper units)");
            var vp = new Viewport { CenterPoint = center, Width = width, Height = height };
            ObjectId id = Append(tr, space, vp, S(req["layer"], "0"));
            vp.On = true;
            int number = -1;
            try { number = vp.Number; } catch { }
            if (number == 1)
                throw new InvalidOperationException($"layout '{layoutName}' took the new viewport as its own paper-space view; nothing was kept");
            if (req["scale"] != null) vp.CustomScale = D(req["scale"]);           // paper : model, e.g. 0.025 = 1" = 40'
            else if (req["view_height"] != null) vp.ViewHeight = D(req["view_height"]);
            if (req["twist_deg"] != null) vp.TwistAngle = Rad(req["twist_deg"]);
            if (req["view_center"] != null) SetViewCenter(vp, Flat(P3(req["view_center"])));
            var frozen = LayersInViewport(tr, db, vp, req["freeze_layers"] as JArray, true);
            string anno = null;
            string want = S(req["annotation_scale"], "auto");
            if (req["scale"] != null && want != "none" && want != "false")
                anno = ApplyAnnotationScale(db, vp, want);
            vp.Locked = B(req["locked"], true);
            var extra = ViewportWindow(vp);
            extra["number"] = vp.Number;
            extra["paper_center"] = Arr(new Point2d(center.X, center.Y));
            extra["paper_size"] = new JArray(R(width), R(height));
            extra["fit"] = fit == "" ? null : fit;
            extra["frozen"] = frozen.Item1;
            extra["missing_layers"] = frozen.Item2;
            extra["annotation_scale"] = anno;
            return Created($"Viewport created in '{layoutName}'.", new[] { id }, extra);
        });

        /// <summary>The extents of what a layout carries besides viewports (border, title block).</summary>
        private static bool LayoutContentExtents(Transaction tr, BlockTableRecord space, out Extents3d ext)
        {
            ext = new Extents3d();
            bool any = false;
            foreach (ObjectId id in space)
            {
                try
                {
                    var e = tr.GetObject(id, OpenMode.ForRead) as AcadEntity;
                    if (e == null || e is Viewport) continue;
                    var ge = e.GeometricExtents;
                    if (!any) { ext = ge; any = true; } else ext.AddExtents(ge);
                }
                catch { }
            }
            return any;
        }

        private static string SetViewport(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var id = Resolve(db, S(req["handle"]));
            var vp = Open<Viewport>(tr, id, OpenMode.ForWrite);
            bool locked = vp.Locked;
            vp.Locked = false;
            if (req["on"] != null) vp.On = B(req["on"]);
            if (req["scale"] != null) vp.CustomScale = D(req["scale"]);
            if (req["twist_deg"] != null) vp.TwistAngle = Rad(req["twist_deg"]);
            if (req["view_center"] != null) SetViewCenter(vp, Flat(P3(req["view_center"])));
            var frozen = LayersInViewport(tr, db, vp, req["freeze_layers"] as JArray, true);
            var thawed = LayersInViewport(tr, db, vp, req["thaw_layers"] as JArray, false);
            string anno = null;
            if (req["annotation_scale"] != null) anno = ApplyAnnotationScale(db, vp, S(req["annotation_scale"]));
            vp.Locked = req["locked"] != null ? B(req["locked"]) : locked;
            var extra = ViewportWindow(vp);
            extra["modified"] = new JArray(id.Handle.ToString());
            extra["frozen"] = frozen.Item1; extra["thawed"] = thawed.Item1;
            extra["missing_layers"] = new JArray(frozen.Item2.Concat(thawed.Item2));
            if (anno != null) extra["annotation_scale"] = anno;
            return Ok("Viewport updated.", extra);
        });

        // ── viewport geometry ─────────────────────────────────────────────────────
        //
        // ViewCenter lives in the viewport's own display coordinates (DCS): with a
        // twist, or a view target away from the origin, it is not a model-space
        // point. These two matrices are the bridge, the same one the snapshot
        // reader uses, so what the planner asks for is what the reader reports.

        public static Matrix3d Dcs2Wcs(Viewport vp) =>
            Matrix3d.Rotation(-vp.TwistAngle, vp.ViewDirection, vp.ViewTarget)
            * Matrix3d.Displacement(vp.ViewTarget - Point3d.Origin)
            * Matrix3d.PlaneToWorld(vp.ViewDirection);

        private static void SetViewCenter(Viewport vp, Point3d wcs)
        {
            var dcs = wcs.TransformBy(Dcs2Wcs(vp).Inverse());
            vp.ViewCenter = new Point2d(dcs.X, dcs.Y);
        }

        /// <summary>The model-space window a viewport shows: its centre, the four
        /// corners (twist applied, so a rotated sheet reports a rotated ring) and the
        /// axis-aligned box around them.</summary>
        public static JObject ViewportWindow(Viewport vp)
        {
            double aspect = vp.Height > 1e-9 ? vp.Width / vp.Height : 1.0;
            double halfH = vp.ViewHeight / 2.0, halfW = halfH * aspect;
            var vc = vp.ViewCenter;
            var m = Dcs2Wcs(vp);
            var corners = new[] { (-halfW, -halfH), (halfW, -halfH), (halfW, halfH), (-halfW, halfH) }
                .Select(o => new Point3d(vc.X + o.Item1, vc.Y + o.Item2, 0).TransformBy(m)).ToArray();
            var c = new Point3d(vc.X, vc.Y, 0).TransformBy(m);
            double R4(double v) => Math.Round(v, 4);
            return new JObject
            {
                ["scale"] = vp.CustomScale,
                ["twist_deg"] = R4(Deg(vp.TwistAngle)),
                // the raw inputs, so a wrong transform can be diagnosed from the snapshot alone
                ["view_center_dcs"] = new JArray(R4(vc.X), R4(vc.Y)),
                ["view_target"] = new JArray(R4(vp.ViewTarget.X), R4(vp.ViewTarget.Y), R4(vp.ViewTarget.Z)),
                ["view_direction"] = new JArray(R4(vp.ViewDirection.X), R4(vp.ViewDirection.Y), R4(vp.ViewDirection.Z)),
                ["view_height"] = R4(vp.ViewHeight),
                ["model_center"] = new JArray(R4(c.X), R4(c.Y)),
                ["model_window"] = new JArray(
                    new JArray(R4(corners.Min(q => q.X)), R4(corners.Min(q => q.Y))),
                    new JArray(R4(corners.Max(q => q.X)), R4(corners.Max(q => q.Y)))),
                ["model_ring"] = new JArray(corners.Select(q => new JArray(R4(q.X), R4(q.Y)))),
            };
        }

        /// <summary>Freezes (or thaws) the named layers in one viewport. Names that are
        /// not layers of this drawing are reported back, never an error: an xref
        /// layer list is allowed to be longer than what is attached.</summary>
        private static Tuple<int, JArray> LayersInViewport(Transaction tr, Database db, Viewport vp, JArray names, bool freeze)
        {
            var missing = new JArray();
            if (names == null || names.Count == 0) return Tuple.Create(0, missing);
            var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
            var ids = new ObjectIdCollection();
            foreach (var n in names)
            {
                string name = n?.ToString();
                if (string.IsNullOrWhiteSpace(name)) continue;
                if (lt.Has(name)) ids.Add(lt[name]); else missing.Add(name);
            }
            if (ids.Count > 0)
            {
                if (freeze) vp.FreezeLayersInViewport(ids.GetEnumerator());
                else vp.ThawLayersInViewport(ids.GetEnumerator());
            }
            return Tuple.Create(ids.Count, missing);
        }

        /// <summary>Gives the viewport the annotation scale that matches its custom scale
        /// (or the one named), so annotative labels and text show on the sheet. Creates
        /// the scale (1" = 40') when the drawing has none for that ratio.</summary>
        private static string ApplyAnnotationScale(Database db, Viewport vp, string want)
        {
            try
            {
                var occ = db.ObjectContextManager?.GetContextCollection("ACDB_ANNOTATIONSCALES");
                if (occ == null) return null;
                double target = vp.CustomScale;
                bool byName = !string.IsNullOrWhiteSpace(want) && !want.Equals("auto", StringComparison.OrdinalIgnoreCase);
                AnnotationScale found = null;
                foreach (ObjectContext ctx in occ)
                {
                    if (!(ctx is AnnotationScale a)) continue;
                    if (byName) { if (a.Name.Equals(want, StringComparison.OrdinalIgnoreCase)) { found = a; break; } }
                    else if (a.DrawingUnits > 0 && Math.Abs(a.PaperUnits / a.DrawingUnits - target) <= Math.Abs(target) * 1e-6) { found = a; break; }
                }
                if (found == null)
                {
                    if (byName) throw new ArgumentException($"annotation scale '{want}' does not exist in this drawing");
                    if (target <= 0) return null;
                    double units = 1.0 / target;
                    string name = Math.Abs(units - Math.Round(units)) < 1e-6 ? $"1\" = {Math.Round(units)}'" : $"1:{units:0.###}";
                    found = new AnnotationScale { Name = name, PaperUnits = 1.0, DrawingUnits = units };
                    occ.AddContext(found);
                }
                vp.AnnotationScale = found;
                return found.Name;
            }
            catch (ArgumentException) { throw; }
            catch (SysException ex) { return $"(not set: {ex.Message})"; }
        }

        private static string ZoomView(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            Editor ed = doc.Editor;
            string mode = S(req["mode"], "extents").ToLowerInvariant();
            Point3d min, max;
            switch (mode)
            {
                case "extents":
                    db.UpdateExt(true);
                    min = db.Extmin; max = db.Extmax;
                    break;
                case "window":
                {
                    var pts = Points(req["points"]);
                    if (pts.Count != 2) throw new ArgumentException("window needs points [[x1,y1],[x2,y2]]");
                    min = new Point3d(Math.Min(pts[0].X, pts[1].X), Math.Min(pts[0].Y, pts[1].Y), 0);
                    max = new Point3d(Math.Max(pts[0].X, pts[1].X), Math.Max(pts[0].Y, pts[1].Y), 0);
                    break;
                }
                case "entities":
                {
                    var ids = ResolveAll(db, req["handles"]);
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
                    if (ext == null) throw new ArgumentException("none of the objects has extents");
                    min = ext.Value.MinPoint; max = ext.Value.MaxPoint;
                    break;
                }
                case "center":
                {
                    var c = Flat(P3(req["point"]));
                    double h = D(req["height"], ed.GetCurrentView().Height);
                    var view0 = ed.GetCurrentView();
                    double w0 = h * view0.Width / view0.Height;
                    min = new Point3d(c.X - w0 / 2, c.Y - h / 2, 0); max = new Point3d(c.X + w0 / 2, c.Y + h / 2, 0);
                    break;
                }
                case "scale":
                {
                    double f = D(req["factor"], 1);
                    if (f <= 0) throw new ArgumentException("factor must be > 0");
                    var view0 = ed.GetCurrentView();
                    double w = view0.Width / f, h = view0.Height / f;
                    min = new Point3d(view0.CenterPoint.X - w / 2, view0.CenterPoint.Y - h / 2, 0);
                    max = new Point3d(view0.CenterPoint.X + w / 2, view0.CenterPoint.Y + h / 2, 0);
                    break;
                }
                default:
                    throw new ArgumentException("mode must be extents, window, entities, center or scale");
            }
            double margin = 1 + D(req["margin"], 0.05);
            using (ViewTableRecord view = ed.GetCurrentView())
            {
                double w = Math.Max(max.X - min.X, 1e-6), h = Math.Max(max.Y - min.Y, 1e-6);
                double aspect = view.Width / view.Height;
                if (w / h > aspect) h = w / aspect; else w = h * aspect;
                view.CenterPoint = new Point2d((min.X + max.X) / 2, (min.Y + max.Y) / 2);
                view.Width = w * margin; view.Height = h * margin;
                ed.SetCurrentView(view);
            }
            if (B(req["regen"], true)) ed.Regen();
            return Ok($"View set ({mode}).", new JObject { ["min"] = Arr(min), ["max"] = Arr(max) });
        });

        private static string SaveDrawing(JObject req, Document doc)
        {
            Database db = doc.Database;
            string path = S(req["path"]);
            if (path == null)
            {
                if (string.IsNullOrEmpty(db.Filename) || db.Filename.EndsWith(".dwt", StringComparison.OrdinalIgnoreCase))
                    throw new ArgumentException("the drawing has never been saved; give a path");
                db.SaveAs(db.Filename, true, DwgVersion.Current, db.SecurityParameters);
                return Ok($"Saved {db.Filename}.", new JObject { ["path"] = db.Filename });
            }
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path)) ?? ".");
            if (path.EndsWith(".dxf", StringComparison.OrdinalIgnoreCase))
            {
                db.DxfOut(path, 16, DwgVersion.Current);
                return Ok($"Exported DXF {path}.", new JObject { ["path"] = Path.GetFullPath(path) });
            }
            db.SaveAs(path, DwgVersion.Current);
            return Ok($"Saved as {path}.", new JObject { ["path"] = Path.GetFullPath(path) });
        }
    }
}
