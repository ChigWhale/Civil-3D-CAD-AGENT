using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CADAgent
{
    public class AgentPaletteControl : UserControl
    {
        private readonly ScrollViewer _scroller;
        private readonly StackPanel  _chatPanel;
        private readonly TextBox     _questionBox;
        private readonly Button      _askButton;
        private readonly TextBlock   _statusLabel;

        public AgentPaletteControl()
        {
            Background = new SolidColorBrush(Color.FromRgb(30, 30, 30));

            // ── Chat history ──────────────────────────────────────────────
            _chatPanel = new StackPanel();
            _scroller  = new ScrollViewer
            {
                Content                  = _chatPanel,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                Margin                   = new Thickness(8, 8, 8, 4),
            };

            // ── Status label ──────────────────────────────────────────────
            _statusLabel = new TextBlock
            {
                Foreground  = new SolidColorBrush(Color.FromRgb(136, 136, 136)),
                FontSize    = 11,
                FontStyle   = FontStyles.Italic,
                Margin      = new Thickness(10, 0, 10, 4),
                TextWrapping = TextWrapping.Wrap,
            };

            // ── Input bar ─────────────────────────────────────────────────
            _questionBox = new TextBox
            {
                Background              = new SolidColorBrush(Color.FromRgb(45, 45, 45)),
                Foreground              = new SolidColorBrush(Color.FromRgb(220, 220, 220)),
                CaretBrush              = new SolidColorBrush(Color.FromRgb(220, 220, 220)),
                BorderBrush             = new SolidColorBrush(Color.FromRgb(85, 85, 85)),
                BorderThickness         = new Thickness(1),
                Padding                 = new Thickness(6, 5, 6, 5),
                FontSize                = 13,
                TextWrapping            = TextWrapping.Wrap,
                AcceptsReturn           = false,
                VerticalContentAlignment = VerticalAlignment.Center,
            };
            _questionBox.KeyDown += OnKeyDown;

            _askButton = new Button
            {
                Content         = "Ask",
                Margin          = new Thickness(6, 0, 0, 0),
                Padding         = new Thickness(12, 5, 12, 5),
                FontSize        = 13,
                Background      = new SolidColorBrush(Color.FromRgb(0, 120, 212)),
                Foreground      = Brushes.White,
                BorderThickness = new Thickness(0),
                Cursor          = Cursors.Hand,
            };
            _askButton.Click += OnAsk;

            var inputRow = new Grid();
            inputRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            inputRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            Grid.SetColumn(_questionBox, 0);
            Grid.SetColumn(_askButton,   1);
            inputRow.Children.Add(_questionBox);
            inputRow.Children.Add(_askButton);
            inputRow.Margin = new Thickness(8, 0, 8, 8);

            // ── Root layout ───────────────────────────────────────────────
            var root = new Grid();
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            Grid.SetRow(_scroller,     0);
            Grid.SetRow(_statusLabel,  1);
            Grid.SetRow(inputRow,      2);
            root.Children.Add(_scroller);
            root.Children.Add(_statusLabel);
            root.Children.Add(inputRow);

            Content = root;
            MinWidth  = 260;
            MinHeight = 300;
        }

        private void OnKeyDown(object sender, KeyEventArgs e)
        {
            if (e.Key == Key.Enter)
                OnAsk(sender, e);
        }

        private async void OnAsk(object sender, RoutedEventArgs e)
        {
            string question = _questionBox.Text.Trim();
            if (string.IsNullOrEmpty(question)) return;

            SetBusy(true);
            AppendMessage("You", question, Color.FromRgb(0, 120, 212));
            _questionBox.Clear();

            var doc = AcadApp.DocumentManager.MdiActiveDocument;
            if (doc == null)
            {
                AppendMessage("Error", "No active Civil 3D document.", Color.FromRgb(224, 82, 82));
                SetBusy(false);
                return;
            }

            ModelSpaceCollector.Collect(doc);

            string response = await Task.Run(() => ModelSpaceCollector.RunAgent(question));

            // ── DEBUG: show raw Python output ─────────────────────────────
            AppendMessage("RAW", string.IsNullOrWhiteSpace(response) ? "(empty)" : response,
                          Color.FromRgb(180, 100, 255));

            var steps = ModelSpaceBuilder.ParseSteps(response);

            // ── DRY-RUN MODE: list planned steps without executing ─────────
            AppendMessage("Plan", $"{steps.Count} step(s) planned:", Color.FromRgb(255, 165, 0));
            for (int i = 0; i < steps.Count; i++)
            {
                string intent   = steps[i]["intent"]?.ToString() ?? "chat";
                string stepJson = steps[i].ToString();
                AppendMessage($"  Step {i + 1}", $"[{intent}] {stepJson}", Color.FromRgb(200, 200, 100));
            }

            //  ── EXECUTION (commented out for troubleshooting) ────────────
            foreach (var step in steps)
            {
                string intent   = step["intent"]?.ToString() ?? "chat";
                string stepJson = step.ToString();

                if (intent == "chat" || intent == "observe")
                {
                    string text = step["text"]?.ToString() ?? stepJson;
                    AppendMessage("Agent", text, Color.FromRgb(78, 201, 78));
                    continue;
                }

                AppendMessage("Agent", $"[{intent}...]", Color.FromRgb(255, 165, 0));
                string summary = await Task.Run(() =>
                {
                    using (doc.LockDocument())
                        return ModelSpaceBuilder.ExecuteStep(intent, stepJson, doc);
                });
                AppendMessage("Agent", summary, Color.FromRgb(78, 201, 78));
            }
            //── END EXECUTION ─────────────────────────────────────────────

            SetBusy(false);
        }

        private void AppendMessage(string sender, string text, Color color)
        {
            var panel = new StackPanel { Margin = new Thickness(0, 0, 0, 10) };

            panel.Children.Add(new TextBlock
            {
                Text       = sender,
                Foreground = new SolidColorBrush(color),
                FontWeight = FontWeights.Bold,
                FontSize   = 12,
            });

            panel.Children.Add(new TextBlock
            {
                Text         = text,
                Foreground   = new SolidColorBrush(Color.FromRgb(220, 220, 220)),
                FontSize     = 13,
                TextWrapping = TextWrapping.Wrap,
            });

            _chatPanel.Children.Add(panel);
            _scroller.ScrollToBottom();
        }

        private void SetBusy(bool busy)
        {
            _askButton.IsEnabled   = !busy;
            _questionBox.IsEnabled = !busy;
            _statusLabel.Text      = busy ? "Thinking..." : string.Empty;
        }
    }
}
