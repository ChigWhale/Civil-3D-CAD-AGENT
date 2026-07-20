using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Shapes;
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
using WpfDoubleCollection = System.Windows.Media.DoubleCollection;
using WpfEllipse         = System.Windows.Shapes.Ellipse;
using WpfLine            = System.Windows.Shapes.Line;
using WpfPoint           = System.Windows.Point;
using WpfTextAlignment   = System.Windows.TextAlignment;
using System.ComponentModel.DataAnnotations.Schema;
using System.ComponentModel;
using Autodesk.Civil.DatabaseServices.Styles;

public class GNNGraphCreator
{
        private Document doc;
        private Database db;
        private Editor ed;
        private Point3d minPoint;
        private Point3d maxPoint;

    [CommandMethod("GNNGraphCreator")]
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

        using (Transaction tr = db.TransactionManager.StartTransaction())
        {
            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
            Dictionary<int,List<double>> nodes = new Dictionary<int,List<double>>();
            Dictionary<int,List<double>> edgeCreation = new Dictionary<int,List<double>>();

            //Grabbing the object Id for all labels in modelspace
            foreach (ObjectId id in ms)
            {
                if (tr.GetObject(id, OpenMode.ForRead) is CivilLabel label && label.Dragged)
                {
                    CivilLabel graphStart = tr.GetObject(id, OpenMode.ForRead) as CivilLabel;
                    Point3d labelPoint = graphStart.LabelLocation;
                    double pointX = labelPoint.X;
                    double pointY = labelPoint.Y;
                    (nodes, edgeCreation) = makeGraphFeatures(tr, id, pointX, pointY);
                    writeGraphToFile();
                }
            }         
        }
    }

    public (Dictionary<int,List<double>>,Dictionary<int,List<double>>) makeGraphFeatures(Transaction tr, ObjectId labelID, double labelPointX, double labelPointY, int boxing = 50, int proximity = 20)
    {
        Point3d graphMin = new Point3d(labelPointX - boxing, labelPointY - boxing, 0);
        Point3d graphMax = new Point3d(labelPointX + boxing, labelPointY + boxing, 0);
        Dictionary<int,List<double>> nodes = new Dictionary<int,List<double>>();
        Dictionary<int,List<double>> edgeCreation = new Dictionary<int,List<double>>();
        int nodeNum = 0;

        PromptSelectionResult boxedObj = ed.SelectCrossingWindow(graphMin, graphMax);

        foreach (SelectedObject withinBox in boxedObj.Value)
        {
            var ent = tr.GetObject(withinBox.ObjectId, OpenMode.ForRead) as AcadEntity;
            (List<(int,List<double>)> nodeFeature, List<(int,List<double>)> nodeCoord) = getObjPoint(ent, tr, graphMin, graphMax, nodeNum);
        }

        return (nodes, edgeCreation);
    }

    public void makeGraphEdges()
    {
        
    }

    public (List<(int,List<double>)>,List<(int,List<double>)>) getObjPoint(AcadEntity entity, Transaction tr, Point3d graphMin, Point3d graphMax, int nodeNum)
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
                return (nodeFeature, nodeCoord);
            
            case Arc ar:
                Point3d normalizedArcCenter = new Point3d((ar.Center.X-minPoint.X)/(maxPoint.X-maxPoint.Y),(ar.Center.Y-minPoint.Y)/(maxPoint.Y-minPoint.Y),0);
                nodeFeature.Add((nodeNum, new List<double> {normalizedArcCenter.X, normalizedArcCenter.Y}));
                nodeCoord.Add((nodeNum, new List<double> {ar.Center.X, ar.Center.Y}));
                nodeNum += 1;
                return (nodeFeature, nodeCoord);
            
            case AcadPolyline pl:
                for (int k = 0; k < pl.NumberOfVertices; k++)
                {
                    Point3d normalizedvertex = new Point3d((pl.GetPoint3dAt(k).X-minPoint.X)/(maxPoint.X-minPoint.X),(pl.GetPointAtDist(k).Y-minPoint.Y)/(maxPoint.Y-minPoint.Y),0);
                    nodeFeature.Add((nodeNum, new List<double> {normalizedvertex.X, normalizedvertex.Y}));
                    nodeCoord.Add((nodeNum, new List<double> {pl.GetPoint3dAt(k).X, pl.GetPointAtDist(k).Y}));
                    nodeNum += 1;
                }
                return (nodeFeature, nodeCoord);

            case CivilLabel label:
                break;
            
            case BlockReference br:
                BlockTableRecord btr = tr.GetObject(br.BlockTableRecord, OpenMode.ForRead) as BlockTableRecord;
                foreach (ObjectId entId in btr)
                {
                    
                }
                break;
        }
        return (nodeFeature,nodeCoord);
    }
    public void writeGraphToFile()
    {
        
    }
}