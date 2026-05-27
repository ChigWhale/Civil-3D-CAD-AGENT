using System.Windows.Forms.Integration;
using Autodesk.AutoCAD.Runtime;
using Autodesk.AutoCAD.Windows;

[assembly: ExtensionApplication(typeof(CADAgent.App))]

namespace CADAgent
{
    public class App : IExtensionApplication
    {
        private static PaletteSet? _paletteSet;

        public static void ShowPalette()
        {
            if (_paletteSet == null)
            {
                _paletteSet = new PaletteSet(
                    "CAD Agent",
                    new System.Guid("A3F7C2D1-84BE-4A10-9E6F-1234567890AB")
                );

                var host = new ElementHost
                {
                    Child = new AgentPaletteControl(),
                    Dock  = System.Windows.Forms.DockStyle.Fill,
                };

                _paletteSet.Add("Agent", host);
            }

            _paletteSet.Visible = true;
        }

        public void Initialize() => ShowPalette();

        public void Terminate() { }
    }
}
