using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Text.RegularExpressions;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;
using Autodesk.Civil.ApplicationServices;
using Autodesk.Civil.DatabaseServices;

// Disambiguate types that exist in both AutoCAD and Civil3D namespaces
using AcadDBObject = Autodesk.AutoCAD.DatabaseServices.DBObject;
using SysException = System.Exception;

[assembly: CommandClass(typeof(CADAgent.FlowLineLabelHandler))]

namespace CADAgent
{
    /// <summary>
    /// AutoCAD command: DynFL
    /// Resolves flow line labels to their referenced pipe and appends a live diameter callout.
    ///
    /// This file only implements Phase 1 (locate labels) and Phase 2 (resolve label -> pipe)
    /// end-to-end. Phase 3 (actually injecting a diameter text component into the label style)
    /// is stubbed — see <see cref="InjectDiameterComponent"/> — because the label-style
    /// component API is not uniform across Civil3D versions/label types and needs to be
    /// confirmed against a live drawing before writing real style-editing code. Run DynFL as-is
    /// first to validate that labels resolve to the correct pipe/diameter, then fill in
    /// InjectDiameterComponent once that API is confirmed.
    /// </summary>
    public class FlowLineLabelHandler
    {
        /// <summary>
        /// AutoCAD command: DynFLDEBUG
        /// Diagnostic only — pick one object and dump its exact .NET type plus every readable
        /// property/value to the command line. Use this to confirm what type the "flow line
        /// label" actually is before trusting IsFlowLineLabel/ResolvePipeId.
        /// </summary>
        /// <summary>
        /// AutoCAD command: DynFLNETWORKS
        /// Diagnostic only — lists every pipe network and pipe name in the drawing, so the
        /// name-matching heuristic in ResolveCandidatePipes can be checked/tuned against what
        /// pipes are actually named here.
        /// </summary>
        [CommandMethod("DynFLNETWORKS")]
        public void DebugListNetworks()
        {
            Document doc = Application.DocumentManager.MdiActiveDocument;
            Database db = doc.Database;
            Editor ed = doc.Editor;
            CivilDocument civilDoc = CivilDocument.GetCivilDocument(db);

            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                ObjectIdCollection netIds = civilDoc.GetPipeNetworkIds();
                ed.WriteMessage($"\n  {netIds.Count} pipe network(s) found:");
                foreach (ObjectId netId in netIds)
                {
                    Network network = tr.GetObject(netId, OpenMode.ForRead) as Network;
                    if (network == null) continue;
                    ed.WriteMessage($"\n  Network '{network.Name}':");
                    foreach (ObjectId pipeId in network.GetPipeIds())
                    {
                        if (tr.GetObject(pipeId, OpenMode.ForRead) is Pipe pipe)
                            ed.WriteMessage($"\n    Pipe '{pipe.Name}' — diameter {pipe.InnerDiameterOrWidth:0.###}");
                    }
                }
                tr.Commit();
            }
        }

        /// <summary>
        /// AutoCAD command: DynFLALIGN
        /// Diagnostic only — pick a flow line label, resolve its Profile -> Alignment, and print
        /// every Alignment method with "Station" in its name plus parameter types. Pipe naming
        /// carries no correlation to the profile/alignment in this drawing (confirmed via
        /// DynFLNETWORKS), so matching has to go through station-along-alignment geometry
        /// instead — this confirms the exact StationOffset overload/signature before it's used.
        /// </summary>
        [CommandMethod("DynFLALIGN")]
        public void DebugInspectAlignmentStationMethods()
        {
            Document doc = Application.DocumentManager.MdiActiveDocument;
            Database db = doc.Database;
            Editor ed = doc.Editor;

            PromptEntityResult per = ed.GetEntity("\nSelect the flow line label: ");
            if (per.Status != PromptStatus.OK) return;

            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                AcadDBObject labelObj = tr.GetObject(per.ObjectId, OpenMode.ForRead);
                ObjectId profileId = ResolveFeatureId(labelObj);
                if (profileId == ObjectId.Null || !(tr.GetObject(profileId, OpenMode.ForRead) is Profile profile))
                {
                    ed.WriteMessage("\n  Could not resolve a Profile from this label's FeatureId.");
                    tr.Commit();
                    return;
                }
                ed.WriteMessage($"\n  Profile: {profile.Name}");

                if (profile.AlignmentId == ObjectId.Null || !(tr.GetObject(profile.AlignmentId, OpenMode.ForRead) is Alignment alignment))
                {
                    ed.WriteMessage("\n  Profile has no resolvable parent Alignment.");
                    tr.Commit();
                    return;
                }
                ed.WriteMessage($"\n  Alignment: {alignment.Name}, station range [{alignment.StartingStation}, {alignment.EndingStation}]");

                foreach (MethodInfo method in alignment.GetType().GetMethods(BindingFlags.Public | BindingFlags.Instance))
                {
                    if (method.Name.IndexOf("Station", StringComparison.OrdinalIgnoreCase) < 0) continue;
                    string parms = string.Join(", ", method.GetParameters()
                        .Select(p => $"{(p.IsOut ? "out " : p.ParameterType.IsByRef ? "ref " : "")}{p.ParameterType.Name} {p.Name}"));
                    ed.WriteMessage($"\n    {method.ReturnType.Name} {method.Name}({parms})");
                }

                tr.Commit();
            }
        }

        [CommandMethod("DynFLDEBUG")]
        public void DebugInspectLabel()
        {
            Document doc = Application.DocumentManager.MdiActiveDocument;
            Database db = doc.Database;
            Editor ed = doc.Editor;

            PromptEntityResult per = ed.GetEntity("\nSelect the flow line label to inspect: ");
            if (per.Status != PromptStatus.OK) return;

            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                AcadDBObject obj = tr.GetObject(per.ObjectId, OpenMode.ForRead);
                Type t = obj.GetType();
                ed.WriteMessage($"\n  Type: {t.FullName}");

                foreach (PropertyInfo prop in t.GetProperties(BindingFlags.Public | BindingFlags.Instance))
                {
                    if (prop.GetIndexParameters().Length > 0) continue;
                    string value;
                    try { value = prop.GetValue(obj)?.ToString() ?? "null"; }
                    catch (SysException ex) { value = $"<error: {ex.Message}>"; }
                    ed.WriteMessage($"\n    {prop.Name} = {value}");
                }

                // FeatureId is what this label is actually "about" — resolve it so we know
                // whether it's a Profile, a Pipe, or something else entirely.
                PropertyInfo featureIdProp = t.GetProperty("FeatureId");
                if (featureIdProp?.GetValue(obj) is ObjectId featureId && !featureId.IsNull)
                {
                    try
                    {
                        AcadDBObject feature = tr.GetObject(featureId, OpenMode.ForRead);
                        ed.WriteMessage($"\n\n  FeatureId resolves to: {feature.GetType().FullName}");
                        PropertyInfo nameProp = feature.GetType().GetProperty("Name");
                        if (nameProp != null)
                            ed.WriteMessage($"\n    Name = {nameProp.GetValue(feature)}");
                    }
                    catch (SysException ex) { ed.WriteMessage($"\n\n  FeatureId could not be resolved: {ex.Message}"); }
                }

                // SubEntities hold the individual per-station labels within this group — dump
                // the first few so we can see what fields/text each one already carries.
                PropertyInfo subEntitiesProp = t.GetProperty("SubEntities");
                if (subEntitiesProp?.GetValue(obj) is System.Collections.IEnumerable subEntities)
                {
                    ed.WriteMessage("\n\n  First SubEntities:");
                    int shown = 0;
                    foreach (object sub in subEntities)
                    {
                        if (shown++ >= 3) break;
                        Type subType = sub.GetType();
                        ed.WriteMessage($"\n    [{shown}] {subType.FullName}");
                        foreach (PropertyInfo sp in subType.GetProperties(BindingFlags.Public | BindingFlags.Instance))
                        {
                            if (sp.GetIndexParameters().Length > 0) continue;
                            string sv;
                            try { sv = sp.GetValue(sub)?.ToString() ?? "null"; }
                            catch (SysException ex) { sv = $"<error: {ex.Message}>"; }
                            ed.WriteMessage($"\n        {sp.Name} = {sv}");
                        }
                    }
                }

                tr.Commit();
            }
        }

        [CommandMethod("DynFL")]
        public void DynamicFlowLineLabels()
        {
            Document doc = Application.DocumentManager.MdiActiveDocument;
            Database db = doc.Database;
            Editor ed = doc.Editor;
            CivilDocument civilDoc = CivilDocument.GetCivilDocument(db);

            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                List<ObjectId> labelIds = ResolveTargetLabels(doc, tr, ed);
                if (labelIds.Count == 0)
                {
                    ed.WriteMessage("\n  DynFL: no flow line labels found (select one or more labels, or run with nothing selected to scan the whole drawing).\n");
                    tr.Commit();
                    return;
                }

                int updated = 0, skipped = 0;
                foreach (ObjectId labelId in labelIds)
                {
                    try
                    {
                        if (ApplyDiameterToLabel(labelId, civilDoc, tr, ed)) updated++;
                        else skipped++;
                    }
                    catch (SysException ex)
                    {
                        ed.WriteMessage($"\n  [Skip label {labelId.Handle}] {ex.Message}");
                        skipped++;
                    }
                }

                tr.Commit();
                ed.WriteMessage($"\n  DynFL complete — {updated} label(s) updated, {skipped} skipped.\n");
            }
        }

        // ── Phase 1 — locate flow line labels ───────────────────────────────────
        // Selection-based by default (safer — only touches labels the user picked);
        // falls back to a full model space scan when nothing is selected.

        private static List<ObjectId> ResolveTargetLabels(Document doc, Transaction tr, Editor ed)
        {
            var result = new List<ObjectId>();

            PromptSelectionResult selRes = ed.SelectImplied();
            if (selRes.Status != PromptStatus.OK || selRes.Value.Count == 0)
            {
                var opts = new PromptSelectionOptions
                {
                    MessageForAdding = "\nSelect flow line labels (Enter to scan entire drawing): "
                };
                selRes = ed.GetSelection(opts);
            }

            if (selRes.Status == PromptStatus.OK && selRes.Value.Count > 0)
            {
                foreach (SelectedObject so in selRes.Value)
                {
                    if (so == null) continue;
                    AcadDBObject obj = tr.GetObject(so.ObjectId, OpenMode.ForRead);
                    if (IsFlowLineLabel(obj)) result.Add(so.ObjectId);
                }
                if (result.Count > 0) return result;
            }

            // Scan-based fallback — walks model space for any label that resolves to a pipe.
            BlockTable bt = tr.GetObject(doc.Database.BlockTableId, OpenMode.ForRead) as BlockTable;
            BlockTableRecord ms = tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead) as BlockTableRecord;
            foreach (ObjectId id in ms)
            {
                try
                {
                    AcadDBObject obj = tr.GetObject(id, OpenMode.ForRead);
                    if (IsFlowLineLabel(obj)) result.Add(id);
                }
                catch { }
            }
            return result;
        }

        /// <summary>
        /// Confirmed via DynFLDEBUG against a live drawing: the actual type is
        /// Autodesk.Civil.DatabaseServices.ProfileStationLabelGroup, which does NOT derive from
        /// Label — matched by type name rather than a static cast so this keeps working if the
        /// exact class differs slightly across Civil3D versions. FeatureId on this type points
        /// to the flowline Profile, not a Pipe — see ResolveFeatureId/ResolveCandidatePipes.
        /// </summary>
        private static bool IsFlowLineLabel(AcadDBObject obj)
        {
            if (obj.GetType().Name.IndexOf("StationLabelGroup", StringComparison.OrdinalIgnoreCase) < 0)
                return false;
            return ResolveFeatureId(obj) != ObjectId.Null;
        }

        // ── Phase 2 — resolve label -> pipe ─────────────────────────────────────

        private static ObjectId ResolveFeatureId(AcadDBObject label)
        {
            PropertyInfo prop = label.GetType().GetProperty("FeatureId");
            if (prop?.GetValue(label) is ObjectId oid && !oid.IsNull) return oid;
            return ObjectId.Null;
        }

        /// <summary>
        /// This label group's FeatureId resolves to a drafted flowline Profile (e.g. "Line
        /// 'SS-OS1' FL"), not a live Pipe — there is no direct ObjectId reference to a pipe
        /// network object at all. Instead, match by name: pull the quoted token out of the
        /// profile name (or fall back to its parent Alignment's name) and look for a Pipe in
        /// any pipe network whose name contains that token.
        /// </summary>
        private static List<Pipe> ResolveCandidatePipes(Profile profile, CivilDocument civilDoc, Transaction tr)
        {
            var candidates = new List<Pipe>();
            var keys = new List<string>();

            Match m = Regex.Match(profile.Name ?? "", @"'([^']+)'");
            if (m.Success) keys.Add(m.Groups[1].Value);
            else if (!string.IsNullOrWhiteSpace(profile.Name)) keys.Add(profile.Name);

            try
            {
                if (profile.AlignmentId != ObjectId.Null &&
                    tr.GetObject(profile.AlignmentId, OpenMode.ForRead) is Alignment alignment &&
                    !string.IsNullOrWhiteSpace(alignment.Name))
                {
                    keys.Add(alignment.Name);
                }
            }
            catch { }

            if (keys.Count == 0) return candidates;

            foreach (ObjectId netId in civilDoc.GetPipeNetworkIds())
            {
                Network network;
                try { network = tr.GetObject(netId, OpenMode.ForRead) as Network; }
                catch { continue; }
                if (network == null) continue;

                foreach (ObjectId pipeId in network.GetPipeIds())
                {
                    Pipe pipe;
                    try { pipe = tr.GetObject(pipeId, OpenMode.ForRead) as Pipe; }
                    catch { continue; }
                    if (pipe?.Name == null) continue;

                    if (keys.Any(key => pipe.Name.IndexOf(key, StringComparison.OrdinalIgnoreCase) >= 0))
                        candidates.Add(pipe);
                }
            }
            return candidates;
        }

        // ── Phase 3/4 — inject diameter + refresh ───────────────────────────────

        private static bool ApplyDiameterToLabel(ObjectId labelId, CivilDocument civilDoc, Transaction tr, Editor ed)
        {
            AcadDBObject labelObj = tr.GetObject(labelId, OpenMode.ForRead);
            ObjectId profileId = ResolveFeatureId(labelObj);
            Profile profile = profileId != ObjectId.Null ? tr.GetObject(profileId, OpenMode.ForRead) as Profile : null;
            if (profile == null)
            {
                ed.WriteMessage($"\n  [Skip label {labelId.Handle}] FeatureId did not resolve to a Profile.");
                return false;
            }

            List<Pipe> candidates = ResolveCandidatePipes(profile, civilDoc, tr);
            if (candidates.Count == 0)
            {
                ed.WriteMessage($"\n  [Skip label {labelId.Handle}] no pipe network object matched profile '{profile.Name}'.");
                return false;
            }

            double[] distinctDiameters = candidates.Select(p => p.InnerDiameterOrWidth).Distinct().ToArray();
            if (distinctDiameters.Length > 1)
            {
                ed.WriteMessage($"\n  [Skip label {labelId.Handle}] ambiguous — profile '{profile.Name}' matched {candidates.Count} pipe(s) with different diameters ({string.Join(", ", distinctDiameters)}).");
                return false;
            }

            double diameter = distinctDiameters[0];
            ed.WriteMessage($"\n  Label {labelId.Handle} -> profile '{profile.Name}' -> {candidates.Count} matching pipe(s) (e.g. '{candidates[0].Name}'), diameter {diameter:0.###}.");

            if (!InjectDiameterComponent(labelId, diameter, tr, ed))
            {
                ed.WriteMessage($"\n  [Skip label {labelId.Handle}] diameter component injection not implemented yet.");
                return false;
            }

            RefreshLabel(labelId, tr);
            return true;
        }

        /// <summary>
        /// TODO: not implemented. Per the design discussion, the intended approach is option 3 —
        /// clone the label's current style, add a diameter text component to the clone, and
        /// reassign only the labels this command touches (keeps the change dynamic and scoped,
        /// rather than editing a style every other flow line label shares). Left unimplemented
        /// because the label-style component API differs by label type and Civil3D version and
        /// needs to be confirmed against a live drawing first — see the Phase 3 discussion.
        /// </summary>
        private static bool InjectDiameterComponent(ObjectId labelId, double diameter, Transaction tr, Editor ed)
        {
            return false;
        }

        /// <summary>Forces the label to reflow so new text isn't clipped, once real injection exists.</summary>
        private static void RefreshLabel(ObjectId labelId, Transaction tr)
        {
            AcadDBObject labelObj = tr.GetObject(labelId, OpenMode.ForWrite);
            MethodInfo refreshMethod = labelObj.GetType().GetMethod("UpdateProfileViewLabelLocation")
                                     ?? labelObj.GetType().GetMethod("Update", Type.EmptyTypes);
            if (refreshMethod != null && refreshMethod.GetParameters().Length == 0)
                refreshMethod.Invoke(labelObj, null);
        }
    }
}
