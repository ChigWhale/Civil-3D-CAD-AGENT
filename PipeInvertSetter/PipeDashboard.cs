using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Media;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Windows;
using Autodesk.Civil.ApplicationServices;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Core.Application;
using AcDocument = Autodesk.AutoCAD.ApplicationServices.Document;
using WpfBinding = System.Windows.Data.Binding;

namespace PipeInvertSetter
{
    /// <summary>
    /// Modeless Civil 3D palette that shows the whole picture at once: the selected
    /// structure, every pipe attached to it (old vs. proposed inverts, cover, slope),
    /// the conflicts the solver refuses to resolve on its own, and an overview of the
    /// entire network (all pipes + all structures) so nothing is hidden behind a prompt.
    /// </summary>
    public static class PipeDashboard
    {
        private static PaletteSet? _ps;
        private static PipeDashboardControl? _ctl;
        private static readonly Guid PaletteGuid = new("7A0D8C7E-4B1B-4C0E-9E3B-2F3B7B1E5C11");

        public static void Show(AcDocument doc)
        {
            if (_ps == null)
            {
                _ps = new PaletteSet("Pipe Elevation Solver", PaletteGuid)
                {
                    Style = PaletteSetStyles.ShowPropertiesMenu | PaletteSetStyles.ShowAutoHideButton | PaletteSetStyles.ShowCloseButton,
                    MinimumSize = new System.Drawing.Size(720, 420),
                    KeepFocus = false,
                };
                _ctl = new PipeDashboardControl();
                _ps.AddVisual("Solver", _ctl);
            }
            _ps.Visible = true;
            _ctl!.LoadNetworks();
        }

        public static void SetStructure(ObjectId id) => _ctl?.SetStructure(id);

        public static void SetRunStartEnd(ObjectId start, ObjectId end) => _ctl?.SetRunStartEnd(start, end);

        public static void RefreshIfOpen()
        {
            if (_ps?.Visible == true) _ctl?.RefreshAll();
        }
    }

    public sealed class PipeDashboardControl : UserControl
    {
        // ── widgets ────────────────────────────────────────────────────────
        private readonly ComboBox _netCombo = new() { MinWidth = 150 };
        private readonly ComboBox _structCombo = new() { MinWidth = 150 };
        private readonly TextBlock _info = new() { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(6, 2, 6, 2) };
        private readonly TextBox _cover = new() { Width = 60, ToolTip = "Minimum cover (ft). Blank = read from each pipe's rule set." };
        private readonly ComboBox _mode = new() { Width = 110 };
        private readonly TextBox _drop = new() { Width = 55, ToolTip = "Drop across the structure (ManualDrop mode only)." };
        private readonly ComboBox _gov = new() { MinWidth = 120, ToolTip = "Pipe that fixes the datum at the structure. Auto = most restrictive cover." };
        private readonly TextBox _override = new() { Width = 70, ToolTip = "Explicit invert for the governing pipe at the structure. Use it to resolve a reported conflict." };
        private readonly TextBox _maxDepth = new() { Width = 45, ToolTip = "Sanity limit: inverts deeper than this below the surface are rejected." };
        private readonly ComboBox _slopeUnits = new() { Width = 75 };
        private readonly Button _compute = new() { Content = "Compute", Padding = new Thickness(10, 2, 10, 2), FontWeight = FontWeights.Bold };
        private readonly Button _apply = new() { Content = "Apply", Padding = new Thickness(10, 2, 10, 2), IsEnabled = false };
        private readonly Button _copy = new() { Content = "Copy JSON", Padding = new Thickness(8, 2, 8, 2), IsEnabled = false };
        private readonly DataGrid _pipes = MakeGrid();
        private readonly ListBox _messages = new() { MinHeight = 60, MaxHeight = 160 };
        private readonly DataGrid _netPipes = MakeGrid();
        private readonly DataGrid _netStructs = MakeGrid();
        private readonly TextBlock _status = new() { Margin = new Thickness(6, 2, 6, 2), Foreground = Brushes.DimGray };

        // ── run tab (elevations along an alignment / network path) ─────────
        private readonly ComboBox _alignCombo = new() { MinWidth = 140, ToolTip = "Structures within the tolerance of this alignment form the run, in station order." };
        private readonly TextBox _tol = new() { Width = 40, Text = "25", ToolTip = "Max offset (ft) from the alignment for a structure to be on the run." };
        private readonly ComboBox _flowCombo = new() { Width = 95, ToolTip = "auto = from pipe flow directions, else downhill along the ground." };
        private readonly CheckBox _holdStart = new() { Content = "Hold start", VerticalAlignment = VerticalAlignment.Center, ToolTip = "Keep the existing invert at the upstream end (tie-in)." };
        private readonly CheckBox _holdEnd = new() { Content = "Hold end", VerticalAlignment = VerticalAlignment.Center, ToolTip = "Keep the existing invert at the downstream end (outfall / tie-in)." };
        private readonly Button _runPickStartEnd = new() { Content = "Pick start/end…", Padding = new Thickness(8, 2, 8, 2), ToolTip = "Run = shortest network path between two picked structures (instead of an alignment)." };
        private readonly Button _runSolve = new() { Content = "Solve run", Padding = new Thickness(10, 2, 10, 2), FontWeight = FontWeights.Bold };
        private readonly Button _runEval = new() { Content = "Re-check edits", Padding = new Thickness(8, 2, 8, 2), IsEnabled = false, ToolTip = "Recompute cover/slope from the inverts typed into the grid." };
        private readonly Button _runApply = new() { Content = "Apply run", Padding = new Thickness(10, 2, 10, 2), IsEnabled = false };
        private readonly Button _runCopy = new() { Content = "Copy JSON", Padding = new Thickness(8, 2, 8, 2), IsEnabled = false };
        private readonly DataGrid _runPipes = MakeGrid();
        private readonly DataGrid _runStructs = MakeGrid();
        private readonly ListBox _runMessages = new() { MinHeight = 50, MaxHeight = 140 };
        private readonly ProfileCanvas _profile = new() { Height = 170, Margin = new Thickness(0, 2, 0, 4) };
        private readonly List<(string name, string handle)> _alignments = new();
        private RunReport? _run;
        private RunInput? _runInput;

        // ── state ──────────────────────────────────────────────────────────
        private readonly List<(string name, string handle)> _networks = new();
        private ContextReport? _ctx;
        private SolveReport? _last;
        private SolverInput? _lastInput;
        private ObjectId _structureId = ObjectId.Null;
        private bool _suppress;

        public PipeDashboardControl()
        {
            Background = SystemColors.ControlBrush;
            BuildLayout();
            BuildColumns();
            _mode.ItemsSource = new[] { "MatchCrowns", "MatchInverts", "ManualDrop" };
            _mode.SelectedIndex = 0;
            _slopeUnits.ItemsSource = new[] { "auto", "ratio", "percent" };
            _slopeUnits.SelectedIndex = 0;
            _flowCombo.ItemsSource = new[] { "auto", "downstation", "upstation" };
            _flowCombo.SelectedIndex = 0;

            _runSolve.Click += (_, _) => Guard(SolveRun);
            _runEval.Click += (_, _) => Guard(EvaluateRun);
            _runApply.Click += (_, _) => Guard(ApplyRun);
            _runCopy.Click += (_, _) => Guard(() => { if (_run != null) Clipboard.SetText(_run.ToJson()); SetStatus("Run JSON copied to clipboard."); });
            _runPickStartEnd.Click += (_, _) => Guard(PickRunInDrawing);
            _runPipes.CellEditEnding += (_, e) =>
            {
                if (e.EditAction == DataGridEditAction.Commit)
                    Dispatcher.BeginInvoke(new Action(() => Guard(EvaluateRun)), System.Windows.Threading.DispatcherPriority.Background);
            };
            _runPipes.SelectionChanged += (_, _) => _profile.Highlight((_runPipes.SelectedItem as RunPipeRow)?.Index ?? -1);
            _maxDepth.Text = SolveDefaults.MaxDepthBelowSurface.ToString(CultureInfo.InvariantCulture);
            _drop.Text = SolveDefaults.ManualDrop.ToString(CultureInfo.InvariantCulture);
            _cover.Text = SolveDefaults.CoverMinimum?.ToString(CultureInfo.InvariantCulture) ?? "";

            _netCombo.SelectionChanged += (_, _) => { if (!_suppress) Guard(LoadNetworkContext); };
            _structCombo.SelectionChanged += (_, _) => { if (!_suppress) Guard(SelectStructureFromCombo); };
            _compute.Click += (_, _) => Guard(Compute);
            _apply.Click += (_, _) => Guard(Apply);
            _copy.Click += (_, _) => Guard(() => { if (_last != null) Clipboard.SetText(_last.ToJson()); SetStatus("Report JSON copied to clipboard."); });
            _netStructs.MouseDoubleClick += (_, _) => Guard(() =>
            {
                if (_netStructs.SelectedItem is NetworkStructureRow row)
                    SetStructure(IdResolver.FindStructure(Doc().Database, row.Handle));
            });
        }

        // ══════════════════════════════════════════════════════════════════
        //  layout
        // ══════════════════════════════════════════════════════════════════

        private void BuildLayout()
        {
            var root = new Grid();
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });   // toolbar
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });   // info
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });   // settings
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) }); // tabs
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });   // status

            var pick = new Button { Content = "Pick structure…", Padding = new Thickness(8, 2, 8, 2) };
            pick.Click += (_, _) => Guard(PickInDrawing);
            var refresh = new Button { Content = "Refresh", Padding = new Thickness(8, 2, 8, 2) };
            refresh.Click += (_, _) => Guard(RefreshAll);
            var zoom = new Button { Content = "Zoom to", Padding = new Thickness(8, 2, 8, 2) };
            zoom.Click += (_, _) => Guard(ZoomToStructure);

            var toolbar = Row(
                Lbl("Network"), _netCombo, Lbl("Structure"), _structCombo, pick, zoom, refresh);
            Grid.SetRow(toolbar, 0); root.Children.Add(toolbar);

            Grid.SetRow(_info, 1); root.Children.Add(_info);

            var settings = Row(
                Lbl("Min cover"), _cover, Lbl("Offset"), _mode, Lbl("Drop"), _drop,
                Lbl("Governing"), _gov, Lbl("Invert override"), _override,
                Lbl("Max depth"), _maxDepth, Lbl("Slope units"), _slopeUnits,
                _compute, _apply, _copy);
            Grid.SetRow(settings, 2); root.Children.Add(settings);

            var tabs = new TabControl { Margin = new Thickness(4) };

            // ── run tab: profile along an alignment, editable by hand ─────
            var runGrid = new Grid();
            runGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            runGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            runGrid.RowDefinitions.Add(new RowDefinition { Height = new GridLength(3, GridUnitType.Star) });
            runGrid.RowDefinitions.Add(new RowDefinition { Height = new GridLength(2, GridUnitType.Star) });
            runGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var runBar = Row(Lbl("Alignment"), _alignCombo, Lbl("± ft"), _tol, _runPickStartEnd,
                             Lbl("Flow"), _flowCombo, _holdStart, _holdEnd,
                             _runSolve, _runEval, _runApply, _runCopy);
            Grid.SetRow(runBar, 0); runGrid.Children.Add(runBar);
            var profileBox = new GroupBox { Header = "Profile (surface, rim, old inverts grey, new inverts blue, cover line dashed)", Content = _profile };
            Grid.SetRow(profileBox, 1); runGrid.Children.Add(profileBox);
            var pipesBox = new GroupBox { Header = "Pipes in flow order — type into 'New inv up/down' to set by hand, then Re-check / Apply", Content = _runPipes };
            Grid.SetRow(pipesBox, 2); runGrid.Children.Add(pipesBox);
            var structsBox = new GroupBox { Header = "Structures along the run", Content = _runStructs };
            Grid.SetRow(structsBox, 3); runGrid.Children.Add(structsBox);
            var runMsgBox = new GroupBox { Header = "Conflicts, warnings, errors", Content = _runMessages };
            Grid.SetRow(runMsgBox, 4); runGrid.Children.Add(runMsgBox);
            tabs.Items.Add(new TabItem { Header = "Run along alignment", Content = runGrid });

            var solveGrid = new Grid();
            solveGrid.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            solveGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            Grid.SetRow(_pipes, 0); solveGrid.Children.Add(_pipes);
            var msgBox = new GroupBox { Header = "Conflicts, warnings, errors (nothing is applied while any conflict/error remains)", Content = _messages, Margin = new Thickness(0, 4, 0, 0) };
            Grid.SetRow(msgBox, 1); solveGrid.Children.Add(msgBox);
            tabs.Items.Add(new TabItem { Header = "Solve at structure", Content = solveGrid });
            tabs.Items.Add(new TabItem { Header = "All pipes in network", Content = _netPipes });
            tabs.Items.Add(new TabItem { Header = "All structures in network", Content = _netStructs });
            Grid.SetRow(tabs, 3); root.Children.Add(tabs);

            Grid.SetRow(_status, 4); root.Children.Add(_status);
            Content = root;
        }

        private static WrapPanel Row(params UIElement[] items)
        {
            var p = new WrapPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(4, 2, 4, 2) };
            foreach (UIElement e in items)
            {
                if (e is FrameworkElement fe) fe.Margin = new Thickness(2, 2, 6, 2);
                p.Children.Add(e);
            }
            return p;
        }

        private static TextBlock Lbl(string text) => new() { Text = text + ":", VerticalAlignment = VerticalAlignment.Center, Foreground = Brushes.DimGray };

        private static DataGrid MakeGrid() => new()
        {
            AutoGenerateColumns = false,
            IsReadOnly = true,
            CanUserAddRows = false,
            HeadersVisibility = DataGridHeadersVisibility.Column,
            GridLinesVisibility = DataGridGridLinesVisibility.Horizontal,
            SelectionMode = DataGridSelectionMode.Single,
            FontSize = 11.5,
            RowHeaderWidth = 0,
        };

        private void BuildColumns()
        {
            // ── solve grid ────────────────────────────────────────────────
            AddCol(_pipes, "Pipe", "Name");
            AddCol(_pipes, "Dia", "InnerDiameter", "0.##");
            AddCol(_pipes, "Flow", "Flow");
            AddCol(_pipes, "End@Str", "StructureEnd");
            AddCol(_pipes, "Gov", "IsGoverning");
            AddCol(_pipes, "Surf@Str", "AtStructure.Surface", "0.00");
            AddCol(_pipes, "Old inv@Str", "AtStructure.OldInvert", "0.000");
            AddCol(_pipes, "New inv@Str", "AtStructure.NewInvert", "0.000", bold: true);
            AddCol(_pipes, "Cover@Str", "AtStructure.NewCover", "0.00");
            AddCol(_pipes, "Far structure", "FarEnd.Structure");
            AddCol(_pipes, "Surf@Far", "FarEnd.Surface", "0.00");
            AddCol(_pipes, "Old inv@Far", "FarEnd.OldInvert", "0.000");
            AddCol(_pipes, "New inv@Far", "FarEnd.NewInvert", "0.000", bold: true);
            AddCol(_pipes, "Cover@Far", "FarEnd.NewCover", "0.00");
            AddCol(_pipes, "Min cover along", "NewMinCoverAlongPipe", "0.00");
            AddCol(_pipes, "Old slope", "OldSlope", "0.0000");
            AddCol(_pipes, "New slope", "NewSlope", "0.0000", bold: true);
            AddCol(_pipes, "Min slope", "MinSlope", "0.0000");
            AddCol(_pipes, "Max slope", "MaxSlope", "0.0000");
            AddCol(_pipes, "Cover rule", "CoverMinimum", "0.00");
            AddCol(_pipes, "Rule set", "RuleSet");
            AddCol(_pipes, "Z conv.", "ZConvention");
            AddCol(_pipes, "Status", "Status", bold: true);
            _pipes.RowStyle = StatusRowStyle("Status",
                ("conflict", Brushes.MistyRose), ("error", Brushes.LightCoral),
                ("applied", Brushes.Honeydew), ("unchanged", Brushes.WhiteSmoke));

            // ── run pipes (two editable columns) ──────────────────────────
            _runPipes.IsReadOnly = false;
            AddCol(_runPipes, "#", "Index");
            AddCol(_runPipes, "Pipe", "Name");
            AddCol(_runPipes, "From", "Upstream");
            AddCol(_runPipes, "To", "Downstream");
            AddCol(_runPipes, "Dia", "InnerDiameter", "0.##");
            AddCol(_runPipes, "Length", "Length2D", "0.0");
            AddCol(_runPipes, "Surf up", "SurfaceUp", "0.00");
            AddCol(_runPipes, "Surf down", "SurfaceDown", "0.00");
            AddCol(_runPipes, "Old inv up", "OldUpInvert", "0.000");
            AddCol(_runPipes, "Old inv down", "OldDownInvert", "0.000");
            AddCol(_runPipes, "New inv up ✎", "NewUpInvert", null, bold: true, editable: true);
            AddCol(_runPipes, "New inv down ✎", "NewDownInvert", null, bold: true, editable: true);
            AddCol(_runPipes, "Cover up", "NewCoverUp", "0.00");
            AddCol(_runPipes, "Cover down", "NewCoverDown", "0.00");
            AddCol(_runPipes, "Min cover along", "NewMinCoverAlongPipe", "0.00");
            AddCol(_runPipes, "Cover req.", "CoverMinimum", "0.00");
            AddCol(_runPipes, "Old slope", "OldSlope", "0.0000");
            AddCol(_runPipes, "New slope", "NewSlope", "0.0000", bold: true);
            AddCol(_runPipes, "Min slope", "MinSlope", "0.0000");
            AddCol(_runPipes, "Max slope", "MaxSlope", "0.0000");
            AddCol(_runPipes, "Rule set", "RuleSet");
            AddCol(_runPipes, "Status", "Status", bold: true);
            _runPipes.RowStyle = StatusRowStyle("Status",
                ("conflict", Brushes.MistyRose), ("unsolved", Brushes.LightYellow),
                ("applied", Brushes.Honeydew), ("unchanged", Brushes.WhiteSmoke));

            AddCol(_runStructs, "#", "Index");
            AddCol(_runStructs, "Structure", "Name");
            AddCol(_runStructs, "Station", "Station", "0.00");
            AddCol(_runStructs, "Offset", "Offset", "0.0");
            AddCol(_runStructs, "Surface", "Surface", "0.00");
            AddCol(_runStructs, "Rim", "Rim", "0.00");
            AddCol(_runStructs, "Old inv in", "OldInvertIn", "0.000");
            AddCol(_runStructs, "New inv in", "NewInvertIn", "0.000", bold: true);
            AddCol(_runStructs, "Old inv out", "OldInvertOut", "0.000");
            AddCol(_runStructs, "New inv out", "NewInvertOut", "0.000", bold: true);
            AddCol(_runStructs, "Drop", "Drop", "0.000");
            AddCol(_runStructs, "Held", "FixedInvert", "0.000");
            AddCol(_runStructs, "Other pipes (untouched)", "OtherPipes");

            // ── network pipes ─────────────────────────────────────────────
            AddCol(_netPipes, "Pipe", "Name");
            AddCol(_netPipes, "From", "From");
            AddCol(_netPipes, "To", "To");
            AddCol(_netPipes, "Dia", "InnerDiameter", "0.##");
            AddCol(_netPipes, "Length", "Length2D", "0.0");
            AddCol(_netPipes, "Start inv", "StartInvert", "0.000");
            AddCol(_netPipes, "End inv", "EndInvert", "0.000");
            AddCol(_netPipes, "Slope", "Slope", "0.0000");
            AddCol(_netPipes, "Cover start", "CoverStart", "0.00");
            AddCol(_netPipes, "Cover end", "CoverEnd", "0.00");
            AddCol(_netPipes, "Min cover rule", "MinCoverRule", "0.00");
            AddCol(_netPipes, "Cover violated", "MinCoverViolated");
            AddCol(_netPipes, "Flow", "Flow");
            AddCol(_netPipes, "Rule set", "RuleSet");
            AddCol(_netPipes, "Handle", "Handle");
            _netPipes.RowStyle = StatusRowStyle("MinCoverViolated", ("True", Brushes.MistyRose));

            // ── network structures ────────────────────────────────────────
            AddCol(_netStructs, "Structure", "Name");
            AddCol(_netStructs, "Rim", "Rim", "0.00");
            AddCol(_netStructs, "Sump", "Sump", "0.00");
            AddCol(_netStructs, "Surface", "SurfaceElevation", "0.00");
            AddCol(_netStructs, "# pipes", "ConnectedPipes");
            AddCol(_netStructs, "Pipes", "Pipes");
            AddCol(_netStructs, "X", "X", "0.00");
            AddCol(_netStructs, "Y", "Y", "0.00");
            AddCol(_netStructs, "Handle", "Handle");
        }

        private static void AddCol(DataGrid g, string header, string path, string? fmt = null, bool bold = false, bool editable = false)
        {
            var b = new WpfBinding(path) { Mode = editable ? BindingMode.TwoWay : BindingMode.OneWay };
            if (fmt != null) b.StringFormat = "{0:" + fmt + "}";
            var col = new DataGridTextColumn { Header = header, Binding = b, IsReadOnly = !editable };
            if (bold) col.FontWeight = FontWeights.SemiBold;
            if (editable)
            {
                var cellStyle = new Style(typeof(DataGridCell));
                cellStyle.Setters.Add(new Setter(Control.BackgroundProperty, Brushes.LightCyan));
                col.CellStyle = cellStyle;
            }
            g.Columns.Add(col);
        }

        private static Style StatusRowStyle(string property, params (string value, Brush brush)[] map)
        {
            var style = new Style(typeof(DataGridRow));
            foreach (var (value, brush) in map)
            {
                var trig = new DataTrigger { Binding = new WpfBinding(property), Value = value };
                trig.Setters.Add(new Setter(Control.BackgroundProperty, brush));
                style.Triggers.Add(trig);
            }
            return style;
        }

        // ══════════════════════════════════════════════════════════════════
        //  data
        // ══════════════════════════════════════════════════════════════════

        private static AcDocument Doc() =>
            AcApp.DocumentManager.MdiActiveDocument ?? throw new InvalidOperationException("No active drawing.");

        public void LoadNetworks()
        {
            Guard(() =>
            {
                AcDocument doc = Doc();
                _networks.Clear();
                using (doc.LockDocument())
                using (Transaction tr = doc.Database.TransactionManager.StartTransaction())
                {
                    foreach (ObjectId id in CivilDocument.GetCivilDocument(doc.Database).GetPipeNetworkIds())
                        if (tr.GetObject(id, OpenMode.ForRead, false) is Autodesk.Civil.DatabaseServices.Network n)
                            _networks.Add((n.Name, id.Handle.ToString()));
                    tr.Commit();
                }
                _alignments.Clear();
                using (doc.LockDocument())
                using (Transaction tr = doc.Database.TransactionManager.StartTransaction())
                {
                    foreach (ObjectId id in CivilDocument.GetCivilDocument(doc.Database).GetAlignmentIds())
                        if (tr.GetObject(id, OpenMode.ForRead, false) is Autodesk.Civil.DatabaseServices.Alignment a)
                            _alignments.Add((a.Name, id.Handle.ToString()));
                    tr.Commit();
                }
                _suppress = true;
                _netCombo.ItemsSource = _networks.Select(n => n.name).ToList();
                if (_networks.Count > 0 && _netCombo.SelectedIndex < 0) _netCombo.SelectedIndex = 0;
                int keepAlign = _alignCombo.SelectedIndex;
                _alignCombo.ItemsSource = _alignments.Select(a => a.name).ToList();
                if (_alignments.Count > 0) _alignCombo.SelectedIndex = keepAlign >= 0 && keepAlign < _alignments.Count ? keepAlign : 0;
                _suppress = false;
                if (_networks.Count == 0) SetStatus("This drawing has no pipe networks.");
                else LoadNetworkContext();
            });
        }

        private void LoadNetworkContext()
        {
            if (_netCombo.SelectedIndex < 0) return;
            AcDocument doc = Doc();
            ObjectId netId = IdResolver.FindNetwork(doc.Database, _networks[_netCombo.SelectedIndex].handle);
            _ctx = PipeElevationSolver.GetContext(doc, ObjectId.Null, netId);
            ShowContext();
        }

        private void ShowContext()
        {
            if (_ctx == null) return;
            _netPipes.ItemsSource = _ctx.Pipes;
            _netStructs.ItemsSource = _ctx.Structures;

            _suppress = true;
            _structCombo.ItemsSource = _ctx.Structures.Select(s => s.Name).ToList();
            if (!_structureId.IsNull)
            {
                string h = _structureId.Handle.ToString();
                int i = _ctx.Structures.FindIndex(s => s.Handle == h);
                _structCombo.SelectedIndex = i;
            }
            _suppress = false;

            int violated = _ctx.Pipes.Count(p => p.MinCoverViolated);
            SetStatus($"Network '{_ctx.Network}': {_ctx.Structures.Count} structures, {_ctx.Pipes.Count} pipes, " +
                      $"reference surface '{(_ctx.ReferenceSurface is { Length: > 0 } rs ? rs : "<none>")}', " +
                      $"{violated} pipe(s) currently violate minimum cover." +
                      (_ctx.Errors.Count > 0 ? "  " + string.Join(" | ", _ctx.Errors) : ""));
        }

        private void SelectStructureFromCombo()
        {
            if (_ctx == null || _structCombo.SelectedIndex < 0) return;
            SetStructure(IdResolver.FindStructure(Doc().Database, _ctx.Structures[_structCombo.SelectedIndex].Handle));
        }

        public void SetStructure(ObjectId id)
        {
            Guard(() =>
            {
                _structureId = id;
                AcDocument doc = Doc();
                _ctx = PipeElevationSolver.GetContext(doc, id, ObjectId.Null);

                // make sure the network combo reflects the structure's network
                _suppress = true;
                int ni = _networks.FindIndex(n => n.handle == _ctx.NetworkHandle);
                if (ni >= 0) _netCombo.SelectedIndex = ni;
                _suppress = false;
                ShowContext();

                StructureReport? s = _ctx.Structure;
                _info.Text = s == null ? "" :
                    $"{s.Name}   rim {s.Rim:0.00}   sump {s.Sump:0.00}   surface {s.SurfaceElevation:0.00}   " +
                    $"ref. surface '{s.ReferenceSurface}'   network '{s.Network}'   parts list '{s.PartsList}'   " +
                    $"{s.ConnectedPipes} connected pipe(s): {string.Join(", ", _ctx.ConnectedPipes)}";

                _gov.ItemsSource = new[] { "Auto (most restrictive)" }.Concat(_ctx.ConnectedPipes).ToList();
                _gov.SelectedIndex = 0;
                Compute();
            });
        }

        private SolverInput BuildInput()
        {
            if (_structureId.IsNull) throw new InvalidOperationException("Pick a structure first.");
            var input = new SolverInput
            {
                StructureId = _structureId,
                CoverMinimum = ParseOpt(_cover.Text),
                Mode = PipeElevationHandler.ParseMode(_mode.SelectedItem as string),
                ManualDrop = ParseOpt(_drop.Text) ?? 0,
                StructureInvertOverride = ParseOpt(_override.Text),
                MaxDepthBelowSurface = ParseOpt(_maxDepth.Text) ?? 50,
                SlopeUnits = _slopeUnits.SelectedItem as string ?? "auto",
            };
            if (_gov.SelectedIndex > 0 && _gov.SelectedItem is string govName)
                input.GoverningPipeId = IdResolver.FindPipe(Doc().Database, govName);

            SolveDefaults.CoverMinimum = input.CoverMinimum;
            SolveDefaults.Mode = input.Mode;
            SolveDefaults.ManualDrop = input.ManualDrop;
            SolveDefaults.MaxDepthBelowSurface = input.MaxDepthBelowSurface;
            return input;
        }

        private void Compute()
        {
            _lastInput = BuildInput();
            _last = PipeElevationSolver.Compute(Doc(), _lastInput);
            ShowReport(_last);
        }

        private void Apply()
        {
            if (_lastInput == null || _last == null || !_last.Ok) { SetStatus("Compute a clean solution first."); return; }
            _last = PipeElevationSolver.Draw(Doc(), _lastInput);
            ShowReport(_last);
            if (_last.Applied)
            {
                SetStatus($"Applied to {_last.Pipes.Count} pipe(s) in one transaction. Civil 3D cover check passed.");
                _ctx = PipeElevationSolver.GetContext(Doc(), _structureId, ObjectId.Null);
                ShowContext();
            }
        }

        private void ShowReport(SolveReport r)
        {
            _pipes.ItemsSource = r.Pipes;
            var msgs = new List<string>();
            msgs.AddRange(r.Errors.Select(e => "ERROR  " + e));
            msgs.AddRange(r.Conflicts.Select(c => $"CONFLICT [{c.Kind}] {c.Message}" +
                (c.SuggestedStructureInvert is double s ? $"   → try invert override {s:0.000}" : "")));
            msgs.AddRange(r.Warnings.Select(w => "warning  " + w));
            foreach (PipeReport p in r.Pipes)
                msgs.AddRange(p.Violations.Select(v => $"{p.Name}: {v}"));
            if (msgs.Count == 0) msgs.Add(r.Applied ? "Applied." : "No conflicts — safe to apply.");
            _messages.ItemsSource = msgs;

            _apply.IsEnabled = r.Ok && !r.Applied && r.Pipes.Any(p => p.Status == "ok");
            _copy.IsEnabled = true;
            if (!r.Applied)
                SetStatus($"{r.Structure.Name}: mode {r.OffsetMode}, governing '{r.GoverningPipe}', slopes read as {r.SlopeUnits} ({r.SlopeUnitsReason}). " +
                          (r.Ok ? "Ready to apply." : $"{r.Conflicts.Count} conflict(s), {r.Errors.Count} error(s)."));
        }

        public void RefreshAll()
        {
            if (!_structureId.IsNull) SetStructure(_structureId);
            else LoadNetworks();
        }

        private void PickInDrawing()
        {
            AcDocument doc = Doc();
            Autodesk.AutoCAD.Internal.Utils.SetFocusToDwgView();
            doc.SendStringToExecute("_PIPEDASHPICK ", true, false, false);
        }

        private void ZoomToStructure()
        {
            if (_ctx?.Structure is not StructureReport s) { SetStatus("Pick a structure first."); return; }
            string x1 = (s.X - 100).ToString("0.###", CultureInfo.InvariantCulture);
            string y1 = (s.Y - 100).ToString("0.###", CultureInfo.InvariantCulture);
            string x2 = (s.X + 100).ToString("0.###", CultureInfo.InvariantCulture);
            string y2 = (s.Y + 100).ToString("0.###", CultureInfo.InvariantCulture);
            Doc().SendStringToExecute($"_.ZOOM _W {x1},{y1} {x2},{y2} ", true, false, false);
        }

        // ══════════════════════════════════════════════════════════════════
        //  run tab
        // ══════════════════════════════════════════════════════════════════

        private ObjectId _runStart = ObjectId.Null, _runEnd = ObjectId.Null;

        /// <summary>Called by PIPEDASHRUNPICK after the user picked two structures.</summary>
        public void SetRunStartEnd(ObjectId start, ObjectId end)
        {
            _runStart = start; _runEnd = end;
            Guard(SolveRun);
        }

        private RunInput BuildRunInput()
        {
            var input = new RunInput
            {
                OffsetTolerance = ParseOpt(_tol.Text) ?? 25.0,
                FlowDirection = _flowCombo.SelectedItem as string ?? "auto",
                CoverMinimum = ParseOpt(_cover.Text),
                Mode = PipeElevationHandler.ParseMode(_mode.SelectedItem as string),
                ManualDrop = ParseOpt(_drop.Text) ?? 0,
                HoldStart = _holdStart.IsChecked == true,
                HoldEnd = _holdEnd.IsChecked == true,
                MaxDepthBelowSurface = ParseOpt(_maxDepth.Text) ?? 50,
                SlopeUnits = _slopeUnits.SelectedItem as string ?? "auto",
            };
            if (!_runStart.IsNull && !_runEnd.IsNull)
            {
                input.StartStructureId = _runStart; input.EndStructureId = _runEnd;
            }
            else
            {
                if (_alignCombo.SelectedIndex < 0) throw new InvalidOperationException("Pick an alignment (or pick start/end structures).");
                input.AlignmentId = PipeRunHandler.FindAlignment(Doc().Database, _alignments[_alignCombo.SelectedIndex].handle);
                if (_netCombo.SelectedIndex >= 0) input.NetworkId = IdResolver.FindNetwork(Doc().Database, _networks[_netCombo.SelectedIndex].handle);
            }
            return input;
        }

        private void SolveRun()
        {
            _runInput = BuildRunInput();
            _run = PipeRunSolver.Compute(Doc(), _runInput);
            foreach (var p in _run.Pipes)
            {
                if (p.NewUpInvert is double u) p.NewUpInvert = Math.Round(u, 4);
                if (p.NewDownInvert is double d) p.NewDownInvert = Math.Round(d, 4);
            }
            PipeRunSolver.Evaluate(_run, _runInput.MaxDepthBelowSurface);
            ShowRun();
            _runStart = ObjectId.Null; _runEnd = ObjectId.Null;   // picked pair is one-shot
        }

        private void EvaluateRun()
        {
            if (_run == null || _runInput == null) return;
            _runPipes.CommitEdit(DataGridEditingUnit.Row, true);
            PipeRunSolver.Evaluate(_run, _runInput.MaxDepthBelowSurface);
            ShowRun();
        }

        private void ApplyRun()
        {
            if (_run == null || _runInput == null) { SetStatus("Solve a run first."); return; }
            _runPipes.CommitEdit(DataGridEditingUnit.Row, true);
            _run = PipeRunSolver.Apply(Doc(), _run, _runInput.MaxDepthBelowSurface);
            ShowRun();
            if (_run.Applied)
            {
                SetStatus($"Run applied: {_run.Pipes.Count} pipe(s) written in one transaction; Civil 3D cover check passed.");
                LoadNetworkContext();
            }
        }

        private void ShowRun()
        {
            if (_run == null) return;
            _runPipes.ItemsSource = null; _runPipes.ItemsSource = _run.Pipes;
            _runStructs.ItemsSource = null; _runStructs.ItemsSource = _run.Structures;
            _profile.Render(_run);

            var msgs = new List<string>();
            msgs.AddRange(_run.Errors.Select(e => "ERROR  " + e));
            msgs.AddRange(_run.Conflicts.Select(c => $"CONFLICT [{c.Kind}] {c.Message}"));
            msgs.AddRange(_run.Warnings.Select(w => "warning  " + w));
            foreach (var p in _run.Pipes) msgs.AddRange(p.Violations.Select(v => $"{p.Name}: {v}"));
            if (msgs.Count == 0) msgs.Add(_run.Applied ? "Applied." : "No conflicts — safe to apply.");
            _runMessages.ItemsSource = msgs;

            _runEval.IsEnabled = _run.Pipes.Count > 0 && !_run.Applied;
            _runApply.IsEnabled = _run.Ok && !_run.Applied;
            _runCopy.IsEnabled = true;
            if (!_run.Applied)
                SetStatus($"Run on '{(_run.Alignment is { Length: > 0 } a ? a : "picked path")}' in '{_run.Network}': {_run.Structures.Count} structures, {_run.Pipes.Count} pipes, " +
                          $"flow {_run.FlowDirection} ({_run.FlowDirectionReason}); slopes read as {_run.SlopeUnits}. " +
                          (_run.Ok ? "Ready to apply — or edit the blue cells and Re-check." : $"{_run.Conflicts.Count} conflict(s), {_run.Errors.Count} error(s)."));
        }

        private void PickRunInDrawing()
        {
            Autodesk.AutoCAD.Internal.Utils.SetFocusToDwgView();
            Doc().SendStringToExecute("_PIPEDASHRUNPICK ", true, false, false);
        }

        // ══════════════════════════════════════════════════════════════════
        //  helpers
        // ══════════════════════════════════════════════════════════════════

        private static double? ParseOpt(string s) =>
            double.TryParse(s?.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out double v) ? v : null;

        private void SetStatus(string text) => _status.Text = text;

        private void Guard(Action a)
        {
            try { a(); }
            catch (Exception ex) { SetStatus("Error: " + ex.Message); }
        }
    }
}
