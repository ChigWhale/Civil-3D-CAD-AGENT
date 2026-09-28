using Autodesk.AutoCAD.Runtime;

[assembly: ExtensionApplication(typeof(CADAgent.App))]

namespace CADAgent
{
    public class App : IExtensionApplication
    {
        private static readonly HttpBridge _bridge = new();

        public void Initialize()
        {
            // Civil 3D handlers that live in this assembly join the PipeInvertSetter registry
            // so POST /handler can dispatch them like the pipe solvers.
            PipeInvertSetter.HandlerRegistry.Register(new GradingLabelHandler());
            PipeInvertSetter.HandlerRegistry.Register(new LabelSpacingHandler());
            _bridge.Start();
            // The CAD-AGENT ribbon tab: "Start Dashboard" without Claude Desktop
            // or a terminal. Built now if the ribbon exists, else when it does.
            Ribbon.Ensure();
            // A packaged install (install.json beside the DLL) starts the dashboard
            // and the model server here, so the user never opens a terminal.
            if (Install.AutoStart) Install.StartServices(openBrowser: false);
        }
        public void Terminate() => _bridge.Stop();
    }
}
