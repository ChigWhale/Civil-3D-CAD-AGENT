using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Core.Application;
using CivilSurface = Autodesk.Civil.DatabaseServices.Surface;

[assembly: CommandClass(typeof(FeatureLineSync.Commands))]
[assembly: ExtensionApplication(typeof(FeatureLineSync.PlugIn))]

namespace FeatureLineSync
{
    /// <summary>Answers remembered for the session so a re-run after an undo is quick.</summary>
    public static class SessionDefaults
    {
        public static string CopyPath { get; set; } = "";
        public static string JsonPath { get; set; } = "";
        public static string SurfaceName { get; set; } = "";
        public static double Tolerance { get; set; } = 0.5;
        public static SyncMode Mode { get; set; } = SyncMode.Grades;
    }

    public class PlugIn : IExtensionApplication
    {
        public void Initialize()
        {
            AcApp.DocumentManager.MdiActiveDocument?.Editor.WriteMessage(
                "\nFeatureLineSync loaded. Commands: FLSYNC (copy .dwg -> this drawing), FLEXPORT (snapshot to JSON), FLIMPORT (JSON -> this drawing), FLGRADES (live grade labels on/off)\n");
        }

        public void Terminate() { }
    }

    public class Commands
    {
        // ── FLSYNC: read the copy drawing, plan, preview, apply ─────────────────

        [CommandMethod("FLSYNC", CommandFlags.Modal)]
        public void FlSync()
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            Editor ed = doc.Editor;

            var pofo = new PromptOpenFileOptions("\nCopy drawing that still has the correct elevations")
            {
                Filter = "Drawing (*.dwg)|*.dwg",
                PreferCommandLine = false,
            };
            if (!string.IsNullOrEmpty(SessionDefaults.CopyPath)) pofo.InitialFileName = SessionDefaults.CopyPath;
            else pofo.InitialDirectory = DrawingFolder(doc);
            PromptFileNameResult pfr = ed.GetFileNameForOpen(pofo);
            if (pfr.Status != PromptStatus.OK) return;
            string copyPath = pfr.StringResult;
            if (!File.Exists(copyPath)) { ed.WriteMessage($"\nFile not found: {copyPath}"); return; }
            if (string.Equals(Path.GetFullPath(copyPath), Path.GetFullPath(doc.Name), StringComparison.OrdinalIgnoreCase))
            { ed.WriteMessage("\nThat is the current drawing. Pick the copy that was saved before the lines were moved."); return; }
            SessionDefaults.CopyPath = copyPath;

            FlSnapshot? copy = ReadCopy(copyPath, ed);
            if (copy == null) return;
            RunSync(doc, ed, copy, SyncMode.Grades);
        }

        // ── FLEXPORT: snapshot of this drawing to JSON (run inside the copy as the fallback) ──

        [CommandMethod("FLEXPORT", CommandFlags.Modal)]
        public void FlExport()
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            Editor ed = doc.Editor;

            FlSnapshot snap;
            try { snap = FeatureLineReader.ReadHost(doc); }
            catch (System.Exception ex) { ed.WriteMessage("\nRead failed: " + ex.Message); return; }

            string suggested = Path.Combine(DrawingFolder(doc), Path.GetFileNameWithoutExtension(doc.Name) + ".flsnapshot.json");
            var psfo = new PromptSaveFileOptions("\nSave feature line snapshot to")
            {
                Filter = "JSON (*.json)|*.json",
                InitialFileName = suggested,
                PreferCommandLine = false,
            };
            PromptFileNameResult pfr = ed.GetFileNameForSave(psfo);
            if (pfr.Status != PromptStatus.OK) return;
            File.WriteAllText(pfr.StringResult, snap.ToJson());
            SessionDefaults.JsonPath = pfr.StringResult;
            ed.WriteMessage($"\n{snap.Lines.Count} feature line(s) written to {pfr.StringResult}" +
                            (snap.Skipped.Count > 0 ? $" ({snap.Skipped.Count} skipped)" : ""));
            foreach (string s in snap.Skipped) ed.WriteMessage("\n  skipped: " + s);
        }

        // ── FLIMPORT: JSON snapshot as the copy (fallback and backup restore) ────

        [CommandMethod("FLIMPORT", CommandFlags.Modal)]
        public void FlImport()
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            Editor ed = doc.Editor;

            var pofo = new PromptOpenFileOptions("\nFeature line snapshot (FLEXPORT or .flsync-backup) to apply")
            {
                Filter = "JSON (*.json)|*.json",
                PreferCommandLine = false,
            };
            if (!string.IsNullOrEmpty(SessionDefaults.JsonPath)) pofo.InitialFileName = SessionDefaults.JsonPath;
            else pofo.InitialDirectory = DrawingFolder(doc);
            PromptFileNameResult pfr = ed.GetFileNameForOpen(pofo);
            if (pfr.Status != PromptStatus.OK) return;

            FlSnapshot copy;
            try { copy = FlSnapshot.FromJson(File.ReadAllText(pfr.StringResult)); }
            catch (System.Exception ex) { ed.WriteMessage("\nCannot read snapshot: " + ex.Message); return; }
            SessionDefaults.JsonPath = pfr.StringResult;
            ed.WriteMessage($"\nSnapshot: {copy.Lines.Count} feature line(s) from {copy.Source} saved {copy.SavedAt:yyyy-MM-dd HH:mm}");

            // Grades = same method as FLSYNC; Elevations = write the snapshot's elevations back (backup restore)
            var mko = new PromptKeywordOptions("\nApply from the snapshot [Grades/Elevations]") { AllowNone = true };
            mko.Keywords.Add("Grades"); mko.Keywords.Add("Elevations");
            mko.Keywords.Default = SessionDefaults.Mode == SyncMode.Elevations ? "Elevations" : "Grades";
            PromptResult mkr = ed.GetKeywords(mko);
            if (mkr.Status == PromptStatus.Cancel) return;
            string m = mkr.Status == PromptStatus.OK ? mkr.StringResult : mko.Keywords.Default;
            SessionDefaults.Mode = m == "Elevations" ? SyncMode.Elevations : SyncMode.Grades;
            RunSync(doc, ed, copy, SessionDefaults.Mode);
        }

        // ── shared pipeline ─────────────────────────────────────────────────────

        private static FlSnapshot? ReadCopy(string path, Editor ed)
        {
            Document? open = null;
            foreach (Document d in AcApp.DocumentManager)
                if (string.Equals(Path.GetFullPath(d.Name), Path.GetFullPath(path), StringComparison.OrdinalIgnoreCase)) { open = d; break; }

            FlSnapshot snap;
            try
            {
                snap = open != null ? FeatureLineReader.ReadHost(open) : FeatureLineReader.ReadSide(path);
            }
            catch (System.Exception ex)
            {
                ed.WriteMessage($"\nCould not read the copy ({ex.Message}).\nFallback: open the copy, run FLEXPORT there, then run FLIMPORT here.");
                return null;
            }
            ed.WriteMessage($"\nCopy ({(open != null ? "open document" : "side database")}): {snap.Lines.Count} feature line(s)" +
                            (snap.Skipped.Count > 0 ? $", {snap.Skipped.Count} skipped" : ""));
            if (snap.Lines.Count == 0)
            {
                ed.WriteMessage("\nNo editable feature lines came back from the copy.\nFallback: open the copy, run FLEXPORT there, then run FLIMPORT here.");
                foreach (string s in snap.Skipped.Take(10)) ed.WriteMessage("\n  " + s);
                return null;
            }
            return snap;
        }

        private static void RunSync(Document doc, Editor ed, FlSnapshot copy, SyncMode mode)
        {
            var opts = new PlannerOptions { Mode = mode };

            ObjectId surfaceId = ObjectId.Null;
            if (opts.Mode == SyncMode.Grades)
            {
                // surface
                List<(string name, ObjectId id)> surfaces;
                using (doc.LockDocument())
                using (Transaction tr = doc.Database.TransactionManager.StartTransaction())
                {
                    try { surfaces = SurfaceProbe.ListSurfaces(tr); }
                    catch (System.Exception ex) { ed.WriteMessage("\nCannot list surfaces: " + ex.Message); return; }
                    tr.Commit();
                }
                if (surfaces.Count == 0) { ed.WriteMessage("\nThis drawing has no surfaces; one is needed to set the fixed points."); return; }
                ed.WriteMessage("\nSurfaces (every feature line point on this surface takes its elevation; the copy's slopes fill the rest):");
                for (int i = 0; i < surfaces.Count; i++) ed.WriteMessage($"\n  {i + 1,3}  {surfaces[i].name}");
                int def = Math.Max(1, surfaces.FindIndex(s => s.name == SessionDefaults.SurfaceName) + 1);
                var pio = new PromptIntegerOptions("\nSurface number") { LowerLimit = 1, UpperLimit = surfaces.Count, DefaultValue = def, UseDefaultValue = true };
                PromptIntegerResult pir = ed.GetInteger(pio);
                if (pir.Status != PromptStatus.OK) return;
                surfaceId = surfaces[pir.Value - 1].id;
                SessionDefaults.SurfaceName = surfaces[pir.Value - 1].name;

                // tolerance
                var pdo = new PromptDoubleOptions("\nSurface tolerance (how far outside the surface edge a point still takes the surface elevation)")
                { AllowNegative = false, AllowZero = true, DefaultValue = SessionDefaults.Tolerance, UseDefaultValue = true };
                PromptDoubleResult pdr = ed.GetDouble(pdo);
                if (pdr.Status == PromptStatus.Cancel) return;
                if (pdr.Status == PromptStatus.OK) SessionDefaults.Tolerance = pdr.Value;
            }

            // read the target and plan
            FlSnapshot target;
            try { target = FeatureLineReader.ReadHost(doc); }
            catch (System.Exception ex) { ed.WriteMessage("\nRead of this drawing failed: " + ex.Message); return; }
            ed.WriteMessage($"\nThis drawing: {target.Lines.Count} feature line(s)" + (target.Skipped.Count > 0 ? $", {target.Skipped.Count} skipped" : ""));
            foreach (string s in target.Skipped) ed.WriteMessage("\n  skipped: " + s);
            if (target.Lines.Count == 0) return;

            SyncPlan plan;
            using (doc.LockDocument())
            using (Transaction tr = doc.Database.TransactionManager.StartTransaction())
            {
                SurfaceProbe? probe = null;
                if (!surfaceId.IsNull)
                {
                    var surface = (CivilSurface)tr.GetObject(surfaceId, OpenMode.ForRead);
                    probe = new SurfaceProbe(surface, SessionDefaults.Tolerance);
                }
                plan = SyncPlanner.Plan(target, copy, probe != null ? probe.Probe : null, opts);

                // why did a line get no fixed point? say how far the surface coverage is
                if (probe != null)
                {
                    foreach (LinePlan lp in plan.Lines.Where(l => l.Status == "unanchored"))
                    {
                        double best = double.MaxValue;
                        foreach (AlignedPoint p in lp.Points)
                        {
                            double? r = probe.NearestCoverage(p.X, p.Y, 50);
                            if (r.HasValue && r.Value < best) best = r.Value;
                        }
                        lp.Warnings.Add(best < double.MaxValue
                            ? $"nearest surface coverage is about {best:0.#} units from this line; a tolerance of at least {best:0.#} would fix a point on it"
                            : "no surface coverage within 50 units of any point of this line");
                    }
                }
                tr.Commit();
            }

            PrintPreview(ed, plan, opts);

            if (plan.LinesToWrite == 0) { ed.WriteMessage("\nNothing to write: no feature line has a point on the chosen surface within the tolerance."); return; }

            var pko = new PromptKeywordOptions($"\nApply to {plan.LinesToWrite} line(s), {plan.PointsToWrite} point(s)? [Yes/No]") { AllowNone = true };
            pko.Keywords.Add("Yes"); pko.Keywords.Add("No"); pko.Keywords.Default = "No";
            PromptResult pr = ed.GetKeywords(pko);
            if (pr.Status != PromptStatus.OK || !pr.StringResult.Equals("Yes", StringComparison.OrdinalIgnoreCase)) return;

            // backup of the current state (restore with FLIMPORT in Elevations mode)
            string backup = Path.Combine(DrawingFolder(doc),
                $"{Path.GetFileNameWithoutExtension(doc.Name)}.flsync-backup-{DateTime.Now:yyyyMMdd_HHmmss}.json");
            try { File.WriteAllText(backup, target.ToJson()); ed.WriteMessage($"\nBackup of the current elevations: {backup}"); }
            catch (System.Exception ex) { ed.WriteMessage($"\nBackup could not be written ({ex.Message}); undo is the only way back."); }

            ApplyReport rep = FeatureLineWriter.Apply(doc, plan);
            if (!rep.Applied)
            {
                ed.WriteMessage("\nNOT applied:\n  " + string.Join("\n  ", rep.Errors));
                return;
            }
            ed.WriteMessage($"\nApplied: {rep.LinesWritten} line(s), {rep.PointsSet} point(s) set, {rep.PointsInserted} elevation point(s) inserted. One undo step.");
            if (rep.LinesFailed > 0)
            {
                ed.WriteMessage($"\n{rep.LinesFailed} line(s) refused by Civil 3D and left unchanged:");
                foreach (string f in rep.LineFailures) ed.WriteMessage("\n  " + f);
            }
            if (rep.Differences.Count > 0)
            {
                ed.WriteMessage($"\nVerification: {rep.Differences.Count} difference(s) between the plan and what Civil 3D now reports:");
                foreach (string d in rep.Differences.Take(60)) ed.WriteMessage("\n  " + d);
                if (rep.Differences.Count > 60) ed.WriteMessage($"\n  ... {rep.Differences.Count - 60} more");
            }
            else ed.WriteMessage("\nVerification: every planned elevation reads back from Civil 3D.");
        }

        private static void PrintPreview(Editor ed, SyncPlan plan, PlannerOptions opts)
        {
            ed.WriteMessage(opts.Mode == SyncMode.Elevations
                ? "\n── plan: write the snapshot's elevations back by point"
                : $"\n── plan: per line, every point on surface '{SessionDefaults.SurfaceName}' (tolerance {SessionDefaults.Tolerance:0.##}) takes its elevation; " +
                  "from those contact points the copy's slope per segment sets every other point, working outward");
            ed.WriteMessage("\n{0,-3} {1,-7} {2,-20} {3,-10} {4,-6} {5,4} {6,7} {7,17} {8,17} {9,8} {10}",
                "#", "Handle", "Name", "Site", "By", "Pts", "Fixed", "Slope copy", "Slope new", "Misclos", "Status");
            int i = 0;
            foreach (LinePlan lp in plan.Lines)
            {
                i++;
                double mis = lp.Runs.Count > 0 ? lp.Runs.Max(r => Math.Abs(r.Misclosure)) : 0;
                ed.WriteMessage("\n{0,-3} {1,-7} {2,-20} {3,-10} {4,-6} {5,4} {6,7} {7,17} {8,17} {9,8} {10}",
                    i, Trunc(lp.Target.Handle, 7), Trunc(lp.Target.Name, 20), Trunc(lp.Target.Site, 10), lp.MatchedBy,
                    lp.Points.Count, lp.ExistingAnchorCount > 0 ? $"{lp.SurfaceAnchorCount}+{lp.ExistingAnchorCount}" : lp.SurfaceAnchorCount.ToString(),
                    GradeRange(lp.MinGradeBefore, lp.MaxGradeBefore), GradeRange(lp.MinGradeAfter, lp.MaxGradeAfter),
                    lp.Runs.Count > 0 ? mis.ToString("0.000") : "-",
                    StatusLabel(lp, opts.Mode));
                foreach (string w in lp.Warnings) ed.WriteMessage("\n      ! " + w);
            }
            foreach (string w in plan.Warnings) ed.WriteMessage("\n  warning: " + w);
            if (plan.UnmatchedInCopy.Count > 0)
                ed.WriteMessage($"\n  in the copy but not in this drawing ({plan.UnmatchedInCopy.Count}): {string.Join(", ", plan.UnmatchedInCopy.Take(15))}" +
                                (plan.UnmatchedInCopy.Count > 15 ? ", ..." : ""));
            if (plan.Conflicts.Count > 0)
                ed.WriteMessage($"\n  {plan.Conflicts.Count} shared location(s) where two lines get different values (Civil 3D's same-site rule decides; see the verification after the write):");
            foreach (string c in plan.Conflicts.Take(40)) ed.WriteMessage("\n    " + c);
            if (plan.Conflicts.Count > 40) ed.WriteMessage($"\n    ... {plan.Conflicts.Count - 40} more");
            int existing = plan.Lines.Sum(l => l.ExistingAnchorCount);
            ed.WriteMessage($"\n  {plan.Lines.Count} line(s), {plan.PointCount} point(s): {plan.SurfaceAnchorCount} on the surface" +
                            (existing > 0 ? $", {existing} kept non-zero" : "") +
                            $"; {plan.LinesToWrite} line(s) to write, {plan.Lines.Count(l => l.Status == "unanchored")} with no fixed point (untouched), " +
                            $"{plan.Lines.Count(l => l.Status == "unmatched")} unmatched, {plan.Lines.Count(l => l.Status == "locked_layer")} on locked layers.");
        }

        private static string StatusLabel(LinePlan lp, SyncMode mode)
        {
            string extra = lp.RebuildElevationPoints ? " rebuild" : "";
            if (lp.Status != "ok") return lp.Status == "unanchored" ? "no fixed point" : lp.Status;
            if (mode == SyncMode.Elevations) return "restore" + extra;
            if (lp.AnchorCount == lp.Points.Count) return "all fixed" + extra;
            return "ok" + extra;
        }

        private static string GradeRange(double? min, double? max) =>
            min.HasValue && max.HasValue ? $"{min * 100:0.00}%..{max * 100:0.00}%" : "-";

        private static string DrawingFolder(Document doc)
        {
            string? dir = Path.GetDirectoryName(doc.Name);
            if (!string.IsNullOrEmpty(dir) && Directory.Exists(dir)) return dir;
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Downloads");
        }

        private static string Trunc(string s, int n) => string.IsNullOrEmpty(s) ? "" : s.Length <= n ? s : s.Substring(0, n);
    }
}
