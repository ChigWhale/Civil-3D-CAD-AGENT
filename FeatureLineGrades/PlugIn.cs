using Autodesk.AutoCAD.Runtime;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Core.Application;

[assembly: ExtensionApplication(typeof(FeatureLineSync.GradesPlugIn))]

namespace FeatureLineSync
{
    /// <summary>Labels-only build: FLGRADES and nothing that writes to the drawing.</summary>
    public class GradesPlugIn : IExtensionApplication
    {
        public void Initialize()
        {
            AcApp.DocumentManager.MdiActiveDocument?.Editor.WriteMessage(
                "\nFeatureLineGrades loaded. FLGRADES toggles live grade labels on feature lines (display only, nothing is changed or saved).\n");
        }

        public void Terminate() { }
    }
}
