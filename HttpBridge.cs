using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CADAgent
{
    /// <summary>
    /// Listens on http://localhost:5199/ and exposes two endpoints:
    ///   GET  /modelspace  — collects the drawing and returns LastJson
    ///   POST /execute     — dispatches a single agent step to ModelSpaceBuilder / CadCommands
    ///   GET  /intents     — lists every intent CadCommands accepts
    ///
    /// Requests are accepted asynchronously (BeginGetContext) and queued.
    /// They are processed during AutoCAD's Idle event, which fires on the
    /// application thread — no cross-thread marshalling required.
    /// </summary>
    public class HttpBridge : IDisposable
    {
        private HttpListener?                          _listener;
        private readonly ConcurrentQueue<HttpListenerContext> _pending = new();
        private readonly object                        _startLock = new object();

        // BOTH loopback spellings are registered on purpose. HttpListener matches
        // on the request's Host header, so a client calling 127.0.0.1 against a
        // listener registered only for "localhost" is rejected by http.sys with
        // "Bad Request - Invalid Hostname" before this code ever sees it.
        // Clients use 127.0.0.1 because resolving "localhost" tries IPv6 ::1
        // first here and stalls ~2s per request before falling back to IPv4
        // (measured 2.06s vs 0.015s), and every tool call makes several requests.
        private static readonly string[] _defaultPrefixes =
        {
            "http://127.0.0.1:5199/",
            "http://localhost:5199/",
        };

        public void Start(params string[] prefixes)
        {
            lock (_startLock)
            {
                if (_listener?.IsListening == true) return;
                _listener = new HttpListener();
                foreach (string prefix in
                         (prefixes == null || prefixes.Length == 0) ? _defaultPrefixes : prefixes)
                    _listener.Prefixes.Add(prefix);
                _listener.Start();
                AcadApp.Idle += OnIdle;
                BeginAccept();
            }
        }

        public void Stop()
        {
            AcadApp.Idle -= OnIdle;
            _listener?.Close();
            _listener = null;
        }

        public void Dispose() => Stop();

        // ── Async accept loop (ThreadPool callback — touches no AutoCAD API) ──────

        private void BeginAccept()
        {
            try   { _listener?.BeginGetContext(OnRequestReceived, null); }
            catch (ObjectDisposedException) { }
            catch (HttpListenerException)   { }
        }

        private void OnRequestReceived(IAsyncResult ar)
        {
            try
            {
                var ctx = _listener!.EndGetContext(ar);
                _pending.Enqueue(ctx);
            }
            catch (ObjectDisposedException) { return; }
            catch (HttpListenerException)   { return; }
            catch { }
            BeginAccept();
        }

        // ── Idle handler (AutoCAD application thread) ─────────────────────────────

        private void OnIdle(object sender, EventArgs e)
        {
            while (_pending.TryDequeue(out var ctx))
            {
                try { Dispatch(ctx); }
                catch
                {
                    try { Respond(ctx, Error("Internal server error"), 500); }
                    catch { }
                }
            }
        }

        // ── Request dispatch ──────────────────────────────────────────────────────

        private const long MaxBodyBytes = 1 * 1024 * 1024;

        /// <summary>
        /// Runs on the AutoCAD application thread. Resolving and opening documents must happen
        /// here. Reads are handled inline because ModelSpaceCollector touches doc.Editor, which
        /// belongs on this thread; writes are handed to a worker thread holding that document's
        /// own lock, so two different drawings can be edited at the same time.
        /// </summary>
        private static void Dispatch(HttpListenerContext ctx)
        {
            string path   = ctx.Request.Url!.AbsolutePath.ToLower();
            string method = ctx.Request.HttpMethod.ToUpper();

            if (path == "/documents" && method == "GET")
            {
                string body; int status;
                try   { body = HandleDocuments(); status = 200; }
                catch (Exception ex) { body = Error(ex.Message); status = 500; }
                Respond(ctx, body, status);
                return;
            }

            if (path == "/nested" && method == "GET")
            {
                string body; int status;
                try
                {
                    Document target = ResolveDocument(ctx.Request.QueryString["file"]);
                    if (target == null) { body = Error("No active document"); status = 500; }
                    else
                    {
                        string layers = ctx.Request.QueryString["layers"] ?? "";
                        using (target.LockDocument())
                            body = NestedCollector.Collect(target, layers.Split(','));
                        status = 200;
                    }
                }
                catch (Exception ex) { body = Error(ex.Message); status = 500; }
                Respond(ctx, body, status);
                return;
            }

            if (path == "/modelspace" && method == "GET")
            {
                string body; int status;
                try   { body = HandleModelSpace(ctx.Request.QueryString["file"]); status = 200; }
                catch (Exception ex) { body = Error(ex.Message); status = 500; }
                Respond(ctx, body, status);
                return;
            }

            if (path == "/execute" && method == "POST")
            {
                if (ctx.Request.ContentLength64 > MaxBodyBytes)
                {
                    Respond(ctx, Error("Request body too large"), 413);
                    return;
                }

                string requestBody;
                using (var reader = new StreamReader(ctx.Request.InputStream, Encoding.UTF8))
                    requestBody = reader.ReadToEnd();

                string intent = ModelSpaceBuilder.GetIntent(requestBody);
                if (intent == "chat")
                {
                    Respond(ctx, Error("Could not parse intent from request body"), 400);
                    return;
                }

                Document doc;
                try { doc = ResolveDocument(GetRequestedFile(requestBody)); }
                catch (Exception ex) { Respond(ctx, Error(ex.Message), 500); return; }

                if (doc == null)
                {
                    Respond(ctx, Error("No active document"), 500);
                    return;
                }

                // Intents that need Editor / LayoutManager / xref loading run right here on the
                // application thread; the rest go to a worker holding the document lock.
                if (ModelSpaceBuilder.RequiresAppThread(intent, requestBody))
                {
                    string body; int status;
                    try
                    {
                        using (doc.LockDocument())
                        {
                            body   = ModelSpaceBuilder.ExecuteStep(intent, requestBody, doc);
                            status = 200;
                        }
                    }
                    catch (Exception ex) { body = Error(ex.Message); status = 500; }
                    Respond(ctx, body, status);
                    return;
                }

                // LockDocument is per-document: writes to different drawings proceed in
                // parallel, writes to the same drawing queue behind each other.
                Task.Run(() =>
                {
                    string body; int status;
                    try
                    {
                        using (doc.LockDocument())
                        {
                            body   = ModelSpaceBuilder.ExecuteStep(intent, requestBody, doc);
                            status = 200;
                        }
                    }
                    catch (Exception ex) { body = Error(ex.Message); status = 500; }
                    try { Respond(ctx, body, status); } catch { }
                });
                return;
            }

            if (path == "/intents" && method == "GET")
            {
                Respond(ctx, new JObject { ["intents"] = new JArray(CadCommands.Intents) }.ToString(), 200);
                return;
            }

            if (path == "/handlers" && method == "GET")
            {
                Respond(ctx, new JObject { ["handlers"] = new JArray(PipeInvertSetter.HandlerRegistry.Names) }.ToString(), 200);
                return;
            }

            // Generic ICadObjectHandler dispatch:  { "handler": "pipe_elevation", "stage": "context|compute|draw",
            //                                       "file": "...dwg", "request": { ...handler-specific... } }
            // Runs inline on the application thread; handlers lock the document themselves and
            // return a JSON report (conflicts included) rather than throwing across the boundary.
            if (path == "/handler" && method == "POST")
            {
                if (ctx.Request.ContentLength64 > MaxBodyBytes)
                {
                    Respond(ctx, Error("Request body too large"), 413);
                    return;
                }

                string body; int status;
                try
                {
                    string raw;
                    using (var reader = new StreamReader(ctx.Request.InputStream, Encoding.UTF8))
                        raw = reader.ReadToEnd();

                    JObject env = JObject.Parse(raw);
                    string handler = env["handler"]?.ToString() ?? "";
                    string stage   = env["stage"]?.ToString() ?? "compute";
                    string request = env["request"]?.ToString(Newtonsoft.Json.Formatting.None) ?? "{}";

                    Document doc = ResolveDocument(env["file"]?.ToString());
                    if (doc == null) { body = Error("No active document"); status = 500; }
                    else
                    {
                        body   = PipeInvertSetter.HandlerRegistry.Run(handler, stage, doc, request);
                        status = 200;
                    }
                }
                catch (Exception ex) { body = Error(ex.Message); status = 500; }
                Respond(ctx, body, status);
                return;
            }

            Respond(ctx, Error("Unknown endpoint"), 404);
        }

        // ── Handlers ──────────────────────────────────────────────────────────────

        /// <summary>Lists the drawings currently open in AutoCAD, so the agent can be
        /// given real paths to target instead of guessing at them.</summary>
        private static string HandleDocuments()
        {
            var drawings = new JArray();
            Document active = AcadApp.DocumentManager.MdiActiveDocument;

            foreach (Document d in AcadApp.DocumentManager)
            {
                try
                {
                    drawings.Add(new JObject
                    {
                        ["path"]    = d.Name,
                        ["focused"] = ReferenceEquals(d, active),
                    });
                }
                catch { }
            }

            return new JObject { ["drawings"] = drawings }.ToString();
        }

        private static string HandleModelSpace(string filePath)
        {
            Document doc = ResolveDocument(filePath);
            if (doc == null) return Error("No active document");
            using (doc.LockDocument())
            {
                ModelSpaceCollector.Collect(doc);
                return ModelSpaceCollector.GetLastJson(doc.Name);
            }
        }

        /// <summary>Returns the "file" field from an /execute payload, or null if absent.</summary>
        private static string GetRequestedFile(string requestBody)
        {
            try
            {
                string json = ModelSpaceBuilder.ExtractJson(requestBody);
                return json == null ? null : JObject.Parse(json)["file"]?.ToString();
            }
            catch { return null; }
        }

        /// <summary>
        /// Returns the open Document matching <paramref name="filePath"/>, opening the drawing if
        /// it is not already open. With no file specified this returns MdiActiveDocument, which
        /// preserves the original single-document behavior for callers that don't target a file.
        /// Must be called on the AutoCAD application thread.
        /// </summary>
        private static Document ResolveDocument(string filePath)
        {
            if (string.IsNullOrWhiteSpace(filePath))
                return AcadApp.DocumentManager.MdiActiveDocument;

            string wanted = Path.GetFullPath(filePath);

            foreach (Document d in AcadApp.DocumentManager)
            {
                try
                {
                    if (string.IsNullOrEmpty(d.Name)) continue;
                    if (string.Equals(Path.GetFullPath(d.Name), wanted, StringComparison.OrdinalIgnoreCase))
                        return d;
                }
                catch { }
            }

            if (!File.Exists(wanted))
                throw new FileNotFoundException($"Drawing not found: {wanted}");

            return AcadApp.DocumentManager.Open(wanted, false);
        }

        // ── Helpers ───────────────────────────────────────────────────────────────

        private static string Error(string msg) =>
            new JObject { ["error"] = msg }.ToString();

        private static void Respond(HttpListenerContext ctx, string body, int status)
        {
            ctx.Response.StatusCode      = status;
            ctx.Response.ContentType     = "application/json; charset=utf-8";
            byte[] bytes                 = Encoding.UTF8.GetBytes(body);
            ctx.Response.ContentLength64 = bytes.Length;
            try
            {
                ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
            }
            finally
            {
                ctx.Response.OutputStream.Close();
            }
        }
    }
}
