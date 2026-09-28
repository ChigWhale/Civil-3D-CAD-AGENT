using System;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using Autodesk.Windows;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CADAgent
{
    /// <summary>
    /// The CAD-AGENT ribbon tab. One panel, one button: "Start Dashboard", which
    /// runs the same thing as the CADAGENT command (Install.OpenDashboard), so a
    /// user with only Civil 3D and this plugin can bring the dashboard up without
    /// Claude Desktop, ChatGPT or a terminal.
    ///
    /// The ribbon does not always exist when the plugin loads (autoloaded
    /// plugins initialise before it is built) and AutoCAD rebuilds it from the
    /// CUI on a workspace switch, dropping tabs added in code. So the tab is
    /// (re)added whenever it is found missing: at load, on ribbon item
    /// initialisation, and on a WSCURRENT change.
    /// </summary>
    internal static class Ribbon
    {
        public const string TabId = "CADAGENT_TAB";
        public const string TabTitle = "CAD-AGENT";

        private static bool _hooked;

        public static void Ensure()
        {
            try
            {
                Build();
                if (_hooked) return;
                _hooked = true;
                ComponentManager.ItemInitialized += (s, e) => Build();
                AcadApp.SystemVariableChanged += (s, e) =>
                {
                    if (string.Equals(e.Name, "WSCURRENT", StringComparison.OrdinalIgnoreCase)) Build();
                };
            }
            catch
            {
                // The ribbon is a convenience; the bridge and the commands must load regardless.
            }
        }

        private static void Build()
        {
            try
            {
                RibbonControl? ribbon = ComponentManager.Ribbon;
                if (ribbon == null || ribbon.FindTab(TabId) != null) return;

                var tab = new RibbonTab { Title = TabTitle, Id = TabId };
                var source = new RibbonPanelSource { Title = "Dashboard" };
                tab.Panels.Add(new RibbonPanel { Source = source });

                var start = new RibbonButton
                {
                    Id = "CADAGENT_START_DASHBOARD",
                    Text = "Start\nDashboard",
                    ShowText = true,
                    ShowImage = true,
                    Image = Glyph(16),
                    LargeImage = Glyph(32),
                    Size = RibbonItemSize.Large,
                    Orientation = System.Windows.Controls.Orientation.Vertical,
                    ToolTip = "Start the CAD-Agent dashboard and open it in your browser "
                              + "(starts the local services if they are not running). "
                              + "Command line: CADAGENT",
                    CommandHandler = new OpenDashboardCommand(),
                };
                source.Items.Add(start);

                ribbon.Tabs.Add(tab);
            }
            catch
            {
                // Same reason as above: never let a ribbon problem take the plugin down.
            }
        }

        /// <summary>A play-triangle on a rounded square, drawn in code so the DLL
        /// needs no image resources.</summary>
        private static ImageSource Glyph(double size)
        {
            var group = new DrawingGroup();
            var back = new RectangleGeometry(new Rect(0, 0, size, size), size * 0.2, size * 0.2);
            group.Children.Add(new GeometryDrawing(new SolidColorBrush(Color.FromRgb(0x1E, 0x6F, 0xD9)), null, back));

            var tri = new PathFigure { StartPoint = new Point(size * 0.36, size * 0.25), IsClosed = true };
            tri.Segments.Add(new LineSegment(new Point(size * 0.76, size * 0.5), true));
            tri.Segments.Add(new LineSegment(new Point(size * 0.36, size * 0.75), true));
            var play = new PathGeometry();
            play.Figures.Add(tri);
            group.Children.Add(new GeometryDrawing(Brushes.White, null, play));

            var image = new DrawingImage(group);
            image.Freeze();
            return image;
        }

        private sealed class OpenDashboardCommand : ICommand
        {
            public event EventHandler? CanExecuteChanged { add { } remove { } }
            public bool CanExecute(object? parameter) => true;

            public void Execute(object? parameter)
            {
                // Runs on the UI thread from the ribbon; starting a detached process
                // is all it does, so no document lock or command context is needed.
                var ed = AcadApp.DocumentManager.MdiActiveDocument?.Editor;
                Install.OpenDashboard(msg => ed?.WriteMessage("\n" + msg));
            }
        }
    }
}
