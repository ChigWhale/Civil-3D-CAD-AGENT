using System;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using Newtonsoft.Json.Linq;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CADAgent
{
    /// <summary>
    /// Listens on http://localhost:5199/ and exposes two endpoints:
    ///   GET  /modelspace  — collects the drawing and returns LastJson
    ///   POST /execute     — dispatches a single agent step to ModelSpaceBuilder
    /// </summary>
    public class HttpBridge
    {
        private HttpListener? _listener;
        private Thread?       _thread;

        public void Start(string prefix = "http://localhost:5199/")
        {
            _listener = new HttpListener();
            _listener.Prefixes.Add(prefix);
            _listener.Start();

            _thread = new Thread(Listen)
            {
                IsBackground = true,
                Name         = "CADAgent-HTTP",
            };
            _thread.Start();
        }

        public void Stop() => _listener?.Stop();

        // ── Request loop ──────────────────────────────────────────────────────────

        private void Listen()
        {
            while (_listener!.IsListening)
            {
                try
                {
                    var ctx = _listener.GetContext();
                    ThreadPool.QueueUserWorkItem(_ => Handle(ctx));
                }
                catch { }
            }
        }

        private void Handle(HttpListenerContext ctx)
        {
            string path   = ctx.Request.Url!.AbsolutePath.ToLower();
            string method = ctx.Request.HttpMethod.ToUpper();

            try
            {
                string body = path switch
                {
                    "/modelspace" when method == "GET"  => HandleModelSpace(),
                    "/execute"    when method == "POST" => HandleExecute(ctx),
                    _                                   => Error("Unknown endpoint"),
                };
                Respond(ctx, body, 200);
            }
            catch (Exception ex)
            {
                Respond(ctx, Error(ex.Message), 500);
            }
        }

        // ── Handlers ──────────────────────────────────────────────────────────────

        private static string HandleModelSpace()
        {
            var doc = AcadApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return Error("No active document");

            ModelSpaceCollector.Collect(doc);
            return ModelSpaceCollector.LastJson;
        }

        private static string HandleExecute(HttpListenerContext ctx)
        {
            string body;
            using (var reader = new StreamReader(ctx.Request.InputStream, Encoding.UTF8))
                body = reader.ReadToEnd();

            var    root   = JObject.Parse(body);
            string intent = root["intent"]?.ToString() ?? "chat";

            var doc = AcadApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return Error("No active document");

            using (doc.LockDocument())
                return ModelSpaceBuilder.ExecuteStep(intent, body, doc);
        }

        // ── Helpers ───────────────────────────────────────────────────────────────

        private static string Error(string msg) =>
            new JObject { ["error"] = msg }.ToString();

        private static void Respond(HttpListenerContext ctx, string body, int status)
        {
            ctx.Response.StatusCode  = status;
            ctx.Response.ContentType = "application/json; charset=utf-8";
            byte[] bytes             = Encoding.UTF8.GetBytes(body);
            ctx.Response.ContentLength64 = bytes.Length;
            ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
            ctx.Response.OutputStream.Close();
        }
    }
}
