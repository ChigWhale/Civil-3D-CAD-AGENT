using System;
using System.Diagnostics;
using Autodesk.AutoCAD.Runtime;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Application;

[assembly: ExtensionApplication(typeof(CADAgent.App))]

namespace CADAgent
{
    public class App : IExtensionApplication
    {
        private static readonly HttpBridge _bridge = new();

        // The bridge listens on one fixed port. A second Civil 3D session, or a
        // previous one that is still shutting down, holds it, and Start() throws.
        // That must never take the ribbon and the commands down with it, so each
        // part of start-up stands on its own and the bridge is retried for a while.
        private const int BridgeRetrySeconds = 10;
        private const int BridgeRetryLimit = 18;        // three minutes
        private static string? _pendingMessage;
        private static bool _bridgeUp;
        private static int _bridgeAttempts;
        private static readonly Stopwatch _sinceAttempt = new();
        private static bool _idleHooked;

        public void Initialize()
        {
            // The CAD-AGENT ribbon tab and the commands first: they need nothing
            // else, and they are how a person sees that the plugin loaded at all.
            Ribbon.Ensure();

            // Civil 3D handlers that live in this assembly join the PipeInvertSetter
            // registry so POST /handler can dispatch them like the pipe solvers.
            try
            {
                PipeInvertSetter.HandlerRegistry.Register(new GradingLabelHandler());
                PipeInvertSetter.HandlerRegistry.Register(new LabelSpacingHandler());
            }
            catch (System.Exception e)
            {
                Say($"CAD-Agent: the label handlers did not register ({e.Message}).");
            }

            TryStartBridge(first: true);
            HookIdle();

            // A packaged install (install.json beside the DLL) starts the dashboard
            // and the model server here, so the user never opens a terminal.
            try { if (Install.AutoStart) Install.StartServices(openBrowser: false); }
            catch { }
        }

        public void Terminate()
        {
            try { _bridge.Stop(); } catch { }
        }

        private static void TryStartBridge(bool first)
        {
            _bridgeAttempts++;
            _sinceAttempt.Restart();
            try
            {
                _bridge.Start();
                _bridgeUp = true;
                if (!first) Say("CAD-Agent: the HTTP bridge is up on port 5199 now.");
            }
            catch (System.Exception e)
            {
                try { _bridge.Stop(); } catch { }      // drop the half-made listener before the next try
                if (first)
                    Say("CAD-Agent: loaded, but the HTTP bridge could not start on port 5199 "
                        + $"({e.Message.Trim()}). Another Civil 3D session, or one that is still closing, "
                        + "is probably holding the port. The ribbon and commands work; the dashboard cannot "
                        + "reach this session until the port is free. Retrying for a few minutes.");
                else if (_bridgeAttempts >= BridgeRetryLimit)
                    Say("CAD-Agent: port 5199 is still held by another session; the bridge stays off in "
                        + "this one. Close the other Civil 3D (or end a leftover acad.exe) and restart.");
            }
        }

        private static void HookIdle()
        {
            if (_idleHooked) return;
            _idleHooked = true;
            AcadApp.Idle += OnIdle;
        }

        private static void OnIdle(object? sender, EventArgs e)
        {
            // print what start-up could not say yet: at load there may be no drawing
            if (_pendingMessage != null)
            {
                var ed = AcadApp.DocumentManager.MdiActiveDocument?.Editor;
                if (ed != null)
                {
                    try { ed.WriteMessage("\n" + _pendingMessage + "\n"); } catch { }
                    _pendingMessage = null;
                }
            }
            if (!_bridgeUp && _bridgeAttempts < BridgeRetryLimit
                && _sinceAttempt.Elapsed.TotalSeconds >= BridgeRetrySeconds)
                TryStartBridge(first: false);
            if (_bridgeUp && _pendingMessage == null || (!_bridgeUp && _bridgeAttempts >= BridgeRetryLimit && _pendingMessage == null))
            {
                AcadApp.Idle -= OnIdle;
                _idleHooked = false;
            }
        }

        private static void Say(string message)
        {
            var ed = AcadApp.DocumentManager.MdiActiveDocument?.Editor;
            if (ed != null)
            {
                try { ed.WriteMessage("\n" + message + "\n"); return; } catch { }
            }
            _pendingMessage = _pendingMessage == null ? message : _pendingMessage + "\n" + message;
            HookIdle();
        }
    }
}
