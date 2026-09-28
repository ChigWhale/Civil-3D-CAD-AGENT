using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using Autodesk.AutoCAD.Runtime;
using Newtonsoft.Json.Linq;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Application;

[assembly: CommandClass(typeof(CADAgent.InstallCommands))]

namespace CADAgent
{
    /// <summary>
    /// A packaged install writes install.json beside this DLL (launcher.py install):
    ///
    ///   {"program_root": "...\app", "python": "...\python\python.exe",
    ///    "pythonw": "...\python\pythonw.exe", "launcher": "...\app\launcher.py",
    ///    "autostart": true, "version": "1.0.0"}
    ///
    /// With it, the plugin knows where its Python and code live and can start the
    /// dashboard and model server when Civil 3D loads. Without it (a developer's
    /// NETLOAD from bin\) nothing starts on load, but "Start Dashboard" on the
    /// CAD-AGENT ribbon tab and the CADAGENT command still work: launcher.py is
    /// found by walking up from the DLL (or CAD_AGENT_ROOT) and Python comes from
    /// CAD_PYTHON or "python" on PATH. Either way the launcher's `open` command
    /// does the work -- starts the model server and the dashboard if they are not
    /// up, then opens the browser -- so Claude Desktop is never required.
    /// </summary>
    internal static class Install
    {
        public const string DashboardUrl = "http://localhost:8765";

        private static JObject? _manifest;
        private static bool _read;

        public static JObject? Manifest
        {
            get
            {
                if (!_read)
                {
                    _read = true;
                    try
                    {
                        string? dir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
                        string path = Path.Combine(dir ?? "", "install.json");
                        if (File.Exists(path)) _manifest = JObject.Parse(File.ReadAllText(path));
                    }
                    catch { _manifest = null; }
                }
                return _manifest;
            }
        }

        public static bool Packaged => Manifest != null;
        public static string? ProgramRoot => Value("program_root", Directory.Exists);
        /// <summary>The bundled interpreter, or null in a developer build (callers
        /// then fall back to CAD_PYTHON / PATH themselves, as HARVEST does).</summary>
        public static string? Python => Value("python", File.Exists);
        public static string? PythonW => Value("pythonw", File.Exists) ?? Python;
        /// <summary>launcher.py: from the manifest, else the developer checkout above the DLL.</summary>
        public static string? Launcher => Value("launcher", File.Exists) ?? DevLauncher();
        public static bool AutoStart => Manifest?["autostart"]?.Value<bool>() ?? false;
        public static string? Version => Manifest?["version"]?.ToString();

        private static string? Value(string key, Func<string, bool> exists)
        {
            string? v = Manifest?[key]?.ToString();
            return !string.IsNullOrWhiteSpace(v) && exists(v) ? v : null;
        }

        private static string? DevLauncher()
        {
            string? env = Environment.GetEnvironmentVariable("CAD_AGENT_ROOT");
            if (!string.IsNullOrWhiteSpace(env))
            {
                string p = Path.Combine(env, "launcher.py");
                if (File.Exists(p)) return p;
            }
            string? dir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            for (int i = 0; dir != null && i < 8; i++)
            {
                string p = Path.Combine(dir, "launcher.py");
                if (File.Exists(p)) return p;
                dir = Path.GetDirectoryName(dir);
            }
            return null;
        }

        /// <summary>The interpreter that runs launcher.py: bundled pythonw, else
        /// CAD_PYTHON, else "python" from PATH (started without a window either way).</summary>
        private static string LauncherPython()
        {
            if (PythonW is string bundled) return bundled;
            string? env = Environment.GetEnvironmentVariable("CAD_PYTHON");
            return string.IsNullOrWhiteSpace(env) ? "python" : env;
        }

        /// <summary>Runs the launcher without a window. Never throws: no launcher
        /// means the DLL was loaded from somewhere outside a checkout or a
        /// package, and the plugin must load anyway.</summary>
        public static bool StartServices(bool openBrowser)
        {
            try
            {
                string? launcher = Launcher;
                if (launcher == null) return false;
                var psi = new ProcessStartInfo(LauncherPython(),
                                               $"\"{launcher}\" {(openBrowser ? "open" : "start --no-browser")}")
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WorkingDirectory = Path.GetDirectoryName(launcher) ?? "",
                };
                Process.Start(psi);
                return true;
            }
            catch { return false; }
        }

        /// <summary>What the ribbon button and the CADAGENT command both do: start
        /// the services if they are down and open the dashboard in the browser.
        /// `report` receives one line for the command line (may be null).</summary>
        public static void OpenDashboard(Action<string>? report)
        {
            string tag = Packaged ? $"CAD-Agent {Version ?? ""}".TrimEnd() : "CAD-Agent (developer build)";
            if (StartServices(openBrowser: true))
            {
                report?.Invoke($"{tag}: starting the dashboard; a browser tab opens once it answers "
                               + "(a few seconds, up to a minute the first time).");
                return;
            }
            // No launcher.py anywhere near the DLL: the best that can be done is to
            // open the address and let the user see whether anything is listening.
            try
            {
                Process.Start(new ProcessStartInfo(DashboardUrl) { UseShellExecute = true });
                report?.Invoke($"{tag}: launcher.py not found above the plugin DLL (set CAD_AGENT_ROOT); "
                               + $"opened {DashboardUrl} in case the dashboard is already running.");
            }
            catch (System.Exception e)
            {
                report?.Invoke($"{tag}: could not open a browser ({e.Message}).");
            }
        }
    }

    public static class InstallCommands
    {
        /// <summary>CADAGENT: the command behind the ribbon's "Start Dashboard".</summary>
        [CommandMethod("CADAGENT")]
        public static void OpenDashboard()
        {
            var ed = AcadApp.DocumentManager.MdiActiveDocument?.Editor;
            Install.OpenDashboard(msg => ed?.WriteMessage("\n" + msg));
        }
    }
}
