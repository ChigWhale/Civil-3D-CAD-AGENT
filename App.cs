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
        }
        public void Terminate() => _bridge.Stop();
    }
}
