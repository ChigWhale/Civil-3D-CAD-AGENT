using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Threading;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CADAgent
{
    /// <summary>
    /// A per-drawing change counter, so a client can tell whether a drawing has changed
    /// since it last read it without downloading the whole model space again.
    ///
    /// The counter goes up on every object appended, modified or erased in the drawing's
    /// database -- edits made by hand in Civil 3D count exactly as edits made through the
    /// bridge do -- and the bridge bumps it once more after any write it dispatches, so a
    /// document whose events failed to hook still moves on every bridge write.
    ///
    /// Read through GET /revision, and sent back on every response as the
    /// X-Drawing-Revision header. The value is only meaningful within one AutoCAD session:
    /// it starts at 0 whenever the drawing is opened.
    /// </summary>
    public static class DrawingRevision
    {
        private static readonly ConcurrentDictionary<string, long> _revisions =
            new ConcurrentDictionary<string, long>(StringComparer.OrdinalIgnoreCase);

        // Keeps the delegates alive and lets them be removed when the document closes.
        private static readonly ConcurrentDictionary<string, Action> _unhook =
            new ConcurrentDictionary<string, Action>(StringComparer.OrdinalIgnoreCase);

        private static int _wired;

        public static long Get(Document doc)
        {
            if (doc == null) return 0;
            return _revisions.TryGetValue(Key(doc), out long r) ? r : 0;
        }

        public static long Bump(Document doc)
        {
            if (doc == null) return 0;
            return _revisions.AddOrUpdate(Key(doc), 1, (_, r) => r + 1);
        }

        /// <summary>Hooks every open document and every document opened from now on.
        /// Must run on the application thread; safe to call more than once.</summary>
        public static void Wire()
        {
            if (Interlocked.Exchange(ref _wired, 1) == 1) return;
            try
            {
                foreach (Document d in AcadApp.DocumentManager) Hook(d);
                AcadApp.DocumentManager.DocumentCreated += (s, e) => Hook(e.Document);
                AcadApp.DocumentManager.DocumentToBeDestroyed += (s, e) => Unhook(e.Document);
            }
            catch { }
        }

        private static string Key(Document doc)
        {
            try { return doc.Name ?? ""; } catch { return ""; }
        }

        private static void Hook(Document doc)
        {
            if (doc == null) return;
            string key = Key(doc);
            if (string.IsNullOrEmpty(key) || _unhook.ContainsKey(key)) return;
            try
            {
                Database db = doc.Database;
                ObjectEventHandler appended = (s, e) => _revisions.AddOrUpdate(key, 1, (_, r) => r + 1);
                ObjectEventHandler modified = (s, e) => _revisions.AddOrUpdate(key, 1, (_, r) => r + 1);
                ObjectErasedEventHandler erased = (s, e) => _revisions.AddOrUpdate(key, 1, (_, r) => r + 1);
                db.ObjectAppended += appended;
                db.ObjectModified += modified;
                db.ObjectErased   += erased;
                _revisions.TryAdd(key, 0);
                _unhook[key] = () =>
                {
                    try { db.ObjectAppended -= appended; } catch { }
                    try { db.ObjectModified -= modified; } catch { }
                    try { db.ObjectErased   -= erased;   } catch { }
                };
            }
            catch { }
        }

        private static void Unhook(Document doc)
        {
            if (doc == null) return;
            string key = Key(doc);
            if (_unhook.TryRemove(key, out var undo))
            {
                try { undo(); } catch { }
            }
            _revisions.TryRemove(key, out _);
        }

        /// <summary>Intents that read the drawing without changing it. Anything else that
        /// goes through POST /execute counts as a write and bumps the revision.</summary>
        public static readonly HashSet<string> ReadOnlyIntents = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "get_drawing_settings", "list_styles", "list_layers", "list_block_definitions",
            "get_properties", "select_entities", "entity_key_points", "closest_point_on",
            "intersections", "tangent_points", "measure_entities", "angle_between",
            "point_along", "bounding_box", "find_duplicates", "zoom_view",
            "list_surface_label_styles", "export_block", "save_drawing",
        };
    }
}
