using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.Runtime;
using Newtonsoft.Json.Linq;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Application;

// The assembly names its command classes explicitly (see ModelSpaceCollector,
// ObjectGraph, CreateGraphs); once any CommandClass attribute exists AutoCAD
// scans ONLY those classes, so every class with commands must be listed here.
[assembly: CommandClass(typeof(CADAgent.HarvestCommands))]

namespace CADAgent
{
    /// <summary>
    /// HARVEST: add the drawing that is open right now to standards/global.json.
    ///
    /// The command collects the same model-space snapshot the HTTP bridge serves,
    /// plus the layer table, block definitions and drawing settings, writes them to
    /// one temp file and hands that file to harvest.py, which owns the merge rules
    /// (one observation per drawing, rebuilt catalog, person fields kept). The
    /// drawing itself is never written to.
    ///
    ///   HARVEST        harvest the active drawing, print the result
    ///   HARVESTOPEN    open standards/global.md in the default viewer
    ///
    /// Where things are found: harvest.py by walking up from this DLL's folder
    /// (or CAD_AGENT_ROOT), python from CAD_PYTHON or "python" on PATH.
    /// </summary>
    public static class HarvestCommands
    {
        private const int PythonTimeoutMs = 180_000;

        [CommandMethod("HARVEST", CommandFlags.Modal)]
        public static void Harvest()
        {
            Document? doc = AcadApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            var ed = doc.Editor;
            try
            {
                string root = RepoRoot();
                string python = PythonExe();
                string source = Path.GetFileName(doc.Name);
                string dwgPath = File.Exists(doc.Name) ? Path.GetFullPath(doc.Name) : doc.Name;

                ed.WriteMessage($"\nHARVEST: reading {source} ...");
                string envelope = BuildEnvelope(doc, source, dwgPath);
                string tmp = Path.Combine(Path.GetTempPath(), $"harvest-{Guid.NewGuid():N}.json");
                File.WriteAllText(tmp, envelope, new UTF8Encoding(false));
                try
                {
                    var (code, output) = RunPython(python, root, $"harvest.py \"{tmp}\"");
                    foreach (var line in output.Split('\n'))
                        if (line.Trim().Length > 0) ed.WriteMessage("\n  " + line.TrimEnd('\r'));
                    ed.WriteMessage(code == 0 ? "\nHARVEST: done." : $"\nHARVEST: harvest.py exited with code {code}.");
                }
                finally
                {
                    try { File.Delete(tmp); } catch { }
                }
            }
            catch (System.Exception ex)
            {
                ed.WriteMessage($"\nHARVEST failed: {ex.Message}");
            }
        }

        [CommandMethod("HARVESTOPEN", CommandFlags.Modal)]
        public static void HarvestOpen()
        {
            Document? doc = AcadApp.DocumentManager.MdiActiveDocument;
            var ed = doc?.Editor;
            try
            {
                string dir = Environment.GetEnvironmentVariable("CAD_STANDARDS_DIR") is string d && d.Trim().Length > 0
                    ? d.Trim() : Path.Combine(RepoRoot(), "standards");
                string md = Path.Combine(dir, "global.md");
                if (!File.Exists(md)) { ed?.WriteMessage($"\nHARVESTOPEN: {md} does not exist yet; run HARVEST first."); return; }
                Process.Start(new ProcessStartInfo(md) { UseShellExecute = true });
            }
            catch (System.Exception ex)
            {
                ed?.WriteMessage($"\nHARVESTOPEN failed: {ex.Message}");
            }
        }

        // ── pieces ────────────────────────────────────────────────────────────────

        /// <summary>{ snapshot, layer_defs, block_defs, settings, source, path } as harvest.py expects.</summary>
        private static string BuildEnvelope(Document doc, string source, string dwgPath)
        {
            ModelSpaceCollector.Collect(doc);
            string snapshot = ModelSpaceCollector.GetLastJson(doc.Name);
            var env = new JObject
            {
                ["harvest"] = 1,
                ["source"] = source,
                ["path"] = dwgPath,
                ["snapshot"] = JObject.Parse(snapshot),
                ["layer_defs"] = Section(CadCommands.Execute("list_layers", "{\"count_entities\": false}", doc), "layers"),
                ["block_defs"] = Section(CadCommands.Execute("list_block_definitions", "{}", doc), "blocks"),
                ["settings"] = Settings(CadCommands.Execute("get_drawing_settings", "{}", doc)),
            };
            return env.ToString(Newtonsoft.Json.Formatting.None);
        }

        private static JToken Section(string json, string key)
        {
            try
            {
                var o = JObject.Parse(json);
                if (o["ok"]?.Value<bool>() == false) return new JArray();
                return o[key] as JArray ?? new JArray();
            }
            catch { return new JArray(); }
        }

        private static JToken Settings(string json)
        {
            try
            {
                var o = JObject.Parse(json);
                if (o["ok"]?.Value<bool>() == false) return new JObject();
                o.Remove("ok"); o.Remove("message"); o.Remove("file");
                return o;
            }
            catch { return new JObject(); }
        }

        private static string RepoRoot()
        {
            string? installed = Install.ProgramRoot;      // a packaged install says where app\ is
            if (installed != null && File.Exists(Path.Combine(installed, "harvest.py"))) return installed;
            string? env = Environment.GetEnvironmentVariable("CAD_AGENT_ROOT");
            if (!string.IsNullOrWhiteSpace(env) && File.Exists(Path.Combine(env, "harvest.py"))) return env;
            string? dir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            for (int i = 0; dir != null && i < 8; i++)
            {
                if (File.Exists(Path.Combine(dir, "harvest.py"))) return dir;
                dir = Path.GetDirectoryName(dir);
            }
            throw new InvalidOperationException("harvest.py not found above the plugin DLL; set CAD_AGENT_ROOT to the CAD-Agent folder");
        }

        private static string PythonExe()
        {
            if (Install.Python is string bundled) return bundled;
            string? env = Environment.GetEnvironmentVariable("CAD_PYTHON");
            return string.IsNullOrWhiteSpace(env) ? "python" : env;
        }

        private static (int code, string output) RunPython(string python, string root, string args)
        {
            var psi = new ProcessStartInfo(python, args)
            {
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            psi.Environment["PYTHONIOENCODING"] = "utf-8";
            using var p = Process.Start(psi) ?? throw new InvalidOperationException($"could not start {python}");
            string stdout = p.StandardOutput.ReadToEnd();
            string stderr = p.StandardError.ReadToEnd();
            if (!p.WaitForExit(PythonTimeoutMs))
            {
                try { p.Kill(); } catch { }
                throw new TimeoutException("harvest.py did not finish in time");
            }
            string output = stdout + (stderr.Trim().Length > 0 ? "\n" + stderr : "");
            return (p.ExitCode, output);
        }
    }
}
