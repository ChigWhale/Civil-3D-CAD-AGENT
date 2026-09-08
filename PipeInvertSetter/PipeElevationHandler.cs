using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text.Json;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.Civil.ApplicationServices;
using Autodesk.Civil.DatabaseServices;

namespace PipeInvertSetter
{
    /// <summary>
    /// JSON boundary for <see cref="PipeElevationSolver"/>.  Accepts handles or names,
    /// never ObjectIds, so the Python / MCP layer can drive it from model-space output.
    /// </summary>
    public sealed class PipeElevationHandler : ICadObjectHandler
    {
        public string Name => "pipe_elevation";

        public string GetContext(Document doc, string requestJson)
        {
            SolveRequest req;
            ObjectId structureId = ObjectId.Null, networkId = ObjectId.Null;
            try
            {
                req = Parse(requestJson);
                using (doc.LockDocument())
                {
                    if (!string.IsNullOrWhiteSpace(req.Structure))
                        structureId = IdResolver.FindStructure(doc.Database, req.Structure!);
                    else if (!string.IsNullOrWhiteSpace(req.Network))
                        networkId = IdResolver.FindNetwork(doc.Database, req.Network!);
                    else
                        networkId = IdResolver.FirstNetwork(doc.Database);
                }
            }
            catch (Exception ex)
            {
                return new ContextReport { Errors = { ex.Message } }.ToJson();
            }
            return PipeElevationSolver.GetContext(doc, structureId, networkId).ToJson();
        }

        public string Compute(Document doc, string requestJson) => Run(doc, requestJson, apply: false);

        public string Draw(Document doc, string requestJson) => Run(doc, requestJson, apply: true);

        private static string Run(Document doc, string requestJson, bool apply)
        {
            SolverInput input;
            try
            {
                SolveRequest req = Parse(requestJson);
                using (doc.LockDocument())
                    input = ToInput(doc.Database, req);
            }
            catch (Exception ex)
            {
                return new SolveReport { Stage = apply ? "draw" : "compute", Ok = false, Errors = { ex.Message } }.ToJson();
            }

            SolveReport report = apply ? PipeElevationSolver.Draw(doc, input) : PipeElevationSolver.Compute(doc, input);
            return report.ToJson();
        }

        public static SolveRequest Parse(string json)
        {
            if (string.IsNullOrWhiteSpace(json)) return new SolveRequest();
            return JsonSerializer.Deserialize<SolveRequest>(json, SolveReport.JsonOptions) ?? new SolveRequest();
        }

        /// <summary>Resolves every handle/name in the request. Throws with a clear message on failure.</summary>
        public static SolverInput ToInput(Database db, SolveRequest req)
        {
            if (string.IsNullOrWhiteSpace(req.Structure))
                throw new ArgumentException("'structure' (handle or name) is required.");

            var input = new SolverInput
            {
                StructureId = IdResolver.FindStructure(db, req.Structure!),
                CoverMinimum = req.CoverMinimum,
                ManualDrop = req.ManualDrop,
                StructureInvertOverride = req.StructureInvertOverride,
                MaxDepthBelowSurface = req.MaxDepthBelowSurface > 0 ? req.MaxDepthBelowSurface : 50.0,
                SlopeUnits = req.SlopeUnits ?? "auto",
            };

            input.Mode = ParseMode(req.OffsetMode);
            if (input.Mode == OffsetMode.ManualDrop && req.ManualDrop < 0)
                throw new ArgumentException("manualDrop must be ≥ 0.");

            if (req.Pipes is { Count: > 0 })
            {
                input.PipeIds = new List<ObjectId>();
                foreach (string p in req.Pipes) input.PipeIds.Add(IdResolver.FindPipe(db, p));
            }
            if (!string.IsNullOrWhiteSpace(req.GoverningPipe))
                input.GoverningPipeId = IdResolver.FindPipe(db, req.GoverningPipe!);

            return input;
        }

        public static OffsetMode ParseMode(string? s)
        {
            string m = (s ?? "").Replace(" ", "").Replace("_", "").ToLowerInvariant();
            return m switch
            {
                "" or "matchcrowns" or "crowns" or "crown" => OffsetMode.MatchCrowns,
                "matchinverts" or "inverts" or "invert"    => OffsetMode.MatchInverts,
                "manualdrop" or "drop"                     => OffsetMode.ManualDrop,
                _ => throw new ArgumentException($"Unknown offsetMode '{s}'. Use MatchCrowns, MatchInverts or ManualDrop."),
            };
        }
    }

    /// <summary>Handle-or-name lookup for pipe network objects.</summary>
    public static class IdResolver
    {
        public static ObjectId FindStructure(Database db, string key) =>
            Find<Structure>(db, key, net => net.GetStructureIds(), "structure");

        public static ObjectId FindPipe(Database db, string key) =>
            Find<Pipe>(db, key, net => net.GetPipeIds(), "pipe");

        public static ObjectId FindNetwork(Database db, string key)
        {
            if (TryHandle(db, key, out ObjectId id))
            {
                using Transaction tr = db.TransactionManager.StartTransaction();
                bool ok = tr.GetObject(id, OpenMode.ForRead, false) is Network;
                tr.Commit();
                if (ok) return id;
            }
            foreach (ObjectId netId in CivilDocument.GetCivilDocument(db).GetPipeNetworkIds())
            {
                using Transaction tr = db.TransactionManager.StartTransaction();
                bool match = tr.GetObject(netId, OpenMode.ForRead, false) is Network n &&
                             string.Equals(n.Name, key, StringComparison.OrdinalIgnoreCase);
                tr.Commit();
                if (match) return netId;
            }
            throw new ArgumentException($"No pipe network named or with handle '{key}'.");
        }

        public static ObjectId FirstNetwork(Database db)
        {
            foreach (ObjectId netId in CivilDocument.GetCivilDocument(db).GetPipeNetworkIds()) return netId;
            throw new InvalidOperationException("The drawing has no pipe networks.");
        }

        private static ObjectId Find<T>(Database db, string key, Func<Network, ObjectIdCollection> ids, string kind)
            where T : Part
        {
            if (TryHandle(db, key, out ObjectId hid))
            {
                using Transaction tr = db.TransactionManager.StartTransaction();
                bool ok = tr.GetObject(hid, OpenMode.ForRead, false) is T;
                tr.Commit();
                if (ok) return hid;
            }

            var matches = new List<(ObjectId id, string net)>();
            foreach (ObjectId netId in CivilDocument.GetCivilDocument(db).GetPipeNetworkIds())
            {
                using Transaction tr = db.TransactionManager.StartTransaction();
                if (tr.GetObject(netId, OpenMode.ForRead, false) is Network net)
                {
                    foreach (ObjectId id in ids(net))
                    {
                        if (tr.GetObject(id, OpenMode.ForRead, false) is T part &&
                            string.Equals(part.Name, key, StringComparison.OrdinalIgnoreCase))
                            matches.Add((id, net.Name));
                    }
                }
                tr.Commit();
            }

            if (matches.Count == 1) return matches[0].id;
            if (matches.Count == 0) throw new ArgumentException($"No {kind} named or with handle '{key}'.");
            throw new ArgumentException($"{kind} name '{key}' is ambiguous across networks ({string.Join(", ", matches.ConvertAll(m => m.net))}); pass its handle instead.");
        }

        private static bool TryHandle(Database db, string key, out ObjectId id)
        {
            id = ObjectId.Null;
            string k = key.Trim();
            if (k.StartsWith("0x", StringComparison.OrdinalIgnoreCase)) k = k.Substring(2);
            if (!long.TryParse(k, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out long h)) return false;
            try { return db.TryGetObjectId(new Handle(h), out id) && !id.IsNull && !id.IsErased; }
            catch { return false; }
        }
    }
}
