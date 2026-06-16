using System;
using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Text;
using Newtonsoft.Json.Linq;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CADAgent
{
    /// <summary>
    /// Listens on http://localhost:5199/ and exposes two endpoints:
    ///   GET  /modelspace  — collects the drawing and returns LastJson
    ///   POST /execute     — dispatches a single agent step to ModelSpaceBuilder
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

        public void Start(string prefix = "http://localhost:5199/")
        {
            lock (_startLock)
            {
                if (_listener?.IsListening == true) return;
                _listener = new HttpListener();
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
                try { Handle(ctx); }
                catch { }
            }
        }

        // ── Request dispatch ──────────────────────────────────────────────────────

        private static void Handle(HttpListenerContext ctx)
        {
            string path   = ctx.Request.Url!.AbsolutePath.ToLower();
            string method = ctx.Request.HttpMethod.ToUpper();

            string body   = Error("Internal server error");
            int    status = 500;

            try
            {
                (body, status) = path switch
                {
                    "/modelspace" when method == "GET"  => (HandleModelSpace(), 200),
                    "/execute"    when method == "POST" => HandleExecute(ctx),
                    _                                   => (Error("Unknown endpoint"), 404),
                };
            }
            catch (Exception ex)
            {
                body   = Error(ex.Message);
                status = 500;
            }

            try { Respond(ctx, body, status); }
            catch { }
        }

        // ── Handlers (already on application thread via OnIdle) ───────────────────

        private static string HandleModelSpace()
        {
            var doc = AcadApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return Error("No active document");
            using (doc.LockDocument())
            {
                ModelSpaceCollector.Collect(doc);
                return ModelSpaceCollector.LastJson;
            }
        }

        private static (string body, int status) HandleExecute(HttpListenerContext ctx)
        {
            const long MaxBodyBytes = 1 * 1024 * 1024;
            if (ctx.Request.ContentLength64 > MaxBodyBytes)
                return (Error("Request body too large"), 413);

            string body;
            using (var reader = new StreamReader(ctx.Request.InputStream, Encoding.UTF8))
                body = reader.ReadToEnd();

            string intent = ModelSpaceBuilder.GetIntent(body);
            if (intent == "chat")
                return (Error("Could not parse intent from request body"), 400);

            var doc = AcadApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return (Error("No active document"), 500);

            using (doc.LockDocument())
                return (ModelSpaceBuilder.ExecuteStep(intent, body, doc), 200);
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
