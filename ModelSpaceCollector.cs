using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;
using Autodesk.Civil.ApplicationServices;
using Autodesk.Civil.DatabaseServices;
using MathNet.Numerics.LinearAlgebra;
using MathNet.Numerics.LinearAlgebra.Double;
using MathNet.Numerics.Statistics;

// Disambiguate types that exist in both AutoCAD and Civil3D namespaces
using AcadDBObject = Autodesk.AutoCAD.DatabaseServices.DBObject;
using AcadEntity   = Autodesk.AutoCAD.DatabaseServices.Entity;
using System.Data.Common;
using Autodesk.Civil;
using System;

[assembly: CommandClass(typeof(CADAgent.ModelSpaceCollector))]

namespace CADAgent
{
    public class ModelSpaceCollector
    {
        /// <summary>
        /// The JSON string produced by the most recent call to Collect() for each document,
        /// keyed by the document's full path (Document.Name). Read by HttpBridge.HandleModelSpace()
        /// and returned as the HTTP response body to the MCP server. Per-document so that
        /// concurrent sessions reading different drawings don't clobber each other's cached JSON.
        /// </summary>
        private static readonly System.Collections.Concurrent.ConcurrentDictionary<string, string> _lastJsonByDoc =
            new System.Collections.Concurrent.ConcurrentDictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        public static string GetLastJson(string docPath) =>
            docPath != null && _lastJsonByDoc.TryGetValue(docPath, out var json) ? json : string.Empty;

        /// <summary>Convenience accessor for the MdiActiveDocument — used by the GETCADOBJECTS command.</summary>
        public static string LastJson => GetLastJson(Application.DocumentManager.MdiActiveDocument?.Name);

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
        /// Prints a message directing the user to Claude Desktop. Commands are now handled via MCP.
        /// </summary>
        [CommandMethod("ASKAGENT")]
        public void AskAgent()
        {
            Document doc = Application.DocumentManager.MdiActiveDocument;
            doc?.Editor.WriteMessage("\n  CAD Agent is running — use Claude Desktop to send commands.\n");
        }

        [CommandMethod("DRAWSPATIALHASH")]
        public void DrawSpatialHash()
        {
            Document doc = Application.DocumentManager.MdiActiveDocument;
            Editor ed = doc.Editor;
            Database db = doc.Database;

            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForWrite);

                // Create debug layer
                var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
                const string layerName = "SPATIALHASH_DEBUG";
                if (!lt.Has(layerName))
                {
                    lt.UpgradeOpen();
                    var ltr = new LayerTableRecord { Name = layerName };
                    ltr.Color = Autodesk.AutoCAD.Colors.Color.FromColorIndex(Autodesk.AutoCAD.Colors.ColorMethod.ByAci, 1); // red
                    lt.Add(ltr);
                    tr.AddNewlyCreatedDBObject(ltr, true);
                }

                // Collect geometry (same as Collect but lightweight)
                var lines     = new List<Line>();
                var arcs      = new List<Arc>();
                var polylines = new List<Polyline>();
                var msRead = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
                foreach (ObjectId id in msRead)
                {
                    try
                    {
                        var obj = tr.GetObject(id, OpenMode.ForRead);
                        if (obj is Line   ln && ln.Layer != layerName) lines.Add(ln);
                        if (obj is Arc    ar && ar.Layer != layerName) arcs.Add(ar);
                        if (obj is Polyline pl && pl.Layer != layerName) polylines.Add(pl);
                    }
                    catch { }
                }

                var hash = new SpatialHash(cellSize: 50.0, adjacencyThreshold: 50.0);
                hash.Build(lines, arcs, polylines);

                // Draw one rectangle per occupied cell
                int cellCount = 0;
                foreach (var (x0, y0, x1, y1) in hash.GetOccupiedCells())
                {
                    var pl = new Polyline();
                    pl.Layer = layerName;
                    pl.AddVertexAt(0, new Autodesk.AutoCAD.Geometry.Point2d(x0, y0), 0, 0, 0);
                    pl.AddVertexAt(1, new Autodesk.AutoCAD.Geometry.Point2d(x1, y0), 0, 0, 0);
                    pl.AddVertexAt(2, new Autodesk.AutoCAD.Geometry.Point2d(x1, y1), 0, 0, 0);
                    pl.AddVertexAt(3, new Autodesk.AutoCAD.Geometry.Point2d(x0, y1), 0, 0, 0);
                    pl.Closed = true;
                    ms.AppendEntity(pl);
                    tr.AddNewlyCreatedDBObject(pl, true);
                    cellCount++;
                }

                // Draw a circle at each adjacency contact point
                foreach (var edge in hash.Edges)
                {
                    var circle = new Circle(
                        new Autodesk.AutoCAD.Geometry.Point3d(edge.ContactPoint.X, edge.ContactPoint.Y, 0),
                        Autodesk.AutoCAD.Geometry.Vector3d.ZAxis,
                        hash.CellSize * 0.1);
                    circle.Layer = layerName;
                    ms.AppendEntity(circle);
                    tr.AddNewlyCreatedDBObject(circle, true);
                }

                tr.Commit();
                ed.WriteMessage($"\n  SPATIALHASH_DEBUG: {cellCount} cells, {hash.Edges.Count} adjacency edges drawn.\n");
                ed.WriteMessage($"  Run ERASE and select layer SPATIALHASH_DEBUG to clean up.\n");
            }
        }

        public static (double[], double[], double[]) PolyPCA(double[] xPoint, double[] yPoint)
        {
            var polyMatrix = DenseMatrix.OfColumns(xPoint.Length, 2, new[] {xPoint, yPoint});
            var mean = polyMatrix.ColumnSums() / xPoint.Length;
            for(int i = 0; i < xPoint.Length; i++)
                polyMatrix.SetRow(i, polyMatrix.Row(i) - mean);

            var covMatrix = (polyMatrix.Transpose() * polyMatrix) / (xPoint.Length-1);
            var evd = covMatrix.Evd(Symmetricity.Symmetric);
            double[] eigenValues = evd.EigenValues.Real().ToArray();
            double[] mainEigenvector = evd.EigenVectors.Column(1).ToArray();
            double[] secondaryEigenvector = evd.EigenVectors.Column(0).ToArray();

            return (eigenValues, mainEigenvector, secondaryEigenvector);
        }

        /// <summary>
        /// Gathers all entities from model space with full geometric data, builds a JSON
        /// string, and stores it in <see cref="LastJson"/>.
        /// Primary caller: HttpBridge.HandleModelSpace() (via GET /modelspace).
        /// </summary>
        public static void Collect(Document doc)
        {
            Database db = doc.Database;
            // Resolve the Civil 3D document from this drawing's database, not the focused one —
            // otherwise collecting a background document silently reads the active drawing's data.
            CivilDocument civilDoc = CivilDocument.GetCivilDocument(db);

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
                var polylines     = new List<Polyline>();
                var blocks        = new List<BlockReference>();
                // Entity types the general drafting command set can create; summarised so the
                // agent gets their handles back from get_model_space and can act on them.
                var circles       = new List<AcadEntity>();
                var ellipses      = new List<AcadEntity>();
                var splines       = new List<AcadEntity>();
                var points        = new List<AcadEntity>();
                var texts         = new List<AcadEntity>();
                var dimensions    = new List<AcadEntity>();
                var hatches       = new List<AcadEntity>();
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
                        if (entity is Line           ln) { lines.Add(ln);        continue; }
                        if (entity is Arc            ar) { arcs.Add(ar);         continue; }
                        if (entity is Polyline       pl) { polylines.Add(pl);    continue; }
                        if (entity is BlockReference br) { blocks.Add(br);       continue; }
                        if (entity is Circle)            { circles.Add(entity);    continue; }
                        if (entity is Ellipse)           { ellipses.Add(entity);   continue; }
                        if (entity is Spline)            { splines.Add(entity);    continue; }
                        if (entity is DBPoint)           { points.Add(entity);     continue; }
                        if (entity is DBText || entity is MText || entity is MLeader) { texts.Add(entity); continue; }
                        if (entity is Dimension)         { dimensions.Add(entity); continue; }
                        if (entity is Hatch)             { hatches.Add(entity);    continue; }

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
                            ["description"]   = align.Description,
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

                // Polylines
                var polylineArray = new JArray();
                foreach (var pl in polylines)
                {
                    var verts = new JArray();
                    var bulges = new JArray();
                    int numVert = pl.NumberOfVertices;
                    double[] xPoint = new double[numVert];
                    double[] yPoint = new double[numVert];
                    for (int i = 0; i < pl.NumberOfVertices; i++)
                    {
                        var pt = pl.GetPoint2dAt(i);
                        xPoint[i] = pt.X;
                        yPoint[i] = pt.Y;
                        verts.Add(new JArray(pt.X, pt.Y));
                        // Bulge i belongs to the segment leaving vertex i. Without
                        // it a curved curb is read as a chord between its vertices,
                        // a sagitta away from the real curb -- consumers that need
                        // the true curve (driveways) rebuild the arc from this.
                        bulges.Add(pl.GetBulgeAt(i));
                    }
                    var (eigenValues, mainEigenvector, secondaryEigenvector) = PolyPCA(xPoint, yPoint);
                    double[] scaledMainEigenvector = mainEigenvector.Select(v => v * Math.Sqrt(eigenValues[1])).ToArray();
                    double[] scaledSecondaryEigenvector = secondaryEigenvector.Select(v => v * Math.Sqrt(eigenValues[0])).ToArray();

                    double area = 0, cx = 0, cy = 0;
                    for (int i = 0; i < numVert; i++)
                    {
                        int j = (i + 1) % numVert;
                        double cross = xPoint[i] * yPoint[j] - xPoint[j] * yPoint[i];
                        area += cross;
                        cx += (xPoint[i] + xPoint[j]) * cross;
                        cy += (yPoint[i] + yPoint[j]) * cross;
                    }
                    area /= 2;
                    cx /= (6 * area);
                    cy /= (6 * area);

                    polylineArray.Add(new JObject
                    {
                        ["handle"]               = pl.Handle.ToString(),
                        ["layer"]                = pl.Layer,
                        ["closed"]               = pl.Closed,
                        ["vertices"]             = verts,
                        ["bulges"]               = bulges,
                        ["length"]               = pl.Length,
                        ["area"]                 = pl.Area,
                        ["centroid"]             = new JArray(cx, cy),
                        ["main_eigenvector"]     = new JArray(scaledMainEigenvector),
                        ["secondary_eigenvector"]= new JArray(scaledSecondaryEigenvector),
                    });
                }
                root["polylines"] = polylineArray;

                // Blocks
                var blockArray = new JArray();
                foreach (var br in blocks)
                {
                    try
                    {
                        var attribs = new JObject();
                        foreach (ObjectId attId in br.AttributeCollection)
                        {
                            try
                            {
                                var att = tr.GetObject(attId, OpenMode.ForRead) as AttributeReference;
                                if (att != null) attribs[att.Tag] = att.TextString;
                            }
                            catch { }
                        }
                        blockArray.Add(new JObject
                        {
                            ["handle"]     = br.Handle.ToString(),
                            ["name"]       = br.Name,
                            ["layer"]      = br.Layer,
                            ["position"]   = new JArray(Math.Round(br.Position.X, 4), Math.Round(br.Position.Y, 4)),
                            ["rotation"]   = Math.Round(br.Rotation * (180.0 / Math.PI), 4),
                            ["scale"]      = Math.Round(br.ScaleFactors.X, 4),
                            ["attributes"] = attribs,
                        });
                    }
                    catch { }
                }
                root["blocks"] = blockArray;

                // Circles, ellipses, splines, points, text, dimensions, hatches: compact summaries
                // (handle, layer, key geometry) from the same code select_entities uses.
                JArray Summaries(List<AcadEntity> list)
                {
                    var arr = new JArray();
                    foreach (var e in list)
                    {
                        try { arr.Add(CadCommands.Summarize(tr, e, false)); } catch { }
                    }
                    return arr;
                }
                root["circles"]    = Summaries(circles);
                root["ellipses"]   = Summaries(ellipses);
                root["splines"]    = Summaries(splines);
                root["points"]     = Summaries(points);
                root["texts"]      = Summaries(texts);
                root["dimensions"] = Summaries(dimensions);
                root["hatches"]    = Summaries(hatches);

                // Layers (name, colour, state) — previously advertised as a category but never emitted.
                var layerArray = new JArray();
                try
                {
                    var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
                    foreach (ObjectId lid in lt)
                    {
                        var ltr = (LayerTableRecord)tr.GetObject(lid, OpenMode.ForRead);
                        layerArray.Add(new JObject
                        {
                            ["name"]    = ltr.Name,
                            ["color"]   = ltr.Color.ColorIndex,
                            ["on"]      = !ltr.IsOff,
                            ["frozen"]  = ltr.IsFrozen,
                            ["locked"]  = ltr.IsLocked,
                            ["current"] = lid == db.Clayer,
                        });
                    }
                }
                catch { }
                root["layers"] = layerArray;

                // Standard AutoCAD entities (all other types, by count)
                root["standard_entities"] = new JArray(
                    standardEntities.Select(kvp => new JObject { ["type"] = kvp.Key, ["count"] = kvp.Value })
                );

                // Spatial adjacency graph
                var spatialHash = new SpatialHash(cellSize: 50.0, adjacencyThreshold: 50.0);
                spatialHash.Build(lines, arcs, polylines);
                root["adjacency"] = spatialHash.ToJson();

                _lastJsonByDoc[doc.Name] = root.ToString(Newtonsoft.Json.Formatting.Indented);
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
                            ["description"]     = pipe.Description,
                            ["network"]         = network.Name,
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
                            ["description"]     = s.Description,
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
