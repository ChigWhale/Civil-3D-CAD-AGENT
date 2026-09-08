using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Shapes;

namespace PipeInvertSetter
{
    /// <summary>
    /// Small WPF profile sketch of a run: surface line, rim ticks, old inverts (grey),
    /// new inverts + crowns (blue), the required-cover line (dashed), and structure labels.
    /// Purely visual — the numbers live in the grids.
    /// </summary>
    public sealed class ProfileCanvas : Canvas
    {
        private RunReport? _run;
        private int _highlight = -1;

        public ProfileCanvas()
        {
            Background = Brushes.White;
            ClipToBounds = true;
            SizeChanged += (_, _) => Redraw();
        }

        public void Render(RunReport run) { _run = run; Redraw(); }

        public void Highlight(int pipeIndex) { _highlight = pipeIndex; Redraw(); }

        private void Redraw()
        {
            Children.Clear();
            if (_run == null || _run.Pipes.Count == 0 || ActualWidth < 50 || ActualHeight < 40) return;

            // ── horizontal positions: cumulative 2D length from the upstream end ──
            int n = _run.Pipes.Count;
            var x = new double[n + 1];
            for (int k = 0; k < n; k++) x[k + 1] = x[k] + Math.Max(_run.Pipes[k].Length2D, 1e-3);
            double totalLen = x[n];

            // ── vertical extent ────────────────────────────────────────────
            var zs = new List<double>();
            foreach (var p in _run.Pipes)
            {
                zs.Add(p.SurfaceUp); zs.Add(p.SurfaceDown);
                zs.Add(p.OldUpInvert); zs.Add(p.OldDownInvert);
                if (p.NewUpInvert is double a) zs.Add(a);
                if (p.NewDownInvert is double b) zs.Add(b);
                foreach (double[] s in p.Samples) zs.Add(s[1]);
            }
            foreach (var s in _run.Structures) if (!double.IsNaN(s.Rim)) zs.Add(s.Rim);
            double zMin = zs.Min(), zMax = zs.Max();
            if (zMax - zMin < 1) { zMax += 0.5; zMin -= 0.5; }
            double pad = (zMax - zMin) * 0.08;
            zMin -= pad; zMax += pad;

            const double left = 44, right = 10, top = 16, bottom = 14;
            double w = ActualWidth - left - right, h = ActualHeight - top - bottom;
            double X(double d) => left + d / totalLen * w;
            double Y(double z) => top + (zMax - z) / (zMax - zMin) * h;

            // elevation grid lines
            double step = NiceStep((zMax - zMin) / 4);
            for (double z = Math.Ceiling(zMin / step) * step; z <= zMax; z += step)
            {
                Add(new Line { X1 = left, X2 = left + w, Y1 = Y(z), Y2 = Y(z), Stroke = Brushes.Gainsboro, StrokeThickness = 1 });
                Text(z.ToString("0.0", CultureInfo.InvariantCulture), 2, Y(z) - 7, Brushes.Gray, 9);
            }

            // surface (with interior samples) and required-cover line
            var surf = new PointCollection();
            var coverLine = new PointCollection();
            for (int k = 0; k < n; k++)
            {
                var p = _run.Pipes[k];
                double top0 = p.InnerHeight + p.WallThickness;
                surf.Add(new Point(X(x[k]), Y(p.SurfaceUp)));
                coverLine.Add(new Point(X(x[k]), Y(p.SurfaceUp - p.CoverMinimum - top0)));
                foreach (double[] s in p.Samples)
                {
                    surf.Add(new Point(X(x[k] + s[0] * p.Length2D), Y(s[1])));
                    coverLine.Add(new Point(X(x[k] + s[0] * p.Length2D), Y(s[1] - p.CoverMinimum - top0)));
                }
                surf.Add(new Point(X(x[k + 1]), Y(p.SurfaceDown)));
                coverLine.Add(new Point(X(x[k + 1]), Y(p.SurfaceDown - p.CoverMinimum - top0)));
            }
            Add(new Polyline { Points = surf, Stroke = Brushes.SaddleBrown, StrokeThickness = 1.5 });
            Add(new Polyline { Points = coverLine, Stroke = Brushes.DarkOrange, StrokeThickness = 1, StrokeDashArray = new DoubleCollection { 4, 3 } });

            // pipes: old (grey) and new (blue) invert + crown
            for (int k = 0; k < n; k++)
            {
                var p = _run.Pipes[k];
                double x0 = X(x[k]), x1 = X(x[k + 1]);
                bool hl = k == _highlight;

                Add(new Line { X1 = x0, X2 = x1, Y1 = Y(p.OldUpInvert), Y2 = Y(p.OldDownInvert), Stroke = Brushes.Silver, StrokeThickness = hl ? 2.5 : 1.5 });
                if (p.NewUpInvert is double nu && p.NewDownInvert is double nd)
                {
                    Brush b = p.Status == "conflict" ? Brushes.Red : Brushes.RoyalBlue;
                    Add(new Line { X1 = x0, X2 = x1, Y1 = Y(nu), Y2 = Y(nd), Stroke = b, StrokeThickness = hl ? 3 : 2 });
                    Add(new Line { X1 = x0, X2 = x1, Y1 = Y(nu + p.InnerHeight), Y2 = Y(nd + p.InnerHeight), Stroke = b, StrokeThickness = 1, Opacity = 0.6 });
                    if (p.NewSlope is double s)
                        Text((s * 100).ToString("0.00", CultureInfo.InvariantCulture) + "%", (x0 + x1) / 2 - 12, Y(Math.Min(nu, nd)) + 2, b, 9);
                }
            }

            // structures: vertical line from lowest invert to rim, name on top
            for (int j = 0; j <= n && j < _run.Structures.Count; j++)
            {
                var s = _run.Structures[j];
                double xs = X(x[j]);
                double lowInv = new[] { s.NewInvertIn, s.NewInvertOut, s.OldInvertIn, s.OldInvertOut }.Where(v => v.HasValue).Select(v => v!.Value).DefaultIfEmpty(s.Surface - 5).Min();
                double topZ = double.IsNaN(s.Rim) ? s.Surface : s.Rim;
                Add(new Line { X1 = xs, X2 = xs, Y1 = Y(topZ), Y2 = Y(lowInv - 0.3), Stroke = Brushes.DimGray, StrokeThickness = 1.2 });
                if (!double.IsNaN(s.Rim))
                    Add(new Line { X1 = xs - 4, X2 = xs + 4, Y1 = Y(s.Rim), Y2 = Y(s.Rim), Stroke = Brushes.DimGray, StrokeThickness = 2 });
                Text(s.Name, xs - 3, 1, Brushes.Black, 9);
            }
        }

        private static double NiceStep(double raw)
        {
            if (raw <= 0) return 1;
            double p = Math.Pow(10, Math.Floor(Math.Log10(raw)));
            double m = raw / p;
            return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * p;
        }

        private void Add(UIElement e) => Children.Add(e);

        private void Text(string s, double x, double y, Brush brush, double size)
        {
            var tb = new TextBlock { Text = s, Foreground = brush, FontSize = size };
            SetLeft(tb, x); SetTop(tb, y);
            Children.Add(tb);
        }
    }
}
