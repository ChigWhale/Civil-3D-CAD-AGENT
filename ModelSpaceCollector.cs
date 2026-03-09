using System.Collections.Generic;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;
using Autodesk.Civil.ApplicationServices;
using Autodesk.Civil.DatabaseServices;

[assembly: CommandClass(typeof(CADAgent.ModelSpaceCollector))]

namespace CADAgent
{
    public class ModelSpaceCollector
    {
        /// <summary>
        /// AutoCAD command: GETCADOBJECTS
        /// Gathers all entities from model space, identifies Civil3D-specific types,
        /// and prints a detailed summary to the command line.
        /// </summary>
        [CommandMethod("GETCADOBJECTS")]
        public void GetAllModelSpaceObjects()
        {
            Document doc = Application.DocumentManager.MdiActiveDocument;
            Database db = doc.Database;
            Editor ed = doc.Editor;
            CivilDocument civilDoc = CivilApplication.ActiveDocument;

            var standardEntities = new List<Entity>();
            var civil3DEntities = new List<(string Category, string Name, string Handle)>();

            // Profiles and sample lines live under alignments in the Civil3D object model,
            // not directly as model space entities — collected separately below.
            var profilesByAlignment = new List<(string AlignmentName, string ProfileName, string ProfileType)>();
            var sampleLinesByGroup = new List<(string AlignmentName, string GroupName, string SampleLineName, string Handle)>();

            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                // ── Model space entity scan ──────────────────────────────────────────────
                BlockTable bt = tr.GetObject(db.BlockTableId, OpenMode.ForRead) as BlockTable;
                BlockTableRecord modelSpace = tr.GetObject(
                    bt[BlockTableRecord.ModelSpace], OpenMode.ForRead) as BlockTableRecord;

                foreach (ObjectId objId in modelSpace)
                {
                    DBObject obj = tr.GetObject(objId, OpenMode.ForRead);
                    if (!(obj is Entity entity)) continue;

                    string handle = entity.Handle.ToString();

                    if (entity is Alignment alignment)
                    { civil3DEntities.Add(("Alignment", alignment.Name, handle)); continue; }

                    if (entity is TinSurface tinSurface)
                    { civil3DEntities.Add(("TIN Surface", tinSurface.Name, handle)); continue; }

                    if (entity is GridSurface gridSurface)
                    { civil3DEntities.Add(("Grid Surface", gridSurface.Name, handle)); continue; }

                    if (entity is Corridor corridor)
                    { civil3DEntities.Add(("Corridor", corridor.Name, handle)); continue; }

                    if (entity is FeatureLine featureLine)
                    { civil3DEntities.Add(("Feature Line", featureLine.Name, handle)); continue; }

                    if (entity is Parcel parcel)
                    { civil3DEntities.Add(("Parcel", parcel.Name, handle)); continue; }

                    if (entity is Pipe pipe)
                    { civil3DEntities.Add(("Pipe", pipe.Name, handle)); continue; }

                    if (entity is Structure structure)
                    { civil3DEntities.Add(("Structure", structure.Name, handle)); continue; }

                    if (entity is Grading grading)
                    { civil3DEntities.Add(("Grading", grading.Name, handle)); continue; }

                    if (entity is SurveyFigure figure)
                    { civil3DEntities.Add(("Survey Figure", figure.Name, handle)); continue; }

                    // Profile Views (the grid/frame drawn in model space that displays profiles)
                    if (entity is ProfileView profileView)
                    { civil3DEntities.Add(("Profile View", profileView.Name, handle)); continue; }

                    // Section Views (the grid/frame drawn in model space for cross-sections)
                    if (entity is SectionView sectionView)
                    { civil3DEntities.Add(("Section View", sectionView.Name, handle)); continue; }

                    // Sample Lines drawn in plan view
                    if (entity is SampleLine sampleLine)
                    { civil3DEntities.Add(("Sample Line", sampleLine.Name, handle)); continue; }

                    standardEntities.Add(entity);
                }

                // ── Profiles — enumerated per alignment ──────────────────────────────────
                // Profiles are children of alignments and are not standalone model space entities.
                ObjectIdCollection alignmentIds = civilDoc.GetAlignmentIds();
                foreach (ObjectId alignId in alignmentIds)
                {
                    Alignment align = tr.GetObject(alignId, OpenMode.ForRead) as Alignment;
                    if (align == null) continue;

                    ObjectIdCollection profileIds = align.GetProfileIds();
                    foreach (ObjectId profId in profileIds)
                    {
                        Profile profile = tr.GetObject(profId, OpenMode.ForRead) as Profile;
                        if (profile == null) continue;

                        string profileType = profile.ProfileType.ToString(); // EG, FG, etc.
                        profilesByAlignment.Add((align.Name, profile.Name, profileType));
                    }
                }

                // ── Sample Line Groups — enumerated per alignment ─────────────────────────
                foreach (ObjectId alignId in alignmentIds)
                {
                    Alignment align = tr.GetObject(alignId, OpenMode.ForRead) as Alignment;
                    if (align == null) continue;

                    ObjectIdCollection slgIds = align.GetSampleLineGroupIds();
                    foreach (ObjectId slgId in slgIds)
                    {
                        SampleLineGroup group = tr.GetObject(slgId, OpenMode.ForRead) as SampleLineGroup;
                        if (group == null) continue;

                        ObjectIdCollection slIds = group.GetSampleLineIds();
                        foreach (ObjectId slId in slIds)
                        {
                            SampleLine sl = tr.GetObject(slId, OpenMode.ForRead) as SampleLine;
                            if (sl == null) continue;

                            sampleLinesByGroup.Add((align.Name, group.Name, sl.Name, sl.Handle.ToString()));
                        }
                    }
                }

                // ── Output ───────────────────────────────────────────────────────────────

                // Civil3D model space entities
                ed.WriteMessage($"\n========================================\n");
                ed.WriteMessage($"  Civil3D Entities in Model Space ({civil3DEntities.Count} found)\n");
                ed.WriteMessage($"========================================\n");

                string currentCategory = null;
                foreach (var (category, name, handle) in civil3DEntities)
                {
                    if (category != currentCategory)
                    {
                        ed.WriteMessage($"\n  [{category}]\n");
                        currentCategory = category;
                    }
                    ed.WriteMessage($"    Name: {name}  |  Handle: {handle}\n");
                }

                // Profiles per alignment
                ed.WriteMessage($"\n========================================\n");
                ed.WriteMessage($"  Profiles by Alignment ({profilesByAlignment.Count} found)\n");
                ed.WriteMessage($"========================================\n");

                string currentAlignment = null;
                foreach (var (alignName, profileName, profileType) in profilesByAlignment)
                {
                    if (alignName != currentAlignment)
                    {
                        ed.WriteMessage($"\n  [Alignment: {alignName}]\n");
                        currentAlignment = alignName;
                    }
                    ed.WriteMessage($"    Profile: {profileName}  |  Type: {profileType}\n");
                }

                // Sample lines per alignment/group
                ed.WriteMessage($"\n========================================\n");
                ed.WriteMessage($"  Sample Lines by Alignment ({sampleLinesByGroup.Count} found)\n");
                ed.WriteMessage($"========================================\n");

                string currentSLAlignment = null;
                string currentGroup = null;
                foreach (var (alignName, groupName, slName, slHandle) in sampleLinesByGroup)
                {
                    if (alignName != currentSLAlignment)
                    {
                        ed.WriteMessage($"\n  [Alignment: {alignName}]\n");
                        currentSLAlignment = alignName;
                        currentGroup = null;
                    }
                    if (groupName != currentGroup)
                    {
                        ed.WriteMessage($"    [Group: {groupName}]\n");
                        currentGroup = groupName;
                    }
                    ed.WriteMessage($"      Sample Line: {slName}  |  Handle: {slHandle}\n");
                }

                // Standard AutoCAD entities
                ed.WriteMessage($"\n========================================\n");
                ed.WriteMessage($"  Standard AutoCAD Entities ({standardEntities.Count} found)\n");
                ed.WriteMessage($"========================================\n");

                var groups = new Dictionary<string, int>();
                foreach (Entity entity in standardEntities)
                {
                    string typeName = entity.GetType().Name;
                    if (!groups.ContainsKey(typeName)) groups[typeName] = 0;
                    groups[typeName]++;
                }
                foreach (var kvp in groups)
                    ed.WriteMessage($"  {kvp.Key}: {kvp.Value}\n");

                int total = civil3DEntities.Count + profilesByAlignment.Count
                          + sampleLinesByGroup.Count + standardEntities.Count;

                ed.WriteMessage($"\n========================================\n");
                ed.WriteMessage($"  Grand Total: {total} objects collected\n");
                ed.WriteMessage($"========================================\n");

                tr.Commit();
            }
        }
    }
}
