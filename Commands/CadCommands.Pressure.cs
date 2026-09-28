using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.Civil.ApplicationServices;
using Autodesk.Civil.DatabaseServices;
using Autodesk.Civil.DatabaseServices.Styles;

using SysException = System.Exception;
using AcadEntity   = Autodesk.AutoCAD.DatabaseServices.Entity;

namespace CADAgent
{
    /// <summary>
    /// Pressure networks (water): what parts the drawing can use, and laying a
    /// run of real Civil 3D pressure parts -- pipes along a path, bend fittings
    /// at the vertices, appurtenances (hydrants, valves) at points -- as one
    /// transaction. `lay_pressure_run` with apply=false is the compute stage: it
    /// resolves every part name against the parts list and reports the geometry
    /// it would draw (`proposed`), so the spatial gate can judge it; apply=true
    /// draws exactly that.
    /// </summary>
    public static partial class CadCommands
    {
        private static void RegisterPressure()
        {
            // Part lists and network creation are Civil 3D document work: application thread.
            Add("list_pressure_parts", ListPressureParts, appThread: true);
            Add("lay_pressure_run",    LayPressureRun,    appThread: true);
        }

        // ── Reading the parts lists ───────────────────────────────────────────────

        private static string ListPressureParts(JObject req, Document doc) => InTransaction(doc, (tr, db) =>
        {
            var civil = CivilDocument.GetCivilDocument(db);
            var networks = new JArray();
            foreach (ObjectId nid in civil.GetPressurePipeNetworkIds())
            {
                try
                {
                    var net = (PressurePipeNetwork)tr.GetObject(nid, OpenMode.ForRead);
                    networks.Add(new JObject
                    {
                        ["name"] = net.Name, ["handle"] = net.Handle.ToString(),
                        ["parts_list"] = SafeStr(() => net.PartsListName),
                        ["reference_surface"] = SafeStr(() => net.ReferenceSurfaceName),
                        ["reference_alignment"] = SafeStr(() => net.ReferenceAlignmentName),
                        ["pipes"] = SafeInt(() => net.GetPipeIds().Count),
                        ["fittings"] = SafeInt(() => net.GetFittingIds().Count),
                        ["appurtenances"] = SafeInt(() => net.GetAppurtenanceIds().Count),
                    });
                }
                catch { }
            }

            string only = S(req["parts_list"]);
            bool full = B(req["full"], false);
            var lists = new JArray();
            foreach (ObjectId lid in PressurePartList.GetAvailablePartLists(db))
            {
                try
                {
                    var list = (PressurePartList)tr.GetObject(lid, OpenMode.ForRead);
                    if (only != null && !list.Name.Equals(only, StringComparison.OrdinalIgnoreCase)) continue;
                    var j = new JObject { ["name"] = list.Name, ["handle"] = list.Handle.ToString(), ["catalog"] = SafeStr(() => list.CatalogGuid) };
                    foreach (PressurePartDomainType domain in new[] { PressurePartDomainType.Pipe, PressurePartDomainType.Fitting, PressurePartDomainType.Appurtenance })
                    {
                        var arr = new JArray();
                        List<PressurePartSize> parts;
                        try { parts = list.GetParts(domain); } catch { continue; }
                        foreach (var p in parts)
                        {
                            var pj = PartJson(p);
                            if (!full) { pj.Remove("guid"); pj.Remove("family_guid"); }
                            arr.Add(pj);
                        }
                        j[domain.ToString().ToLowerInvariant() + "s"] = arr;
                    }
                    lists.Add(j);
                }
                catch { }
            }
            return Ok($"{networks.Count} pressure network(s), {lists.Count} parts list(s).", new JObject
            {
                ["networks"] = networks, ["parts_lists"] = lists,
                ["part_types"] = new JArray(Enum.GetNames(typeof(PressurePartType))),
                ["note"] = "pipe_size / size match a part's description (substring) or its nominal diameter; " +
                           "appurtenance part is a type name (Hydrant, Valve ...) or a description substring.",
            });
        });

        private static JObject PartJson(PressurePartSize p)
        {
            var j = new JObject
            {
                ["description"] = SafeStr(() => p.Description),
                ["type"] = SafeStr(() => p.PartType.ToString()),
                ["family"] = SafeStr(() => p.GetProperty(PressurePartContextType.PartFamilyName)?.ToString()),
                ["nominal_diameter"] = SafeNum(() => p.GetProperty(PressurePartContextType.DiameterNominal)),
                ["material"] = SafeStr(() => p.GetProperty(PressurePartContextType.IdMaterial)?.ToString()),
                ["guid"] = SafeStr(() => p.PartSizeGuid),
                ["family_guid"] = SafeStr(() => p.FamilyGuid),
            };
            if (p.Domain == PressurePartDomainType.Fitting)
                j["angle"] = SafeNum(() => p.GetProperty(PressurePartContextType.FittingAngle));
            return j;
        }

        // ── Laying a run ──────────────────────────────────────────────────────────

        private sealed class RunPlan
        {
            public PressurePartList List;
            public ObjectId ListId;
            public PressurePipeNetwork Network;         // null when it is to be created
            public string NetworkName;
            public PressurePartSize Pipe;
            public List<Point3d> Path = new List<Point3d>();
            public List<(int vertex, double deflection, PressurePartSize part)> Fittings = new List<(int, double, PressurePartSize)>();
            public List<(Point3d point, PressurePartSize part, string asked)> Appurtenances = new List<(Point3d, PressurePartSize, string)>();
            public List<string> Warnings = new List<string>();
            public List<string> Errors = new List<string>();
        }

        private static string LayPressureRun(JObject req, Document doc)
        {
            bool apply = B(req["apply"], false);
            string stage = apply ? "draw" : "compute";
            Database db = doc.Database;
            using (doc.LockDocument())
            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                RunPlan plan;
                try { plan = PlanRun(tr, db, req); }
                catch (SysException ex)
                {
                    tr.Abort();
                    return new JObject { ["ok"] = false, ["stage"] = stage, ["message"] = ex.Message, ["errors"] = new JArray(ex.Message) }.ToString();
                }
                var report = RunReport(stage, plan);
                if (plan.Errors.Count > 0 || !apply)
                {
                    tr.Abort();
                    return report.ToString();
                }
                try
                {
                    var created = DrawRun(tr, db, plan, S(req["layer"]));
                    tr.Commit();
                    foreach (var p in created.Properties()) report[p.Name] = p.Value;
                    report["message"] = $"Laid {plan.Path.Count - 1} pipe(s), {plan.Fittings.Count} fitting(s), {plan.Appurtenances.Count} appurtenance(s) in '{plan.NetworkName}'.";
                    return report.ToString();
                }
                catch (SysException ex)
                {
                    tr.Abort();
                    report["ok"] = false;
                    report["message"] = $"drawing the run failed, nothing was kept: {ex.Message}";
                    report["errors"] = new JArray(report["message"]);
                    return report.ToString();
                }
            }
        }

        private static RunPlan PlanRun(Transaction tr, Database db, JObject req)
        {
            var civil = CivilDocument.GetCivilDocument(db);
            var plan = new RunPlan();
            var path = Points(req["path"]).Select(Flat).ToList();
            for (int i = path.Count - 1; i > 0; i--)
                if (path[i].DistanceTo(path[i - 1]) < 1e-6) path.RemoveAt(i);
            if (path.Count < 2) throw new ArgumentException("path needs at least two distinct points");
            plan.Path = path;

            // the network: existing by name, else created with the parts list asked for
            string netName = S(req["network"], "");
            foreach (ObjectId nid in civil.GetPressurePipeNetworkIds())
            {
                var net = tr.GetObject(nid, OpenMode.ForRead) as PressurePipeNetwork;
                if (net != null && netName != "" && net.Name.Equals(netName, StringComparison.OrdinalIgnoreCase)) { plan.Network = net; break; }
            }
            if (plan.Network == null && netName == "")
            {
                var ids = civil.GetPressurePipeNetworkIds();
                if (ids.Count == 1) plan.Network = (PressurePipeNetwork)tr.GetObject(ids[0], OpenMode.ForRead);
                else if (ids.Count > 1) throw new ArgumentException("network is required; the drawing has several pressure networks (see list_pressure_parts)");
                else netName = "WATER";
            }
            plan.NetworkName = plan.Network != null ? plan.Network.Name : netName;

            // the parts list: the network's own, else the one named, else the only one there is
            string listName = S(req["parts_list"], "");
            var available = PressurePartList.GetAvailablePartLists(db);
            if (plan.Network != null && listName == "")
            {
                try { plan.ListId = plan.Network.PartsListId; } catch { }
            }
            if (plan.ListId.IsNull)
            {
                var names = new List<string>();
                foreach (ObjectId lid in available)
                {
                    var l = (PressurePartList)tr.GetObject(lid, OpenMode.ForRead);
                    names.Add(l.Name);
                    if (listName == "" ? available.Count == 1 : l.Name.Equals(listName, StringComparison.OrdinalIgnoreCase)) plan.ListId = lid;
                }
                if (plan.ListId.IsNull)
                    throw new ArgumentException(listName == ""
                        ? $"parts_list is required; the drawing has: {string.Join(", ", names)}"
                        : $"no pressure parts list named '{listName}'; the drawing has: {string.Join(", ", names)}");
            }
            plan.List = (PressurePartList)tr.GetObject(plan.ListId, OpenMode.ForRead);

            // the pipe size
            string pipeSize = S(req["pipe_size"], "");
            var pipes = plan.List.GetParts(PressurePartDomainType.Pipe);
            plan.Pipe = PickPart(pipes, pipeSize, null, out string why);
            if (plan.Pipe == null)
                throw new ArgumentException(pipeSize == ""
                    ? $"pipe_size is required; the parts list '{plan.List.Name}' has: {Describe(pipes)}"
                    : $"no pipe matching '{pipeSize}' in parts list '{plan.List.Name}' ({why}); it has: {Describe(pipes)}");

            // bend fittings
            string fittings = S(req["fittings"], "auto").ToLowerInvariant();
            double bendTol = D(req["bend_tolerance_deg"], 2.0);
            if (fittings == "auto")
            {
                var elbows = plan.List.GetParts(PressurePartType.Elbow);
                for (int i = 1; i < path.Count - 1; i++)
                {
                    double a1 = Angle(path[i - 1], path[i]), a2 = Angle(path[i], path[i + 1]);
                    double deflection = Math.Abs(Deg(Norm(a2 - a1 + Math.PI) - Math.PI));
                    if (deflection < bendTol) continue;
                    var elbow = NearestAngle(elbows, deflection);
                    if (elbow == null) plan.Warnings.Add($"bend of {deflection:0.#} deg at vertex {i}: the parts list has no elbow; pipes are left unconnected there");
                    plan.Fittings.Add((i, deflection, elbow));
                }
            }

            // appurtenances
            foreach (var a in req["appurtenances"] as JArray ?? new JArray())
            {
                var pt = Flat(P3(a["point"]));
                string part = S(a["part"], "Hydrant");
                string size = S(a["size"], "");
                List<PressurePartSize> pool;
                if (Enum.TryParse(part, true, out PressurePartType ptype)) pool = plan.List.GetParts(ptype);
                else pool = plan.List.GetParts(PressurePartDomainType.Appurtenance).Where(p => (SafeStr(() => p.Description) ?? "").IndexOf(part, StringComparison.OrdinalIgnoreCase) >= 0).ToList();
                var chosen = PickPart(pool, size, plan.Pipe, out string w2);
                if (chosen == null)
                {
                    plan.Errors.Add($"no {part} {(size == "" ? "" : $"'{size}' ")}in parts list '{plan.List.Name}' ({w2}); it has: {Describe(pool)}");
                    continue;
                }
                plan.Appurtenances.Add((pt, chosen, part));
            }
            return plan;
        }

        /// <summary>The part whose description contains `want`, or whose nominal
        /// diameter equals it as a number; with no `want`, the one matching the
        /// pipe's diameter, else the only one, else null.</summary>
        private static PressurePartSize PickPart(List<PressurePartSize> pool, string want, PressurePartSize like, out string why)
        {
            why = "";
            if (pool == null || pool.Count == 0) { why = "nothing of that kind in the list"; return null; }
            want = (want ?? "").Trim();
            if (want != "")
            {
                var byText = pool.Where(p => (SafeStr(() => p.Description) ?? "").IndexOf(want, StringComparison.OrdinalIgnoreCase) >= 0).ToList();
                if (byText.Count == 1) return byText[0];
                if (byText.Count > 1) { why = $"'{want}' matches {byText.Count} parts: {Describe(byText)}"; return byText[0]; }
                if (double.TryParse(want.Replace("\"", "").Replace("inch", "").Trim(), out double dia))
                {
                    var byDia = pool.Where(p => Math.Abs((SafeNum(() => p.GetProperty(PressurePartContextType.DiameterNominal)) ?? -1) - dia) < 1e-6).ToList();
                    if (byDia.Count >= 1) { if (byDia.Count > 1) why = $"{byDia.Count} parts of that diameter, first taken"; return byDia[0]; }
                }
                why = "no description or nominal diameter matches";
                return null;
            }
            if (like != null)
            {
                double? d = SafeNum(() => like.GetProperty(PressurePartContextType.DiameterNominal));
                if (d != null)
                {
                    var same = pool.Where(p => Math.Abs((SafeNum(() => p.GetProperty(PressurePartContextType.DiameterNominal)) ?? -1) - d.Value) < 1e-6).ToList();
                    if (same.Count >= 1) return same[0];
                }
            }
            if (pool.Count == 1) return pool[0];
            why = "several parts and no size given";
            return null;
        }

        private static PressurePartSize NearestAngle(List<PressurePartSize> elbows, double deflection)
        {
            PressurePartSize best = null; double bestDiff = double.MaxValue;
            foreach (var e in elbows)
            {
                double? ang = SafeNum(() => e.GetProperty(PressurePartContextType.FittingAngle));
                if (ang == null) continue;
                double diff = Math.Abs(ang.Value - deflection);
                if (diff < bestDiff) { bestDiff = diff; best = e; }
            }
            return best;
        }

        private static string Describe(IEnumerable<PressurePartSize> parts) =>
            string.Join("; ", parts.Select(p => SafeStr(() => p.Description) ?? "?").Distinct().Take(12));

        private static JObject RunReport(string stage, RunPlan plan)
        {
            var segments = new JArray();
            for (int i = 0; i < plan.Path.Count - 1; i++)
                segments.Add(new JArray(Arr(plan.Path[i]), Arr(plan.Path[i + 1])));
            var points = new JArray(plan.Appurtenances.Select(a => Arr(a.point)));
            bool ok = plan.Errors.Count == 0;
            return new JObject
            {
                ["ok"] = ok, ["stage"] = stage,
                ["message"] = ok ? $"{plan.Path.Count - 1} pipe(s), {plan.Fittings.Count} fitting(s), {plan.Appurtenances.Count} appurtenance(s) planned in '{plan.NetworkName}'."
                                 : string.Join(" ", plan.Errors),
                ["network"] = plan.NetworkName, ["network_exists"] = plan.Network != null,
                ["parts_list"] = plan.List?.Name,
                ["pipe"] = plan.Pipe != null ? PartJson(plan.Pipe) : null,
                ["fittings"] = new JArray(plan.Fittings.Select(f => new JObject
                {
                    ["vertex"] = f.vertex, ["point"] = Arr(plan.Path[f.vertex]), ["deflection_deg"] = Math.Round(f.deflection, 2),
                    ["part"] = f.part != null ? SafeStr(() => f.part.Description) : null,
                })),
                ["appurtenances"] = new JArray(plan.Appurtenances.Select(a => new JObject
                {
                    ["point"] = Arr(a.point), ["asked"] = a.asked, ["part"] = SafeStr(() => a.part.Description),
                })),
                ["proposed"] = new JObject { ["segments"] = segments, ["points"] = points },
                ["warnings"] = new JArray(plan.Warnings), ["errors"] = new JArray(plan.Errors),
            };
        }

        private static JObject DrawRun(Transaction tr, Database db, RunPlan plan, string layer)
        {
            PressurePipeNetwork net = plan.Network;
            if (net == null)
            {
                ObjectId nid = PressurePipeNetwork.Create(db, plan.NetworkName);
                net = (PressurePipeNetwork)tr.GetObject(nid, OpenMode.ForWrite);
                net.PartsListId = plan.ListId;
            }
            else
            {
                net.UpgradeOpen();
            }
            var pipeIds = new List<ObjectId>();
            for (int i = 0; i < plan.Path.Count - 1; i++)
                pipeIds.Add(net.AddLinePipe(new LineSegment3d(plan.Path[i], plan.Path[i + 1]), plan.Pipe));
            var fittingIds = new List<ObjectId>();
            int connected = 0;
            foreach (var f in plan.Fittings)
            {
                if (f.part == null) continue;
                ObjectId fid = net.AddFitting(plan.Path[f.vertex], f.part);
                fittingIds.Add(fid);
                // the pipe ending at this vertex and the one leaving it
                try
                {
                    var fitting = (PressureFitting)tr.GetObject(fid, OpenMode.ForWrite);
                    fitting.ConnectToPipe(0, pipeIds[f.vertex - 1], 1);
                    fitting.ConnectToPipe(1, pipeIds[f.vertex], 0);
                    connected += 2;
                }
                catch (SysException ex) { plan.Warnings.Add($"fitting at vertex {f.vertex} could not be connected: {ex.Message}"); }
            }
            var appIds = new List<ObjectId>();
            foreach (var a in plan.Appurtenances)
                appIds.Add(net.AddAppurtenance(a.point, a.part));

            int relayered = 0;
            if (!string.IsNullOrWhiteSpace(layer))
            {
                string name = EnsureLayer(tr, db, layer);
                foreach (ObjectId id in pipeIds.Concat(fittingIds).Concat(appIds))
                {
                    try { ((AcadEntity)tr.GetObject(id, OpenMode.ForWrite)).Layer = name; relayered++; } catch { }
                }
            }
            return new JObject
            {
                ["created"] = Handles(pipeIds.Concat(fittingIds).Concat(appIds)),
                ["pipes"] = Handles(pipeIds), ["fitting_handles"] = Handles(fittingIds), ["appurtenance_handles"] = Handles(appIds),
                ["connected_ports"] = connected, ["relayered"] = relayered,
                ["network_handle"] = net.Handle.ToString(),
                ["warnings"] = new JArray(plan.Warnings),
            };
        }

        // ── helpers ───────────────────────────────────────────────────────────────

        private static string SafeStr(Func<string> f) { try { return f(); } catch { return null; } }
        private static int SafeInt(Func<int> f) { try { return f(); } catch { return 0; } }
        private static double? SafeNum(Func<object> f)
        {
            try
            {
                object v = f();
                if (v == null) return null;
                return Convert.ToDouble(v);
            }
            catch { return null; }
        }
    }
}
