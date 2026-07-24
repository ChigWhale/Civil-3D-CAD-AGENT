using System;
using System.Collections.Generic;
using Newtonsoft.Json;
using System.IO;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;
using Autodesk.Civil.ApplicationServices;
using Autodesk.Civil.DatabaseServices;

// Disambiguate types shared between AutoCAD/Civil and WPF namespaces
using AcadApp            = Autodesk.AutoCAD.ApplicationServices.Application;
using CivilLabel         = Autodesk.Civil.DatabaseServices.Label;
using AcadEntity         = Autodesk.AutoCAD.DatabaseServices.Entity;
using AcadLine           = Autodesk.AutoCAD.DatabaseServices.Line;
using AcadPolyline       = Autodesk.AutoCAD.DatabaseServices.Polyline;

[assembly: CommandClass(typeof(CADAgent.GNNGraphCreator))]

namespace CADAgent
{
public class GNNGraphCreator
{
        private Document doc;
        private Database db;
        private Editor ed;
        private Point3d minPoint;
        private Point3d maxPoint;

    [CommandMethod("GraphNeuralNetworkCreator")]
    public void CreateGraph()
    {
        //Creating gloabal variables, and needed objects for transactions
        bool isLabel = false;
        doc = AcadApp.DocumentManager.MdiActiveDocument;
        db = doc.Database;
        ed = doc.Editor;
        minPoint = db.Extmin;
        maxPoint = db.Extmax;

        //Checking for label or feedfoward creation
        PromptStringOptions typeGraph = new PromptStringOptions("Is this a label graph or a feedforward graph? (L/F)");
        PromptResult pr = ed.GetString(typeGraph);
        if(pr.StringResult.ToUpper() == "L")
        {
            isLabel = true;
        }
        PromptStringOptions project = new PromptStringOptions("What project are these graphs for: ");
        PromptResult result = ed.GetString(project);
        string projectInput = result.StringResult;

        using (Transaction tr = db.TransactionManager.StartTransaction())
        {
            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
            List<(int,List<double>)> nodes = new List<(int,List<double>)>();
            List<(int,List<double>)> edgeCreation = new List<(int,List<double>)>();
            List<int> fromList = new List<int>();
            List<int> toList = new List<int>();
            int graphNum = 1;

            //Grabbing the object Id for all labels in modelspace
            foreach (ObjectId id in ms)
            {
                if (tr.GetObject(id, OpenMode.ForRead) is CivilLabel label && label.Dragged)
                {
                    CivilLabel graphStart = tr.GetObject(id, OpenMode.ForRead) as CivilLabel;
                    Point3d labelPoint = graphStart.LabelLocation;
                    double pointX = labelPoint.X;
                    double pointY = labelPoint.Y;
                    (nodes, edgeCreation) = makeGraph(tr, id, pointX, pointY);
                    (fromList, toList) = makeGraphEdges(edgeCreation);
                    if(isLabel)
                    {
                        List<double> disp = new List<double> {graphStart.DraggedOffset.X, graphStart.DraggedOffset.Y};
                        writeGraphToFile(fromList, toList, nodes, projectInput, ref graphNum, disp);
                    }
                    else
                    {
                        writeGraphToFile(fromList, toList, nodes, projectInput, ref graphNum);
                    }
                    graphNum += 1;
                }
            }         
        }
    }

    public (List<(int,List<double>)>,List<(int,List<double>)>) makeGraph(Transaction tr, ObjectId labelID, double labelPointX, double labelPointY, double boxing = 50.0, double proximity = 20.0)
    {
        Point3d graphMin = new Point3d(labelPointX - boxing, labelPointY - boxing, 0);
        Point3d graphMax = new Point3d(labelPointX + boxing, labelPointY + boxing, 0);
        List<(int,List<double>)> nodes = new List<(int,List<double>)>();
        List<(int,List<double>)> edgeCreation = new List<(int,List<double>)>();
        int nodeNum = 1;

        PromptSelectionResult boxedObj = ed.SelectCrossingWindow(graphMin, graphMax);

        foreach (SelectedObject withinBox in boxedObj.Value)
        {
            var ent = tr.GetObject(withinBox.ObjectId, OpenMode.ForRead) as AcadEntity;
            (List<(int,List<double>)> nodeFeature, List<(int,List<double>)> nodeCoord) = getObjPoint(ent, tr, graphMin, graphMax, ref nodeNum);
            for (int i = 0; i < nodeFeature.Count; i++)
            {
                nodes.Add(nodeFeature[i]);
                edgeCreation.Add(nodeCoord[i]);
            }
        }
        return (nodes, edgeCreation);
    }

    public (List<int>, List<int>) makeGraphEdges(List<(int,List<double>)> edgeCreation, double proximity = 20.0)
    {
        List<int> fromList = new List<int>();
        List<int> toList = new List<int>();

        for (int k = 0; k < edgeCreation.Count; k++)
        {
            List<double> nodeInQuestion = edgeCreation[k].Item2;
            for(int j = 0; j < edgeCreation.Count; j++)
            {
                if(nodeInQuestion[0] - proximity <= edgeCreation[j].Item2[0] && 
                edgeCreation[j].Item2[0] <= nodeInQuestion[0] + proximity &&
                nodeInQuestion[1] - proximity <= edgeCreation[j].Item2[1] && 
                edgeCreation[j].Item2[1] <= nodeInQuestion[1] + proximity && k != j)
                {
                    fromList.Add(edgeCreation[k].Item1);
                    toList.Add(edgeCreation[j].Item1);
                }
            }
        }  

        return (fromList, toList); 
    }

    public (List<(int,List<double>)>,List<(int,List<double>)>) getObjPoint(AcadEntity entity, Transaction tr, Point3d graphMin, Point3d graphMax, ref int nodeNum)
    {
        List<(int,List<double>)> nodeFeature = new List<(int,List<double>)>();
        List<(int,List<double>)> nodeCoord = new List<(int,List<double>)>();

        switch(entity)
        {
            case AcadLine ln:
                Point3d normalizedStartLine = new Point3d((ln.StartPoint.X-minPoint.X)/(maxPoint.X-minPoint.X),(ln.StartPoint.Y-minPoint.Y)/(maxPoint.Y-minPoint.Y),0);
                Point3d normalizedEndLine = new Point3d((ln.EndPoint.X-minPoint.X)/(maxPoint.X-minPoint.X),(ln.EndPoint.Y-minPoint.Y)/(maxPoint.Y-minPoint.Y),0);
                nodeFeature.Add((nodeNum, new List<double> {normalizedStartLine.X, normalizedStartLine.Y}));
                nodeCoord.Add((nodeNum, new List<double> {ln.StartPoint.X, ln.StartPoint.Y}));
                nodeNum += 1;
                nodeFeature.Add((nodeNum, new List<double> {normalizedEndLine.X, normalizedEndLine.Y}));
                nodeCoord.Add((nodeNum, new List<double> {ln.EndPoint.X, ln.EndPoint.Y}));
                nodeNum += 1;
                break;
            
            case Arc ar:
                Point3d normalizedArcCenter = new Point3d((ar.Center.X-minPoint.X)/(maxPoint.X-minPoint.X),(ar.Center.Y-minPoint.Y)/(maxPoint.Y-minPoint.Y),0);
                nodeFeature.Add((nodeNum, new List<double> {normalizedArcCenter.X, normalizedArcCenter.Y}));
                nodeCoord.Add((nodeNum, new List<double> {ar.Center.X, ar.Center.Y}));
                nodeNum += 1;
                break;
            
            case AcadPolyline pl:
                for (int k = 0; k < pl.NumberOfVertices; k++)
                {
                    Point3d normalizedvertex = new Point3d((pl.GetPoint3dAt(k).X-minPoint.X)/(maxPoint.X-minPoint.X),(pl.GetPoint3dAt(k).Y-minPoint.Y)/(maxPoint.Y-minPoint.Y),0);
                    nodeFeature.Add((nodeNum, new List<double> {normalizedvertex.X, normalizedvertex.Y}));
                    nodeCoord.Add((nodeNum, new List<double> {pl.GetPoint3dAt(k).X, pl.GetPoint3dAt(k).Y}));
                    nodeNum += 1;
                }
                break;

            case CivilLabel label:
                Point3d normalizedLabelPoint = new Point3d((label.LabelLocation.X-minPoint.X)/(maxPoint.X-minPoint.X),(label.LabelLocation.Y-minPoint.Y)/(maxPoint.Y-minPoint.Y),0);
                nodeFeature.Add((nodeNum, new List<double> {normalizedLabelPoint.X, normalizedLabelPoint.Y}));
                nodeCoord.Add((nodeNum, new List<double> {label.LabelLocation.X, label.LabelLocation.Y}));
                nodeNum += 1;
                break;
            
            case BlockReference br:
                BlockTableRecord btr = tr.GetObject(br.BlockTableRecord, OpenMode.ForRead) as BlockTableRecord;
                foreach (ObjectId entId in btr)
                {
                    AcadEntity ent = tr.GetObject(entId, OpenMode.ForRead) as AcadEntity;
                    switch(ent)
                    {
                        case AcadLine ln:
                            Point3d transformedStartPoint = ln.StartPoint.TransformBy(br.BlockTransform);
                            Point3d transformedEndPoint = ln.EndPoint.TransformBy(br.BlockTransform);
                            Point3d normalizedBlockStartLine = new Point3d((transformedStartPoint.X-minPoint.X)/(maxPoint.X-minPoint.X),(transformedStartPoint.Y-minPoint.Y)/(maxPoint.Y-minPoint.Y),0);
                            Point3d normalizedBlockEndLine = new Point3d((transformedEndPoint.X-minPoint.X)/(maxPoint.X-minPoint.X),(transformedEndPoint.Y-minPoint.Y)/(maxPoint.Y-minPoint.Y),0);
                            nodeFeature.Add((nodeNum, new List<double> {normalizedBlockStartLine.X, normalizedBlockStartLine.Y}));
                            nodeCoord.Add((nodeNum, new List<double> {transformedStartPoint.X, transformedStartPoint.Y}));
                            nodeNum += 1;
                            nodeFeature.Add((nodeNum, new List<double> {normalizedBlockEndLine.X, normalizedBlockEndLine.Y}));
                            nodeCoord.Add((nodeNum, new List<double> {transformedEndPoint.X, transformedEndPoint.Y}));
                            nodeNum += 1;
                            break;

                        case Arc ar:
                            Point3d transformedArcCenter = ar.Center.TransformBy(br.BlockTransform);
                            Point3d normalizedBlockArcCenter = new Point3d((transformedArcCenter.X-minPoint.X)/(maxPoint.X-minPoint.X),(transformedArcCenter.Y-minPoint.Y)/(maxPoint.Y-minPoint.Y),0);
                            nodeFeature.Add((nodeNum, new List<double> {normalizedBlockArcCenter.X, normalizedBlockArcCenter.Y}));
                            nodeCoord.Add((nodeNum, new List<double> {transformedArcCenter.X, transformedArcCenter.Y}));
                            nodeNum += 1;
                            break;
                        
                        case AcadPolyline pl:
                            for (int k = 0; k < pl.NumberOfVertices; k++)
                            {
                                Point3d transformedVertex = pl.GetPoint3dAt(k).TransformBy(br.BlockTransform);
                                Point3d normalizedBlockVertex = new Point3d((transformedVertex.X-minPoint.X)/(maxPoint.X-minPoint.X),(transformedVertex.Y-minPoint.Y)/(maxPoint.Y-minPoint.Y),0);
                                nodeFeature.Add((nodeNum, new List<double> {normalizedBlockVertex.X, normalizedBlockVertex.Y}));
                                nodeCoord.Add((nodeNum, new List<double> {transformedVertex.X, transformedVertex.Y}));
                                nodeNum += 1;
                            }
                            break;

                        case CivilLabel label:
                            Point3d transformedLabelPoint = label.LabelLocation.TransformBy(br.BlockTransform);
                            Point3d normalizedBlockLabelPoint = new Point3d((transformedLabelPoint.X-minPoint.X)/(maxPoint.X-minPoint.X),(transformedLabelPoint.Y-minPoint.Y)/(maxPoint.Y-minPoint.Y),0);
                            nodeFeature.Add((nodeNum, new List<double> {normalizedBlockLabelPoint.X, normalizedBlockLabelPoint.Y}));
                            nodeCoord.Add((nodeNum, new List<double> {transformedLabelPoint.X, transformedLabelPoint.Y}));
                            nodeNum += 1;
                            break;
                    }
                }
                break;
        }
        return (nodeFeature,nodeCoord);
    }
    public void writeGraphToFile(List<int> fromList, List<int> toList, List<(int Node,List<double> normalizedCoords)> nodes, string project, ref int graphNum, List<double> graphLabel = null)
    {
        var toBeJSON = new Dictionary<string, object>();

        toBeJSON["From"] = fromList;
        toBeJSON["To"] = toList;
        toBeJSON["Features"] = nodes;
        if(graphLabel != null)
        {
            toBeJSON["Label"] = graphLabel;
        }

        string json = JsonConvert.SerializeObject(toBeJSON);
        string folderPath = System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),"Downloads", "JSON GNN Representation");
        Directory.CreateDirectory(folderPath);

        string filePath = System.IO.Path.Combine(folderPath, $"graph_{DateTime.Now:yyyyMMdd_HHmmssff}_{project}_{graphNum}.json"
);
        File.WriteAllText(filePath, json);
    }
}
}