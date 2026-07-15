using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Shapes;
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

public class GNNGraphCreator
{
    [CommandMethod("GNNGraphCreator")]

    public void CreateGraph()
    {
        Document doc = AcadApp.DocumentManager.MdiActiveDocument;
        Database db = doc.Database;
        Editor ed = doc.Editor;

        using (Transaction tr = db.TransactionManager.StartTransaction())
        {
            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            var ms = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);
            List<ObjectId> allLabels = new();

            //Grabbing the object Id for all labels in modelspace
            foreach (ObjectId id in ms)
            {
                if (tr.GetObject(id, OpenMode.ForRead) is CivilLabel label && label.Dragged)
                {
                    allLabels.Add(id);
                }
            }  

            foreach (ObjectId draggedLabels in allLabels)
            {
                for (int hold = 0; hold < 5; hold++)
                {
                    
                } 
            }        
        }
    }

    public List<ObjectId> GetNearObj(ObjectId center)
    {
        List<ObjectId> hold = new();

        return hold;
    }
}