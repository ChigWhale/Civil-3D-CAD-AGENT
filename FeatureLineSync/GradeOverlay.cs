using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.Colors;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.GraphicsInterface;
using Autodesk.AutoCAD.Runtime;
using Autodesk.Civil.DatabaseServices;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Core.Application;

[assembly: CommandClass(typeof(FeatureLineSync.GradeCommands))]

namespace FeatureLineSync
{
    public class GradeCommands
    {
        /// <summary>
        /// FLGRADES toggles a live overlay that writes the grade of every feature line segment next to it.
        /// The labels are transient graphics: nothing is added to the drawing and nothing is saved.
        /// </summary>
        [CommandMethod("FLGRADES", CommandFlags.Modal)]
        public void FlGrades()
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            Editor ed = doc.Editor;

            if (GradeOverlay.IsOn(doc))
            {
                GradeOverlay.Stop(doc);
                ed.WriteMessage("\nFeature line grades: off.");
                return;
            }

            var pko = new PromptKeywordOptions("\nShow grades on [All feature lines/Selected]") { AllowNone = true };
            pko.Keywords.Add("All");
            pko.Keywords.Add("Selected");
            pko.Keywords.Default = "All";
            PromptResult pr = ed.GetKeywords(pko);
            if (pr.Status != PromptStatus.OK && pr.Status != PromptStatus.None) return;
            bool all = pr.Status == PromptStatus.None || pr.StringResult == "All";

            HashSet<ObjectId>? only = null;
            if (!all)
            {
                var pso = new PromptSelectionOptions { MessageForAdding = "\nSelect feature lines" };
                PromptSelectionResult psr = ed.GetSelection(pso);
                if (psr.Status != PromptStatus.OK) return;
                only = new HashSet<ObjectId>();
                using (Transaction tr = doc.Database.TransactionManager.StartTransaction())
                {
                    foreach (ObjectId id in psr.Value.GetObjectIds())
                    {
                        try { if (tr.GetObject(id, OpenMode.ForRead, false) is FeatureLine) only.Add(id); } catch { }
                    }
                    tr.Commit();
                }
                if (only.Count == 0) { ed.WriteMessage("\nNo feature lines in that selection."); return; }
            }

            int n = GradeOverlay.Start(doc, only);
            ed.WriteMessage($"\nFeature line grades: on, {n} labels. Arrow points uphill. Run FLGRADES again to turn off.");
        }
    }

    /// <summary>
    /// Per-document transient grade labels. Rebuilt for a feature line whenever it is modified
    /// (grip edit, elevation editor, undo), re-scaled on zoom, removed when the document closes.
    /// </summary>
    public sealed class GradeOverlay : IDisposable
    {
        private static readonly Dictionary<Document, GradeOverlay> ByDoc = new();

        /// <summary>Label colour (ACI). 6 = magenta: readable on black and white backgrounds, rarely used by linework.</summary>
        public static short ColorIndex { get; set; } = 6;
        /// <summary>Label height as a fraction of the view height.</summary>
        public static double HeightFraction { get; set; } = 0.012;
        public static int Decimals { get; set; } = 2;

        private readonly Document _doc;
        private readonly Database _db;
        private readonly HashSet<ObjectId>? _only;            // null = every feature line in model space
        private readonly Dictionary<ObjectId, List<(GradeLabel g, MText mt)>> _labels = new();
        private readonly HashSet<ObjectId> _dirty = new();
        private double _textHeight = 1.0;
        private bool _disposed;

        public static bool IsOn(Document doc) => ByDoc.ContainsKey(doc);

        public static int Start(Document doc, HashSet<ObjectId>? only)
        {
            Stop(doc);
            var ov = new GradeOverlay(doc, only);
            ByDoc[doc] = ov;
            return ov.RebuildAll();
        }

        public static void Stop(Document doc)
        {
            if (ByDoc.TryGetValue(doc, out GradeOverlay? ov))
            {
                ByDoc.Remove(doc);
                ov.Dispose();
            }
        }

        private GradeOverlay(Document doc, HashSet<ObjectId>? only)
        {
            _doc = doc;
            _db = doc.Database;
            _only = only;
            _db.ObjectModified += OnModified;
            _db.ObjectAppended += OnAppended;
            _db.ObjectErased += OnErased;
            _doc.CommandEnded += OnCommandDone;
            _doc.CommandCancelled += OnCommandDone;
            _doc.CommandFailed += OnCommandDone;
            _doc.ViewChanged += OnViewChanged;
            AcApp.DocumentManager.DocumentToBeDestroyed += OnDocDestroyed;
            AcApp.Idle += OnIdle;
        }

        public void Dispose()
        {
            if (_disposed) return;
            _disposed = true;
            try { _db.ObjectModified -= OnModified; } catch { }
            try { _db.ObjectAppended -= OnAppended; } catch { }
            try { _db.ObjectErased -= OnErased; } catch { }
            try { _doc.CommandEnded -= OnCommandDone; } catch { }
            try { _doc.CommandCancelled -= OnCommandDone; } catch { }
            try { _doc.CommandFailed -= OnCommandDone; } catch { }
            try { _doc.ViewChanged -= OnViewChanged; } catch { }
            try { AcApp.DocumentManager.DocumentToBeDestroyed -= OnDocDestroyed; } catch { }
            try { AcApp.Idle -= OnIdle; } catch { }
            foreach (ObjectId id in _labels.Keys.ToList()) Remove(id);
        }

        // ── events ──────────────────────────────────────────────────────────────

        private void OnModified(object? sender, ObjectEventArgs e)
        {
            try { if (e.DBObject is FeatureLine) _dirty.Add(e.DBObject.ObjectId); } catch { }
        }

        private void OnAppended(object? sender, ObjectEventArgs e)
        {
            try { if (_only == null && e.DBObject is FeatureLine) _dirty.Add(e.DBObject.ObjectId); } catch { }
        }

        private void OnErased(object? sender, ObjectErasedEventArgs e)
        {
            // fires for erase and for un-erase (undo of an erase); both are handled by a rebuild
            try { if (e.DBObject is FeatureLine) _dirty.Add(e.DBObject.ObjectId); } catch { }
        }

        private void OnCommandDone(object? sender, CommandEventArgs e) => Flush();

        private void OnIdle(object? sender, EventArgs e)
        {
            if (_dirty.Count == 0) return;
            if (AcApp.DocumentManager.MdiActiveDocument != _doc) return;
            if (!string.IsNullOrEmpty(_doc.CommandInProgress)) return;      // wait for CommandEnded
            Flush();
        }

        private void OnDocDestroyed(object? sender, DocumentCollectionEventArgs e)
        {
            if (e.Document == _doc) Stop(_doc);
        }

        private void OnViewChanged(object? sender, EventArgs e)
        {
            if (AcApp.DocumentManager.MdiActiveDocument != _doc) return;
            double before = _textHeight;
            MeasureTextHeight();
            if (Math.Abs(_textHeight - before) < before * 0.05) return;
            try
            {
                foreach (var list in _labels.Values)
                    foreach (var (g, mt) in list)
                    {
                        mt.TextHeight = _textHeight;
                        mt.Location = Place(g);
                        TransientManager.CurrentTransientManager.UpdateTransient(mt, new IntegerCollection());
                    }
            }
            catch { }
        }

        // ── building ────────────────────────────────────────────────────────────

        private int RebuildAll()
        {
            MeasureTextHeight();
            int n = 0;
            try
            {
                using Transaction tr = _db.TransactionManager.StartTransaction();
                if (_only != null)
                {
                    foreach (ObjectId id in _only) n += Rebuild(id, tr);
                }
                else
                {
                    var bt = (BlockTable)tr.GetObject(_db.BlockTableId, OpenMode.ForRead);
                    var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
                    foreach (ObjectId id in ms)
                    {
                        if (id.IsErased) continue;
                        if (!id.ObjectClass.IsDerivedFrom(RXObject.GetClass(typeof(FeatureLine)))) continue;
                        n += Rebuild(id, tr);
                    }
                }
                tr.Commit();
            }
            catch (System.Exception ex)
            {
                _doc.Editor.WriteMessage("\nFLGRADES: " + ex.Message);
            }
            return n;
        }

        private void Flush()
        {
            if (_disposed || _dirty.Count == 0) return;
            var ids = _dirty.ToList();
            _dirty.Clear();
            try
            {
                using Transaction tr = _db.TransactionManager.StartTransaction();
                foreach (ObjectId id in ids) Rebuild(id, tr);
                tr.Commit();
            }
            catch
            {
                foreach (ObjectId id in ids) _dirty.Add(id);                // try again on the next command / idle
            }
        }

        private int Rebuild(ObjectId id, Transaction tr)
        {
            Remove(id);
            if (id.IsNull || id.IsErased) return 0;
            if (_only != null && !_only.Contains(id)) return 0;

            FeatureLine? fl;
            try { fl = tr.GetObject(id, OpenMode.ForRead, false) as FeatureLine; }
            catch { return 0; }
            if (fl == null) return 0;

            FlLine line;
            try { line = FeatureLineReader.ReadGeometry(fl); }
            catch { return 0; }

            var list = new List<(GradeLabel, MText)>();
            foreach (GradeLabel g in GradeLabels.Compute(line, Decimals))
            {
                MText mt = MakeText(g);
                try
                {
                    TransientManager.CurrentTransientManager.AddTransient(mt, TransientDrawingMode.DirectTopmost, 128, new IntegerCollection());
                    list.Add((g, mt));
                }
                catch { mt.Dispose(); }
            }
            if (list.Count > 0) _labels[id] = list;
            return list.Count;
        }

        private void Remove(ObjectId id)
        {
            if (!_labels.TryGetValue(id, out var list)) return;
            _labels.Remove(id);
            foreach (var (_, mt) in list)
            {
                try { TransientManager.CurrentTransientManager.EraseTransient(mt, new IntegerCollection()); } catch { }
                try { mt.Dispose(); } catch { }
            }
        }

        private MText MakeText(GradeLabel g)
        {
            var mt = new MText
            {
                TextHeight = _textHeight,
                Attachment = AttachmentPoint.BottomCenter,
                Rotation = g.Angle,
                Contents = g.Text,
                BackgroundFill = true,
                UseBackgroundColor = true,
                BackgroundScaleFactor = 1.1,
            };
            try { mt.TextStyleId = _db.Textstyle; } catch { }
            try { mt.Color = Color.FromColorIndex(ColorMethod.ByAci, ColorIndex); } catch { }
            mt.Location = Place(g);
            return mt;
        }

        /// <summary>Just above the segment midpoint, on the text's "up" side.</summary>
        private Point3d Place(GradeLabel g)
        {
            double off = _textHeight * 0.35;
            return new Point3d(g.X - Math.Sin(g.Angle) * off, g.Y + Math.Cos(g.Angle) * off, 0.0);
        }

        private void MeasureTextHeight()
        {
            try
            {
                using ViewTableRecord view = _doc.Editor.GetCurrentView();
                _textHeight = Math.Max(1e-6, view.Height * HeightFraction);
            }
            catch { if (_textHeight <= 0) _textHeight = 1.0; }
        }
    }
}
