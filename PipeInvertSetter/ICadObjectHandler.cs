using System;
using System.Collections.Generic;
using Autodesk.AutoCAD.ApplicationServices;

namespace PipeInvertSetter
{
    /// <summary>
    /// A focused, JSON-in / JSON-out unit of CAD work that follows the agent workflow
    /// <c>get_model_space → get_context → compute → draw</c>:
    /// <list type="bullet">
    ///   <item><see cref="GetContext"/> — read-only snapshot of the objects involved.</item>
    ///   <item><see cref="Compute"/>    — pure computation, no drawing mutation; returns a report
    ///         with any conflicts so the calling layer (Python / LLM) can decide.</item>
    ///   <item><see cref="Draw"/>       — compute again and, only if the report is clean,
    ///         write the result in one transaction (all-or-nothing).</item>
    /// </list>
    /// Only semantic results cross the boundary; no LLM reasoning happens in here.
    /// </summary>
    public interface ICadObjectHandler
    {
        /// <summary>Stable identifier used by the HTTP bridge / MCP layer (e.g. "pipe_elevation").</summary>
        string Name { get; }

        /// <summary>Read-only context JSON for the objects named in <paramref name="requestJson"/>.</summary>
        string GetContext(Document doc, string requestJson);

        /// <summary>Solves without writing. Returns a JSON report (always, even on validation failure).</summary>
        string Compute(Document doc, string requestJson);

        /// <summary>Solves and, if clean, writes transactionally. Returns the JSON report with <c>applied</c> set.</summary>
        string Draw(Document doc, string requestJson);
    }

    /// <summary>Lookup of handlers by name so the bridge can dispatch generically.</summary>
    public static class HandlerRegistry
    {
        private static readonly Dictionary<string, ICadObjectHandler> _handlers =
            new(StringComparer.OrdinalIgnoreCase);

        static HandlerRegistry()
        {
            Register(new PipeElevationHandler());
            Register(new PipeRunHandler());
        }

        public static void Register(ICadObjectHandler handler) => _handlers[handler.Name] = handler;

        public static ICadObjectHandler? Get(string name) =>
            _handlers.TryGetValue(name, out var h) ? h : null;

        public static IEnumerable<string> Names => _handlers.Keys;

        /// <summary>Dispatch one stage ("context" | "compute" | "draw") to a named handler.</summary>
        public static string Run(string handlerName, string stage, Document doc, string requestJson)
        {
            ICadObjectHandler? h = Get(handlerName);
            if (h == null)
                throw new ArgumentException($"Unknown handler '{handlerName}'. Known: {string.Join(", ", _handlers.Keys)}");

            return (stage ?? "compute").ToLowerInvariant() switch
            {
                "context" => h.GetContext(doc, requestJson),
                "compute" => h.Compute(doc, requestJson),
                "draw"    => h.Draw(doc, requestJson),
                "apply"   => h.Draw(doc, requestJson),
                _ => throw new ArgumentException($"Unknown stage '{stage}'. Use context, compute or draw."),
            };
        }
    }
}
