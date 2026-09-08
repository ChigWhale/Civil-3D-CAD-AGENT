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
    /// <summary>JSON boundary for <see cref="PipeRunSolver"/> (handler name "pipe_run").</summary>
    public sealed class PipeRunHandler : ICadObjectHandler
    {
        public string Name => "pipe_run";

        /// <summary>Context for a run = the compute report with nothing applied (it already carries the whole profile).</summary>
        public string GetContext(Document doc, string requestJson) => Run(doc, requestJson, apply: false);

        public string Compute(Document doc, string requestJson) => Run(doc, requestJson, apply: false);

        public string Draw(Document doc, string requestJson) => Run(doc, requestJson, apply: true);

        private static string Run(Document doc, string requestJson, bool apply)
        {
            RunInput input;
            try
            {
                RunRequest req = JsonSerializer.Deserialize<RunRequest>(string.IsNullOrWhiteSpace(requestJson) ? "{}" : requestJson, SolveReport.JsonOptions) ?? new RunRequest();
                using (doc.LockDocument())
                    input = ToInput(doc.Database, req);
            }
            catch (Exception ex)
            {
                return new RunReport { Stage = apply ? "draw" : "compute", Ok = false, Errors = { ex.Message } }.ToJson();
            }

            RunReport report = apply ? PipeRunSolver.Draw(doc, input) : PipeRunSolver.Compute(doc, input);
            return report.ToJson();
        }

        public static RunInput ToInput(Database db, RunRequest req)
        {
            var input = new RunInput
            {
                OffsetTolerance = req.OffsetTolerance > 0 ? req.OffsetTolerance : 25.0,
                FlowDirection = req.FlowDirection ?? "auto",
                CoverMinimum = req.CoverMinimum,
                Mode = PipeElevationHandler.ParseMode(req.OffsetMode),
                ManualDrop = req.ManualDrop,
                HoldStart = req.HoldStart,
                HoldEnd = req.HoldEnd,
                MaxDepthBelowSurface = req.MaxDepthBelowSurface > 0 ? req.MaxDepthBelowSurface : 50.0,
                SlopeUnits = req.SlopeUnits ?? "auto",
            };

            if (!string.IsNullOrWhiteSpace(req.Network)) input.NetworkId = IdResolver.FindNetwork(db, req.Network!);

            if (req.Structures is { Count: >= 2 })
            {
                input.StructureIds = new List<ObjectId>();
                foreach (string s in req.Structures) input.StructureIds.Add(IdResolver.FindStructure(db, s));
            }
            else if (!string.IsNullOrWhiteSpace(req.StartStructure) && !string.IsNullOrWhiteSpace(req.EndStructure))
            {
                input.StartStructureId = IdResolver.FindStructure(db, req.StartStructure!);
                input.EndStructureId = IdResolver.FindStructure(db, req.EndStructure!);
            }
            else if (!string.IsNullOrWhiteSpace(req.Alignment))
            {
                input.AlignmentId = FindAlignment(db, req.Alignment!);
            }
            else throw new ArgumentException("Give 'alignment', an ordered 'structures' list, or 'startStructure' + 'endStructure'.");

            if (req.FixedInverts is { Count: > 0 })
            {
                input.FixedInverts = new Dictionary<ObjectId, double>();
                foreach (var kv in req.FixedInverts) input.FixedInverts[IdResolver.FindStructure(db, kv.Key)] = kv.Value;
            }
            if (req.Overrides is { Count: > 0 })
            {
                input.Overrides = new Dictionary<ObjectId, PipeInvertOverride>();
                foreach (var kv in req.Overrides) input.Overrides[IdResolver.FindPipe(db, kv.Key)] = kv.Value;
            }
            return input;
        }

        public static ObjectId FindAlignment(Database db, string key)
        {
            string k = key.Trim();
            if (k.StartsWith("0x", StringComparison.OrdinalIgnoreCase)) k = k.Substring(2);
            if (long.TryParse(k, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out long h))
            {
                try
                {
                    if (db.TryGetObjectId(new Handle(h), out ObjectId id) && !id.IsNull)
                    {
                        using Transaction tr = db.TransactionManager.StartTransaction();
                        bool ok = tr.GetObject(id, OpenMode.ForRead, false) is Alignment;
                        tr.Commit();
                        if (ok) return id;
                    }
                }
                catch { }
            }
            foreach (ObjectId id in CivilDocument.GetCivilDocument(db).GetAlignmentIds())
            {
                using Transaction tr = db.TransactionManager.StartTransaction();
                bool match = tr.GetObject(id, OpenMode.ForRead, false) is Alignment a &&
                             string.Equals(a.Name, key, StringComparison.OrdinalIgnoreCase);
                tr.Commit();
                if (match) return id;
            }
            throw new ArgumentException($"No alignment named or with handle '{key}'.");
        }
    }
}
