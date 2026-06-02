using Autodesk.AutoCAD.Runtime;

[assembly: ExtensionApplication(typeof(CADAgent.App))]

namespace CADAgent
{
    public class App : IExtensionApplication
    {
        private static readonly HttpBridge _bridge = new();

        public void Initialize() => _bridge.Start();
        public void Terminate() => _bridge.Stop();
    }
}
