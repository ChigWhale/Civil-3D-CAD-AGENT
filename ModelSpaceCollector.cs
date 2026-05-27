using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using Newtonsoft.Json.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;
using Autodesk.Civil.ApplicationServices;
using Autodesk.Civil.DatabaseServices;

// Disambiguate types that exist in both AutoCAD and Civil3D namespaces
using AcadDBObject = Autodesk.AutoCAD.DatabaseServices.DBObject;
using AcadEntity   = Autodesk.AutoCAD.DatabaseServices.Entity;

[assembly: CommandClass(typeof(CADAgent.ModelSpaceCollector))]

namespace CADAgent
{
    public class ModelSpaceCollector
    {
        /// <summary>
        /// The JSON string produced by the most recent call to Collect().
        /// The custom UI reads this after calling Collect() and pipes it to the Python agent.
        /// </summary>
        public static string LastJson { get; private set; } = string.Empty;

        /// <summary>
        /// AutoCAD command: GETCADOBJECTS
        /// Convenience wrapper — collects model space and prints a confirmation.
        /// </summary>
        [CommandMethod("GETCADOBJECTS")]
        public void GetAllModelSpaceObjects()
        {
            Document doc = Application.DocumentManager.MdiActiveDocument;
            Editor ed = doc.Editor;
            Collect(doc);
            ed.WriteMessage($"\n  Model space JSON ready ({LastJson.Length} chars).\n");
        }

        /// <summary>
        /// AutoCAD command: ASKAGENT
        /// Prompts for a question, collects model space, pipes JSON + question to the Python agent,
        /// and prints the response in the AutoCAD command line.
        /// </summary>
        [CommandMethod("ASKAGENT")]
        public void AskAgent()
        {
            App.ShowPalette();
        }

        /// <summary>
        /// Spawns the Python agent as a child process, pipes the collected JSON to its stdin,
        /// and returns the agent's stdout output.
        /// </summary>
        public static string RunAgent(string question)
        {
            // Path to the Python agent script — adjust if needed
            const string scriptPath = @"C:\Users\me\OneDrive\Desktop\CADAgent-Files\CAD-Agent\ollama_agent.py";

            var psi = new ProcessStartInfo
            {
                FileName               = "python",
                Arguments              = $"\"{scriptPath}\" \"{question}\"",
                RedirectStandardInput  = true,
                RedirectStandardOutput = true,
                RedirectStandardError  = true,
                UseShellExecute        = false,
                CreateNoWindow         = true,
            };

            using (Process proc = Process.Start(psi))
            {
                proc.StandardInput.Write(LastJson);
                proc.StandardInput.Close();  // signals EOF so sys.stdin.read() returns

                // Read stdout and stderr concurrently to avoid buffer-deadlock
                var stdoutTask = System.Threading.Tasks.Task.Run(() => proc.StandardOutput.ReadToEnd());
                var stderrTask = System.Threading.Tasks.Task.Run(() => proc.StandardError.ReadToEnd());

                // 120-second timeout — prevents the palette hanging indefinitely
                bool exited = proc.WaitForExit(120_000);
                if (!exited)
                {
                    try { proc.Kill(); } catch { }
                    return "[Error] Python agent timed out after 120 s. Is Ollama running?";
                }

                string output = stdoutTask.Result;
                string stderr = stderrTask.Result;
                return string.IsNullOrWhiteSpace(output) ? stderr : output;
            }
        }

        /// <summary>
        /// Gathers all entities from model space with full geometric data, builds a JSON
        /// string, and stores it in <see cref="LastJson"/>.
        /// Call this from the custom UI button handler.
        /// </summary>
        public static void Collect(Document doc)
        {
            Database db = doc.Database;
            CivilDocument civilDoc = CivilApplication.ActiveDocument;

            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                BlockTable bt = tr.GetObject(db.BlockTableId, OpenMode.ForRead) as BlockTable;
                BlockTableRecord ms = tr.GetObject(
                    bt[BlockTableRecord.ModelSpace], OpenMode.ForRead) as BlockTableRecord;

                // ── Categorize model space entities ───────────────────────────────────
                var tinSurfaces   = new List<TinSurface>();
                var gridSurfaces  = new List<GridSurface>();
                var corridors     = new List<Corridor>();
                var featureLines  = new List<FeatureLine>();
                var parcels       = new List<Parcel>();
                var profileViews  = new List<ProfileView>();
                var sectionViews  = new List<SectionView>();
                var lines         = new List<Line>();
                var arcs          = new List<Arc>();
                var standardEntities = new Dictionary<string, int>();

                foreach (ObjectId objId in ms)
                {
                    try
                    {
                        AcadDBObject obj = tr.GetObject(objId, OpenMode.ForRead);
                        if (!(obj is AcadEntity entity)) continue;

                        // These are collected via the civilDoc API instead
                        if (entity is Alignment || entity is SampleLine ||
                            entity is Pipe      || entity is Structure) continue;

                        if (entity is TinSurface  ts) { tinSurfaces.Add(ts);  continue; }
                        if (entity is GridSurface gs) { gridSurfaces.Add(gs); continue; }
                        if (entity is Corridor    c)  { corridors.Add(c);     continue; }
                        if (entity is FeatureLine fl) { featureLines.Add(fl); continue; }
                        if (entity is Parcel      p)  { parcels.Add(p);       continue; }
                        if (entity is ProfileView pv) { profileViews.Add(pv); continue; }
                        if (entity is SectionView sv) { sectionViews.Add(sv); continue; }
                        if (entity is Line        ln) { lines.Add(ln);        continue; }
                        if (entity is Arc         ar) { arcs.Add(ar);         continue; }

                        string typeName = entity.GetType().Name;
                        standardEntities[typeName] =
                            standardEntities.TryGetValue(typeName, out int cnt) ? cnt + 1 : 1;
                    }
                    catch { }
                }

                // ── Build JSON root ───────────────────────────────────────────────────
                var root = new JObject();

                // Alignments (profiles nested inside)
                var alignArray = new JArray();
                ObjectIdCollection alignIds = civilDoc.GetAlignmentIds();
                foreach (ObjectId alignId in alignIds)
                {
                    try
                    {
                        Alignment align = tr.GetObject(alignId, OpenMode.ForRead) as Alignment;
                        if (align == null) continue;
                        alignArray.Add(new JObject
                        {
                            ["name"]          = align.Name,
                            ["handle"]        = align.Handle.ToString(),
                            ["start_station"] = align.StartingStation,
                            ["end_station"]   = align.EndingStation,
                            ["length"]        = align.Length,
                            ["entities"]      = BuildAlignmentEntities(align),
                            ["profiles"]      = BuildProfiles(align, tr),
                        });
                    }
                    catch { }
                }
                root["alignments"] = alignArray;

                // Surfaces
                var surfArray = new JArray();
                foreach (var ts in tinSurfaces)  surfArray.Add(BuildSurface(ts, "TIN"));
                foreach (var gs in gridSurfaces) surfArray.Add(BuildSurface(gs, "Grid"));
                root["surfaces"] = surfArray;

                // Corridors
                var corrArray = new JArray();
                foreach (var corridor in corridors) corrArray.Add(BuildCorridor(corridor, tr));
                root["corridors"] = corrArray;

                // Pipe networks
                var netArray = new JArray();
                ObjectIdCollection netIds = civilDoc.GetPipeNetworkIds();
                foreach (ObjectId netId in netIds)
                {
                    try
                    {
                        Network network = tr.GetObject(netId, OpenMode.ForRead) as Network;
                        if (network == null) continue;
                        netArray.Add(BuildNetwork(network, tr));
                    }
                    catch { }
                }
                root["pipe_networks"] = netArray;

                // Feature lines
                var flArray = new JArray();
                foreach (var fl in featureLines) flArray.Add(BuildFeatureLine(fl));
                root["feature_lines"] = flArray;

                // Parcels
                root["parcels"] = new JArray(
                    parcels.Select(p => new JObject
                    {
                        ["name"]   = p.Name,
                        ["handle"] = p.Handle.ToString(),
                        ["area"]   = p.Area,
                    })
                );

                // Profile / section views
                root["profile_views"] = new JArray(
                    profileViews.Select(pv => new JObject { ["name"] = pv.Name, ["handle"] = pv.Handle.ToString() })
                );
                root["section_views"] = new JArray(
                    sectionViews.Select(sv => new JObject { ["name"] = sv.Name, ["handle"] = sv.Handle.ToString() })
                );

                // Lines
                root["lines"] = new JArray(
                    lines.Select(ln => new JObject
                    {
                        ["handle"]      = ln.Handle.ToString(),
                        ["layer"]       = ln.Layer,
                        ["start_point"] = new JArray(ln.StartPoint.X, ln.StartPoint.Y, ln.StartPoint.Z),
                        ["end_point"]   = new JArray(ln.EndPoint.X,   ln.EndPoint.Y,   ln.EndPoint.Z),
                        ["length"]      = ln.Length,
                    })
                );

                // Arcs
                root["arcs"] = new JArray(
                    arcs.Select(ar => new JObject
                    {
                        ["handle"]      = ar.Handle.ToString(),
                        ["layer"]       = ar.Layer,
                        ["center"]      = new JArray(ar.Center.X, ar.Center.Y, ar.Center.Z),
                        ["radius"]      = ar.Radius,
                        ["start_angle"] = ar.StartAngle,
                        ["end_angle"]   = ar.EndAngle,
                        ["length"]      = ar.Length,
                    })
                );

                // Standard AutoCAD entities (all other types, by count)
                root["standard_entities"] = new JArray(
                    standardEntities.Select(kvp => new JObject { ["type"] = kvp.Key, ["count"] = kvp.Value })
                );

                LastJson = root.ToString(Newtonsoft.Json.Formatting.Indented);
                tr.Commit();
            }
        }

        // ── Alignment geometry ────────────────────────────────────────────────────
        private static JArray BuildAlignmentEntities(Alignment align)
        {
            var arr = new JArray();
            try
            {
                foreach (AlignmentEntity ae in align.Entities)
                {
                    try { arr.Add(AlignmentEntityToJson(ae)); }
                    catch { }
                }
            }
            catch { }
            return arr;
        }

        private static JObject AlignmentEntityToJson(AlignmentEntity ae)
        {
            if (ae is AlignmentLine t)
                return new JObject
                {
                    ["type"]      = "Tangent",
                    ["start"]     = new JArray(t.StartPoint.X, t.StartPoint.Y),
                    ["end"]       = new JArray(t.EndPoint.X, t.EndPoint.Y),
                    ["length"]    = t.Length,
                    ["direction"] = t.Direction,
                };

            if (ae is AlignmentArc arc)
                return new JObject
                {
                    ["type"]      = "Arc",
                    ["center"]    = new JArray(arc.CenterPoint.X, arc.CenterPoint.Y),
                    ["radius"]    = arc.Radius,
                    ["start"]     = new JArray(arc.StartPoint.X, arc.StartPoint.Y),
                    ["end"]       = new JArray(arc.EndPoint.X, arc.EndPoint.Y),
                    ["delta"]     = arc.Delta,
                    ["clockwise"] = arc.Clockwise,
                    ["length"]    = arc.Length,
                };

            // Spirals and compound types — capture what's available
            return new JObject { ["type"] = ae.GetType().Name };
        }

        // ── Profiles ──────────────────────────────────────────────────────────────
        private static JArray BuildProfiles(Alignment align, Transaction tr)
        {
            var arr = new JArray();
            try
            {
                foreach (ObjectId profId in align.GetProfileIds())
                {
                    try
                    {
                        Profile profile = tr.GetObject(profId, OpenMode.ForRead) as Profile;
                        if (profile == null) continue;

                        // PVIs
                        var pviArray = new JArray();
                        foreach (ProfilePVI pvi in profile.PVIs)
                            pviArray.Add(new JObject
                            {
                                ["station"]   = pvi.Station,
                                ["elevation"] = pvi.Elevation,
                            });

                        // Tangents and vertical curves
                        var entArray = new JArray();
                        foreach (ProfileEntity pe in profile.Entities)
                        {
                            try { entArray.Add(ProfileEntityToJson(pe)); }
                            catch { }
                        }

                        arr.Add(new JObject
                        {
                            ["name"]     = profile.Name,
                            ["handle"]   = profile.Handle.ToString(),
                            ["type"]     = profile.ProfileType.ToString(),
                            ["pvis"]     = pviArray,
                            ["entities"] = entArray,
                        });
                    }
                    catch { }
                }
            }
            catch { }
            return arr;
        }

        private static JObject ProfileEntityToJson(ProfileEntity pe)
        {
            if (pe is ProfileTangent t)
                return new JObject
                {
                    ["type"]            = "Tangent",
                    ["start_station"]   = t.StartStation,
                    ["end_station"]     = t.EndStation,
                    ["start_elevation"] = t.StartElevation,
                    ["end_elevation"]   = t.EndElevation,
                };

            if (pe is ProfileParabolaSymmetric pps)
                return new JObject
                {
                    ["type"]            = "ParabolicCurve",
                    ["start_station"]   = pps.StartStation,
                    ["end_station"]     = pps.EndStation,
                    ["start_elevation"] = pps.StartElevation,
                    ["end_elevation"]   = pps.EndElevation,
                    ["length"]          = pps.Length,
                };

            if (pe is ProfileParabolaAsymmetric ppa)
                return new JObject
                {
                    ["type"]            = "ParabolicCurveAsymmetric",
                    ["start_station"]   = ppa.StartStation,
                    ["end_station"]     = ppa.EndStation,
                    ["start_elevation"] = ppa.StartElevation,
                    ["end_elevation"]   = ppa.EndElevation,
                    ["length"]          = ppa.Length,
                };

            if (pe is ProfileCircular pc)
                return new JObject
                {
                    ["type"]            = "CircularCurve",
                    ["start_station"]   = pc.StartStation,
                    ["end_station"]     = pc.EndStation,
                    ["start_elevation"] = pc.StartElevation,
                    ["end_elevation"]   = pc.EndElevation,
                    ["radius"]          = pc.Radius,
                };

            return new JObject { ["type"] = pe.GetType().Name };
        }

        // ── Surfaces ──────────────────────────────────────────────────────────────
        private static JObject BuildSurface(Autodesk.Civil.DatabaseServices.Surface surface, string type)
        {
            var obj = new JObject
            {
                ["name"]                = surface.Name,
                ["handle"]              = surface.Handle.ToString(),
                ["type"]                = type,
                ["reconstruction_note"] = "Use LandXML for full point data",
            };
            try
            {
                var ext = surface.GeometricExtents;
                obj["extents"] = new JObject
                {
                    ["min"] = new JArray(ext.MinPoint.X, ext.MinPoint.Y, ext.MinPoint.Z),
                    ["max"] = new JArray(ext.MaxPoint.X, ext.MaxPoint.Y, ext.MaxPoint.Z),
                };
            }
            catch { }
            return obj;
        }

        // ── Corridors ─────────────────────────────────────────────────────────────
        private static JObject BuildCorridor(Corridor corridor, Transaction tr)
        {
            var baselines = new JArray();
            try
            {
                foreach (Baseline bl in corridor.Baselines)
                {
                    string alignName = "", profileName = "";
                    try { alignName   = (tr.GetObject(bl.AlignmentId, OpenMode.ForRead) as Alignment)?.Name ?? ""; } catch { }
                    try { profileName = (tr.GetObject(bl.ProfileId,   OpenMode.ForRead) as Profile)?.Name   ?? ""; } catch { }

                    var regions = new JArray();
                    try
                    {
                        foreach (BaselineRegion region in bl.BaselineRegions)
                        {
                            string asmName = "";
                            try { asmName = (tr.GetObject(region.AssemblyId, OpenMode.ForRead) as Assembly)?.Name ?? ""; } catch { }
                            regions.Add(new JObject
                            {
                                ["assembly"]      = asmName,
                                ["start_station"] = region.StartStation,
                                ["end_station"]   = region.EndStation,
                            });
                        }
                    }
                    catch { }

                    baselines.Add(new JObject
                    {
                        ["alignment"] = alignName,
                        ["profile"]   = profileName,
                        ["regions"]   = regions,
                    });
                }
            }
            catch { }

            return new JObject
            {
                ["name"]      = corridor.Name,
                ["handle"]    = corridor.Handle.ToString(),
                ["baselines"] = baselines,
            };
        }

        // ── Pipe networks ─────────────────────────────────────────────────────────
        private static JObject BuildNetwork(Network network, Transaction tr)
        {
            var pipes = new JArray();
            try
            {
                foreach (ObjectId pipeId in network.GetPipeIds())
                {
                    try
                    {
                        Pipe pipe = tr.GetObject(pipeId, OpenMode.ForRead) as Pipe;
                        if (pipe == null) continue;

                        string startStr = "", endStr = "";
                        try { startStr = (tr.GetObject(pipe.StartStructureId, OpenMode.ForRead) as Structure)?.Name ?? ""; } catch { }
                        try { endStr   = (tr.GetObject(pipe.EndStructureId,   OpenMode.ForRead) as Structure)?.Name ?? ""; } catch { }

                        var sp = pipe.StartPoint;
                        var ep = pipe.EndPoint;
                        pipes.Add(new JObject
                        {
                            ["name"]            = pipe.Name,
                            ["handle"]          = pipe.Handle.ToString(),
                            ["inner_diameter"]  = pipe.InnerDiameterOrWidth,
                            ["start_point"]     = new JArray(sp.X, sp.Y, sp.Z),
                            ["end_point"]       = new JArray(ep.X, ep.Y, ep.Z),
                            ["start_structure"] = startStr,
                            ["end_structure"]   = endStr,
                            ["length"]          = pipe.Length3DToInsideEdge,
                            ["slope"]           = pipe.Slope,
                        });
                    }
                    catch { }
                }
            }
            catch { }

            var structures = new JArray();
            try
            {
                foreach (ObjectId structId in network.GetStructureIds())
                {
                    try
                    {
                        Structure s = tr.GetObject(structId, OpenMode.ForRead) as Structure;
                        if (s == null) continue;

                        var ip = s.Location;
                        structures.Add(new JObject
                        {
                            ["name"]            = s.Name,
                            ["handle"]          = s.Handle.ToString(),
                            ["insertion_point"] = new JArray(ip.X, ip.Y),
                            ["rim_elevation"]   = s.RimElevation,
                            ["sump_elevation"]  = s.SumpElevation,
                            ["inner_diameter"]  = s.InnerDiameterOrWidth,
                        });
                    }
                    catch { }
                }
            }
            catch { }

            return new JObject
            {
                ["name"]       = network.Name,
                ["handle"]     = network.Handle.ToString(),
                ["pipes"]      = pipes,
                ["structures"] = structures,
            };
        }

        // ── Feature lines ─────────────────────────────────────────────────────────
        private static JObject BuildFeatureLine(FeatureLine fl)
        {
            var pts = new JArray();
            try
            {
                // FeatureLinePointType enum was removed in Civil 3D 2026.
                // Attempt to call GetPoints via reflection to stay forward-compatible.
                var method = fl.GetType().GetMethod("GetPoints");
                if (method != null)
                {
                    var result = method.Invoke(fl, null)
                                 ?? method.Invoke(fl, new object[] { 0 });
                    if (result is System.Collections.IEnumerable enumerable)
                        foreach (var pt in enumerable)
                        {
                            dynamic dpt = pt;
                            pts.Add(new JArray((double)dpt.X, (double)dpt.Y, (double)dpt.Z));
                        }
                }
            }
            catch { }

            return new JObject
            {
                ["name"]   = fl.Name,
                ["handle"] = fl.Handle.ToString(),
                ["points"] = pts,
            };
        }
    }
}
