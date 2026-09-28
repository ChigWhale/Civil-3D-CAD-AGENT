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

using SysException = System.Exception;
using AcadEntity   = Autodesk.AutoCAD.DatabaseServices.Entity;
using CivilSurface = Autodesk.Civil.DatabaseServices.Surface;
using CivilLabel   = Autodesk.Civil.DatabaseServices.Label;

namespace CADAgent
{
    /// <summary>
    /// Sanitary sewer profile views. Three intents:
    ///
    ///   profile_context     read-only: per alignment its station range, surface
    ///                       elevations sampled along it, the pipes and structures
    ///                       that sit on it (station, inverts, rims), existing
    ///                       profiles and profile views; the style catalogues and
    ///                       defaults; the drawing extent. Python plans from this.
    ///   draw_profile_views  the plan, drawn in ONE transaction: restation to 1+00,
    ///                       an EG surface profile (reused when there is one), an
    ///                       optional flowline profile from the pipe inverts, a
    ///                       profile view per window with user-specified station
    ///                       and elevation ranges, the network parts added to it,
    ///                       pipe / structure / station labels, and the pipe and
    ///                       station labels dragged to just above the datum.
    ///                       apply=false validates and reports without writing.
    ///   label_profile_views relabel existing views by name (same label rules).
    ///
    /// Styles are preferences, not preconditions: an exact name, else a unique
    /// substring, else the drawing default with a warning (GradingLabels.PickStyle).
    /// </summary>
    public static partial class CadCommands
    {
        private static void RegisterProfiles()
        {
            // Civil 3D document work (alignments, profiles, label styles): application thread.
            Add("profile_context",     ProfileContext,    appThread: true);
            Add("draw_profile_views",  DrawProfileViews,  appThread: true);
            Add("label_profile_views", LabelProfileViews, appThread: true);
            Add("arrange_profile_views", ArrangeProfileViews, appThread: true);
            Add("drag_profile_labels",   DragProfileLabels,   appThread: true);
            Add("block_dynamic_properties", BlockDynamicProperties, appThread: true);
        }

        // ── Dragging the labels of existing views (a second transaction) ─────────
        //
        // Labels created in the same transaction as their view have no evaluated
        // geometry yet, so a drag applied there has nothing to measure against. This
        // intent runs afterwards, on labels Civil 3D has laid out: pipe labels onto their
        // dimension line, "MH" labels to the bottom row, "MH -> Alignment" to the top,
        // station labels above the dimension-line rows.

        private static string DragProfileLabels(JObject req, Document doc)
        {
            Database db = doc.Database;
            var madeIds = new List<ObjectId>();
            int madeTotal = 0;
            var madeWarnings = new List<string>();
            using (doc.LockDocument())
            {
                // FIRST transaction: the labels a view lacks are created and committed, so the
                // placement below works on labels Civil 3D has laid out (a label has no
                // evaluated location inside the transaction that creates it)
                using (Transaction tr0 = db.TransactionManager.StartTransaction())
                {
                    try
                    {
                        var civil0 = CivilDocument.GetCivilDocument(db);
                        var plan0 = new ProfilePlan { Apply = true, StationHost = S(req["station_host"], "eg"), StationIncrement = D(req["station_increment"], 100.0), Deferred = true };
                        plan0.Catalogue = ReadProfileCatalogue(tr0, civil0);
                        plan0.Styles = PickProfileStyles(plan0.Catalogue, req["styles"] as JObject, plan0.Warnings);
                        ReadDimLines(plan0, req["dim_lines"] as JObject);
                        ReadAnchors(plan0, req["dimension_anchor"] as JObject);
                        var wanted0 = (req["views"] as JArray)?.Select(t => t.ToString().Trim()).Where(x => x != "").ToList() ?? new List<string>();
                        foreach (ObjectId aid in civil0.GetAlignmentIds())
                        {
                            var align = tr0.GetObject(aid, OpenMode.ForRead) as Alignment;
                            if (align == null) continue;
                            AlignmentWork w = null;
                            foreach (ObjectId vid in align.GetProfileViewIds())
                            {
                                var pv = tr0.GetObject(vid, OpenMode.ForRead) as ProfileView;
                                if (pv == null) continue;
                                if (wanted0.Count > 0 && !wanted0.Any(n => n.Equals(pv.Name, StringComparison.OrdinalIgnoreCase))) continue;
                                var v = new ViewPlan
                                {
                                    Name = pv.Name, AlignmentName = align.Name, AlignmentId = aid,
                                    StationStart = pv.StationStart, StationEnd = pv.StationEnd,
                                    ElevationMin = pv.ElevationMin, ElevationMax = pv.ElevationMax, Insertion = pv.Location,
                                };
                                // the view's one dimension line, inserted unstretched; the second
                                // transaction stretches it over the view
                                if (B((req["do"] as JObject)?["dim_lines"], true)) InsertDimLineIn(tr0, db, pv, v, plan0, madeIds);
                                if (HasOurLabels(pv)) continue;
                                var partMap = PartsShownIn(tr0, db, pv, civil0);
                                v.Pipes = partMap.Keys.Where(k => { try { return tr0.GetObject(k, OpenMode.ForRead) is Pipe; } catch { return false; } }).ToList();
                                v.Structures = partMap.Keys.Where(k => { try { return tr0.GetObject(k, OpenMode.ForRead) is Structure; } catch { return false; } }).ToList();
                                pv.UpgradeOpen();
                                w = w ?? WorkFor(tr0, align);
                                var made = LabelView(tr0, db, pv, v, w, partMap, plan0, madeIds, null, 0);
                                madeTotal += I(made["total"]);
                            }
                        }
                        madeWarnings.AddRange(plan0.Warnings);
                        tr0.Commit();
                    }
                    catch (SysException ex)
                    {
                        tr0.Abort();
                        return Fail($"drag_profile_labels failed while creating the labels, nothing was kept: {ex.Message}");
                    }
                }
            }
            using (doc.LockDocument())
            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                try
                {
                    var civil = CivilDocument.GetCivilDocument(db);
                    var plan = new ProfilePlan { Apply = true, StationHost = S(req["station_host"], "eg"), StationIncrement = D(req["station_increment"], 100.0) };
                    var drag = req["drag_to_datum"] as JObject ?? new JObject();
                    plan.DragPipe = B(drag["pipe"], true); plan.DragStation = B(drag["station"], true); plan.Lift = D(drag["lift"], 5.0);
                    plan.Catalogue = ReadProfileCatalogue(tr, civil);
                    plan.Styles = PickProfileStyles(plan.Catalogue, req["styles"] as JObject, plan.Warnings);
                    ReadDimLines(plan, req["dim_lines"] as JObject);
                    ReadAnchors(plan, req["dimension_anchor"] as JObject);
                    var doo = req["do"] as JObject ?? new JObject();
                    // anchors: the per-label DimensionAnchorOption/Value pair (live 2026-09-17: 62/62, no
                    // crash); the LabelBase.DefaultDimensionAnchor* pair overflowed Civil 3D's stack
                    // the MH labels are created with their styles and left alone (the user's call,
                    // 2026-09-17); anchors only when asked for
                    plan.DoDimLines = B(doo["dim_lines"], true); plan.DoAnchors = B(doo["anchors"], false);
                    plan.ResetStructureAnchors = B(doo["reset_structure_anchors"], false);
                    plan.DoPipes = B(doo["pipes"], true); plan.DoStations = B(doo["stations"], true);
                    var wanted = (req["views"] as JArray)?.Select(t => t.ToString().Trim()).Where(x => x != "").ToList() ?? new List<string>();
                    var viewsOut = new JArray();
                    var created = new List<ObjectId>();
                    int total = 0, dragged = 0, labelsMade = 0;
                    foreach (ObjectId aid in civil.GetAlignmentIds())
                    {
                        var align = tr.GetObject(aid, OpenMode.ForRead) as Alignment;
                        if (align == null) continue;
                        foreach (ObjectId vid in align.GetProfileViewIds())
                        {
                            var pv = tr.GetObject(vid, OpenMode.ForRead) as ProfileView;
                            if (pv == null) continue;
                            if (wanted.Count > 0 && !wanted.Any(n => n.Equals(pv.Name, StringComparison.OrdinalIgnoreCase))) continue;
                            var v = new ViewPlan
                            {
                                Name = pv.Name, AlignmentName = align.Name, AlignmentId = aid,
                                StationStart = pv.StationStart, StationEnd = pv.StationEnd,
                                ElevationMin = pv.ElevationMin, ElevationMax = pv.ElevationMax, Insertion = pv.Location,
                            };
                            // the view's parts, and its labels if it has none yet (the draw
                            // leaves labelling to this pass, so they are made where the view stands)
                            var partMap = PartsShownIn(tr, db, pv, civil);
                            v.Pipes = partMap.Keys.Where(k => { try { return tr.GetObject(k, OpenMode.ForRead) is Pipe; } catch { return false; } }).ToList();
                            v.Structures = partMap.Keys.Where(k => { try { return tr.GetObject(k, OpenMode.ForRead) is Structure; } catch { return false; } }).ToList();
                            var r = DragLabelsOfView(tr, db, pv, v, plan);
                            total += I(r["total"]); dragged += I(r["dragged"]);
                            var vj = ProfileViewJson(pv); vj["labels"] = r; viewsOut.Add(vj);
                        }
                    }
                    if (viewsOut.Count == 0) { tr.Abort(); return Fail(wanted.Count > 0 ? $"no profile view named {string.Join(", ", wanted)}" : "the drawing has no profile views"); }
                    tr.Commit();
                    labelsMade = madeTotal;
                    created.AddRange(madeIds);
                    plan.Warnings.InsertRange(0, madeWarnings);
                    var msg = (labelsMade > 0 ? $"{labelsMade} label(s) created; " : "") + $"{dragged} of {total} label(s) placed on {viewsOut.Count} profile view(s).";
                    var dragWarnings = plan.Warnings.Where(w => w.Contains("could not be dragged")).ToList();
                    if (dragWarnings.Count > 0) msg += " " + string.Join(" | ", dragWarnings.Take(3));
                    var extra = new JObject { ["views"] = viewsOut, ["dragged"] = dragged, ["total"] = total, ["labels_created"] = labelsMade, ["warnings"] = new JArray(plan.Warnings) };
                    if (created.Count > 0) return Created(msg, created, extra);
                    return Ok(msg, extra);
                }
                catch (SysException ex)
                {
                    tr.Abort();
                    return Fail($"drag_profile_labels failed, nothing was kept: {ex.Message}");
                }
            }
        }

        /// <summary>The dimension-line block references inside a view, bottom row first.</summary>
        private static List<DimLine> DimLinesIn(Transaction tr, Database db, ProfileView pv, string blockName)
        {
            var found = new List<DimLine>();
            if (string.IsNullOrWhiteSpace(blockName)) return found;
            Extents3d ext;
            try { ext = pv.GeometricExtents; } catch { return found; }
            foreach (ObjectId id in ModelSpace(tr, db, false))
            {
                try
                {
                    var br = tr.GetObject(id, OpenMode.ForRead) as BlockReference;
                    if (br == null) continue;
                    string name = EffectiveBlockName(tr, br);
                    if (!name.Equals(blockName, StringComparison.OrdinalIgnoreCase)) continue;
                    var e = br.GeometricExtents;
                    double cy = (e.MinPoint.Y + e.MaxPoint.Y) / 2;
                    if (e.MinPoint.X < ext.MinPoint.X - 1 || e.MaxPoint.X > ext.MaxPoint.X + 1 || cy < ext.MinPoint.Y || cy > ext.MaxPoint.Y) continue;
                    found.Add(new DimLine { X0 = e.MinPoint.X, X1 = e.MaxPoint.X, Y = e.MaxPoint.Y });
                }
                catch { }
            }
            found.Sort((a, b) => a.Y != b.Y ? a.Y.CompareTo(b.Y) : a.X0.CompareTo(b.X0));
            for (int i = 0; i < found.Count; i++) found[i].Row = i;
            return found;
        }

        private static string EffectiveBlockName(Transaction tr, BlockReference br)
        {
            try
            {
                ObjectId defId = br.IsDynamicBlock ? br.DynamicBlockTableRecord : br.BlockTableRecord;
                return ((BlockTableRecord)tr.GetObject(defId, OpenMode.ForRead)).Name;
            }
            catch { return ""; }
        }

        private static JObject DragLabelsOfView(Transaction tr, Database db, ProfileView pv, ViewPlan v, ProfilePlan plan)
        {
            var styles = plan.Styles;
            var rows = new Rows();
            double gap = Math.Max(0.5, plan.Lift * 0.5);
            var notDragged = new List<string>();
            void Note(string what) { if (_dragWhy != null && notDragged.Count < 3) notDragged.Add($"{what}: {_dragWhy}"); }
            int pipeN = 0, structN = 0, structAlignN = 0, stationN = 0, dragged = 0;

            // the dimension lines drawn in this view: stretched now (the draw left them unstretched), then measured
            if (plan.DoDimLines)
            {
                try
                {
                    var shown = PartsShownIn(tr, db, pv, null);
                    v.Pipes = shown.Keys.Where(k => { try { return tr.GetObject(k, OpenMode.ForRead) is Pipe; } catch { return false; } }).ToList();
                    AdjustDimLinesIn(tr, db, pv, v, plan);
                }
                catch (SysException ex) { plan.Warnings.Add($"{v.Name}: dimension lines not stretched ({ex.Message})"); }
            }
            var dims = DimLinesIn(tr, db, pv, plan.DimBlock);
            double datumY = 0, dx0 = 0;
            pv.FindXYAtStationAndElevation(v.StationStart, v.ElevationMin, ref dx0, ref datumY);
            // the bottom of the view, from the spec alone (a stale block found high in the view
            // once pushed every label to the top): the dimension line `DimLift` above the datum,
            // a row of `DimRow` per pipe label above it, then the station / MH row
            var pipeRuns = PipeRuns(tr, pv, v);
            int pipeRows = pipeRuns.Count == 0 ? 1 : pipeRuns.Values.Max(r => r.row) + 1;
            bool hasLine = plan.DoDimLines && !string.IsNullOrWhiteSpace(plan.DimBlock);
            double lineY = datumY + (hasLine ? plan.DimLift : 0.0);
            double rowLift = (hasLine ? plan.DimLift : 0.0) + pipeRows * plan.DimRow + plan.Lift;

            // station label groups first: the lowest row above the dimension lines
            if (plan.DoStations) try
            {
                var profileIds = new List<ObjectId>();
                try
                {
                    var owner = tr.GetObject(v.AlignmentId, OpenMode.ForRead) as Alignment;
                    if (owner != null) foreach (ObjectId pid in owner.GetProfileIds()) profileIds.Add(pid);
                }
                catch { }
                foreach (ObjectId pid in profileIds)
                {
                    ObjectIdCollection gids;
                    try { gids = ProfileStationLabelGroup.GetAvailableLabelGroupIds(pv.ObjectId, pid, false); } catch { continue; }
                    foreach (ObjectId gid in gids)
                    {
                        var group = tr.GetObject(gid, OpenMode.ForWrite) as ProfileStationLabelGroup;
                        if (group == null) continue;
                        foreach (LabelGroupSubEntity se in group.SubEntities)
                        {
                            stationN++;
                            bool seDragged = false;
                            try { seDragged = se.Dragged; } catch { }
                            if (seDragged) { dragged++; continue; }          // placed on an earlier pass
                            if (plan.AnchorPipeStation && AnchorSub(se, false, plan.AnchorStation)) dragged++;
                            else if (plan.DragStation && DragSubEntityToDatum(se, pv, v, rowLift)) dragged++;
                            else Note("station label");
                        }
                        try { rows.Bottom.Add(group.GeometricExtents); } catch { }
                    }
                }
            }
            catch (SysException ex) { plan.Warnings.Add($"{v.Name}: station labels not dragged ({ex.Message})"); }

            // structure labels anchored by an earlier pass: back to the drawing's default placement
            if (!plan.DoAnchors && plan.ResetStructureAnchors) try
            {
                int reset = 0;
                foreach (ObjectId lid in StructureProfileLabel.GetAvailableLabelIds(pv.ObjectId))
                {
                    var lbl = tr.GetObject(lid, OpenMode.ForRead) as StructureProfileLabel;
                    if (lbl == null) continue;
                    bool overridden = false;
                    try { overridden = lbl.DimensionAnchorOption != Autodesk.Civil.DimensionAnchorOptionType.Default; } catch { }
                    if (!overridden) continue;
                    try { lbl.UpgradeOpen(); lbl.DimensionAnchorOption = Autodesk.Civil.DimensionAnchorOptionType.Default; reset++; }
                    catch (SysException ex) { plan.Warnings.Add($"{v.Name}: a structure label's anchor was not reset ({ex.Message})"); break; }
                }
                if (reset > 0) plan.Warnings.Add($"{v.Name}: {reset} structure label(s) put back to the default placement");
            }
            catch (SysException ex) { plan.Warnings.Add($"{v.Name}: structure label anchors not reset ({ex.Message})"); }

            // structure labels: the "MH -> Alignment" style to the top, every other to the bottom
            if (plan.DoAnchors) try
            {
                foreach (ObjectId lid in StructureProfileLabel.GetAvailableLabelIds(pv.ObjectId))
                {
                    var lbl = tr.GetObject(lid, OpenMode.ForWrite) as StructureProfileLabel;
                    if (lbl == null) continue;
                    bool top = !styles.StructureAlignmentLabel.Id.IsNull && lbl.StyleId == styles.StructureAlignmentLabel.Id;
                    if (top)
                    {
                        structAlignN++;
                        if (AnchorLabel(lbl, true, plan.AnchorTop)) dragged++;
                        else if (DragToRow(lbl, pv, v, plan.Lift, bottom: false) && Settle(lbl, rows.Top, upward: false, gap)) dragged++;
                        else Note("MH -> Alignment label");
                    }
                    else
                    {
                        structN++;
                        if (AnchorLabel(lbl, false, plan.AnchorBottom)) dragged++;
                        else if (DragToRow(lbl, pv, v, rowLift, bottom: true) && Settle(lbl, rows.Bottom, upward: true, gap)) dragged++;
                        else Note("MH label");
                    }
                }
            }
            catch (SysException ex) { plan.Warnings.Add($"{v.Name}: structure labels not dragged ({ex.Message})"); }

            // pipe labels: dragged onto the dimension line, centred on their pipe's run; two
            // pipes over one run get their labels a row apart
            if (plan.DoPipes) try
            {
                var runs = pipeRuns;
                var shown = PartsShownIn(tr, db, pv, null);
                var pipeLabels = new List<PipeProfileLabel>();
                foreach (ObjectId lid in PipeProfileLabel.GetAvailableLabelIds(pv.ObjectId))
                {
                    var lbl = tr.GetObject(lid, OpenMode.ForWrite) as PipeProfileLabel;
                    if (lbl != null) pipeLabels.Add(lbl);
                }
                var rowUse = new Dictionary<int, int>();
                foreach (var lbl in pipeLabels.OrderBy(l => { try { return l.LabelLocation.X; } catch { return 0.0; } }))
                {
                    pipeN++;
                    if (!plan.DragPipe) continue;
                    bool lblDragged = false;
                    try { lblDragged = lbl.Dragged; } catch { }
                    if (lblDragged) { dragged++; continue; }                  // placed on an earlier pass
                    int row = -1;
                    double cx = double.NaN;
                    ObjectId fid = ObjectId.Null;
                    try { fid = lbl.FeatureId; } catch { }
                    if (!fid.IsNull)
                    {
                        if (runs.TryGetValue(fid, out var r)) { row = r.row; cx = (r.x0 + r.x1) / 2.0; }
                        else foreach (var kv in shown) if (kv.Value == fid && runs.TryGetValue(kv.Key, out var r2)) { row = r2.row; cx = (r2.x0 + r2.x1) / 2.0; break; }
                    }
                    if (row < 0)
                    {
                        // by the label's own x: the run under it that has had the fewest labels
                        double ax = 0;
                        try { ax = lbl.LabelLocation.X; } catch { }
                        var under = runs.Values.Where(r => r.x0 - 1 <= ax && ax <= r.x1 + 1).OrderBy(r => rowUse.TryGetValue(r.row, out int n) ? n : 0).ToList();
                        if (under.Count > 0) { row = under[0].row; cx = (under[0].x0 + under[0].x1) / 2.0; }
                        else { row = 0; cx = ax; }
                    }
                    rowUse[row] = (rowUse.TryGetValue(row, out int used) ? used : 0) + 1;
                    double seatY = lineY + Math.Max(0.5, plan.DimLift * 0.25) + row * plan.DimRow;
                    if (plan.AnchorPipeStation && AnchorLabel(lbl, false, plan.AnchorPipe + row * plan.AnchorPipeRow)) dragged++;
                    else if (!double.IsNaN(cx) && DragToPoint(lbl, cx, seatY)) dragged++;
                    else if (DragToRow(lbl, pv, v, rowLift, bottom: true) && Settle(lbl, rows.Bottom, upward: true, gap)) dragged++;
                    else Note("pipe label");
                }
            }
            catch (SysException ex) { plan.Warnings.Add($"{v.Name}: pipe labels not placed ({ex.Message})"); }

            int total = pipeN + structN + structAlignN + stationN;
            if (notDragged.Count > 0 || dragged < total)
                plan.Warnings.Add($"{v.Name}: {total - dragged} of {total} label(s) could not be dragged" + (notDragged.Count > 0 ? " -- " + string.Join(" | ", notDragged) : ""));
            return new JObject
            {
                ["pipe"] = pipeN, ["structure"] = structN, ["structure_alignment"] = structAlignN, ["station"] = stationN,
                ["dim_lines_found"] = dims.Count, ["dragged"] = dragged, ["not_dragged"] = total - dragged, ["total"] = total,
            };
        }

        // ── A block's dynamic properties (what its grips control) ─────────────────

        /// <summary>{name, source_file?}: the dynamic properties of a block -- name, value, units,
        /// allowed values, read-only -- read from a reference of it in the drawing, else from
        /// a reference inserted for the look and discarded. Plus the definition's X extents.</summary>
        private static string BlockDynamicProperties(JObject req, Document doc)
        {
            Database db = doc.Database;
            string name = S(req["name"]) ?? throw new ArgumentException("name is required");
            using (doc.LockDocument())
            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                try
                {
                    ObjectId btrId = ResolveBlockDefinition(tr, db, name, S(req["source_file"]), null);
                    BlockReference br = null;
                    foreach (ObjectId id in ModelSpace(tr, db, false))
                    {
                        try
                        {
                            var cand = tr.GetObject(id, OpenMode.ForRead) as BlockReference;
                            if (cand != null && EffectiveBlockName(tr, cand).Equals(name, StringComparison.OrdinalIgnoreCase)) { br = cand; break; }
                        }
                        catch { }
                    }
                    bool temporary = br == null;
                    if (temporary)
                    {
                        br = new BlockReference(Point3d.Origin, btrId);
                        Append(tr, ModelSpace(tr, db), br, "0");
                    }
                    var props = new JArray();
                    bool dynamic = false;
                    try { dynamic = br.IsDynamicBlock; } catch { }
                    if (dynamic)
                    {
                        foreach (DynamicBlockReferenceProperty dp in br.DynamicBlockReferencePropertyCollection)
                        {
                            var j = new JObject
                            {
                                ["name"] = dp.PropertyName, ["value"] = SafeStr(() => dp.Value?.ToString()),
                                ["units"] = SafeStr(() => dp.UnitsType.ToString()), ["read_only"] = SafeBool(() => dp.ReadOnly),
                                ["description"] = SafeStr(() => dp.Description),
                            };
                            try { j["allowed"] = new JArray(dp.GetAllowedValues().Select(x => x?.ToString())); } catch { }
                            props.Add(j);
                        }
                    }
                    bool spanKnown = BlockDefinitionSpanX(tr, btrId, out double minX, out double maxX);
                    var extra = new JObject
                    {
                        ["name"] = name, ["dynamic"] = dynamic, ["properties"] = props,
                        ["geometry_x"] = spanKnown ? new JArray(R(minX), R(maxX)) : null,
                        ["read_from"] = temporary ? "a temporary reference (discarded)" : $"reference {br.Handle}",
                    };
                    tr.Abort();      // never keeps the temporary reference
                    return Ok($"'{name}': {props.Count} dynamic propert{(props.Count == 1 ? "y" : "ies")}.", extra);
                }
                catch (SysException ex)
                {
                    tr.Abort();
                    return Fail($"block_dynamic_properties failed: {ex.Message}");
                }
            }
        }

        private static bool SafeBool(Func<bool> f) { try { return f(); } catch { return false; } }

        /// <summary>Sets a dynamic block's distance property to `span` when it has one --
        /// the grip the drafter would stretch -- and reports which; false when the block is
        /// not dynamic or has no distance property (then it is scaled instead).</summary>
        private static bool StretchDynamic(BlockReference br, double span, string wanted, out string property)
        {
            property = null;
            try
            {
                if (!br.IsDynamicBlock) return false;
                DynamicBlockReferenceProperty best = null, first = null;
                foreach (DynamicBlockReferenceProperty dp in br.DynamicBlockReferencePropertyCollection)
                {
                    bool ro = true; try { ro = dp.ReadOnly; } catch { }
                    if (ro) continue;
                    bool distance = false; try { distance = dp.UnitsType == DynamicBlockReferencePropertyUnitsType.Distance; } catch { }
                    if (!distance) continue;
                    if (first == null) first = dp;
                    if (!string.IsNullOrWhiteSpace(wanted) && string.Equals(dp.PropertyName, wanted, StringComparison.OrdinalIgnoreCase)) { best = dp; break; }
                }
                // the named property (the spec's dim_line.property, "Distance1" for Ano-ProfDimLine:
                // measured from the left end, its grip stretches the right end), else the first
                // distance property the block has
                best = best ?? first;
                if (best == null) return false;
                best.Value = span;
                property = best.PropertyName;
                return true;
            }
            catch { return false; }
        }

        // ── Moving views into their sheet windows ─────────────────────────────────

        /// <summary>{views: [{name | handle, location: [x, y]}]}: sets each profile view's
        /// insertion (its datum corner); profiles, parts and labels move with it. All or nothing.</summary>
        private static string ArrangeProfileViews(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var civil = CivilDocument.GetCivilDocument(db);
            var byName = new Dictionary<string, ObjectId>(StringComparer.OrdinalIgnoreCase);
            foreach (ObjectId aid in civil.GetAlignmentIds())
            {
                try
                {
                    var al = tr.GetObject(aid, OpenMode.ForRead) as Alignment;
                    if (al == null) continue;
                    foreach (ObjectId vid in al.GetProfileViewIds())
                        try { byName[((ProfileView)tr.GetObject(vid, OpenMode.ForRead)).Name] = vid; } catch { }
                }
                catch { }
            }
            var moved = new JArray();
            var ids = new List<ObjectId>();
            // the dimension-line blocks placed under a view go with it (labels are the view's own
            // children and follow by themselves)
            string dimBlock = S(req["dim_block"], S((req["dim_lines"] as JObject)?["block"], "")).Trim();
            var dimRefs = new List<(ObjectId id, Extents3d ext)>();
            if (dimBlock != "")
                foreach (ObjectId id in ModelSpace(tr, db, false))
                    try
                    {
                        var br = tr.GetObject(id, OpenMode.ForRead) as BlockReference;
                        if (br != null && EffectiveBlockName(tr, br).Equals(dimBlock, StringComparison.OrdinalIgnoreCase)) dimRefs.Add((id, br.GeometricExtents));
                    }
                    catch { }
            int carried = 0;
            foreach (JToken t in req["views"] as JArray ?? throw new ArgumentException("views is required"))
            {
                var v = t as JObject ?? throw new ArgumentException("every view must be an object");
                ObjectId id;
                string h = S(v["handle"], "");
                if (h != "") id = Resolve(db, h);
                else if (!byName.TryGetValue(S(v["name"], ""), out id)) throw new ArgumentException($"no profile view named '{S(v["name"])}'");
                var pv = tr.GetObject(id, OpenMode.ForWrite) as ProfileView ?? throw new ArgumentException($"{h} is not a profile view");
                Point3d to = Flat(P3(v["location"]));
                Point3d from = pv.Location;
                if (from.DistanceTo(to) < 1e-6) continue;
                Extents3d? was = null;
                try { was = pv.GeometricExtents; } catch { }
                pv.Location = to;
                ids.Add(id);
                if (was != null && dimRefs.Count > 0)
                {
                    var shift = Matrix3d.Displacement(to - from);
                    var mine = dimRefs.Where(d => Inside(d.ext, was.Value)).ToList();
                    foreach (var d in mine)
                    {
                        try { ((BlockReference)tr.GetObject(d.id, OpenMode.ForWrite)).TransformBy(shift); ids.Add(d.id); carried++; }
                        catch { }
                        dimRefs.Remove(d);
                    }
                }
                moved.Add(new JObject { ["name"] = pv.Name, ["handle"] = id.Handle.ToString(), ["from"] = new JArray(R(from.X), R(from.Y)), ["to"] = new JArray(R(to.X), R(to.Y)) });
            }
            var extra = new JObject { ["moved"] = moved, ["modified"] = Handles(ids), ["dim_lines_carried"] = carried };
            if (req["proposed"] is JObject proposed) extra["proposed"] = proposed;
            return Ok($"{moved.Count} profile view(s) moved" + (carried > 0 ? $", {carried} dimension line(s) carried along." : "."), extra);
        });

        /// <summary>The alignment's EG profile (first by type) and flowline ("... FL") profile.</summary>
        private static AlignmentWork WorkFor(Transaction tr, Alignment align)
        {
            var w = new AlignmentWork { Alignment = align };
            foreach (ObjectId pid in align.GetProfileIds())
            {
                try
                {
                    var p = (Profile)tr.GetObject(pid, OpenMode.ForRead);
                    if (p.Name.EndsWith(" FL", StringComparison.OrdinalIgnoreCase)) w.Flowline = pid;
                    else if (p.ProfileType == ProfileType.EG && w.EgProfile.IsNull) w.EgProfile = pid;
                }
                catch { }
            }
            return w;
        }

        /// <summary>True when the view already carries pipe or structure profile labels.</summary>
        private static bool HasOurLabels(ProfileView pv)
        {
            try { if (PipeProfileLabel.GetAvailableLabelIds(pv.ObjectId).Count > 0) return true; } catch { }
            try { if (StructureProfileLabel.GetAvailableLabelIds(pv.ObjectId).Count > 0) return true; } catch { }
            return false;
        }

        /// <summary>True when the centre of `e` lies within `box` (a foot of slack).</summary>
        private static bool Inside(Extents3d e, Extents3d box)
        {
            double cx = (e.MinPoint.X + e.MaxPoint.X) / 2, cy = (e.MinPoint.Y + e.MaxPoint.Y) / 2;
            return cx >= box.MinPoint.X - 1 && cx <= box.MaxPoint.X + 1 && cy >= box.MinPoint.Y - 1 && cy <= box.MaxPoint.Y + 1;
        }

        // ── Context ───────────────────────────────────────────────────────────────

        private static string ProfileContext(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var civil = CivilDocument.GetCivilDocument(db);
            double tol = D(req["offset_tolerance"], 25.0);
            double step = Math.Max(1.0, D(req["sample_step"], 25.0));
            double restationTo = D(req["restation_to"], 100.0);
            var wanted = (req["alignments"] as JArray)?.Select(t => t.ToString().Trim().ToLowerInvariant()).Where(s => s != "").ToList()
                         ?? new List<string>();
            string netFilter = S(req["network"], "").Trim();
            var warnings = new List<string>();

            // the surface: asked for, else "eg", else the only one
            CivilSurface surface = null;
            string surfaceError = null;
            try
            {
                surface = GradingLabels.ResolveSurface(tr, civil, S(req["surface"], "") == "" ? "eg" : S(req["surface"]), out _);
            }
            catch (ArgumentException ex)
            {
                var all = GradingLabels.Surfaces(tr, civil);
                if (all.Count == 1 && S(req["surface"], "") == "") surface = all[0].s;
                else surfaceError = ex.Message;
            }

            var networks = new List<Network>();
            foreach (ObjectId nid in civil.GetPipeNetworkIds())
            {
                try
                {
                    var net = tr.GetObject(nid, OpenMode.ForRead) as Network;
                    if (net == null) continue;
                    if (netFilter != "" && !net.Name.Equals(netFilter, StringComparison.OrdinalIgnoreCase)) continue;
                    networks.Add(net);
                }
                catch { }
            }

            var alignments = new JArray();
            foreach (ObjectId aid in civil.GetAlignmentIds())
            {
                Alignment align;
                try { align = tr.GetObject(aid, OpenMode.ForRead) as Alignment; } catch { continue; }
                if (align == null) continue;
                if (wanted.Count > 0 && !wanted.Any(w => align.Name.Equals(w, StringComparison.OrdinalIgnoreCase)
                                                        || align.Name.IndexOf(w, StringComparison.OrdinalIgnoreCase) >= 0))
                    continue;
                var a = new JObject
                {
                    ["name"] = align.Name, ["handle"] = align.Handle.ToString(),
                    ["start_station"] = R(align.StartingStation), ["end_station"] = R(align.EndingStation),
                    ["length"] = R(align.Length),
                    ["restation"] = new JObject
                    {
                        ["needed"] = Math.Abs(align.StartingStation - restationTo) > 1e-6,
                        ["shift"] = R(restationTo - align.StartingStation),
                    },
                };
                a["samples"] = surface == null ? new JArray() : SampleSurface(align, surface, step);
                a["profiles"] = ProfilesJson(tr, align);
                var parts = PartsOnAlignment(tr, align, networks, tol);
                a["pipes"] = parts.pipes; a["structures"] = parts.structures;
                a["existing_profile_views"] = ProfileViewsJson(tr, align);
                alignments.Add(a);
            }
            if (alignments.Count == 0)
                return Fail(wanted.Count > 0 ? $"no alignment matches {string.Join(", ", wanted)}" : "the drawing has no alignments");

            var cat = ReadProfileCatalogue(tr, civil);
            var extent = new JObject();
            try { extent["min"] = new JArray(R(db.Extmin.X), R(db.Extmin.Y)); extent["max"] = new JArray(R(db.Extmax.X), R(db.Extmax.Y)); } catch { }

            var extra = new JObject
            {
                ["restation_to"] = restationTo,
                ["alignments"] = alignments,
                ["surfaces"] = new JArray(GradingLabels.Surfaces(tr, civil).Select(s => GradingLabels.SurfaceJson(s.s))),
                ["surface_pick"] = surface == null ? null : new JObject { ["name"] = surface.Name, ["handle"] = surface.Handle.ToString() },
                ["networks"] = new JArray(networks.Select(n => new JObject
                {
                    ["name"] = n.Name, ["handle"] = n.Handle.ToString(),
                    ["reference_alignment"] = SafeStr(() => n.ReferenceAlignmentName),
                    ["pipes"] = SafeInt(() => n.GetPipeIds().Count), ["structures"] = SafeInt(() => n.GetStructureIds().Count),
                })),
                ["styles"] = cat.styles, ["defaults"] = cat.defaults,
                ["drawing_extent"] = extent,
                ["z_convention"] = "pipe start/end Z is the centreline; invert = Z - inner diameter / 2",
                ["warnings"] = new JArray(warnings),
            };
            if (surfaceError != null) extra["surface_error"] = surfaceError;
            return Ok($"{alignments.Count} alignment(s), {networks.Count} pipe network(s).", extra);
        });

        private static JArray SampleSurface(Alignment align, CivilSurface surface, double step)
        {
            var arr = new JArray();
            double s0 = align.StartingStation, s1 = align.EndingStation;
            for (double st = s0; st <= s1 + 1e-6; st += step)
            {
                double at = Math.Min(st, s1);
                JObject sample = SampleAt(align, surface, at);
                if (sample != null) arr.Add(sample);
                if (at >= s1) break;
            }
            if (arr.Count == 0 || Math.Abs(D(((JObject)arr.Last)["station"]) - s1) > 1e-6)
            {
                var last = SampleAt(align, surface, s1);
                if (last != null) arr.Add(last);
            }
            return arr;
        }

        private static JObject SampleAt(Alignment align, CivilSurface surface, double station)
        {
            try
            {
                double x = 0, y = 0;
                align.PointLocation(station, 0.0, ref x, ref y);
                double z = surface.FindElevationAtXY(x, y);
                if (double.IsNaN(z)) return null;
                return new JObject { ["station"] = R(station), ["elevation"] = Math.Round(z, 3) };
            }
            catch { return null; }   // off the surface: no sample, never a failure
        }

        private static JArray ProfilesJson(Transaction tr, Alignment align)
        {
            var arr = new JArray();
            try
            {
                foreach (ObjectId pid in align.GetProfileIds())
                {
                    try
                    {
                        var p = tr.GetObject(pid, OpenMode.ForRead) as Profile;
                        if (p == null) continue;
                        arr.Add(new JObject
                        {
                            ["name"] = p.Name, ["handle"] = p.Handle.ToString(),
                            ["type"] = SafeStr(() => p.ProfileType.ToString()),
                            ["data_source"] = SafeStr(() => p.DataSourceName),
                            ["elevation_min"] = SafeNum(() => p.ElevationMin), ["elevation_max"] = SafeNum(() => p.ElevationMax),
                        });
                    }
                    catch { }
                }
            }
            catch { }
            return arr;
        }

        private static JArray ProfileViewsJson(Transaction tr, Alignment align)
        {
            var arr = new JArray();
            try
            {
                foreach (ObjectId vid in align.GetProfileViewIds())
                {
                    try
                    {
                        var pv = tr.GetObject(vid, OpenMode.ForRead) as ProfileView;
                        if (pv == null) continue;
                        arr.Add(ProfileViewJson(pv));
                    }
                    catch { }
                }
            }
            catch { }
            return arr;
        }

        private static JObject ProfileViewJson(ProfileView pv)
        {
            var j = new JObject
            {
                ["name"] = pv.Name, ["handle"] = pv.Handle.ToString(),
                ["station_start"] = SafeNum(() => pv.StationStart), ["station_end"] = SafeNum(() => pv.StationEnd),
                ["elevation_min"] = SafeNum(() => pv.ElevationMin), ["elevation_max"] = SafeNum(() => pv.ElevationMax),
            };
            try { j["location"] = new JArray(R(pv.Location.X), R(pv.Location.Y)); } catch { }
            try
            {
                var ext = pv.GeometricExtents;
                j["extents"] = new JArray(
                    new JArray(R(ext.MinPoint.X), R(ext.MinPoint.Y)), new JArray(R(ext.MaxPoint.X), R(ext.MinPoint.Y)),
                    new JArray(R(ext.MaxPoint.X), R(ext.MaxPoint.Y)), new JArray(R(ext.MinPoint.X), R(ext.MaxPoint.Y)));
            }
            catch { }
            return j;
        }

        /// <summary>The pipes and structures of the networks that lie along an alignment:
        /// a pipe whose both ends are within `tol` of it (an end past the alignment's ends
        /// still counts, as `partial`), a structure within `tol`.</summary>
        private static (JArray pipes, JArray structures) PartsOnAlignment(Transaction tr, Alignment align, List<Network> networks, double tol)
        {
            var pipes = new JArray();
            var structures = new JArray();
            foreach (var net in networks)
            {
                ObjectIdCollection pipeIds, structIds;
                try { pipeIds = net.GetPipeIds(); structIds = net.GetStructureIds(); } catch { continue; }
                foreach (ObjectId pid in pipeIds)
                {
                    try
                    {
                        var pipe = tr.GetObject(pid, OpenMode.ForRead) as Pipe;
                        if (pipe == null) continue;
                        var a = StationOf(align, pipe.StartPoint);
                        var b = StationOf(align, pipe.EndPoint);
                        if (a == null || b == null) continue;
                        if (Math.Abs(a.Value.off) > tol || Math.Abs(b.Value.off) > tol) continue;
                        double dia = SafeNum(() => pipe.InnerDiameterOrWidth) ?? 0.0;
                        string startStr = "", endStr = "";
                        try { startStr = (tr.GetObject(pipe.StartStructureId, OpenMode.ForRead) as Structure)?.Name ?? ""; } catch { }
                        try { endStr = (tr.GetObject(pipe.EndStructureId, OpenMode.ForRead) as Structure)?.Name ?? ""; } catch { }
                        pipes.Add(new JObject
                        {
                            ["name"] = pipe.Name, ["handle"] = pipe.Handle.ToString(), ["network"] = net.Name,
                            ["start_station"] = R(a.Value.st), ["end_station"] = R(b.Value.st),
                            ["start_offset"] = R(a.Value.off), ["end_offset"] = R(b.Value.off),
                            ["start_z"] = R(pipe.StartPoint.Z), ["end_z"] = R(pipe.EndPoint.Z),
                            ["start_invert"] = R(pipe.StartPoint.Z - dia / 2.0), ["end_invert"] = R(pipe.EndPoint.Z - dia / 2.0),
                            ["diameter"] = R(dia), ["slope"] = SafeNum(() => pipe.Slope),
                            ["start_structure"] = startStr, ["end_structure"] = endStr,
                            ["part_size"] = RStr(pipe, "PartSizeName"),
                            ["partial"] = a.Value.outside || b.Value.outside,
                        });
                    }
                    catch { }
                }
                foreach (ObjectId sid in structIds)
                {
                    try
                    {
                        var s = tr.GetObject(sid, OpenMode.ForRead) as Structure;
                        if (s == null) continue;
                        var at = StationOf(align, s.Location);
                        if (at == null || at.Value.outside || Math.Abs(at.Value.off) > tol) continue;
                        structures.Add(new JObject
                        {
                            ["name"] = s.Name, ["handle"] = s.Handle.ToString(), ["network"] = net.Name,
                            ["station"] = R(at.Value.st), ["offset"] = R(at.Value.off),
                            ["rim"] = SafeNum(() => s.RimElevation), ["sump"] = SafeNum(() => s.SumpElevation),
                            ["part_size"] = RStr(s, "PartSizeName"), ["description"] = RStr(s, "Description", "PartDescription"),
                        });
                    }
                    catch { }
                }
            }
            return (pipes, structures);
        }

        private static (double st, double off, bool outside)? StationOf(Alignment align, Point3d p)
        {
            double st = 0, off = 0;
            try { align.StationOffset(p.X, p.Y, ref st, ref off); return (st, off, false); }
            catch { }
            try
            {
                bool outside = false;
                align.StationOffsetAcceptOutOfRange(p.X, p.Y, ref st, ref off, ref outside);
                return (st, off, outside);
            }
            catch { return null; }
        }

        // ── Style catalogues and defaults ─────────────────────────────────────────

        private sealed class ProfileCatalogue
        {
            public List<(string name, ObjectId id)> ProfileViewStyles, BandSets, ProfileStyles, ProfileLabelSets,
                                                    PipeProfileLabels, StructureLabels, MajorStationLabels;
            public Dictionary<ObjectId, double> Exaggeration = new Dictionary<ObjectId, double>();
            public ObjectId DefProfileView = ObjectId.Null, DefBandSet = ObjectId.Null, DefProfile = ObjectId.Null,
                            DefProfileLabelSet = ObjectId.Null, DefPipeLabel = ObjectId.Null, DefStructureLabel = ObjectId.Null;
            public string DefProfileViewName, DefBandSetName, DefProfileName, DefProfileLabelSetName, DefPipeLabelName, DefStructureLabelName;

            public JObject styles => new JObject
            {
                ["profile_view"] = new JArray(ProfileViewStyles.Select(s => new JObject
                {
                    ["name"] = s.name, ["vertical_exaggeration"] = Exaggeration.TryGetValue(s.id, out double ve) ? (double?)ve : null,
                })),
                ["band_sets"] = Names(BandSets), ["profile"] = Names(ProfileStyles), ["profile_label_sets"] = Names(ProfileLabelSets),
                ["pipe_profile_labels"] = Names(PipeProfileLabels), ["structure_labels"] = Names(StructureLabels),
                ["profile_major_station_labels"] = Names(MajorStationLabels),
                ["label_style_parents"] = new JObject(PipeProfileLabels.Concat(StructureLabels).Concat(MajorStationLabels)
                    .Where(x => ParentOf(x.id) != "")
                    .GroupBy(x => x.name)
                    .Select(g => new JProperty(g.Key, new JArray(g.Select(x => ParentOf(x.id)).Distinct())))),
                ["note"] = "a label style name used under several parents may be given as 'name @ parent-substring'",
            };

            public JObject defaults => new JObject
            {
                ["profile_view"] = DefProfileViewName, ["band_set"] = DefBandSetName, ["profile"] = DefProfileName,
                ["profile_label_set"] = DefProfileLabelSetName, ["pipe_profile_label"] = DefPipeLabelName,
                ["structure_profile_label"] = DefStructureLabelName,
            };

            private static JArray Names(List<(string name, ObjectId id)> l) => new JArray(l.Select(s => s.name));
        }

        private static ProfileCatalogue ReadProfileCatalogue(Transaction tr, CivilDocument civil)
        {
            _styleParents = new Dictionary<ObjectId, string>();
            var c = new ProfileCatalogue();
            var styles = civil.Styles;
            c.ProfileViewStyles = Safe(() => GradingLabels.StyleNames(tr, styles.ProfileViewStyles));
            c.BandSets = Safe(() => GradingLabels.StyleNames(tr, styles.ProfileViewBandSetStyles));
            c.ProfileStyles = Safe(() => GradingLabels.StyleNames(tr, styles.ProfileStyles));
            c.ProfileLabelSets = Safe(() => GradingLabels.StyleNames(tr, styles.LabelSetStyles.ProfileLabelSetStyles));
            c.PipeProfileLabels = Safe(() => LabelStyleNames(tr, styles.LabelStyles.PipeLabelStyles.PlanProfileLabelStyles));
            c.StructureLabels = Safe(() => LabelStyleNames(tr, styles.LabelStyles.StructureLabelStyles.LabelStyles));
            c.MajorStationLabels = Safe(() => LabelStyleNames(tr, styles.LabelStyles.ProfileLabelStyles.MajorStationLabelStyles));
            foreach (var s in c.ProfileViewStyles)
            {
                try
                {
                    var pvs = tr.GetObject(s.id, OpenMode.ForRead) as ProfileViewStyle;
                    if (pvs != null) c.Exaggeration[s.id] = pvs.GraphStyle.VerticalExaggeration;
                }
                catch { }
            }
            try
            {
                var st = civil.Settings.GetSettings<SettingsProfileView>().Styles;
                try { c.DefProfileView = st.ProfileViewStyleId.Value; } catch { }
                try { c.DefProfileViewName = st.ProfileViewStyle.Value; } catch { }
                try { c.DefBandSet = st.ProfileViewBandStyleId.Value; } catch { }
                try { c.DefBandSetName = st.ProfileViewBandStyle.Value; } catch { }
            }
            catch { }
            try
            {
                var st = civil.Settings.GetSettings<SettingsProfile>().StyleSettings;
                try { c.DefProfile = st.ProfileStyleId.Value; } catch { }
                try { c.DefProfileName = st.ProfileStyle.Value; } catch { }
                try { c.DefProfileLabelSet = st.ProfileLabelSetId.Value; } catch { }
                try { c.DefProfileLabelSetName = st.ProfileLabelSet.Value; } catch { }
            }
            catch { }
            try
            {
                var st = civil.Settings.GetSettings<SettingsPipeNetwork>().Styles;
                try { c.DefPipeLabel = st.PipeProfileLabelStyleId.Value; } catch { }
                try { c.DefPipeLabelName = st.PipeProfileLabel.Value; } catch { }
                try { c.DefStructureLabel = st.StructureProfileLabelStyleId.Value; } catch { }
                try { c.DefStructureLabelName = st.StructureProfileLabel.Value; } catch { }
            }
            catch { }
            // a name the settings did not give: the first style there is, so nothing is ever created style-less
            if (c.DefProfileView.IsNull && c.ProfileViewStyles.Count > 0) { c.DefProfileView = c.ProfileViewStyles[0].id; c.DefProfileViewName = c.ProfileViewStyles[0].name; }
            if (c.DefBandSet.IsNull && c.BandSets.Count > 0) { c.DefBandSet = c.BandSets[0].id; c.DefBandSetName = c.BandSets[0].name; }
            if (c.DefProfile.IsNull && c.ProfileStyles.Count > 0) { c.DefProfile = c.ProfileStyles[0].id; c.DefProfileName = c.ProfileStyles[0].name; }
            if (c.DefProfileLabelSet.IsNull && c.ProfileLabelSets.Count > 0) { c.DefProfileLabelSet = c.ProfileLabelSets[0].id; c.DefProfileLabelSetName = c.ProfileLabelSets[0].name; }
            if (c.DefPipeLabel.IsNull && c.PipeProfileLabels.Count > 0) { c.DefPipeLabel = c.PipeProfileLabels[0].id; c.DefPipeLabelName = c.PipeProfileLabels[0].name; }
            if (c.DefStructureLabel.IsNull && c.StructureLabels.Count > 0) { c.DefStructureLabel = c.StructureLabels[0].id; c.DefStructureLabelName = c.StructureLabels[0].name; }
            return c;
        }

        private static List<(string name, ObjectId id)> Safe(Func<List<(string name, ObjectId id)>> f)
        {
            try { return f() ?? new List<(string, ObjectId)>(); } catch { return new List<(string, ObjectId)>(); }
        }

        /// <summary>Label styles are a tree: "— SANITARY PROFILE [STRUCTURES] —" holds "MH" and
        /// "MH —> Alignment" as children. Every level is listed, by the style's own name.</summary>
        /// <summary>Parent style name per style id, filled while the trees are walked; "" for a root.</summary>
        [ThreadStatic] private static Dictionary<ObjectId, string> _styleParents;

        private static string ParentOf(ObjectId id) =>
            _styleParents != null && _styleParents.TryGetValue(id, out string p) ? p ?? "" : "";

        private static List<(string name, ObjectId id)> LabelStyleNames(Transaction tr, IEnumerable<ObjectId> ids)
        {
            var list = new List<(string, ObjectId)>();
            _styleParents = _styleParents ?? new Dictionary<ObjectId, string>();
            foreach (ObjectId id in ids) AddLabelStyle(tr, id, list, 0, "");
            return list;
        }

        private static void AddLabelStyle(Transaction tr, ObjectId id, List<(string name, ObjectId id)> list, int depth, string parent)
        {
            if (depth > 8 || list.Any(x => x.id == id)) return;
            LabelStyle ls;
            try { ls = tr.GetObject(id, OpenMode.ForRead, false) as LabelStyle; } catch { return; }
            if (ls == null) return;
            string name;
            try { name = ls.Name; list.Add((name, id)); } catch { return; }
            _styleParents[id] = parent;
            int n = 0;
            try { n = ls.ChildrenCount; } catch { }
            for (int i = 0; i < n; i++)
            {
                ObjectId cid;
                try { cid = ls[i]; } catch { continue; }
                AddLabelStyle(tr, cid, list, depth + 1, name);
            }
        }

        /// <summary>"MH --> Alignment", "MH —> Alignment" and "MH -> Alignment" are one name: dashes
        /// of any kind and length before a ">" collapse, as does spacing and case.</summary>
        private static string NormStyle(string name)
        {
            if (name == null) return "";
            string t = name.ToLowerInvariant().Replace('\u2014', '-').Replace('\u2013', '-').Replace('\u2192', '>');
            t = System.Text.RegularExpressions.Regex.Replace(t, @"[-=]+\s*>", ">");
            t = System.Text.RegularExpressions.Regex.Replace(t, @"\s*>\s*", ">");
            t = System.Text.RegularExpressions.Regex.Replace(t, @"\s+", " ").Trim();
            return t;
        }

        /// <summary>A style picked loosely: the requested name matched after NormStyle (exact, else a
        /// unique substring), else the default with a warning that lists what exists. Never throws.</summary>
        private static GradingLabels.StylePick PickLoose(List<(string name, ObjectId id)> styles, string wanted, string what,
                                                         ObjectId defaultId, string defaultName, List<string> warnings)
        {
            var pick = new GradingLabels.StylePick
            {
                Requested = string.IsNullOrWhiteSpace(wanted) ? null : wanted.Trim(),
                Id = defaultId,
                Name = string.IsNullOrWhiteSpace(defaultName) ? "(drawing default)" : defaultName,
            };
            if (pick.Requested == null) return pick;
            string wantedName = pick.Requested, wantedParent = null;
            int at = pick.Requested.IndexOf('@');
            if (at > 0) { wantedName = pick.Requested.Substring(0, at).Trim(); wantedParent = pick.Requested.Substring(at + 1).Trim(); }
            string key = NormStyle(wantedName);
            var hit = styles.Where(x => NormStyle(x.name) == key).ToList();
            if (hit.Count == 0) hit = styles.Where(x => NormStyle(x.name).Contains(key)).ToList();
            if (hit.Count > 1 && !string.IsNullOrWhiteSpace(wantedParent))
            {
                string pk = NormStyle(wantedParent);
                var under = hit.Where(h => NormStyle(ParentOf(h.id)).Contains(pk)).ToList();
                if (under.Count >= 1) hit = under;
            }
            if (hit.Count == 1 || (hit.Count > 1 && hit.All(h => NormStyle(h.name) == key)))
            {
                pick.Id = hit[0].id; pick.Name = hit[0].name; pick.IsDefault = false;
                if (hit.Count > 1)
                    warnings.Add($"{what} style '{pick.Requested}': {hit.Count} styles carry that name (under "
                                 + string.Join(", ", hit.Select(h => "'" + ParentOf(h.id) + "'")) + "); the one under '" + ParentOf(hit[0].id)
                                 + "' was used -- say which with 'name @ parent'.");
                return pick;
            }
            string available = string.Join(", ", styles.Select(x => x.name).Take(40));
            warnings.Add(hit.Count == 0
                ? $"{what} style '{pick.Requested}' not found (dashes and arrows are matched loosely); using the drawing default '{pick.Name}'. Available: {available}."
                : $"{what} style '{pick.Requested}' matches several ({string.Join(", ", hit.Select(h => h.name))}); using the drawing default '{pick.Name}'. Give the exact name.");
            return pick;
        }

        /// <summary>Every style the draw needs, picked once; misses are warnings.</summary>
        private sealed class ProfileStyles
        {
            public GradingLabels.StylePick ProfileView, BandSet, EgProfile, EgLabelSet, FlowlineProfile, FlowlineLabelSet,
                                           PipeLabel, StructureLabel, StructureAlignmentLabel, StationLabel;
            public JObject Json() => new JObject
            {
                ["profile_view"] = ProfileView.Json(), ["band_set"] = BandSet.Json(),
                ["eg_profile"] = EgProfile.Json(), ["eg_label_set"] = EgLabelSet.Json(),
                ["flowline_profile"] = FlowlineProfile.Json(), ["flowline_label_set"] = FlowlineLabelSet.Json(),
                ["pipe_label"] = PipeLabel.Json(), ["structure_label"] = StructureLabel.Json(),
                ["structure_alignment_label"] = StructureAlignmentLabel.Json(), ["station_label"] = StationLabel.Json(),
            };
        }

        private static ProfileStyles PickProfileStyles(ProfileCatalogue cat, JObject styles, List<string> warnings)
        {
            styles = styles ?? new JObject();
            string W(string k) => S(styles[k], "");
            var p = new ProfileStyles
            {
                ProfileView = PickLoose(cat.ProfileViewStyles, W("profile_view"), "profile view", cat.DefProfileView, cat.DefProfileViewName, warnings),
                BandSet = PickLoose(cat.BandSets, W("band_set"), "profile view band set", cat.DefBandSet, cat.DefBandSetName, warnings),
                EgProfile = PickLoose(cat.ProfileStyles, W("eg_profile"), "profile", cat.DefProfile, cat.DefProfileName, warnings),
                EgLabelSet = PickLoose(cat.ProfileLabelSets, W("eg_label_set"), "profile label set", cat.DefProfileLabelSet, cat.DefProfileLabelSetName, warnings),
                FlowlineLabelSet = PickLoose(cat.ProfileLabelSets, W("flowline_label_set"), "profile label set", cat.DefProfileLabelSet, cat.DefProfileLabelSetName, warnings),
                PipeLabel = PickLoose(cat.PipeProfileLabels, W("pipe_label"), "pipe profile label", cat.DefPipeLabel, cat.DefPipeLabelName, warnings),
                StructureLabel = PickLoose(cat.StructureLabels, W("structure_label"), "structure label", cat.DefStructureLabel, cat.DefStructureLabelName, warnings),
                StructureAlignmentLabel = PickLoose(cat.StructureLabels, W("structure_alignment_label"), "structure label", ObjectId.Null, null, warnings),
            };
            // the flowline profile: asked for, else a "no display" style if the drawing has one, else the default
            var hidden = cat.ProfileStyles.FirstOrDefault(s => s.name.IndexOf("no display", StringComparison.OrdinalIgnoreCase) >= 0
                                                            || s.name.IndexOf("hidden", StringComparison.OrdinalIgnoreCase) >= 0
                                                            || s.name.IndexOf("_no", StringComparison.OrdinalIgnoreCase) >= 0);
            p.FlowlineProfile = PickLoose(cat.ProfileStyles, W("flowline_profile"), "profile",
                hidden.id.IsNull ? cat.DefProfile : hidden.id, hidden.id.IsNull ? cat.DefProfileName : hidden.name, warnings);
            // major station labels have no settings default: the first style there is
            ObjectId defStation = cat.MajorStationLabels.Count > 0 ? cat.MajorStationLabels[0].id : ObjectId.Null;
            string defStationName = cat.MajorStationLabels.Count > 0 ? cat.MajorStationLabels[0].name : null;
            p.StationLabel = PickLoose(cat.MajorStationLabels, W("station_label"), "profile major station label", defStation, defStationName, warnings);
            return p;
        }

        // ── The plan as the plugin sees it ────────────────────────────────────────

        private sealed class ViewPlan
        {
            public string Name, AlignmentName;
            public ObjectId AlignmentId;
            public double StationStart, StationEnd, ElevationMin, ElevationMax;
            public Point3d Insertion;
            public List<ObjectId> Pipes = new List<ObjectId>(), Structures = new List<ObjectId>();
        }

        private sealed class ProfilePlan
        {
            public bool Apply;
            public double Span, RestationTo, StationIncrement, Lift;
            public bool Restation, DragPipe, DragStation;
            public string StationHost;
            public string DimBlock, DimSource, DimLayer;      // the "Ano-ProfDimLine" block between manholes
            public string DimProperty = "Distance1";          // the dynamic grip that stretches it
            public double DimRow = 12.0, DimLift = 4.0;
            // structure profile labels are placed by their DIMENSION ANCHOR (graph view top /
            // bottom + an offset in plotted inches), not by dragging: the "MH -> Alignment"
            // label hangs from the top line, the "MH" label stands on the bottom line
            public double AnchorTop = -3.0, AnchorBottom = 0.75;
            /// <summary>Plotted inches above the graph view bottom for the station labels, the
            /// pipe labels (first row) and each further row of pipe labels over one run.</summary>
            public double AnchorStation = 0.5, AnchorPipe = 0.2, AnchorPipeRow = 0.35;
            /// <summary>Place pipe and station labels by dimension anchor instead of dragging.
            /// Off: dragging is what is proven on the styles.</summary>
            public bool AnchorPipeStation;
            /// <summary>Set the pipe network's "Default Profile Label Placement" (dimension anchor
            /// option + plot height for structures) to top / bottom around each structure label
            /// Create, so the label is born anchored; the setting is restored afterwards.</summary>
            public bool AnchorAtCreation = false;
            public bool AnchorSettingWarned;
            // true while creating: nothing is stretched, anchored or dragged in that transaction
            // (Civil 3D overflowed its stack when that was tried on unevaluated objects); the
            // second pass, drag_profile_labels, does all of it afterwards
            public bool Deferred;
            // second-pass switches (request "do": {dim_lines, anchors, pipes, stations}), to run one
            // adjustment at a time when hunting a crash
            public bool DoDimLines = true, DoAnchors = false, DoPipes = true, DoStations = true;
            /// <summary>Structure labels that carry a dimension anchor override (an earlier pass
            /// anchored them) are put back to the drawing's default placement.</summary>
            public bool ResetStructureAnchors;
            public CivilSurface Surface;
            public ProfileStyles Styles;
            public ProfileCatalogue Catalogue;
            public List<ViewPlan> Views = new List<ViewPlan>();
            public List<string> Warnings = new List<string>(), Errors = new List<string>();
        }

        private static ProfilePlan PlanProfileViews(Transaction tr, Database db, JObject req)
        {
            var civil = CivilDocument.GetCivilDocument(db);
            var plan = new ProfilePlan
            {
                Apply = B(req["apply"], false),
                Span = D(req["span"], 40.0),
                Restation = B(req["restation"], true),
                RestationTo = D(req["restation_to"], 100.0),
                StationHost = S(req["station_host"], "eg").Trim().ToLowerInvariant(),
                StationIncrement = D(req["station_increment"], 100.0),
            };
            var drag = req["drag_to_datum"] as JObject ?? new JObject();
            plan.DragPipe = B(drag["pipe"], true);
            plan.DragStation = B(drag["station"], true);
            plan.Lift = D(drag["lift"], 5.0);
            ReadDimLines(plan, req["dim_lines"] as JObject);
            ReadAnchors(plan, req["dimension_anchor"] as JObject);
            if (plan.StationHost != "flowline" && plan.StationHost != "eg") plan.Errors.Add("station_host must be flowline or eg");

            try { plan.Surface = GradingLabels.ResolveSurface(tr, civil, S(req["surface"], "") == "" ? "eg" : S(req["surface"]), out _); }
            catch (ArgumentException ex)
            {
                var all = GradingLabels.Surfaces(tr, civil);
                if (all.Count == 1 && S(req["surface"], "") == "") plan.Surface = all[0].s;
                else plan.Errors.Add(ex.Message);
            }

            plan.Catalogue = ReadProfileCatalogue(tr, civil);
            plan.Styles = PickProfileStyles(plan.Catalogue, req["styles"] as JObject, plan.Warnings);
            if (plan.Styles.ProfileView.Id.IsNull) plan.Errors.Add("the drawing has no profile view styles");

            var existing = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (ObjectId aid in civil.GetAlignmentIds())
            {
                try
                {
                    var al = tr.GetObject(aid, OpenMode.ForRead) as Alignment;
                    if (al == null) continue;
                    foreach (ObjectId vid in al.GetProfileViewIds())
                        try { existing.Add(((ProfileView)tr.GetObject(vid, OpenMode.ForRead)).Name); } catch { }
                }
                catch { }
            }

            var views = req["views"] as JArray;
            if (views == null || views.Count == 0) plan.Errors.Add("views is required: the profile views to draw (see profiles.plan)");
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (JToken t in views ?? new JArray())
            {
                var v = t as JObject;
                if (v == null) { plan.Errors.Add("every view must be an object"); continue; }
                var vp = new ViewPlan
                {
                    Name = S(v["name"], "").Trim(), AlignmentName = S(v["alignment"], ""),
                    StationStart = D(v["station_start"]), StationEnd = D(v["station_end"]),
                    ElevationMin = D(v["elevation_min"]), ElevationMax = D(v["elevation_max"]),
                };
                if (vp.Name == "") { plan.Errors.Add("a view has no name"); continue; }
                if (existing.Contains(vp.Name)) plan.Errors.Add($"profile view '{vp.Name}' already exists");
                if (!seen.Add(vp.Name)) plan.Errors.Add($"view name '{vp.Name}' is used twice in the request");
                if (vp.StationEnd <= vp.StationStart) plan.Errors.Add($"{vp.Name}: station_end must be past station_start");
                if (vp.ElevationMax <= vp.ElevationMin) plan.Errors.Add($"{vp.Name}: elevation_max must be above elevation_min");
                try { vp.Insertion = Flat(P3(v["insertion"])); } catch { plan.Errors.Add($"{vp.Name}: insertion [x, y] is required"); }
                try
                {
                    string ah = S(v["alignment_handle"], "");
                    vp.AlignmentId = ah != "" ? Resolve(db, ah) : FindAlignment(tr, civil, vp.AlignmentName);
                    var al = tr.GetObject(vp.AlignmentId, OpenMode.ForRead) as Alignment
                             ?? throw new ArgumentException($"{ah} is not an alignment");
                    vp.AlignmentName = al.Name;
                }
                catch (ArgumentException ex) { plan.Errors.Add($"{vp.Name}: {ex.Message}"); }
                foreach (JToken h in v["pipes"] as JArray ?? new JArray())
                    try { vp.Pipes.Add(Resolve(db, h.ToString())); } catch (ArgumentException ex) { plan.Errors.Add($"{vp.Name}: pipe {ex.Message}"); }
                foreach (JToken h in v["structures"] as JArray ?? new JArray())
                    try { vp.Structures.Add(Resolve(db, h.ToString())); } catch (ArgumentException ex) { plan.Errors.Add($"{vp.Name}: structure {ex.Message}"); }
                plan.Views.Add(vp);
            }
            return plan;
        }

        private static ObjectId FindAlignment(Transaction tr, CivilDocument civil, string name)
        {
            if (string.IsNullOrWhiteSpace(name)) throw new ArgumentException("alignment (name) or alignment_handle is required");
            var names = new List<string>();
            foreach (ObjectId aid in civil.GetAlignmentIds())
            {
                var al = tr.GetObject(aid, OpenMode.ForRead) as Alignment;
                if (al == null) continue;
                names.Add(al.Name);
                if (al.Name.Equals(name.Trim(), StringComparison.OrdinalIgnoreCase)) return aid;
            }
            throw new ArgumentException($"no alignment named '{name}'; the drawing has {string.Join(", ", names)}");
        }

        private static JObject ProfileReport(string stage, ProfilePlan plan, JObject req)
        {
            bool ok = plan.Errors.Count == 0;
            var report = new JObject
            {
                ["ok"] = ok, ["stage"] = stage,
                ["message"] = ok ? $"{plan.Views.Count} profile view(s) planned over {plan.Views.Select(v => v.AlignmentName).Distinct().Count()} alignment(s)."
                                 : string.Join(" ", plan.Errors),
                ["span"] = plan.Span, ["station_host"] = plan.StationHost,
                ["surface"] = plan.Surface?.Name,
                ["styles"] = plan.Styles?.Json(),
                ["views"] = new JArray(plan.Views.Select(v => new JObject
                {
                    ["name"] = v.Name, ["alignment"] = v.AlignmentName,
                    ["station_start"] = R(v.StationStart), ["station_end"] = R(v.StationEnd),
                    ["elevation_min"] = R(v.ElevationMin), ["elevation_max"] = R(v.ElevationMax),
                    ["insertion"] = new JArray(R(v.Insertion.X), R(v.Insertion.Y)),
                    ["pipes"] = v.Pipes.Count, ["structures"] = v.Structures.Count,
                })),
                ["warnings"] = new JArray(plan.Warnings), ["errors"] = new JArray(plan.Errors),
            };
            if (req["proposed"] is JObject proposed) report["proposed"] = proposed;   // the gate judges what Python planned
            return report;
        }

        // ── Drawing ───────────────────────────────────────────────────────────────

        private static string DrawProfileViews(JObject req, Document doc)
        {
            bool apply = B(req["apply"], false);
            string stage = apply ? "draw" : "compute";
            Database db = doc.Database;
            if (apply && B(req["skip_failures"], false)) return DrawProfileViewsPerAlignment(req, doc);
            using (doc.LockDocument())
            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                ProfilePlan plan;
                try { plan = PlanProfileViews(tr, db, req); }
                catch (SysException ex)
                {
                    tr.Abort();
                    return new JObject { ["ok"] = false, ["stage"] = stage, ["message"] = ex.Message, ["errors"] = new JArray(ex.Message) }.ToString();
                }
                var report = ProfileReport(stage, plan, req);
                if (plan.Errors.Count > 0 || !apply)
                {
                    tr.Abort();
                    return report.ToString();
                }
                try
                {
                    plan.Deferred = true;
                    var drawn = DrawViews(tr, db, plan);
                    tr.Commit();
                    foreach (var p in drawn.Properties()) report[p.Name] = p.Value;
                    report["warnings"] = new JArray(plan.Warnings);
                    var ordered = plan.Warnings.Where(w => w.Contains("dragged") || w.Contains("dimension")).Concat(
                                  plan.Warnings.Where(w => !(w.Contains("dragged") || w.Contains("dimension")))).ToList();
                    report["message"] = $"Drew {plan.Views.Count} profile view(s): {drawn["labels_total"]} label(s), " +
                                        $"{drawn["parts_total"]} part(s) shown." +
                                        (ordered.Count == 0 ? "" : $" {ordered.Count} warning(s): " +
                                         string.Join(" | ", ordered.Take(3)) + (ordered.Count > 3 ? " | ..." : ""));
                    return report.ToString();
                }
                catch (SysException ex)
                {
                    tr.Abort();
                    report["ok"] = false;
                    report["message"] = $"drawing the profile views failed, nothing was kept: {ex.Message}"
                                        + (string.IsNullOrEmpty(_drawStage) ? "" : $" -- while {_drawStage}");
                    report["failed_at"] = _drawStage;
                    report["errors"] = new JArray(report["message"]);
                    return report.ToString();
                }
            }
        }

        /// <summary>`skip_failures`: one transaction per alignment, in the order the views
        /// come (alphabetical from the planner); an alignment whose draw throws is rolled back
        /// and reported under `skipped` while the others are kept.</summary>
        private static string DrawProfileViewsPerAlignment(JObject req, Document doc)
        {
            Database db = doc.Database;
            var allViews = req["views"] as JArray ?? new JArray();
            var order = new List<string>();
            foreach (JObject v in allViews.OfType<JObject>())
            {
                string a = S(v["alignment"], "");
                if (!order.Contains(a)) order.Add(a);
            }
            JObject report = null;
            var views = new JArray(); var profiles = new JArray(); var restationed = new JArray();
            var created = new JArray(); var warnings = new List<string>(); var skipped = new JArray();
            var drewFor = new List<string>();
            int labels = 0, parts = 0;
            using (doc.LockDocument())
            {
                foreach (string a in order)
                {
                    var sub = (JObject)req.DeepClone();
                    sub["views"] = new JArray(allViews.OfType<JObject>().Where(v => S(v["alignment"], "") == a));
                    using (Transaction tr = db.TransactionManager.StartTransaction())
                    {
                        ProfilePlan plan;
                        try { plan = PlanProfileViews(tr, db, sub); }
                        catch (SysException ex)
                        {
                            tr.Abort();
                            skipped.Add(new JObject { ["alignment"] = a, ["reason"] = ex.Message });
                            continue;
                        }
                        if (report == null) report = ProfileReport("draw", plan, req);
                        if (plan.Errors.Count > 0)
                        {
                            tr.Abort();
                            skipped.Add(new JObject { ["alignment"] = a, ["reason"] = string.Join(" | ", plan.Errors) });
                            continue;
                        }
                        try
                        {
                            plan.Deferred = true;
                            var drawn = DrawViews(tr, db, plan);
                            tr.Commit();
                            foreach (var t in drawn["views"] as JArray ?? new JArray()) views.Add(t);
                            foreach (var t in drawn["profiles"] as JArray ?? new JArray()) profiles.Add(t);
                            foreach (var t in drawn["restationed"] as JArray ?? new JArray()) restationed.Add(t);
                            foreach (var t in drawn["created"] as JArray ?? new JArray()) created.Add(t);
                            labels += I(drawn["labels_total"]); parts += I(drawn["parts_total"]);
                            warnings.AddRange(plan.Warnings);
                            drewFor.Add(a);
                        }
                        catch (SysException ex)
                        {
                            tr.Abort();
                            skipped.Add(new JObject
                            {
                                ["alignment"] = a,
                                ["reason"] = ex.Message + (string.IsNullOrEmpty(_drawStage) ? "" : $" -- while {_drawStage}"),
                            });
                        }
                    }
                }
            }
            report = report ?? new JObject { ["stage"] = "draw" };
            report["ok"] = views.Count > 0;
            report["views"] = views; report["profiles"] = profiles; report["restationed"] = restationed;
            report["created"] = created; report["labels_total"] = labels; report["parts_total"] = parts;
            report["skipped"] = skipped; report["warnings"] = new JArray(warnings);
            string skippedWords = skipped.Count == 0 ? "" :
                " Skipped " + string.Join("; ", skipped.OfType<JObject>().Select(x => $"{S(x["alignment"])} ({S(x["reason"])})")) + ".";
            report["message"] = views.Count == 0
                ? "no profile view was drawn." + skippedWords
                : $"Drew {views.Count} profile view(s) for {string.Join(", ", drewFor)}: {parts} part(s) shown." + skippedWords
                  + (warnings.Count == 0 ? "" : $" {warnings.Count} warning(s): " + string.Join(" | ", warnings.Take(3)) + (warnings.Count > 3 ? " | ..." : ""));
            if (views.Count == 0) report["errors"] = new JArray(report["message"]);
            return report.ToString();
        }

        private sealed class AlignmentWork
        {
            public Alignment Alignment;
            public ObjectId EgProfile = ObjectId.Null, Flowline = ObjectId.Null;
            public bool EgReused;
            public JObject Json = new JObject();
        }

        /// <summary>What the draw is doing right now, for the failure message: Civil 3D's
        /// "Value does not fall within the expected range" names nothing by itself.</summary>
        [ThreadStatic] private static string _drawStage;

        private static JObject DrawViews(Transaction tr, Database db, ProfilePlan plan)
        {
            _drawStage = "start";
            var civil = CivilDocument.GetCivilDocument(db);
            var created = new List<ObjectId>();
            var restationed = new JArray();
            var profiles = new JArray();
            var viewsOut = new JArray();
            var work = new Dictionary<ObjectId, AlignmentWork>();
            int labelsTotal = 0, partsTotal = 0;

            // per alignment: restation, the EG profile, the flowline profile
            foreach (var group in plan.Views.GroupBy(v => v.AlignmentId))
            {
                var align = (Alignment)tr.GetObject(group.Key, OpenMode.ForWrite);
                var w = new AlignmentWork { Alignment = align };
                work[group.Key] = w;
                _drawStage = $"restationing {align.Name}";
                if (plan.Restation && Math.Abs(align.StartingStation - plan.RestationTo) > 1e-6)
                {
                    double from = align.StartingStation;
                    if (SafeInt(() => align.GetProfileIds().Count) > 0)
                        plan.Warnings.Add($"{align.Name} already had profiles before it was restationed from {from:0.##} to {plan.RestationTo:0.##}; check their stations");
                    align.ReferencePointStation = align.ReferencePointStation + (plan.RestationTo - from);
                    restationed.Add(new JObject { ["alignment"] = align.Name, ["from_start"] = R(from), ["to_start"] = R(align.StartingStation), ["shift"] = R(plan.RestationTo - from) });
                }
                _drawStage = $"the surface profile of {align.Name} from '{plan.Surface?.Name}'";
                var (egId, reused) = EnsureEgProfile(tr, db, align, plan);
                w.EgProfile = egId; w.EgReused = reused;
                if (!reused) created.Add(egId);
                var pj = new JObject { ["alignment"] = align.Name, ["eg"] = new JObject { ["handle"] = egId.Handle.ToString(), ["reused"] = reused } };
                if (plan.StationHost == "flowline")
                {
                    var allPipes = group.SelectMany(v => v.Pipes).Distinct().ToList();
                    _drawStage = $"the flowline profile of {align.Name} ({allPipes.Count} pipes)";
                    var (flId, flReused, flNote) = EnsureFlowlineProfile(tr, db, align, allPipes, plan);
                    w.Flowline = flId;
                    if (!flId.IsNull && !flReused) created.Add(flId);
                    if (flNote != null) plan.Warnings.Add(flNote);
                    pj["flowline"] = flId.IsNull ? null : new JObject { ["handle"] = flId.Handle.ToString(), ["reused"] = flReused };
                }
                profiles.Add(pj);
            }

            foreach (var v in plan.Views)
            {
                var w = work[v.AlignmentId];
                _drawStage = $"creating view {v.Name} at ({v.Insertion.X:0.#}, {v.Insertion.Y:0.#})";
                ObjectId pvId = ProfileView.Create(v.AlignmentId, v.Insertion, v.Name, plan.Styles.BandSet.Id, plan.Styles.ProfileView.Id);
                created.Add(pvId);
                var pv = (ProfileView)tr.GetObject(pvId, OpenMode.ForWrite);
                _drawStage = $"the station range {v.StationStart:0.##}-{v.StationEnd:0.##} / elevations {v.ElevationMin:0.##}-{v.ElevationMax:0.##} of {v.Name} (alignment {w.Alignment.StartingStation:0.##}-{w.Alignment.EndingStation:0.##})";
                SetRanges(pv, v, w.Alignment, plan.Warnings);
                _drawStage = $"the bands of {v.Name}";
                SetBands(pv, w.EgProfile, w.Flowline.IsNull ? w.EgProfile : w.Flowline, plan.Warnings, v.Name);

                _drawStage = $"adding {v.Pipes.Count} pipes and {v.Structures.Count} structures to {v.Name}";
                var partMap = AddPartsToView(tr, db, pv, v.Pipes.Concat(v.Structures).ToList(), plan.Warnings, v.Name);
                partsTotal += partMap.Count;
                _drawStage = $"the dimension line of {v.Name}";
                var dims = DrawDimLines(tr, db, pv, v, w.Alignment, plan, created, out double dimHeight);
                // deferred: the labels are created by the placement pass, on the view where it
                // finally stands, and anchored at once -- nothing is labelled before the move
                _drawStage = $"the labels of {v.Name}";
                var labels = plan.Deferred
                    ? new JObject { ["total"] = 0, ["deferred"] = true }
                    : LabelView(tr, db, pv, v, w, partMap, plan, created, dims, dimHeight);
                labels["dim_lines"] = dims.Count;
                labelsTotal += I(labels["total"]);
                _drawStage = $"reporting {v.Name}";

                var vj = ProfileViewJson(pv);
                vj["alignment"] = v.AlignmentName;
                vj["vertical_exaggeration"] = plan.Catalogue.Exaggeration.TryGetValue(plan.Styles.ProfileView.Id, out double ve) ? (double?)ve : null;
                vj["parts"] = new JObject { ["pipes"] = v.Pipes.Count(p => partMap.ContainsKey(p)), ["structures"] = v.Structures.Count(p => partMap.ContainsKey(p)) };
                vj["labels"] = labels;
                viewsOut.Add(vj);
            }

            return new JObject
            {
                ["created"] = Handles(created), ["restationed"] = restationed, ["profiles"] = profiles,
                ["views"] = viewsOut, ["labels_total"] = labelsTotal, ["parts_total"] = partsTotal,
            };
        }

        private static (ObjectId id, bool reused) EnsureEgProfile(Transaction tr, Database db, Alignment align, ProfilePlan plan)
        {
            foreach (ObjectId pid in align.GetProfileIds())
            {
                try
                {
                    var p = tr.GetObject(pid, OpenMode.ForRead) as Profile;
                    if (p == null) continue;
                    bool eg = false;
                    try { eg = p.ProfileType == ProfileType.EG; } catch { }
                    bool same = false;
                    try { same = p.DataSourceId == plan.Surface.ObjectId; } catch { }
                    if (!same) try { same = string.Equals(p.DataSourceName, plan.Surface.Name, StringComparison.OrdinalIgnoreCase); } catch { }
                    if (eg && same) return (pid, true);
                }
                catch { }
            }
            string name = UniqueProfileName(tr, align, $"{align.Name} EG");
            ObjectId id = Profile.CreateFromSurface(name, align.ObjectId, plan.Surface.ObjectId, align.LayerId,
                                                    plan.Styles.EgProfile.Id, plan.Styles.EgLabelSet.Id);
            return (id, false);
        }

        /// <summary>A profile by layout through the pipe inverts (a PVI at every pipe end,
        /// the lowest invert winning where ends share a station), so station labels on it
        /// read the flowline elevation. Reused when one of that name already exists.</summary>
        private static (ObjectId id, bool reused, string note) EnsureFlowlineProfile(Transaction tr, Database db, Alignment align,
                                                                                     List<ObjectId> pipes, ProfilePlan plan)
        {
            string name = $"{align.Name} FL";
            foreach (ObjectId pid in align.GetProfileIds())
            {
                try
                {
                    var p = tr.GetObject(pid, OpenMode.ForRead) as Profile;
                    if (p != null && p.Name.Equals(name, StringComparison.OrdinalIgnoreCase))
                        return (pid, true, $"{align.Name}: flowline profile '{name}' already existed and was reused; its PVIs were not rebuilt");
                }
                catch { }
            }
            var pvis = new SortedDictionary<double, double>();
            double s0 = align.StartingStation, s1 = align.EndingStation;
            foreach (ObjectId pid in pipes)
            {
                var pipe = tr.GetObject(pid, OpenMode.ForRead) as Pipe;
                if (pipe == null) continue;
                double dia = SafeNum(() => pipe.InnerDiameterOrWidth) ?? 0.0;
                foreach (var (pt, z) in new[] { (pipe.StartPoint, pipe.StartPoint.Z - dia / 2.0), (pipe.EndPoint, pipe.EndPoint.Z - dia / 2.0) })
                {
                    var at = StationOf(align, pt);
                    if (at == null) continue;
                    // rounded THEN clamped: rounding after the clamp put an end vertex a hair past
                    // the alignment's end (1624.0999 -> 1624.1), which Civil 3D refuses
                    double st = Math.Max(s0, Math.Min(s1, Math.Round(at.Value.st, 2)));
                    // ends within 0.05 ft of a PVI already there share it (two pipes into one
                    // manhole rarely meet to the hundredth); Civil 3D refuses PVIs that close
                    double near = pvis.Keys.FirstOrDefault(k => Math.Abs(k - st) < 0.05 && Math.Abs(k - st) > 1e-9);
                    if (pvis.Count > 0 && pvis.ContainsKey(near) && Math.Abs(near - st) < 0.05) st = near;
                    if (!pvis.TryGetValue(st, out double have) || z < have) pvis[st] = z;
                }
            }
            if (pvis.Count < 2)
                return (ObjectId.Null, false, $"{align.Name}: fewer than two pipe ends on the alignment, no flowline profile; station labels go on the EG profile");
            ObjectId id = Profile.CreateByLayout(name, align.ObjectId, align.LayerId, plan.Styles.FlowlineProfile.Id, plan.Styles.FlowlineLabelSet.Id);
            var prof = (Profile)tr.GetObject(id, OpenMode.ForWrite);
            foreach (var kv in pvis)
            {
                _drawStage = $"the flowline profile of {align.Name}: PVI at station {kv.Key:0.##}, elevation {kv.Value:0.##} (alignment {s0:0.##}-{s1:0.##}, {pvis.Count} PVIs)";
                try { prof.PVIs.AddPVI(kv.Key, kv.Value); }
                catch (SysException) when (Math.Abs(kv.Key - s0) < 0.05 || Math.Abs(kv.Key - s1) < 0.05)
                {
                    // a vertex on the very end of the alignment: a hundredth inside is accepted
                    double inside = Math.Abs(kv.Key - s0) < 0.05 ? s0 + 0.01 : s1 - 0.01;
                    prof.PVIs.AddPVI(inside, kv.Value);
                }
            }
            return (id, false, null);
        }

        private static string UniqueProfileName(Transaction tr, Alignment align, string name)
        {
            var taken = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (ObjectId pid in align.GetProfileIds())
                try { taken.Add(((Profile)tr.GetObject(pid, OpenMode.ForRead)).Name); } catch { }
            string candidate = name;
            for (int i = 2; taken.Contains(candidate); i++) candidate = $"{name} ({i})";
            return candidate;
        }

        private static void SetRanges(ProfileView pv, ViewPlan v, Alignment align = null, List<string> warnings = null)
        {
            if (align != null)
            {
                // a window past the alignment's ends is refused by Civil 3D ("Value does not fall
                // within the expected range"): clamp it and say so
                double a0 = align.StartingStation, a1 = align.EndingStation;
                if (v.StationStart < a0 - 1e-6 || v.StationEnd > a1 + 1e-6)
                {
                    warnings?.Add($"{v.Name}: station window {v.StationStart:0.##}-{v.StationEnd:0.##} clamped to the alignment's {a0:0.##}-{a1:0.##}");
                    v.StationStart = Math.Max(a0, v.StationStart);
                    v.StationEnd = Math.Min(a1, v.StationEnd);
                }
            }
            pv.StationRangeMode = StationRangeType.UserSpecified;
            // widen first so a start past the current end (or an end before the current start) never throws
            if (v.StationEnd > pv.StationEnd) { pv.StationEnd = v.StationEnd; pv.StationStart = v.StationStart; }
            else { pv.StationStart = v.StationStart; pv.StationEnd = v.StationEnd; }
            pv.ElevationRangeMode = ElevationRangeType.UserSpecified;
            if (v.ElevationMax > pv.ElevationMax) { pv.ElevationMax = v.ElevationMax; pv.ElevationMin = v.ElevationMin; }
            else { pv.ElevationMin = v.ElevationMin; pv.ElevationMax = v.ElevationMax; }
        }

        /// <summary>Band items are copies: point profile 1 at the EG and profile 2 at the flowline
        /// (or EG again), then write the collection back.</summary>
        private static void SetBands(ProfileView pv, ObjectId profile1, ObjectId profile2, List<string> warnings, string viewName)
        {
            try
            {
                var bottom = pv.Bands.GetBottomBandItems();
                foreach (ProfileViewBandItem item in bottom)
                {
                    try { item.Profile1Id = profile1; } catch { }
                    try { item.Profile2Id = profile2; } catch { }
                }
                pv.Bands.SetBottomBandItems(bottom);
                var top = pv.Bands.GetTopBandItems();
                foreach (ProfileViewBandItem item in top)
                {
                    try { item.Profile1Id = profile1; } catch { }
                    try { item.Profile2Id = profile2; } catch { }
                }
                pv.Bands.SetTopBandItems(top);
            }
            catch (SysException ex) { warnings.Add($"{viewName}: band profiles not set ({ex.Message}); set them in Profile View Properties"); }
        }

        /// <summary>Adds parts to the view and returns part -> its ProfileViewPart in this view,
        /// found by diffing model space around the call (Part.ProfileViewPartId is one id and
        /// is ambiguous for a pipe shown in two views).</summary>
        private static Dictionary<ObjectId, ObjectId> AddPartsToView(Transaction tr, Database db, ProfileView pv, List<ObjectId> parts,
                                                                     List<string> warnings, string viewName)
        {
            var map = new Dictionary<ObjectId, ObjectId>();
            if (parts.Count == 0) return map;
            var before = ModelSpaceIds(tr, db);
            var wanted = new HashSet<ObjectId>(parts);
            foreach (ObjectId pid in parts)
            {
                try
                {
                    var part = (Part)tr.GetObject(pid, OpenMode.ForWrite);
                    part.AddToProfileView(pv.ObjectId);
                }
                catch (SysException ex) { warnings.Add($"{viewName}: part {pid.Handle} not added ({ex.Message})"); }
            }
            foreach (ObjectId id in ModelSpaceIds(tr, db))
            {
                if (before.Contains(id)) continue;
                try
                {
                    var pvp = tr.GetObject(id, OpenMode.ForRead) as ProfileViewPart;
                    if (pvp != null && wanted.Contains(pvp.ModelPartId) && !map.ContainsKey(pvp.ModelPartId)) map[pvp.ModelPartId] = id;
                }
                catch { }
            }
            foreach (ObjectId pid in parts)
            {
                if (map.ContainsKey(pid)) continue;
                try
                {
                    var part = (Part)tr.GetObject(pid, OpenMode.ForRead);
                    ObjectId single = part.ProfileViewPartId;
                    if (!single.IsNull) map[pid] = single;
                }
                catch { }
            }
            return map;
        }

        private static HashSet<ObjectId> ModelSpaceIds(Transaction tr, Database db)
        {
            var set = new HashSet<ObjectId>();
            foreach (ObjectId id in ModelSpace(tr, db, false)) set.Add(id);
            return set;
        }

        // ── Labels ────────────────────────────────────────────────────────────────
        //
        // Two rows inside the view. Bottom, just above the datum line: the station
        // labels every 1+00 (one label group; its extents reserve the row), then a
        // structure label per manhole, then the pipe labels. Top, just under the top
        // line: the second structure label ("MH —> Alignment"). Every label is dragged
        // to its row and then settled against the labels already placed there -- a
        // label whose text would overlap another is moved further into the view --
        // so two pipes over the same run get their labels one above the other.

        private sealed class Rows
        {
            public List<Extents3d> Bottom = new List<Extents3d>(), Top = new List<Extents3d>();
        }

        private static void ReadAnchors(ProfilePlan plan, JObject anchor)
        {
            if (anchor == null) return;
            plan.AnchorTop = D(anchor["top"], -3.0);
            plan.AnchorBottom = D(anchor["bottom"], 0.75);
            plan.AnchorStation = D(anchor["station"], 0.5);
            plan.AnchorPipe = D(anchor["pipe"], 0.2);
            plan.AnchorPipeRow = D(anchor["pipe_row"], 0.35);
            plan.AnchorPipeStation = B(anchor["pipe_station"], false);
            plan.AnchorAtCreation = B(anchor["at_creation"], false);
        }

        /// <summary>Places a structure profile label by its dimension anchor: hung from the
        /// graph view's top line (top) or stood on its bottom line, `value` plotted inches
        /// from that line. This is the grip the drafter moves on these labels; dragging does
        /// nothing visible to them. False (with _dragWhy) when the label has no anchors.</summary>
        private static bool AnchorLabel(CivilLabel lbl, bool top, double value)
        {
            _dragWhy = null;
            try
            {
                bool allows = true;
                try { allows = lbl.AllowsDimensionAnchors; } catch { }
                if (!allows) { _dragWhy = "the style has no dimension anchor"; return false; }
                // the per-label pair (what the Properties panel shows as "Dimension Anchor Option /
                // Value"); LabelBase.DefaultDimensionAnchorOption/Value is the label GROUP default and
                // overflowed Civil 3D's stack when written on a single structure label
                lbl.DimensionAnchorOption = top ? Autodesk.Civil.DimensionAnchorOptionType.ViewTop
                                                : Autodesk.Civil.DimensionAnchorOptionType.ViewBottom;
                // the spec speaks plotted inches; the API takes feet (live 2026-09-17: -3.0 drew a 36 in tail)
                lbl.DimensionAnchorValue = value / 12.0;
                return true;
            }
            catch (SysException ex) { _dragWhy = "dimension anchor: " + ex.Message; return false; }
        }

        /// <summary>Creates a structure profile label with the drawing's default dimension anchor
        /// for structures (Pipe Network feature settings > Default Profile Label Placement) set to
        /// the graph view top or bottom + plotted inches for the duration of the Create, then put
        /// back. The label takes the default at birth; nothing on the label itself is written.</summary>
        private static ObjectId CreateStructureLabel(Database db, ProfilePlan plan, ObjectId pvId, ObjectId pvPartId, ObjectId styleId, bool top)
        {
            if (!plan.AnchorAtCreation) return StructureProfileLabel.Create(pvId, pvPartId, styleId);
            SettingsPipeNetwork.SettingsProfileLabelPlacement place = null;
            Autodesk.Civil.DimensionAnchorType oldOption = Autodesk.Civil.DimensionAnchorType.GraphViewBottom;
            double oldValue = 0; bool saved = false;
            try
            {
                place = CivilDocument.GetCivilDocument(db).Settings.GetSettings<SettingsPipeNetwork>().ProfileLabelPlacement;
                oldOption = place.DimensionAnchorOptionForStructures.Value;
                oldValue = place.DimensionAnchorPlotHeightValueForStructures.Value;
                saved = true;
                place.DimensionAnchorOptionForStructures.Value = top ? Autodesk.Civil.DimensionAnchorType.GraphViewTop
                                                                     : Autodesk.Civil.DimensionAnchorType.GraphViewBottom;
                place.DimensionAnchorPlotHeightValueForStructures.Value = top ? plan.AnchorTop : plan.AnchorBottom;
            }
            catch (SysException ex)
            {
                if (!plan.AnchorSettingWarned)
                    plan.Warnings.Add("structure label anchor default not set (" + ex.Message + "); the styles' own anchors apply");
                plan.AnchorSettingWarned = true;
            }
            try { return StructureProfileLabel.Create(pvId, pvPartId, styleId); }
            finally
            {
                if (saved) try
                {
                    place.DimensionAnchorOptionForStructures.Value = oldOption;
                    place.DimensionAnchorPlotHeightValueForStructures.Value = oldValue;
                }
                catch { }
            }
        }

        /// <summary>The same for one label of a group (a station label): the per-sub-entity
        /// DimensionAnchorOption / Value pair.</summary>
        private static bool AnchorSub(LabelGroupSubEntity se, bool top, double value)
        {
            _dragWhy = null;
            try
            {
                bool allows = true;
                try { allows = se.AllowsDimensionAnchors; } catch { }
                if (!allows) { _dragWhy = "the style has no dimension anchor"; return false; }
                se.DimensionAnchorOption = top ? Autodesk.Civil.DimensionAnchorOptionType.ViewTop
                                               : Autodesk.Civil.DimensionAnchorOptionType.ViewBottom;
                se.DimensionAnchorValue = value / 12.0;
                return true;
            }
            catch (SysException ex) { _dragWhy = "dimension anchor: " + ex.Message; return false; }
        }

        /// <summary>The x-range of each of the view's pipes along the view (clipped to the view)
        /// and the row each gets when runs overlap (0 = the first), in station order.</summary>
        private static Dictionary<ObjectId, (double x0, double x1, int row)> PipeRuns(Transaction tr, ProfileView pv, ViewPlan v)
        {
            var runs = new Dictionary<ObjectId, (double, double, int)>();
            var align = tr.GetObject(v.AlignmentId, OpenMode.ForRead) as Alignment;
            if (align == null) return runs;
            var order = new List<(ObjectId id, double s0, double s1)>();
            foreach (ObjectId pid in v.Pipes)
            {
                var pipe = tr.GetObject(pid, OpenMode.ForRead) as Pipe;
                if (pipe == null) continue;
                var a = StationOf(align, pipe.StartPoint); var b = StationOf(align, pipe.EndPoint);
                if (a == null || b == null) continue;
                double s0 = Math.Max(v.StationStart, Math.Min(a.Value.st, b.Value.st));
                double s1 = Math.Min(v.StationEnd, Math.Max(a.Value.st, b.Value.st));
                if (s1 - s0 < 1.0) continue;
                order.Add((pid, s0, s1));
            }
            var rows = new List<List<(double a, double b)>>();
            foreach (var (pid, s0, s1) in order.OrderBy(o => o.s0).ThenBy(o => o.s1))
            {
                double x0 = 0, y0 = 0, x1 = 0, y1 = 0;
                if (!pv.FindXYAtStationAndElevation(s0, v.ElevationMin, ref x0, ref y0) ||
                    !pv.FindXYAtStationAndElevation(s1, v.ElevationMin, ref x1, ref y1)) continue;
                if (x1 < x0) { double t = x0; x0 = x1; x1 = t; }
                int row = 0;
                while (row < rows.Count && rows[row].Any(iv => x0 < iv.b - 1e-6 && x1 > iv.a + 1e-6)) row++;
                if (row == rows.Count) rows.Add(new List<(double, double)>());
                rows[row].Add((x0, x1));
                runs[pid] = (x0, x1, row);
            }
            return runs;
        }

        /// <summary>The one dimension line of a view: its whole station range along the datum,
        /// `lift` above it.</summary>
        private static bool ViewSpan(ProfileView pv, ViewPlan v, double lift, out double x0, out double x1, out double y)
        {
            double xs = 0, ys = 0, xe = 0, ye = 0;
            x0 = x1 = y = 0;
            if (!pv.FindXYAtStationAndElevation(v.StationStart, v.ElevationMin, ref xs, ref ys) ||
                !pv.FindXYAtStationAndElevation(v.StationEnd, v.ElevationMin, ref xe, ref ye)) return false;
            x0 = Math.Min(xs, xe); x1 = Math.Max(xs, xe); y = ys + lift;
            return true;
        }

        private static void ReadDimLines(ProfilePlan plan, JObject dim)
        {
            if (dim == null) return;
            plan.DimBlock = S(dim["block"], "").Trim();
            plan.DimSource = S(dim["source_file"], "");
            plan.DimLayer = S(dim["layer"], "");
            plan.DimProperty = S(dim["property"], "Distance1");
            plan.DimRow = Math.Max(1.0, D(dim["row_height"], 12.0));
            plan.DimLift = D(dim["lift"], 4.0);
        }

        private sealed class DimLine { public double X0, X1, Y; public int Row; }

        /// <summary>The dimension-line block between manholes: one per pipe in the view,
        /// from the pipe's start to its end (cut at the view's edges), just above the datum
        /// line; pipes that share a run stack into rows. The block's geometry is stretched in
        /// X from its own width to the span. Returns pipe -> its line, and the height the
        /// rows take, so the labels can sit above them.</summary>
        private static Dictionary<ObjectId, DimLine> DrawDimLines(Transaction tr, Database db, ProfileView pv, ViewPlan v, Alignment align,
                                                                  ProfilePlan plan, List<ObjectId> created, out double height)
        {
            var map = new Dictionary<ObjectId, DimLine>();
            height = 0;
            if (string.IsNullOrWhiteSpace(plan.DimBlock) || v.Pipes.Count == 0) return map;
            // deferred: the placement pass (drag_profile_labels) inserts and stretches the lines
            // once the views sit where they will stay, so nothing is left behind by a move
            if (plan.Deferred) return map;
            ObjectId btrId;
            try { btrId = ResolveBlockDefinition(tr, db, plan.DimBlock, string.IsNullOrWhiteSpace(plan.DimSource) ? null : plan.DimSource, null); }
            catch (SysException ex)
            {
                plan.Warnings.Add($"{v.Name}: dimension-line block '{plan.DimBlock}' not available ({ex.Message}); no dimension lines drawn");
                plan.DimBlock = "";
                return map;
            }
            bool spanKnown = BlockDefinitionSpanX(tr, btrId, out double defMin, out double defMax);
            double defWidth = spanKnown ? defMax - defMin : 0.0;
            if (!spanKnown) plan.Warnings.Add($"{v.Name}: block '{plan.DimBlock}' has no width to stretch; inserted unscaled at the run's start");
            string dynamicProperty = null;
            var space = ModelSpace(tr, db);
            string layer = string.IsNullOrWhiteSpace(plan.DimLayer) ? null : EnsureLayer(tr, db, plan.DimLayer);
            // ONE line per view, its whole station range along the datum; every pipe seats on it
            if (ViewSpan(pv, v, plan.DimLift, out double x0, out double x1, out double y))
            {
                try
                {
                    var br = new BlockReference(new Point3d(x0, y, 0), btrId);
                    ObjectId id = Append(tr, space, br, layer);
                    AdjustDimLine(br, x0, x1, y, plan, spanKnown, defMin, defWidth, ref dynamicProperty);
                    created.Add(id);
                    var line = new DimLine { X0 = x0, X1 = x1, Y = y, Row = 0 };
                    foreach (ObjectId pid in v.Pipes) map[pid] = line;
                }
                catch (SysException ex) { plan.Warnings.Add($"{v.Name}: dimension line failed ({ex.Message})"); }
            }
            height = plan.DimRow;
            if (dynamicProperty != null && !plan.Warnings.Any(w => w.Contains("dynamic property")))
                plan.Warnings.Add($"dimension lines: dynamic property '{dynamicProperty}' of '{plan.DimBlock}' set to each span");
            return map;
        }

        /// <summary>Stretches one dimension-line reference from x0 to x1: the dynamic grip
        /// (`dim_line.property`) set to the span and the geometry shifted so its left extent is
        /// on x0; a plain block scaled between its own extents instead.</summary>
        private static void AdjustDimLine(BlockReference br, double x0, double x1, double y, ProfilePlan plan,
                                          bool spanKnown, double defMin, double defWidth, ref string dynamicProperty)
        {
            if (StretchDynamic(br, x1 - x0, plan.DimProperty, out string prop))
            {
                if (dynamicProperty == null) dynamicProperty = prop;
                try
                {
                    var e = br.GeometricExtents;
                    double shift = x0 - e.MinPoint.X;
                    if (Math.Abs(shift) > 1e-3) br.TransformBy(Matrix3d.Displacement(new Vector3d(shift, 0, 0)));
                }
                catch { }
            }
            else
            {
                double sx = spanKnown ? (x1 - x0) / defWidth : 1.0;
                br.ScaleFactors = new Scale3d(sx, 1.0, 1.0);
                if (spanKnown) br.Position = new Point3d(x0 - defMin * sx, y, 0);
            }
        }

        /// <summary>The placement pass's dimension lines: one reference of the block per pipe
        /// run the view shows, inserted now at the view's current place (the draw inserts none)
        /// unless one already sits there, then stretched to the run's end. Runs and rows are
        /// recomputed from the pipes the view shows.</summary>
        private static int AdjustDimLinesIn(Transaction tr, Database db, ProfileView pv, ViewPlan v, ProfilePlan plan)
        {
            if (string.IsNullOrWhiteSpace(plan.DimBlock)) return 0;
            ObjectId btrId;
            try { btrId = ResolveBlockDefinition(tr, db, plan.DimBlock, string.IsNullOrWhiteSpace(plan.DimSource) ? null : plan.DimSource, null); }
            catch (SysException ex)
            {
                if (!plan.Warnings.Any(w => w.Contains("dimension-line block")))
                    plan.Warnings.Add($"dimension-line block '{plan.DimBlock}' not available ({ex.Message}); no dimension lines drawn");
                return 0;
            }
            BlockTableRecord space = null;
            string layer = string.IsNullOrWhiteSpace(plan.DimLayer) ? null : EnsureLayer(tr, db, plan.DimLayer);
            bool spanKnown = BlockDefinitionSpanX(tr, btrId, out double defMin, out double defMax);
            double defWidth = spanKnown ? defMax - defMin : 0.0;
            // ONE line per view: its whole station range along the datum
            var targets = new List<(double x0, double x1, double y)>();
            if (ViewSpan(pv, v, plan.DimLift, out double sx0, out double sx1, out double sy)) targets.Add((sx0, sx1, sy));
            // the references, by their base point
            var refs = new List<BlockReference>();
            foreach (ObjectId id in ModelSpace(tr, db, false))
            {
                try
                {
                    var br = tr.GetObject(id, OpenMode.ForRead) as BlockReference;
                    if (br == null || !EffectiveBlockName(tr, br).Equals(plan.DimBlock, StringComparison.OrdinalIgnoreCase)) continue;
                    refs.Add(br);
                }
                catch { }
            }
            int adjusted = 0, inserted = 0;
            string dynamicProperty = null;
            foreach (var (x0, x1, y) in targets)
            {
                // already placed on an earlier pass: a stretched reference spanning this run
                if (refs.Any(r => SpansRun(r, x0, x1, y, plan.DimRow * 0.5))) continue;
                var br = refs.FirstOrDefault(r => Math.Abs(r.Position.X - x0) < 0.5 && Math.Abs(r.Position.Y - y) < 0.5
                                                  && Math.Abs(r.ScaleFactors.X - 1.0) < 1e-9);
                try
                {
                    if (br == null)
                    {
                        space = space ?? ModelSpace(tr, db);
                        br = new BlockReference(new Point3d(x0, y, 0), btrId);
                        Append(tr, space, br, layer);
                        inserted++;
                    }
                    else br.UpgradeOpen();
                    AdjustDimLine(br, x0, x1, y, plan, spanKnown, defMin, defWidth, ref dynamicProperty);
                    adjusted++;
                }
                catch (SysException ex) { plan.Warnings.Add($"{v.Name}: dimension line at {x0:0.#} not placed ({ex.Message})"); }
            }
            if (dynamicProperty != null && !plan.Warnings.Any(w => w.Contains("dynamic property")))
                plan.Warnings.Add($"dimension lines: dynamic property '{dynamicProperty}' of '{plan.DimBlock}' set to each span");
            return adjusted;
        }

        /// <summary>Inserts the view's dimension-line block, unstretched, at the start of its
        /// span -- unless a reference of the block already sits there or already spans the view.</summary>
        private static void InsertDimLineIn(Transaction tr, Database db, ProfileView pv, ViewPlan v, ProfilePlan plan, List<ObjectId> created)
        {
            if (string.IsNullOrWhiteSpace(plan.DimBlock)) return;
            if (!ViewSpan(pv, v, plan.DimLift, out double x0, out double x1, out double y)) return;
            ObjectId btrId;
            try { btrId = ResolveBlockDefinition(tr, db, plan.DimBlock, string.IsNullOrWhiteSpace(plan.DimSource) ? null : plan.DimSource, null); }
            catch (SysException ex)
            {
                if (!plan.Warnings.Any(w => w.Contains("dimension-line block")))
                    plan.Warnings.Add($"dimension-line block '{plan.DimBlock}' not available ({ex.Message}); no dimension lines drawn");
                return;
            }
            foreach (ObjectId id in ModelSpace(tr, db, false))
            {
                try
                {
                    var br = tr.GetObject(id, OpenMode.ForRead) as BlockReference;
                    if (br == null || !EffectiveBlockName(tr, br).Equals(plan.DimBlock, StringComparison.OrdinalIgnoreCase)) continue;
                    if (SpansRun(br, x0, x1, y, plan.DimRow * 0.5)) return;
                    if (Math.Abs(br.Position.X - x0) < 0.5 && Math.Abs(br.Position.Y - y) < 0.5) return;
                }
                catch { }
            }
            try
            {
                string layer = string.IsNullOrWhiteSpace(plan.DimLayer) ? null : EnsureLayer(tr, db, plan.DimLayer);
                var nb = new BlockReference(new Point3d(x0, y, 0), btrId);
                created.Add(Append(tr, ModelSpace(tr, db), nb, layer));
            }
            catch (SysException ex) { plan.Warnings.Add($"{v.Name}: dimension line not inserted ({ex.Message})"); }
        }

        private static bool SpansRun(BlockReference r, double x0, double x1, double y, double yTol)
        {
            try
            {
                var e = r.GeometricExtents;
                return Math.Abs(e.MinPoint.X - x0) < 0.5 && Math.Abs(e.MaxPoint.X - x1) < 0.5 && Math.Abs(e.MaxPoint.Y - y) < yTol;
            }
            catch { return false; }
        }

        private static JObject LabelView(Transaction tr, Database db, ProfileView pv, ViewPlan v, AlignmentWork w,
                                         Dictionary<ObjectId, ObjectId> partMap, ProfilePlan plan, List<ObjectId> created,
                                         Dictionary<ObjectId, DimLine> dims = null, double dimHeight = 0)
        {
            int pipeLabels = 0, structLabels = 0, structAlignLabels = 0, stationLabels = 0, dragged = 0;
            var styles = plan.Styles;
            var rows = new Rows();
            double gap = Math.Max(0.5, plan.Lift * 0.5);
            dims = dims ?? new Dictionary<ObjectId, DimLine>();
            // the rows of dimension lines take the bottom; everything else starts above them
            double rowLift = plan.Lift + (dimHeight > 0 ? dimHeight + plan.DimLift : 0);

            // station labels first: they take the lowest row
            ObjectId host = plan.StationHost == "flowline" && !w.Flowline.IsNull ? w.Flowline : w.EgProfile;
            if (!styles.StationLabel.Id.IsNull && !host.IsNull)
            {
                try
                {
                    ObjectId gid;
                    try { gid = ProfileStationLabelGroup.Create(pv.ObjectId, host, styles.StationLabel.Id, plan.StationIncrement); }
                    catch (SysException) { gid = ProfileStationLabelGroup.CreateMajor(pv.ObjectId, host, styles.StationLabel.Id, plan.StationIncrement); }
                    created.Add(gid);
                    var group = (ProfileStationLabelGroup)tr.GetObject(gid, OpenMode.ForWrite);
                    try { group.SetRange(v.StationStart, v.StationEnd); } catch { }
                    try
                    {
                        foreach (LabelGroupSubEntity se in group.SubEntities)
                        {
                            stationLabels++;
                            if (!plan.Deferred && plan.DragStation && DragSubEntityToDatum(se, pv, v, rowLift)) dragged++;
                        }
                    }
                    catch (SysException ex) { plan.Warnings.Add($"{v.Name}: station labels created but not dragged ({ex.Message})"); }
                    if (stationLabels == 0) plan.Warnings.Add($"{v.Name}: the station label group has no labels (style '{styles.StationLabel.Name}' on profile {host.Handle})");
                    try { if (!plan.Deferred && plan.DragStation) rows.Bottom.Add(group.GeometricExtents); } catch { }
                }
                catch (SysException ex) { plan.Warnings.Add($"{v.Name}: station labels failed ({ex.Message})"); }
            }
            else if (styles.StationLabel.Id.IsNull) plan.Warnings.Add($"{v.Name}: no major station label style in the drawing; station labels skipped");

            var notDragged = new List<string>();
            void Note(string what) { if (_dragWhy != null && notDragged.Count < 3) notDragged.Add($"{what}: {_dragWhy}"); }
            bool secondStructure = !styles.StructureAlignmentLabel.Id.IsNull && styles.StructureAlignmentLabel.Id != styles.StructureLabel.Id;
            if (!styles.StructureAlignmentLabel.Id.IsNull && !secondStructure)
                plan.Warnings.Add($"{v.Name}: structure_label and structure_alignment_label are the same style ('{styles.StructureLabel.Name}'); one label per manhole");
            foreach (ObjectId sid in v.Structures)
            {
                if (!partMap.TryGetValue(sid, out ObjectId pvp)) continue;
                if (!styles.StructureLabel.Id.IsNull)
                {
                    try
                    {
                        ObjectId lid = CreateStructureLabel(db, plan, pv.ObjectId, pvp, styles.StructureLabel.Id, top: false);
                        created.Add(lid); structLabels++;
                        var lbl = (PartProfileLabel)tr.GetObject(lid, OpenMode.ForWrite);
                        try { lbl.ReferenceAlignmentId = v.AlignmentId; } catch { }
                        if (plan.Deferred) { }
                        else if (AnchorLabel(lbl, false, plan.AnchorBottom)) dragged++;
                        else if (DragToRow(lbl, pv, v, rowLift, bottom: true) && Settle(lbl, rows.Bottom, upward: true, gap)) dragged++;
                        else Note("MH label");
                    }
                    catch (SysException ex) { plan.Warnings.Add($"{v.Name}: structure {sid.Handle} label failed ({ex.Message})"); }
                }
                if (secondStructure)
                {
                    try
                    {
                        ObjectId lid = CreateStructureLabel(db, plan, pv.ObjectId, pvp, styles.StructureAlignmentLabel.Id, top: true);
                        created.Add(lid); structAlignLabels++;
                        var lbl = (PartProfileLabel)tr.GetObject(lid, OpenMode.ForWrite);
                        try { lbl.ReferenceAlignmentId = v.AlignmentId; } catch { }
                        if (plan.Deferred) { }
                        else if (AnchorLabel(lbl, true, plan.AnchorTop)) dragged++;
                        else if (DragToRow(lbl, pv, v, plan.Lift, bottom: false) && Settle(lbl, rows.Top, upward: false, gap)) dragged++;
                        else Note("MH -> Alignment label");
                    }
                    catch (SysException ex) { plan.Warnings.Add($"{v.Name}: structure {sid.Handle} alignment label failed ({ex.Message})"); }
                }
            }

            foreach (ObjectId pid in v.Pipes)
            {
                if (!partMap.TryGetValue(pid, out ObjectId pvp) || styles.PipeLabel.Id.IsNull) continue;
                try
                {
                    ObjectId lid = PipeProfileLabel.Create(pvp, pv.ObjectId, 0.5, styles.PipeLabel.Id);
                    created.Add(lid);
                    pipeLabels++;
                    var lbl = (PipeProfileLabel)tr.GetObject(lid, OpenMode.ForWrite);
                    try { lbl.ReferenceAlignmentId = v.AlignmentId; } catch { }
                    if (plan.DragPipe && !plan.Deferred)
                    {
                        // seated on the pipe's own dimension line when there is one (text centred
                        // on the line, just above it); else the bottom row like everything else
                        if (dims.TryGetValue(pid, out DimLine dl))
                        {
                            if (DragToPoint(lbl, (dl.X0 + dl.X1) / 2.0, dl.Y + Math.Max(0.5, plan.DimLift * 0.25))) dragged++;
                            else Note("pipe label");
                        }
                        else if (DragToRow(lbl, pv, v, rowLift, bottom: true) && Settle(lbl, rows.Bottom, upward: true, gap)) dragged++;
                        else Note("pipe label");
                    }
                }
                catch (SysException ex) { plan.Warnings.Add($"{v.Name}: pipe {pid.Handle} label failed ({ex.Message})"); }
            }
            int wanted = pipeLabels + structLabels + structAlignLabels + stationLabels;
            if (!plan.Deferred && notDragged.Count > 0)
                plan.Warnings.Add($"{v.Name}: {wanted - dragged} of {wanted} label(s) could not be dragged -- " + string.Join(" | ", notDragged));
            return new JObject
            {
                ["pipe"] = pipeLabels, ["structure"] = structLabels, ["structure_alignment"] = structAlignLabels,
                ["station"] = stationLabels, ["dragged"] = dragged, ["not_dragged"] = wanted - dragged,
                ["total"] = wanted,
            };
        }

        /// <summary>Model-space Y of the datum line (bottom) or the top line under the label's anchor.</summary>
        private static double? LineY(ProfileView pv, ViewPlan v, double x, bool bottom)
        {
            try
            {
                double st = 0, el = 0;
                if (!pv.FindStationAndElevationAtXY(x, pv.Location.Y + 1.0, ref st, ref el)) st = v.StationStart;
                st = Math.Max(v.StationStart, Math.Min(v.StationEnd, st));
                double dx = 0, dy = 0;
                if (!pv.FindXYAtStationAndElevation(st, bottom ? v.ElevationMin : v.ElevationMax, ref dx, ref dy)) return null;
                return dy;
            }
            catch { return null; }
        }

        private static double? DatumY(ProfileView pv, ViewPlan v, double x, double lift)
        {
            double? y = LineY(pv, v, x, true);
            return y == null ? (double?)null : y.Value + lift;
        }

        /// <summary>The last reason a drag did not take, for the report.</summary>
        [ThreadStatic] private static string _dragWhy;

        /// <summary>Drags a label so its text sits at (cx, y): the dragged location is set
        /// outright (that is what a grip drag does), the offset is the fallback, and the
        /// extents -- when Civil 3D has them yet -- correct the text's bottom (or top) onto y.
        /// True only when the label reports itself dragged afterwards.</summary>
        private static bool DragLabel(CivilLabel lbl, double? cx, double y, bool bottom)
        {
            _dragWhy = null;
            try
            {
                bool allows = true;
                try { allows = lbl.AllowsDragging; } catch { }
                if (!allows) { _dragWhy = "the style does not allow dragging"; return false; }
                Point3d before = lbl.LabelLocation;
                double x = cx ?? before.X;
                bool set = false;
                try { lbl.DraggedOffset = new Vector3d(x - before.X, y - before.Y, 0); set = true; }
                catch (SysException ex) { _dragWhy = "DraggedOffset: " + ex.Message; }
                if (!set) return false;
                try
                {
                    var ext = lbl.GeometricExtents;
                    double h = ext.MaxPoint.Y - ext.MinPoint.Y, w = ext.MaxPoint.X - ext.MinPoint.X;
                    if (h > 1e-6 && h < 1e5 && w < 1e5)
                    {
                        double dy = bottom ? y - ext.MinPoint.Y : y - ext.MaxPoint.Y;
                        double dx = cx == null ? 0 : cx.Value - (ext.MinPoint.X + ext.MaxPoint.X) / 2.0;
                        if (Math.Abs(dy) > 1e-3 || Math.Abs(dx) > 1e-3) lbl.DraggedOffset = lbl.DraggedOffset + new Vector3d(dx, dy, 0);
                    }
                }
                catch { }
                bool dragged = false;
                try { dragged = lbl.Dragged; } catch { dragged = true; }
                if (!dragged) _dragWhy = "the label does not report itself dragged after the move";
                return dragged;
            }
            catch (SysException ex) { _dragWhy = ex.Message; return false; }
        }

        /// <summary>Drags a label so its text sits `lift` above the datum line (bottom) or
        /// `lift` below the top line, under its own anchor.</summary>
        private static bool DragToRow(CivilLabel lbl, ProfileView pv, ViewPlan v, double lift, bool bottom)
        {
            try
            {
                Point3d anchor = lbl.LabelLocation;
                double? line = LineY(pv, v, anchor.X, bottom);
                if (line == null) { _dragWhy = "no datum line under the label"; return false; }
                return DragLabel(lbl, null, bottom ? line.Value + lift : line.Value - lift, bottom);
            }
            catch (SysException ex) { _dragWhy = ex.Message; return false; }
        }

        private static bool DragToDatum(CivilLabel lbl, ProfileView pv, ViewPlan v, double lift) => DragToRow(lbl, pv, v, lift, true);

        /// <summary>Drags a label so its text is centred on x with its bottom at y.</summary>
        private static bool DragToPoint(CivilLabel lbl, double cx, double bottomY) => DragLabel(lbl, cx, bottomY, true);

        /// <summary>Moves a dragged label up (bottom row) or down (top row) until its text overlaps
        /// none of the labels already in that row, then records it there.</summary>
        private static bool Settle(CivilLabel lbl, List<Extents3d> placed, bool upward, double gap)
        {
            try
            {
                for (int round = 0; round < 12; round++)
                {
                    var ext = lbl.GeometricExtents;
                    if (ext.MaxPoint.X - ext.MinPoint.X > 1e5 || ext.MaxPoint.Y - ext.MinPoint.Y > 1e5) return true;
                    Extents3d? hit = null;
                    foreach (var p in placed)
                    {
                        bool xo = ext.MinPoint.X < p.MaxPoint.X - 1e-6 && ext.MaxPoint.X > p.MinPoint.X + 1e-6;
                        bool yo = ext.MinPoint.Y < p.MaxPoint.Y - 1e-6 && ext.MaxPoint.Y > p.MinPoint.Y + 1e-6;
                        if (xo && yo) { hit = p; break; }
                    }
                    if (hit == null) { placed.Add(ext); return true; }
                    double dy = upward ? hit.Value.MaxPoint.Y - ext.MinPoint.Y + gap : hit.Value.MinPoint.Y - ext.MaxPoint.Y - gap;
                    lbl.DraggedOffset = lbl.DraggedOffset + new Vector3d(0, dy, 0);
                }
                placed.Add(lbl.GeometricExtents);
                return true;
            }
            catch { return true; }
        }

        private static bool DragSubEntityToDatum(LabelGroupSubEntity se, ProfileView pv, ViewPlan v, double lift)
        {
            try
            {
                Point3d anchor = se.LabelLocation;
                double? y = DatumY(pv, v, anchor.X, lift);
                if (y == null) return false;
                bool set = false;
                try { se.DraggedOffset = new Vector3d(0, y.Value - anchor.Y, 0); set = true; } catch { set = false; }
                return set;
            }
            catch { return false; }
        }

        // ── Relabelling existing views ────────────────────────────────────────────

        /// <summary>{views: [names], styles, station_host, station_increment, drag_to_datum}: the
        /// label rules of draw_profile_views on views that already exist, for every part shown
        /// in each. Existing labels are left alone.</summary>
        private static string LabelProfileViews(JObject req, Document doc)
        {
            Database db = doc.Database;
            using (doc.LockDocument())
            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                try
                {
                    var civil = CivilDocument.GetCivilDocument(db);
                    var plan = new ProfilePlan
                    {
                        Apply = true, StationHost = S(req["station_host"], "eg").Trim().ToLowerInvariant(),
                        StationIncrement = D(req["station_increment"], 100.0),
                    };
                    var drag = req["drag_to_datum"] as JObject ?? new JObject();
                    plan.DragPipe = B(drag["pipe"], true); plan.DragStation = B(drag["station"], true); plan.Lift = D(drag["lift"], 5.0);
                    plan.Catalogue = ReadProfileCatalogue(tr, civil);
                    plan.Styles = PickProfileStyles(plan.Catalogue, req["styles"] as JObject, plan.Warnings);
                    ReadDimLines(plan, req["dim_lines"] as JObject);
                    plan.Deferred = true;      // creates only; drag_profile_labels places afterwards
                    var wanted = (req["views"] as JArray)?.Select(t => t.ToString().Trim()).Where(s => s != "").ToList() ?? new List<string>();
                    var created = new List<ObjectId>();
                    var viewsOut = new JArray();
                    int total = 0;
                    foreach (ObjectId aid in civil.GetAlignmentIds())
                    {
                        var align = tr.GetObject(aid, OpenMode.ForRead) as Alignment;
                        if (align == null) continue;
                        var w = new AlignmentWork { Alignment = align };
                        foreach (ObjectId pid in align.GetProfileIds())
                        {
                            try
                            {
                                var p = (Profile)tr.GetObject(pid, OpenMode.ForRead);
                                if (p.Name.EndsWith(" FL", StringComparison.OrdinalIgnoreCase)) w.Flowline = pid;
                                else if (p.ProfileType == ProfileType.EG && w.EgProfile.IsNull) w.EgProfile = pid;
                            }
                            catch { }
                        }
                        foreach (ObjectId vid in align.GetProfileViewIds())
                        {
                            var pv = tr.GetObject(vid, OpenMode.ForWrite) as ProfileView;
                            if (pv == null) continue;
                            if (wanted.Count > 0 && !wanted.Any(n => n.Equals(pv.Name, StringComparison.OrdinalIgnoreCase))) continue;
                            var v = new ViewPlan
                            {
                                Name = pv.Name, AlignmentName = align.Name, AlignmentId = aid,
                                StationStart = pv.StationStart, StationEnd = pv.StationEnd,
                                ElevationMin = pv.ElevationMin, ElevationMax = pv.ElevationMax, Insertion = pv.Location,
                            };
                            var partMap = PartsShownIn(tr, db, pv, civil);
                            v.Pipes = partMap.Keys.Where(k => tr.GetObject(k, OpenMode.ForRead) is Pipe).ToList();
                            v.Structures = partMap.Keys.Where(k => tr.GetObject(k, OpenMode.ForRead) is Structure).ToList();
                            var dims = DrawDimLines(tr, db, pv, v, align, plan, created, out double dimHeight);
                            var labels = LabelView(tr, db, pv, v, w, partMap, plan, created, dims, dimHeight);
                            labels["dim_lines"] = dims.Count;
                            total += I(labels["total"]);
                            var vj = ProfileViewJson(pv); vj["labels"] = labels; viewsOut.Add(vj);
                        }
                    }
                    if (viewsOut.Count == 0) { tr.Abort(); return Fail(wanted.Count > 0 ? $"no profile view named {string.Join(", ", wanted)}" : "the drawing has no profile views"); }
                    tr.Commit();
                    return Created($"{total} label(s) on {viewsOut.Count} profile view(s); run drag_profile_labels to place them.", created, new JObject
                    {
                        ["views"] = viewsOut, ["styles"] = plan.Styles.Json(), ["warnings"] = new JArray(plan.Warnings),
                    });
                }
                catch (SysException ex)
                {
                    tr.Abort();
                    return Fail($"label_profile_views failed, nothing was kept: {ex.Message}");
                }
            }
        }

        /// <summary>part -> ProfileViewPart for the parts a view shows: every ProfileViewPart in
        /// model space whose extents centre falls inside the view's extents.</summary>
        private static Dictionary<ObjectId, ObjectId> PartsShownIn(Transaction tr, Database db, ProfileView pv, CivilDocument civil)
        {
            var map = new Dictionary<ObjectId, ObjectId>();
            Extents3d ext;
            try { ext = pv.GeometricExtents; } catch { return map; }
            foreach (ObjectId id in ModelSpace(tr, db, false))
            {
                try
                {
                    var pvp = tr.GetObject(id, OpenMode.ForRead) as ProfileViewPart;
                    if (pvp == null) continue;
                    var e = pvp.GeometricExtents;
                    double cx = (e.MinPoint.X + e.MaxPoint.X) / 2, cy = (e.MinPoint.Y + e.MaxPoint.Y) / 2;
                    if (cx < ext.MinPoint.X || cx > ext.MaxPoint.X || cy < ext.MinPoint.Y || cy > ext.MaxPoint.Y) continue;
                    if (!map.ContainsKey(pvp.ModelPartId)) map[pvp.ModelPartId] = id;
                }
                catch { }
            }
            return map;
        }

        // ── helpers ───────────────────────────────────────────────────────────────

        private static string RStr(object o, params string[] names)
        {
            foreach (var n in names)
            {
                try
                {
                    var prop = o.GetType().GetProperty(n);
                    object v = prop?.GetValue(o);
                    if (v != null) return v.ToString();
                }
                catch { }
            }
            return null;
        }
    }
}
