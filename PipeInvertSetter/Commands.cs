using System;
using System.Collections.Generic;
using System.IO;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;
using Autodesk.Civil.DatabaseServices;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Core.Application;

[assembly: CommandClass(typeof(PipeInvertSetter.Commands))]
[assembly: ExtensionApplication(typeof(PipeInvertSetter.PlugIn))]

namespace PipeInvertSetter
{
    /// <summary>Session-wide defaults shared by the commands and the dashboard.</summary>
    public static class SolveDefaults
    {
        public static double? CoverMinimum { get; set; } = null;       // null → from rule set
        public static OffsetMode Mode { get; set; } = OffsetMode.MatchCrowns;
        public static double ManualDrop { get; set; } = 0.1;
        public static double MaxDepthBelowSurface { get; set; } = 50.0;
    }

    public class PlugIn : IExtensionApplication
    {
        public void Initialize()
        {
            AcApp.DocumentManager.MdiActiveDocument?.Editor.WriteMessage(
                "\nPipeInvertSetter loaded. Commands: PIPESOLVE, PIPEDASH, PIPESOLVEJSON\n");
        }

        public void Terminate() { }
    }

    public class Commands
    {
        // ── PIPESOLVE: pick a structure, solve, review, optionally apply ────────

        [CommandMethod("PIPESOLVE", CommandFlags.Modal)]
        public void PipeSolve()
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            Editor ed = doc.Editor;

            ObjectId structureId = PickStructure(ed);
            if (structureId.IsNull) return;

            SolverInput? input = PromptForInput(ed, structureId);
            if (input == null) return;

            SolveReport report = PipeElevationSolver.Compute(doc, input);
            PrintReport(ed, report);

            if (!report.Ok)
            {
                ed.WriteMessage("\nNothing applied — resolve the items above (or use PIPEDASH to iterate).");
                return;
            }

            var pko = new PromptKeywordOptions("\nApply these elevations? [Yes/No]") { AllowNone = true };
            pko.Keywords.Add("Yes"); pko.Keywords.Add("No"); pko.Keywords.Default = "No";
            PromptResult r = ed.GetKeywords(pko);
            if (r.Status != PromptStatus.OK || !r.StringResult.Equals("Yes", StringComparison.OrdinalIgnoreCase)) return;

            SolveReport applied = PipeElevationSolver.Draw(doc, input);
            ed.WriteMessage(applied.Applied
                ? $"\nApplied to {applied.Pipes.Count} pipe(s)."
                : "\nNOT applied:\n  " + string.Join("\n  ", applied.Errors));
            PipeDashboard.RefreshIfOpen();
        }

        // ── PIPESOLVEJSON: same solve, but the report goes to a file for the Python layer ──

        [CommandMethod("PIPESOLVEJSON", CommandFlags.Modal)]
        public void PipeSolveJson()
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            Editor ed = doc.Editor;

            ObjectId structureId = PickStructure(ed);
            if (structureId.IsNull) return;
            SolverInput? input = PromptForInput(ed, structureId);
            if (input == null) return;

            SolveReport report = PipeElevationSolver.Compute(doc, input);
            string path = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Downloads",
                                       $"pipe_solve_{report.Structure.Name}_{DateTime.Now:yyyyMMdd_HHmmss}.json");
            var pso = new PromptStringOptions("\nSave report to") { DefaultValue = path, UseDefaultValue = true, AllowSpaces = true };
            PromptResult pr = ed.GetString(pso);
            if (pr.Status != PromptStatus.OK) return;
            File.WriteAllText(pr.StringResult, report.ToJson());
            ed.WriteMessage($"\nReport written: {pr.StringResult}  (ok={report.Ok}, conflicts={report.Conflicts.Count})");
        }

        // ── PIPESOLVERUN: solve a whole run along an alignment (or between two structures) ──

        [CommandMethod("PIPESOLVERUN", CommandFlags.Modal)]
        public void PipeSolveRun()
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            Editor ed = doc.Editor;

            var input = new RunInput { MaxDepthBelowSurface = SolveDefaults.MaxDepthBelowSurface };

            var aeo = new PromptEntityOptions("\nSelect the alignment the run follows (Enter = pick start and end structures instead)") { AllowNone = true };
            aeo.SetRejectMessage("\nThat is not an alignment.");
            aeo.AddAllowedClass(typeof(Alignment), false);
            PromptEntityResult aer = ed.GetEntity(aeo);
            if (aer.Status == PromptStatus.Cancel) return;
            if (aer.Status == PromptStatus.OK)
            {
                input.AlignmentId = aer.ObjectId;
                var tdo = new PromptDoubleOptions("\nMax offset from alignment for a structure to be on the run") { DefaultValue = 25, UseDefaultValue = true, AllowNegative = false };
                PromptDoubleResult tdr = ed.GetDouble(tdo);
                if (tdr.Status == PromptStatus.Cancel) return;
                input.OffsetTolerance = tdr.Status == PromptStatus.OK ? tdr.Value : 25;
            }
            else
            {
                ObjectId a = PickStructure(ed); if (a.IsNull) return;
                ObjectId b = PickStructure(ed); if (b.IsNull) return;
                input.StartStructureId = a; input.EndStructureId = b;
            }

            // shared criteria
            var pdo = new PromptDoubleOptions("\nMinimum cover (Enter = from rule set)")
            { AllowNone = true, AllowNegative = false, AllowZero = true, UseDefaultValue = SolveDefaults.CoverMinimum.HasValue, DefaultValue = SolveDefaults.CoverMinimum ?? 0 };
            PromptDoubleResult pdr = ed.GetDouble(pdo);
            if (pdr.Status == PromptStatus.Cancel) return;
            input.CoverMinimum = pdr.Status == PromptStatus.OK ? pdr.Value : SolveDefaults.CoverMinimum;

            var mko = new PromptKeywordOptions("\nOffset at each structure [Crowns/Inverts/Drop]") { AllowNone = true };
            mko.Keywords.Add("Crowns"); mko.Keywords.Add("Inverts"); mko.Keywords.Add("Drop");
            mko.Keywords.Default = SolveDefaults.Mode switch { OffsetMode.MatchInverts => "Inverts", OffsetMode.ManualDrop => "Drop", _ => "Crowns" };
            PromptResult mkr = ed.GetKeywords(mko);
            if (mkr.Status == PromptStatus.Cancel) return;
            string m = mkr.Status == PromptStatus.OK ? mkr.StringResult : mko.Keywords.Default;
            input.Mode = m == "Inverts" ? OffsetMode.MatchInverts : m == "Drop" ? OffsetMode.ManualDrop : OffsetMode.MatchCrowns;
            if (input.Mode == OffsetMode.ManualDrop)
            {
                var ddo = new PromptDoubleOptions("\nDrop across each structure") { AllowNegative = false, AllowZero = true, UseDefaultValue = true, DefaultValue = SolveDefaults.ManualDrop };
                PromptDoubleResult ddr = ed.GetDouble(ddo);
                if (ddr.Status == PromptStatus.Cancel) return;
                input.ManualDrop = ddr.Status == PromptStatus.OK ? ddr.Value : SolveDefaults.ManualDrop;
            }

            var fko = new PromptKeywordOptions("\nFlow direction [Auto/Downstation/Upstation]") { AllowNone = true };
            fko.Keywords.Add("Auto"); fko.Keywords.Add("Downstation"); fko.Keywords.Add("Upstation"); fko.Keywords.Default = "Auto";
            PromptResult fkr = ed.GetKeywords(fko);
            if (fkr.Status == PromptStatus.Cancel) return;
            input.FlowDirection = (fkr.Status == PromptStatus.OK ? fkr.StringResult : "Auto").ToLowerInvariant();

            var hko = new PromptKeywordOptions("\nHold existing inverts at [None/Start/End/Both]") { AllowNone = true };
            hko.Keywords.Add("None"); hko.Keywords.Add("Start"); hko.Keywords.Add("End"); hko.Keywords.Add("Both"); hko.Keywords.Default = "None";
            PromptResult hkr = ed.GetKeywords(hko);
            if (hkr.Status == PromptStatus.Cancel) return;
            string hold = hkr.Status == PromptStatus.OK ? hkr.StringResult : "None";
            input.HoldStart = hold is "Start" or "Both";
            input.HoldEnd = hold is "End" or "Both";

            SolveDefaults.CoverMinimum = input.CoverMinimum; SolveDefaults.Mode = input.Mode;
            if (input.Mode == OffsetMode.ManualDrop) SolveDefaults.ManualDrop = input.ManualDrop;

            RunReport report = PipeRunSolver.Compute(doc, input);
            PrintRun(ed, report);
            if (!report.Ok)
            {
                ed.WriteMessage("\nNothing applied — resolve the items above (or open PIPEDASH to adjust by hand).");
                return;
            }

            var pko = new PromptKeywordOptions("\nApply these elevations to the whole run? [Yes/No]") { AllowNone = true };
            pko.Keywords.Add("Yes"); pko.Keywords.Add("No"); pko.Keywords.Default = "No";
            PromptResult r = ed.GetKeywords(pko);
            if (r.Status != PromptStatus.OK || !r.StringResult.Equals("Yes", StringComparison.OrdinalIgnoreCase)) return;

            RunReport applied = PipeRunSolver.Apply(doc, report, input.MaxDepthBelowSurface);
            ed.WriteMessage(applied.Applied
                ? $"\nApplied to {applied.Pipes.Count} pipe(s) in one transaction."
                : "\nNOT applied:\n  " + string.Join("\n  ", applied.Errors));
            PipeDashboard.RefreshIfOpen();
        }

        /// <summary>Invoked by the dashboard: pick two structures, then solve the network path between them.</summary>
        [CommandMethod("PIPEDASHRUNPICK", CommandFlags.Modal | CommandFlags.NoHistory)]
        public void PipeDashRunPick()
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            ObjectId a = PickStructure(doc.Editor); if (a.IsNull) return;
            ObjectId b = PickStructure(doc.Editor); if (b.IsNull) return;
            PipeDashboard.SetRunStartEnd(a, b);
        }

        internal static void PrintRun(Editor ed, RunReport r)
        {
            ed.WriteMessage($"\n── run on '{(r.Alignment is { Length: > 0 } a ? a : "picked path")}' network '{r.Network}' surface '{r.ReferenceSurface}'  " +
                            $"flow {r.FlowDirection} ({r.FlowDirectionReason})  mode {r.OffsetMode}  slopes as {r.SlopeUnits}");
            ed.WriteMessage("\n{0,-3} {1,-14} {2,-12} {3,-12} {4,5} {5,7} | {6,9} {7,9} {8,9} {9,9} | {10,7} {11,7} {12,8} {13,-9}",
                "#", "Pipe", "From", "To", "Dia", "Len", "OldInvUp", "NewInvUp", "OldInvDn", "NewInvDn", "CovUp", "CovDn", "Slope", "Status");
            foreach (RunPipeRow p in r.Pipes)
            {
                ed.WriteMessage("\n{0,-3} {1,-14} {2,-12} {3,-12} {4,5:0.##} {5,7:0.0} | {6,9:0.000} {7,9} {8,9:0.000} {9,9} | {10,7} {11,7} {12,8} {13,-9}",
                    p.Index, Trunc(p.Name, 14), Trunc(p.Upstream, 12), Trunc(p.Downstream, 12), p.InnerDiameter, p.Length2D,
                    p.OldUpInvert, F(p.NewUpInvert), p.OldDownInvert, F(p.NewDownInvert),
                    F(p.NewCoverUp, "0.00"), F(p.NewCoverDown, "0.00"), F(p.NewSlope, "0.0000"), p.Status);
                foreach (string v in p.Violations) ed.WriteMessage($"\n      ! {v}");
            }
            foreach (string w in r.Warnings) ed.WriteMessage($"\n  warning: {w}");
            foreach (ConflictReport c in r.Conflicts) ed.WriteMessage($"\n  CONFLICT [{c.Kind}] {c.Message}");
            foreach (string e in r.Errors) ed.WriteMessage($"\n  ERROR: {e}");
        }

        // ── PIPEDASH: the dashboard palette ─────────────────────────────────────

        [CommandMethod("PIPEDASH", CommandFlags.Modal)]
        public void PipeDash()
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            PipeDashboard.Show(doc);
        }

        /// <summary>Invoked by the dashboard's "Pick" button so the prompt runs in a proper command context.</summary>
        [CommandMethod("PIPEDASHPICK", CommandFlags.Modal | CommandFlags.NoHistory)]
        public void PipeDashPick()
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            ObjectId id = PickStructure(doc.Editor);
            if (!id.IsNull) PipeDashboard.SetStructure(id);
        }

        // ── shared prompts ──────────────────────────────────────────────────────

        internal static ObjectId PickStructure(Editor ed)
        {
            var peo = new PromptEntityOptions("\nSelect the shared structure (manhole/junction)");
            peo.SetRejectMessage("\nThat is not a pipe network structure.");
            peo.AddAllowedClass(typeof(Structure), false);
            PromptEntityResult per = ed.GetEntity(peo);
            return per.Status == PromptStatus.OK ? per.ObjectId : ObjectId.Null;
        }

        private static SolverInput? PromptForInput(Editor ed, ObjectId structureId)
        {
            var input = new SolverInput { StructureId = structureId, MaxDepthBelowSurface = SolveDefaults.MaxDepthBelowSurface };

            // cover
            var pdo = new PromptDoubleOptions("\nMinimum cover (Enter = from rule set)")
            {
                AllowNone = true, AllowNegative = false, AllowZero = true,
                UseDefaultValue = SolveDefaults.CoverMinimum.HasValue,
                DefaultValue = SolveDefaults.CoverMinimum ?? 0,
            };
            PromptDoubleResult pdr = ed.GetDouble(pdo);
            if (pdr.Status == PromptStatus.Cancel) return null;
            input.CoverMinimum = pdr.Status == PromptStatus.OK ? pdr.Value : SolveDefaults.CoverMinimum;

            // mode
            var pko = new PromptKeywordOptions("\nOffset at structure [Crowns/Inverts/Drop]") { AllowNone = true };
            pko.Keywords.Add("Crowns"); pko.Keywords.Add("Inverts"); pko.Keywords.Add("Drop");
            pko.Keywords.Default = SolveDefaults.Mode switch { OffsetMode.MatchInverts => "Inverts", OffsetMode.ManualDrop => "Drop", _ => "Crowns" };
            PromptResult pkr = ed.GetKeywords(pko);
            if (pkr.Status == PromptStatus.Cancel) return null;
            string m = pkr.Status == PromptStatus.OK ? pkr.StringResult : pko.Keywords.Default;
            input.Mode = m == "Inverts" ? OffsetMode.MatchInverts : m == "Drop" ? OffsetMode.ManualDrop : OffsetMode.MatchCrowns;

            if (input.Mode == OffsetMode.ManualDrop)
            {
                var ddo = new PromptDoubleOptions("\nDrop across structure (outgoing invert below lowest incoming)")
                { AllowNegative = false, AllowZero = true, UseDefaultValue = true, DefaultValue = SolveDefaults.ManualDrop };
                PromptDoubleResult ddr = ed.GetDouble(ddo);
                if (ddr.Status == PromptStatus.Cancel) return null;
                input.ManualDrop = ddr.Status == PromptStatus.OK ? ddr.Value : SolveDefaults.ManualDrop;
            }

            // governing pipe (optional)
            var geo = new PromptEntityOptions("\nGoverning pipe (Enter = most restrictive cover)") { AllowNone = true };
            geo.SetRejectMessage("\nThat is not a pipe.");
            geo.AddAllowedClass(typeof(Pipe), false);
            PromptEntityResult ger = ed.GetEntity(geo);
            if (ger.Status == PromptStatus.Cancel) return null;
            if (ger.Status == PromptStatus.OK) input.GoverningPipeId = ger.ObjectId;

            SolveDefaults.CoverMinimum = input.CoverMinimum;
            SolveDefaults.Mode = input.Mode;
            if (input.Mode == OffsetMode.ManualDrop) SolveDefaults.ManualDrop = input.ManualDrop;
            return input;
        }

        internal static void PrintReport(Editor ed, SolveReport r)
        {
            ed.WriteMessage($"\n── {r.Structure.Name}  rim {r.Structure.Rim:0.00}  surf {r.Structure.SurfaceElevation:0.00}  " +
                            $"surface '{r.Structure.ReferenceSurface}'  mode {r.OffsetMode}  governing '{r.GoverningPipe}'  slopes as {r.SlopeUnits}");
            ed.WriteMessage("\n{0,-16} {1,5} {2,-8} {3,9} {4,9} {5,8} | {6,9} {7,9} {8,8} | {9,8} {10,-10}",
                "Pipe", "Dia", "Flow", "OldInv@S", "NewInv@S", "Cover@S", "OldInvFar", "NewInvFar", "CoverFar", "Slope", "Status");
            foreach (PipeReport p in r.Pipes)
            {
                ed.WriteMessage("\n{0,-16} {1,5:0.##} {2,-8} {3,9:0.000} {4,9} {5,8} | {6,9:0.000} {7,9} {8,8} | {9,8} {10,-10}",
                    Trunc(p.Name, 16), p.InnerDiameter, p.Flow,
                    p.AtStructure.OldInvert, F(p.AtStructure.NewInvert), F(p.AtStructure.NewCover, "0.00"),
                    p.FarEnd.OldInvert, F(p.FarEnd.NewInvert), F(p.FarEnd.NewCover, "0.00"),
                    F(p.NewSlope, "0.0000"), p.Status);
                foreach (string v in p.Violations) ed.WriteMessage($"\n    ! {v}");
            }
            foreach (string w in r.Warnings) ed.WriteMessage($"\n  warning: {w}");
            foreach (ConflictReport c in r.Conflicts)
                ed.WriteMessage($"\n  CONFLICT [{c.Kind}] {c.Message}" +
                                (c.SuggestedStructureInvert is double s ? $"  (try structureInvertOverride={s:0.000})" : ""));
            foreach (string e in r.Errors) ed.WriteMessage($"\n  ERROR: {e}");
        }

        private static string F(double? v, string fmt = "0.000") => v.HasValue ? v.Value.ToString(fmt) : "-";
        private static string Trunc(string s, int n) => string.IsNullOrEmpty(s) ? "" : s.Length <= n ? s : s.Substring(0, n);
    }
}
