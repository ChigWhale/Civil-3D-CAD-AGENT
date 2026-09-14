using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.GraphicsInterface;

using AcadEntity = Autodesk.AutoCAD.DatabaseServices.Entity;

namespace CADAgent
{
    public static partial class CadCommands
    {
        private static void RegisterAnnotate()
        {
            Add("draw_text",            DrawText);
            Add("draw_mtext",           DrawMText);
            Add("edit_text",            EditText);
            Add("create_hatch",         CreateHatch, appThread: true);   // may trace a boundary via Editor
            Add("edit_hatch",           EditHatch);
            Add("dimension_linear",     DimensionLinear);
            Add("dimension_angular",    DimensionAngular);
            Add("dimension_radial",     DimensionRadial);
            Add("dimension_arc_length", DimensionArcLength);
            Add("dimension_ordinate",   DimensionOrdinate);
            Add("dimension_continue",   DimensionContinue);
            Add("edit_dimension",       EditDimension);
            Add("draw_leader",          DrawLeader);
            Add("draw_tolerance",       DrawTolerance);
            Add("create_table",         CreateTable);
            Add("set_table_cell",       SetTableCell);
            Add("list_styles",          ListStyles);
            Add("create_text_style",    CreateTextStyle);
            Add("create_dim_style",     CreateDimStyle);
        }

        // ── style lookups ─────────────────────────────────────────────────────────

        private static ObjectId TextStyleId(Transaction tr, Database db, string name)
        {
            if (string.IsNullOrWhiteSpace(name)) return db.Textstyle;
            var tst = (TextStyleTable)tr.GetObject(db.TextStyleTableId, OpenMode.ForRead);
            if (!tst.Has(name)) throw new ArgumentException($"text style '{name}' does not exist (see list_styles)");
            return tst[name];
        }

        private static ObjectId DimStyleId(Transaction tr, Database db, string name)
        {
            if (string.IsNullOrWhiteSpace(name)) return db.Dimstyle;
            var dst = (DimStyleTable)tr.GetObject(db.DimStyleTableId, OpenMode.ForRead);
            if (!dst.Has(name)) throw new ArgumentException($"dimension style '{name}' does not exist (see list_styles)");
            return dst[name];
        }

        private static AttachmentPoint Attachment(string s, AttachmentPoint fallback)
        {
            if (string.IsNullOrWhiteSpace(s)) return fallback;
            string key = s.Trim().ToLowerInvariant().Replace("_", "").Replace("-", "").Replace(" ", "");
            return key switch
            {
                "left" or "baseleft" or "bottomleft" => AttachmentPoint.BaseLeft,
                "center" or "basecenter" or "bottomcenter" => AttachmentPoint.BaseCenter,
                "right" or "baseright" or "bottomright" => AttachmentPoint.BaseRight,
                "middle" or "middlecenter" or "mc" => AttachmentPoint.MiddleCenter,
                "middleleft" or "ml" => AttachmentPoint.MiddleLeft,
                "middleright" or "mr" => AttachmentPoint.MiddleRight,
                "topleft" or "tl" => AttachmentPoint.TopLeft,
                "topcenter" or "tc" => AttachmentPoint.TopCenter,
                "topright" or "tr" => AttachmentPoint.TopRight,
                "bl" => AttachmentPoint.BottomLeft, "bc" => AttachmentPoint.BottomCenter, "br" => AttachmentPoint.BottomRight,
                "aligned" => AttachmentPoint.BaseAlign, "fit" => AttachmentPoint.BaseFit, "basemid" or "mid" => AttachmentPoint.BaseMid,
                _ => throw new ArgumentException($"unknown justification '{s}'"),
            };
        }

        // ── text ──────────────────────────────────────────────────────────────────

        private static string DrawText(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string text = S(req["text"]) ?? throw new ArgumentException("text is required");
            Point3d pos = Flat(P3(req["position"]));
            var txt = new DBText();
            txt.SetDatabaseDefaults(db);
            txt.TextString = text;
            txt.Height = D(req["height"], db.Textsize > 0 ? db.Textsize : 2.5);
            txt.Rotation = Rad(req["rotation_deg"]);
            txt.TextStyleId = TextStyleId(tr, db, S(req["style"]));
            if (req["width_factor"] != null) txt.WidthFactor = D(req["width_factor"]);
            if (req["oblique_deg"] != null) txt.Oblique = Rad(req["oblique_deg"]);
            var just = Attachment(S(req["justification"]), AttachmentPoint.BaseLeft);
            if (just == AttachmentPoint.BaseLeft) txt.Position = pos;
            else { txt.Justify = just; txt.AlignmentPoint = pos; }
            ObjectId id = Append(tr, TargetSpace(tr, db, req), txt, S(req["layer"], "0"));
            if (req["color"] != null) txt.Color = ColorFrom(req["color"]);
            txt.AdjustAlignment(db);
            return Created("Text created.", new[] { id }, new JObject { ["bounds"] = BoundsJson(txt) });
        });

        private static string DrawMText(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string text = S(req["text"]) ?? throw new ArgumentException("text is required");
            var mt = new MText();
            mt.SetDatabaseDefaults(db);
            mt.Contents = text;
            mt.Location = Flat(P3(req["position"]));
            mt.TextHeight = D(req["height"], db.Textsize > 0 ? db.Textsize : 2.5);
            mt.Width = D(req["width"], 0);
            mt.Rotation = Rad(req["rotation_deg"]);
            mt.TextStyleId = TextStyleId(tr, db, S(req["style"]));
            mt.Attachment = Attachment(S(req["attachment"]), AttachmentPoint.TopLeft);
            if (req["line_spacing_factor"] != null) mt.LineSpacingFactor = D(req["line_spacing_factor"]);
            if (req["background_mask"] != null) mt.BackgroundFill = B(req["background_mask"]);
            ObjectId id = Append(tr, TargetSpace(tr, db, req), mt, S(req["layer"], "0"));
            if (req["color"] != null) mt.Color = ColorFrom(req["color"]);
            return Created("MText created.", new[] { id }, new JObject { ["bounds"] = BoundsJson(mt) });
        });

        private static string EditText(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var id = Resolve(db, S(req["handle"]));
            var e = Open<AcadEntity>(tr, id, OpenMode.ForWrite);
            switch (e)
            {
                case DBText t:
                    if (req["text"] != null) t.TextString = S(req["text"]);
                    if (req["height"] != null) t.Height = D(req["height"]);
                    if (req["rotation_deg"] != null) t.Rotation = Rad(req["rotation_deg"]);
                    if (req["style"] != null) t.TextStyleId = TextStyleId(tr, db, S(req["style"]));
                    if (req["width_factor"] != null) t.WidthFactor = D(req["width_factor"]);
                    if (req["justification"] != null)
                    {
                        var j = Attachment(S(req["justification"]), AttachmentPoint.BaseLeft);
                        Point3d anchor = t.Justify == AttachmentPoint.BaseLeft ? t.Position : t.AlignmentPoint;
                        if (j == AttachmentPoint.BaseLeft) { t.Justify = j; t.Position = anchor; }
                        else { t.Justify = j; t.AlignmentPoint = anchor; }
                    }
                    if (req["position"] != null)
                    {
                        var p = Flat(P3(req["position"]));
                        if (t.Justify == AttachmentPoint.BaseLeft) t.Position = p; else t.AlignmentPoint = p;
                    }
                    t.AdjustAlignment(db);
                    break;
                case MText m:
                    if (req["text"] != null) m.Contents = S(req["text"]);
                    if (req["height"] != null) m.TextHeight = D(req["height"]);
                    if (req["rotation_deg"] != null) m.Rotation = Rad(req["rotation_deg"]);
                    if (req["width"] != null) m.Width = D(req["width"]);
                    if (req["style"] != null) m.TextStyleId = TextStyleId(tr, db, S(req["style"]));
                    if (req["attachment"] != null) m.Attachment = Attachment(S(req["attachment"]), m.Attachment);
                    if (req["position"] != null) m.Location = Flat(P3(req["position"]));
                    break;
                case MLeader ml when ml.ContentType == ContentType.MTextContent:
                {
                    var m2 = ml.MText;
                    if (req["text"] != null) m2.Contents = S(req["text"]);
                    if (req["height"] != null) m2.TextHeight = D(req["height"]);
                    ml.MText = m2;
                    break;
                }
                case Dimension d:
                    if (req["text"] != null) d.DimensionText = S(req["text"]);
                    if (req["height"] != null) d.Dimtxt = D(req["height"]);
                    break;
                default:
                    throw new ArgumentException($"{id.Handle} is a {e.GetType().Name}, not text");
            }
            return Ok("Text updated.", new JObject { ["modified"] = new JArray(id.Handle.ToString()) });
        });

        // ── hatch ─────────────────────────────────────────────────────────────────

        private static string CreateHatch(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string pattern = S(req["pattern"], "SOLID").ToUpperInvariant();
            bool solid = pattern == "SOLID";
            bool associative = B(req["associative"], true);
            var boundaryIds = new List<ObjectId>();
            var temp = new List<ObjectId>();

            if (req["handles"] != null) boundaryIds.AddRange(ResolveAll(db, req["handles"]));
            if (req["point"] != null)
            {
                var seed = Flat(P3(req["point"]));
                DBObjectCollection found;
                try { found = doc.Editor.TraceBoundary(seed, false); }
                catch (Autodesk.AutoCAD.Runtime.Exception ex) { throw new ArgumentException($"no closed boundary encloses {Arr(seed)} ({ex.ErrorStatus})"); }
                if (found.Count == 0) throw new ArgumentException($"no closed boundary encloses {Arr(seed)}");
                var ms0 = ModelSpace(tr, db);
                foreach (DBObject o in found)
                {
                    ObjectId bid = Append(tr, ms0, (AcadEntity)o, S(req["layer"], "0"));
                    boundaryIds.Add(bid);
                    if (!B(req["keep_boundary"])) temp.Add(bid);
                }
            }
            if (boundaryIds.Count == 0) throw new ArgumentException("give handles of closed boundaries or a point inside the area");
            if (temp.Count > 0) associative = false;   // the traced boundary is removed afterwards

            var hatch = new Hatch();
            hatch.SetDatabaseDefaults(db);
            ObjectId id = Append(tr, ModelSpace(tr, db), hatch, S(req["layer"], "0"));
            hatch.SetHatchPattern(solid ? HatchPatternType.PreDefined : ParsePatternType(S(req["pattern_type"], "predefined")), pattern);
            if (!solid)
            {
                hatch.PatternScale = D(req["scale"], 1);
                hatch.PatternAngle = Rad(req["angle_deg"]);
                hatch.SetHatchPattern(hatch.PatternType, pattern);   // re-apply so scale/angle take effect
            }
            hatch.HatchStyle = S(req["island_style"], "normal").ToLowerInvariant() switch
            {
                "outer" => HatchStyle.Outer, "ignore" => HatchStyle.Ignore, _ => HatchStyle.Normal,
            };
            hatch.Associative = associative;
            if (req["color"] != null) hatch.Color = ColorFrom(req["color"]);
            if (req["background_color"] != null) hatch.BackgroundColor = ColorFrom(req["background_color"]);

            foreach (var bid in boundaryIds)
            {
                try { hatch.AppendLoop(HatchLoopTypes.Default, new ObjectIdCollection(new[] { bid })); }
                catch (Autodesk.AutoCAD.Runtime.Exception ex) { throw new ArgumentException($"{bid.Handle} is not a valid closed boundary ({ex.ErrorStatus})"); }
            }
            hatch.EvaluateHatch(true);
            double area = 0; try { area = hatch.Area; } catch { }

            foreach (var tid in temp) Open<AcadEntity>(tr, tid, OpenMode.ForWrite).Erase();
            return Created("Hatch created.", new[] { id }, new JObject { ["area"] = R(area), ["loops"] = hatch.NumberOfLoops, ["boundaries"] = Handles(boundaryIds.Except(temp)) });
        });

        private static HatchPatternType ParsePatternType(string s) => s.ToLowerInvariant() switch
        {
            "user" or "userdefined" or "user_defined" => HatchPatternType.UserDefined,
            "custom" => HatchPatternType.CustomDefined,
            _ => HatchPatternType.PreDefined,
        };

        private static string EditHatch(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var id = Resolve(db, S(req["handle"]));
            var h = Open<Hatch>(tr, id, OpenMode.ForWrite);
            string pattern = S(req["pattern"])?.ToUpperInvariant() ?? h.PatternName;
            if (req["pattern"] != null || req["scale"] != null || req["angle_deg"] != null)
            {
                if (pattern != "SOLID")
                {
                    if (req["scale"] != null) h.PatternScale = D(req["scale"]);
                    if (req["angle_deg"] != null) h.PatternAngle = Rad(req["angle_deg"]);
                }
                h.SetHatchPattern(pattern == "SOLID" ? HatchPatternType.PreDefined : h.PatternType, pattern);
            }
            if (req["color"] != null) h.Color = ColorFrom(req["color"]);
            if (req["background_color"] != null) h.BackgroundColor = ColorFrom(req["background_color"]);
            if (req["island_style"] != null)
                h.HatchStyle = S(req["island_style"]).ToLowerInvariant() switch { "outer" => HatchStyle.Outer, "ignore" => HatchStyle.Ignore, _ => HatchStyle.Normal };
            h.EvaluateHatch(true);
            return Ok("Hatch updated.", new JObject { ["modified"] = new JArray(id.Handle.ToString()) });
        });

        // ── dimensions ────────────────────────────────────────────────────────────

        private static JObject DimInfo(Dimension d) => new JObject
        {
            ["measurement"] = R(d.Measurement),
            ["text"] = d.DimensionText,
            ["style"] = d.DimensionStyleName,
        };

        private static string DimensionLinear(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            Point3d p1 = Flat(P3(req["point1"])), p2 = Flat(P3(req["point2"]));
            Point3d dl = Flat(P3(req["dimension_line_point"]));
            string text = S(req["text_override"], "");
            ObjectId style = DimStyleId(tr, db, S(req["style"]));
            Dimension dim;
            string kind = S(req["kind"], "aligned").ToLowerInvariant();
            if (kind == "aligned" && req["rotation_deg"] == null)
                dim = new AlignedDimension(p1, p2, dl, text, style);
            else
            {
                double rot = kind switch { "horizontal" => 0, "vertical" => Math.PI / 2, _ => Rad(req["rotation_deg"]) };
                dim = new RotatedDimension(rot, p1, p2, dl, text, style);
            }
            ObjectId id = Append(tr, ModelSpace(tr, db), dim, S(req["layer"], "0"));
            if (req["scale"] != null) dim.Dimscale = D(req["scale"]);
            return Created("Linear dimension created.", new[] { id }, DimInfo(dim));
        });

        private static string DimensionAngular(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string text = S(req["text_override"], "");
            ObjectId style = DimStyleId(tr, db, S(req["style"]));
            Point3d arcPt = Flat(P3(req["arc_point"]));
            Dimension dim;
            string method = S(req["method"], "").ToLowerInvariant();
            if (method == "lines" || (req["line_a"] != null && req["line_b"] != null))
            {
                var a = Open<Line>(tr, Resolve(db, S(req["line_a"]))); var b = Open<Line>(tr, Resolve(db, S(req["line_b"])));
                dim = new LineAngularDimension2(Flat(a.StartPoint), Flat(a.EndPoint), Flat(b.StartPoint), Flat(b.EndPoint), arcPt, text, style);
            }
            else if (method == "arc" || req["arc_handle"] != null)
            {
                var arc = Open<Arc>(tr, Resolve(db, S(req["arc_handle"])));
                dim = new Point3AngularDimension(Flat(arc.Center), Flat(arc.StartPoint), Flat(arc.EndPoint), arcPt, text, style);
            }
            else
            {
                Point3d c = Flat(P3(req["center"])), p1 = Flat(P3(req["point1"])), p2 = Flat(P3(req["point2"]));
                dim = new Point3AngularDimension(c, p1, p2, arcPt, text, style);
            }
            ObjectId id = Append(tr, ModelSpace(tr, db), dim, S(req["layer"], "0"));
            return Created("Angular dimension created.", new[] { id }, new JObject { ["measurement_deg"] = R(Deg(dim.Measurement)), ["text"] = dim.DimensionText });
        });

        private static string DimensionRadial(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var curve = OpenCurve(tr, Resolve(db, S(req["handle"])));
            Point3d center; double radius;
            switch (curve)
            {
                case Circle c: center = c.Center; radius = c.Radius; break;
                case Arc a: center = a.Center; radius = a.Radius; break;
                default: throw new ArgumentException("radial dimensions need a circle or an arc");
            }
            center = Flat(center);
            string text = S(req["text_override"], "");
            ObjectId style = DimStyleId(tr, db, S(req["style"]));
            double leader = D(req["leader_length"], radius * 0.5);
            Point3d chord;
            if (req["chord_point"] != null) chord = curve.GetClosestPointTo(Flat(P3(req["chord_point"])), true);
            else chord = Polar(center, Rad(req["angle_deg"], 45), radius);
            chord = Flat(chord);

            Dimension dim;
            bool diameter = S(req["kind"], "radius").ToLowerInvariant().StartsWith("dia");
            if (diameter)
            {
                Point3d far = center + (center - chord);
                dim = new DiametricDimension(chord, far, leader, text, style);
            }
            else dim = new RadialDimension(center, chord, leader, text, style);
            ObjectId id = Append(tr, ModelSpace(tr, db), dim, S(req["layer"], "0"));
            return Created($"{(diameter ? "Diameter" : "Radius")} dimension created.", new[] { id }, DimInfo(dim));
        });

        private static string DimensionArcLength(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var arc = Open<Arc>(tr, Resolve(db, S(req["handle"])));
            string text = S(req["text_override"], "");
            ObjectId style = DimStyleId(tr, db, S(req["style"]));
            Point3d arcPt;
            if (req["arc_point"] != null) arcPt = Flat(P3(req["arc_point"]));
            else
            {
                double mid = (arc.StartAngle + Norm(arc.EndAngle - arc.StartAngle) / 2);
                arcPt = Polar(Flat(arc.Center), mid, arc.Radius + D(req["offset"], arc.Radius * 0.25));
            }
            var dim = new ArcDimension(Flat(arc.Center), Flat(arc.StartPoint), Flat(arc.EndPoint), arcPt, text, style);
            ObjectId id = Append(tr, ModelSpace(tr, db), dim, S(req["layer"], "0"));
            return Created("Arc length dimension created.", new[] { id }, DimInfo(dim));
        });

        private static string DimensionOrdinate(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            Point3d p = Flat(P3(req["point"])), leader = Flat(P3(req["leader_end_point"]));
            bool useX = S(req["axis"], "x").ToLowerInvariant() == "x";
            var dim = new OrdinateDimension(useX, p, leader, S(req["text_override"], ""), DimStyleId(tr, db, S(req["style"])));
            ObjectId id = Append(tr, ModelSpace(tr, db), dim, S(req["layer"], "0"));
            return Created("Ordinate dimension created.", new[] { id }, DimInfo(dim));
        });

        private static string DimensionContinue(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var baseDim = Open<Dimension>(tr, Resolve(db, S(req["base_dimension"])));
            var nextPts = req["points"] != null ? Points(req["points"]) : new List<Point3d> { P3(req["next_point"]) };
            if (nextPts.Count == 0) throw new ArgumentException("give next_point or points");
            bool baseline = S(req["mode"], "continue").ToLowerInvariant() == "baseline";
            string text = S(req["text_override"], "");
            var ms = ModelSpace(tr, db);
            var created = new List<ObjectId>();

            double dli = D(req["spacing"], 0);
            if (dli <= 0) { try { dli = baseDim.Dimdli; } catch { dli = 0.38; } if (dli <= 0) dli = 0.38; }

            Point3d anchor, last, dimLine;
            double rotation; bool rotated;
            switch (baseDim)
            {
                case RotatedDimension rd:
                    anchor = Flat(rd.XLine1Point); last = Flat(rd.XLine2Point); dimLine = Flat(rd.DimLinePoint); rotation = rd.Rotation; rotated = true; break;
                case AlignedDimension ad:
                    anchor = Flat(ad.XLine1Point); last = Flat(ad.XLine2Point); dimLine = Flat(ad.DimLinePoint); rotation = Angle(anchor, last); rotated = false; break;
                default:
                    throw new ArgumentException("continue/baseline works from a linear (rotated or aligned) dimension");
            }
            var along = new Vector3d(Math.Cos(rotation), Math.Sin(rotation), 0);
            var normal = along.RotateBy(Math.PI / 2, Vector3d.ZAxis);
            double offsetSign = Math.Sign((dimLine - anchor).DotProduct(normal));
            if (offsetSign == 0) offsetSign = 1;

            int n = 0;
            foreach (var raw in nextPts)
            {
                var next = Flat(raw);
                n++;
                Dimension d;
                if (baseline)
                {
                    Point3d dl = dimLine + normal * (offsetSign * dli * n);
                    d = rotated ? new RotatedDimension(rotation, anchor, next, dl, text, baseDim.DimensionStyle)
                                : (Dimension)new AlignedDimension(anchor, next, dl, text, baseDim.DimensionStyle);
                }
                else
                {
                    d = rotated ? new RotatedDimension(rotation, last, next, dimLine, text, baseDim.DimensionStyle)
                                : (Dimension)new AlignedDimension(last, next, dimLine, text, baseDim.DimensionStyle);
                    last = next;
                }
                d.SetPropertiesFrom(baseDim);
                created.Add(Append(tr, ms, d));
            }
            return Created($"{created.Count} {(baseline ? "baseline" : "continued")} dimension(s) created.", created);
        });

        private static string EditDimension(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var id = Resolve(db, S(req["handle"]));
            var d = Open<Dimension>(tr, id, OpenMode.ForWrite);
            if (req["text_override"] != null) d.DimensionText = S(req["text_override"]);
            if (req["style"] != null) d.DimensionStyle = DimStyleId(tr, db, S(req["style"]));
            if (req["text_position"] != null) { d.TextPosition = Flat(P3(req["text_position"])); d.UsingDefaultTextPosition = false; }
            if (B(req["reset_text_position"])) d.UsingDefaultTextPosition = true;
            if (req["text_rotation_deg"] != null) d.TextRotation = Rad(req["text_rotation_deg"]);
            if (req["scale"] != null) d.Dimscale = D(req["scale"]);
            if (req["text_height"] != null) d.Dimtxt = D(req["text_height"]);
            if (req["arrow_size"] != null) d.Dimasz = D(req["arrow_size"]);
            if (req["decimal_places"] != null) d.Dimdec = I(req["decimal_places"]);
            if (req["dimension_line_point"] != null)
            {
                var p = Flat(P3(req["dimension_line_point"]));
                switch (d)
                {
                    case RotatedDimension rd: rd.DimLinePoint = p; break;
                    case AlignedDimension ad: ad.DimLinePoint = p; break;
                    case Point3AngularDimension p3: p3.ArcPoint = p; break;
                    case LineAngularDimension2 la: la.ArcPoint = p; break;
                    case ArcDimension arcd: arcd.ArcPoint = p; break;
                    case RadialDimension radd: radd.ChordPoint = p; break;
                    default: throw new ArgumentException("dimension_line_point is not applicable to this dimension type");
                }
            }
            return Ok("Dimension updated.", new JObject { ["modified"] = new JArray(id.Handle.ToString()), ["measurement"] = R(d.Measurement) });
        });

        // ── leaders / tolerance ───────────────────────────────────────────────────

        private static string DrawLeader(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var pts = Points(req["points"]).Select(Flat).ToList();
            if (pts.Count < 2) throw new ArgumentException("need at least 2 points (arrow tip first)");
            string text = S(req["text"], "");
            var ml = new MLeader();
            ml.SetDatabaseDefaults(db);
            if (req["style"] != null)
            {
                var dict = (DBDictionary)tr.GetObject(db.MLeaderStyleDictionaryId, OpenMode.ForRead);
                string st = S(req["style"]);
                if (!dict.Contains(st)) throw new ArgumentException($"multileader style '{st}' does not exist (see list_styles)");
                ml.MLeaderStyle = dict.GetAt(st);
            }
            int line = ml.AddLeaderLine(pts[0]);
            for (int i = 1; i < pts.Count; i++) ml.AddLastVertex(line, pts[i]);
            ml.EnableLanding = B(req["landing"], true);
            ml.EnableDogleg = B(req["landing"], true);
            if (req["landing_length"] != null) ml.DoglegLength = D(req["landing_length"]);
            if (req["arrow_size"] != null) ml.ArrowSize = D(req["arrow_size"]);

            if (text.Length > 0)
            {
                ml.ContentType = ContentType.MTextContent;
                var mt = new MText();
                mt.SetDatabaseDefaults(db);
                mt.Contents = text;
                if (req["height"] != null) mt.TextHeight = D(req["height"]);
                if (req["text_style"] != null) mt.TextStyleId = TextStyleId(tr, db, S(req["text_style"]));
                Point3d last = pts[pts.Count - 1];
                Vector3d dir = pts.Count >= 2 ? (last - pts[pts.Count - 2]) : Vector3d.XAxis;
                double landing = req["landing_length"] != null ? D(req["landing_length"]) : mt.TextHeight * 1.5;
                mt.Location = req["text_position"] != null ? Flat(P3(req["text_position"])) : last + new Vector3d(dir.X >= 0 ? landing : -landing, 0, 0);
                ml.MText = mt;
                ml.TextAlignmentType = TextAlignmentType.LeftAlignment;
            }
            else ml.ContentType = ContentType.NoneContent;

            ObjectId id = Append(tr, TargetSpace(tr, db, req), ml, S(req["layer"], "0"));
            return Created("Leader created.", new[] { id });
        });

        private static string DrawTolerance(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string text = S(req["text"]) ?? throw new ArgumentException("text is required, e.g. \"{\\\\Fgdt;j}%%v0.01%%vA\"");
            var fcf = new FeatureControlFrame();
            fcf.SetDatabaseDefaults(db);
            fcf.Text = text;
            fcf.Location = Flat(P3(req["position"]));
            fcf.DimensionStyle = DimStyleId(tr, db, S(req["style"]));
            ObjectId id = Append(tr, ModelSpace(tr, db), fcf, S(req["layer"], "0"));
            if (req["rotation_deg"] != null) fcf.TransformBy(Matrix3d.Rotation(Rad(req["rotation_deg"]), Vector3d.ZAxis, fcf.Location));
            return Created("Feature control frame created.", id);
        });

        // ── tables ────────────────────────────────────────────────────────────────

        private static string CreateTable(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var cells = req["cells"] as JArray;
            int rows = I(req["rows"], cells?.Count ?? 0);
            int cols = I(req["columns"], cells != null && cells.Count > 0 ? (cells[0] as JArray)?.Count ?? 0 : 0);
            if (rows < 1 || cols < 1) throw new ArgumentException("give rows and columns (or a cells matrix)");

            var tb = new Table();
            tb.SetDatabaseDefaults(db);
            tb.TableStyle = db.Tablestyle;
            if (req["style"] != null)
            {
                var dict = (DBDictionary)tr.GetObject(db.TableStyleDictionaryId, OpenMode.ForRead);
                string st = S(req["style"]);
                if (!dict.Contains(st)) throw new ArgumentException($"table style '{st}' does not exist");
                tb.TableStyle = dict.GetAt(st);
            }
            tb.Position = Flat(P3(req["position"]));
            tb.SetSize(rows, cols);
            tb.SetRowHeight(D(req["row_height"], 2.5 * 1.5));
            tb.SetColumnWidth(D(req["column_width"], 25));
            if (req["text_height"] != null)
                for (int r = 0; r < rows; r++) for (int c = 0; c < cols; c++) tb.Cells[r, c].TextHeight = D(req["text_height"]);

            if (cells != null)
                for (int r = 0; r < Math.Min(rows, cells.Count); r++)
                {
                    var row = cells[r] as JArray;
                    if (row == null) continue;
                    for (int c = 0; c < Math.Min(cols, row.Count); c++)
                        tb.Cells[r, c].TextString = row[c]?.ToString() ?? "";
                }
            if (req["title"] != null)
            {
                tb.MergeCells(CellRange.Create(tb, 0, 0, 0, cols - 1));
                tb.Cells[0, 0].TextString = S(req["title"]);
            }
            tb.GenerateLayout();
            ObjectId id = Append(tr, TargetSpace(tr, db, req), tb, S(req["layer"], "0"));
            return Created("Table created.", new[] { id }, new JObject { ["rows"] = rows, ["columns"] = cols, ["bounds"] = BoundsJson(tb) });
        });

        private static string SetTableCell(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var id = Resolve(db, S(req["handle"]));
            var tb = Open<Table>(tr, id, OpenMode.ForWrite);
            var edits = req["cells"] as JArray ?? new JArray(new JObject { ["row"] = req["row"], ["column"] = req["column"], ["text"] = req["text"] });
            int n = 0;
            foreach (var e in edits)
            {
                int r = I(e["row"], -1), c = I(e["column"], -1);
                if (r < 0 || r >= tb.Rows.Count || c < 0 || c >= tb.Columns.Count) throw new ArgumentException($"cell ({r},{c}) is outside the {tb.Rows.Count}x{tb.Columns.Count} table");
                if (e["text"] != null) tb.Cells[r, c].TextString = S(e["text"]);
                if (e["text_height"] != null) tb.Cells[r, c].TextHeight = D(e["text_height"]);
                if (e["alignment"] != null) tb.Cells[r, c].Alignment = (CellAlignment)Enum.Parse(typeof(CellAlignment), S(e["alignment"]), true);
                n++;
            }
            if (req["insert_rows"] != null) tb.InsertRows(tb.Rows.Count, tb.Rows[tb.Rows.Count - 1].Height, I(req["insert_rows"]));
            if (req["insert_columns"] != null) tb.InsertColumns(tb.Columns.Count, tb.Columns[tb.Columns.Count - 1].Width, I(req["insert_columns"]));
            tb.GenerateLayout();
            return Ok($"Updated {n} cell(s).", new JObject { ["modified"] = new JArray(id.Handle.ToString()), ["rows"] = tb.Rows.Count, ["columns"] = tb.Columns.Count });
        });

        // ── styles ────────────────────────────────────────────────────────────────

        private static string ListStyles(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string kind = S(req["kind"], "all").ToLowerInvariant();
            var o = new JObject();
            if (kind is "all" or "text")
            {
                var arr = new JArray();
                var tst = (TextStyleTable)tr.GetObject(db.TextStyleTableId, OpenMode.ForRead);
                foreach (ObjectId id in tst)
                {
                    var r = (TextStyleTableRecord)tr.GetObject(id, OpenMode.ForRead);
                    if (r.IsShapeFile) continue;
                    arr.Add(new JObject { ["name"] = r.Name, ["font"] = string.IsNullOrEmpty(r.FileName) ? r.Font.TypeFace : r.FileName, ["height"] = R(r.TextSize), ["width_factor"] = R(r.XScale), ["current"] = id == db.Textstyle });
                }
                o["text_styles"] = arr;
            }
            if (kind is "all" or "dimension" or "dim")
            {
                var arr = new JArray();
                var dst = (DimStyleTable)tr.GetObject(db.DimStyleTableId, OpenMode.ForRead);
                foreach (ObjectId id in dst)
                {
                    var r = (DimStyleTableRecord)tr.GetObject(id, OpenMode.ForRead);
                    arr.Add(new JObject { ["name"] = r.Name, ["text_height"] = R(r.Dimtxt), ["arrow_size"] = R(r.Dimasz), ["scale"] = R(r.Dimscale), ["decimal_places"] = r.Dimdec, ["current"] = id == db.Dimstyle });
                }
                o["dimension_styles"] = arr;
            }
            if (kind is "all" or "mleader" or "leader")
            {
                var dict = (DBDictionary)tr.GetObject(db.MLeaderStyleDictionaryId, OpenMode.ForRead);
                o["mleader_styles"] = new JArray(dict.Cast<DBDictionaryEntry>().Select(e => e.Key));
            }
            if (kind is "all" or "table")
            {
                var dict = (DBDictionary)tr.GetObject(db.TableStyleDictionaryId, OpenMode.ForRead);
                o["table_styles"] = new JArray(dict.Cast<DBDictionaryEntry>().Select(e => e.Key));
            }
            if (kind is "all" or "linetype")
            {
                var arr = new JArray();
                var ltt = (LinetypeTable)tr.GetObject(db.LinetypeTableId, OpenMode.ForRead);
                foreach (ObjectId id in ltt)
                {
                    var r = (LinetypeTableRecord)tr.GetObject(id, OpenMode.ForRead);
                    arr.Add(new JObject { ["name"] = r.Name, ["description"] = r.AsciiDescription });
                }
                o["linetypes"] = arr;
            }
            return Ok("Styles listed.", o);
        });

        private static string CreateTextStyle(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string name = S(req["name"]) ?? throw new ArgumentException("name is required");
            var tst = (TextStyleTable)tr.GetObject(db.TextStyleTableId, OpenMode.ForWrite);
            TextStyleTableRecord r;
            bool existed = tst.Has(name);
            if (existed) r = (TextStyleTableRecord)tr.GetObject(tst[name], OpenMode.ForWrite);
            else { r = new TextStyleTableRecord { Name = name }; tst.Add(r); tr.AddNewlyCreatedDBObject(r, true); }

            string font = S(req["font"]);
            if (font != null)
            {
                if (font.EndsWith(".shx", StringComparison.OrdinalIgnoreCase)) r.FileName = font;
                else r.Font = new FontDescriptor(font, B(req["bold"]), B(req["italic"]), 0, 0);
            }
            if (req["height"] != null) r.TextSize = D(req["height"]);
            if (req["width_factor"] != null) r.XScale = D(req["width_factor"]);
            if (req["oblique_deg"] != null) r.ObliquingAngle = Rad(req["oblique_deg"]);
            if (B(req["set_current"])) db.Textstyle = r.ObjectId;
            return Ok($"Text style '{name}' {(existed ? "updated" : "created")}.", new JObject { ["name"] = name });
        });

        private static string CreateDimStyle(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            string name = S(req["name"]) ?? throw new ArgumentException("name is required");
            var dst = (DimStyleTable)tr.GetObject(db.DimStyleTableId, OpenMode.ForWrite);
            DimStyleTableRecord r;
            bool existed = dst.Has(name);
            if (existed) r = (DimStyleTableRecord)tr.GetObject(dst[name], OpenMode.ForWrite);
            else
            {
                r = new DimStyleTableRecord();
                string basedOn = S(req["based_on"]);
                if (basedOn != null)
                {
                    if (!dst.Has(basedOn)) throw new ArgumentException($"based_on style '{basedOn}' does not exist");
                    r.CopyFrom(tr.GetObject(dst[basedOn], OpenMode.ForRead));
                }
                r.Name = name;
                dst.Add(r); tr.AddNewlyCreatedDBObject(r, true);
            }
            if (req["text_height"] != null) r.Dimtxt = D(req["text_height"]);
            if (req["arrow_size"] != null) r.Dimasz = D(req["arrow_size"]);
            if (req["extension_line_offset"] != null) r.Dimexo = D(req["extension_line_offset"]);
            if (req["extension_line_extension"] != null) r.Dimexe = D(req["extension_line_extension"]);
            if (req["scale"] != null) r.Dimscale = D(req["scale"]);
            if (req["decimal_places"] != null) r.Dimdec = I(req["decimal_places"]);
            if (req["linear_factor"] != null) r.Dimlfac = D(req["linear_factor"]);
            if (req["text_style"] != null) r.Dimtxsty = TextStyleId(tr, db, S(req["text_style"]));
            if (req["text_above_line"] != null) r.Dimtad = B(req["text_above_line"]) ? 1 : 0;
            if (req["suffix"] != null) r.Dimpost = S(req["suffix"]);
            if (req["baseline_spacing"] != null) r.Dimdli = D(req["baseline_spacing"]);
            if (req["text_gap"] != null) r.Dimgap = D(req["text_gap"]);
            if (B(req["set_current"])) db.Dimstyle = r.ObjectId;
            return Ok($"Dimension style '{name}' {(existed ? "updated" : "created")}.", new JObject { ["name"] = name });
        });
    }
}
