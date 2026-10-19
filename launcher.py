"""Starts and stops the CAD-Agent services: the model server and the dashboard.

    python launcher.py start [--no-browser] [--no-models]
    python launcher.py stop | status | open | doctor
    python launcher.py fetch-models [--only NAME]
    python launcher.py install --bundle DIR [--data-dir DIR] [--no-autostart]

In a packaged install the Civil 3D plugin runs `start --no-browser` when it
loads (install.json beside the DLL says so), the Start Menu entry runs
`open`, and the installer runs `install` once to write cad-agent.json and
install.json with the machine's real paths. In development the same
commands work against the repo: models come from Ollama if it is running,
the dashboard from the current Python.

Model server: llama-swap (MIT) in front of llama-server (MIT) serves the
OpenAI-compatible endpoint the pipeline uses and swaps the vision and
planner models in and out of an 8 GB card on demand. When neither binary is
present, Ollama on the same port is used if it is up; otherwise the
dashboard starts without models and `doctor` says so.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cadagent_config as cfg  # noqa: E402

DEFAULT_MODELS = {
    # Named as Ollama names them, so the pipeline asks for the same model on
    # either server. The plain qwen3-vl:4b tag is a thinking build that spends
    # its whole output budget reasoning; the -instruct build answers directly.
    "qwen3-vl:4b-instruct": {
        # Qwen's own GGUF repo; the ggml-org mirror is gated (HTTP 401 without a token)
        "file": "Qwen3VL-4B-Instruct-Q4_K_M.gguf",
        "mmproj": "mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf",
        "url": "https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/Qwen3VL-4B-Instruct-Q4_K_M.gguf",
        "mmproj_url": "https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf",
        "context": 8192,
    },
    "qwen2.5-coder:7b": {
        "file": "qwen2.5-coder-7b-instruct-q4_k_m.gguf",
        "url": "https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF/resolve/main/qwen2.5-coder-7b-instruct-q4_k_m.gguf",
        "context": 16384,
    },
}


# ── paths ────────────────────────────────────────────────────────────────────

def package_root() -> str:
    return cfg.package_root() or os.path.dirname(HERE)


def models_dir() -> str:
    return str(cfg.get("models_dir") or os.path.join(package_root(), "models"))


def run_dir() -> str:
    return cfg.writable("run", os.path.join(HERE, ".run"))


def log_dir() -> str:
    return cfg.writable("logs", os.path.join(HERE, ".run", "logs"))


def dashboard_port() -> int:
    return int(str(cfg.get("dashboard_base")).rsplit(":", 1)[-1].strip("/") or 8765)


def model_port() -> int:
    base = str(cfg.get("model_base"))
    try:
        return int(base.split("//", 1)[1].split("/", 1)[0].rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 11434


def models_config() -> dict:
    data = cfg.load().get("models")
    return data if isinstance(data, dict) and data else DEFAULT_MODELS


# ── process bookkeeping ──────────────────────────────────────────────────────

def _pid_file(name: str) -> str:
    return os.path.join(run_dir(), f"{name}.pid")


def _write_pid(name: str, pid: int) -> None:
    with open(_pid_file(name), "w") as f:
        f.write(str(pid))


def _read_pid(name: str) -> int | None:
    try:
        with open(_pid_file(name)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill(pid: int | None) -> None:
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def _spawn(name: str, cmd: list, cwd: str | None = None) -> int:
    """A background process that outlives this one, logging to <logs>/<name>.log."""
    log = open(os.path.join(log_dir(), f"{name}.log"), "a", encoding="utf-8")
    kwargs = {"cwd": cwd, "stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.Popen(cmd, **kwargs)
    _write_pid(name, p.pid)
    return p.pid


def _http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < 500
    except Exception:
        return False


def _wait(url: str, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _http_ok(url):
            return True
        time.sleep(0.4)
    return False


# ── model server ─────────────────────────────────────────────────────────────

def _binary(name: str) -> str | None:
    p = os.path.join(models_dir(), name)
    return p if os.path.isfile(p) else None


def write_swap_config() -> str | None:
    """llama-swap.yaml: one llama-server command per model, swapped on demand."""
    server = _binary("llama-server.exe") or _binary("llama-server")
    if server is None:
        return None
    lines = ["# written by launcher.py; edit cad-agent.json 'models' instead", "healthCheckTimeout: 600", "models:"]
    count = 0
    for name, spec in models_config().items():
        path = os.path.join(models_dir(), spec.get("file", ""))
        if not spec.get("file") or not os.path.isfile(path):
            continue
        cmd = f'"{server}" --port ${{PORT}} -m "{path}" -ngl 99 -c {int(spec.get("context", 8192))} --jinja'
        mm = spec.get("mmproj")
        if mm and os.path.isfile(os.path.join(models_dir(), mm)):
            # llama.cpp asks for at least 1024 image tokens on Qwen-VL for grounding
            # (the redline pipeline asks where a mark sits inside a crop)
            cmd += f' --mmproj "{os.path.join(models_dir(), mm)}" --image-min-tokens 1024'
        lines += [f'  "{name}":', f"    cmd: '{cmd}'", f"    ttl: {int(spec.get('ttl', 600))}"]
        count += 1
    if count == 0:
        return None
    path = os.path.join(run_dir(), "llama-swap.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def start_models(log=print) -> str:
    base = f"http://127.0.0.1:{model_port()}"
    if _http_ok(f"{base}/v1/models"):
        return "model server already up"
    swap = _binary("llama-swap.exe") or _binary("llama-swap")
    conf = write_swap_config()
    if swap and conf:
        _spawn("llama-swap", [swap, "--config", conf, "--listen", f"127.0.0.1:{model_port()}"], cwd=models_dir())
        return "llama-swap started" if _wait(f"{base}/v1/models", 30) else "llama-swap did not answer in 30 s (see logs)"
    if shutil.which("ollama"):
        _spawn("ollama", ["ollama", "serve"])
        return "ollama started" if _wait(f"{base}/v1/models", 30) else "ollama did not answer in 30 s"
    missing = "no llama-swap/llama-server in " + models_dir() if not swap else "no model files in " + models_dir()
    return f"no model server ({missing}); run `launcher.py fetch-models`"


# ── dashboard ────────────────────────────────────────────────────────────────

def dashboard_url() -> str:
    return f"http://127.0.0.1:{dashboard_port()}"


def code_version() -> str | None:
    """DASHBOARD_VERSION as written in dashboard/server.py, read without
    importing the server (which would pull in FastAPI and the spatial index)."""
    try:
        with open(os.path.join(HERE, "dashboard", "server.py"), encoding="utf-8") as f:
            m = re.search(r'^DASHBOARD_VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def running_version(url: str) -> str | None:
    try:
        with urllib.request.urlopen(f"{url}/version", timeout=2) as r:
            return str(json.loads(r.read().decode("utf-8")).get("version") or "") or None
    except Exception:
        return None


def _port_pid(port: int) -> int | None:
    """The process listening on a local TCP port (Windows), for when the pid
    file is stale: the server outlived the launcher that wrote it."""
    if os.name != "nt":
        return None
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                continue
    return None


def _is_our_dashboard(pid: int | None) -> bool:
    """True when the process runs dashboard.server, so stopping it replaces
    our own server and never someone else's service on the port."""
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine"],
                             capture_output=True, text=True).stdout
    else:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                out = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            out = ""
    return "dashboard.server" in out


def start_dashboard() -> str:
    """Starts the dashboard, or replaces a running one of ours whose /version
    no longer matches the code (a server left up across a code change keeps
    serving the old blocks and the old page). A server on the port that is
    not ours is left alone and reported."""
    url = dashboard_url()
    note = "dashboard started"
    if _http_ok(f"{url}/version"):
        want, have = code_version(), running_version(url)
        if not want or not have or want == have:
            return "dashboard already up"
        pid = _read_pid("dashboard")
        if not _alive(pid):
            pid = _port_pid(dashboard_port())
        if not _is_our_dashboard(pid):
            return (f"dashboard already up but out of date (running {have}, code is {want}) and not started by "
                    f"this launcher: stop it, then run this again")
        _kill(pid)
        try:
            os.remove(_pid_file("dashboard"))
        except OSError:
            pass
        for _ in range(50):
            if not _http_ok(f"{url}/version", 0.5):
                break
            time.sleep(0.2)
        note = f"dashboard restarted ({have} -> {want})"
    _spawn("dashboard", [sys.executable, "-m", "uvicorn", "dashboard.server:app", "--host", "127.0.0.1",
                         "--port", str(dashboard_port()), "--log-level", "warning"], cwd=HERE)
    return note if _wait(f"{url}/version", 60) else "dashboard did not answer in 60 s (see logs)"


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_start(args) -> int:
    os.makedirs(run_dir(), exist_ok=True)
    os.makedirs(log_dir(), exist_ok=True)
    if not args.no_models:
        print(start_models())
    print(start_dashboard())
    if not args.no_browser:
        webbrowser.open(dashboard_url())
    return 0


def cmd_stop(args) -> int:
    for name in ("dashboard", "llama-swap", "ollama"):
        pid = _read_pid(name)
        if _alive(pid):
            _kill(pid)
            print(f"stopped {name} ({pid})")
        try:
            os.remove(_pid_file(name))
        except OSError:
            pass
    return 0


def cmd_status(args) -> int:
    print(json.dumps({"dashboard": _http_ok(f"{dashboard_url()}/version"),
                      "model_server": _http_ok(f"http://127.0.0.1:{model_port()}/v1/models"),
                      "pids": {n: _read_pid(n) for n in ("dashboard", "llama-swap", "ollama")},
                      "models_dir": models_dir(), "run_dir": run_dir()}, indent=2))
    return 0


def cmd_open(args) -> int:
    if not _http_ok(f"{dashboard_url()}/version"):
        args.no_browser, args.no_models = True, False
        cmd_start(args)
    else:
        # up, but maybe from before the code changed: start_dashboard replaces a stale one of ours
        os.makedirs(run_dir(), exist_ok=True)
        os.makedirs(log_dir(), exist_ok=True)
        msg = start_dashboard()
        if msg != "dashboard already up":
            print(msg)
    webbrowser.open(dashboard_url())
    return 0


def cmd_doctor(args) -> int:
    d = cfg.describe()
    d["models_dir"] = models_dir()
    d["llama_swap"] = bool(_binary("llama-swap.exe") or _binary("llama-swap"))
    d["llama_server"] = bool(_binary("llama-server.exe") or _binary("llama-server"))
    d["model_files"] = {n: os.path.isfile(os.path.join(models_dir(), s.get("file", ""))) for n, s in models_config().items()}
    d["dashboard_up"] = _http_ok(f"{dashboard_url()}/version")
    d["model_server_up"] = _http_ok(f"http://127.0.0.1:{model_port()}/v1/models")
    d["python"] = sys.executable
    print(json.dumps(d, indent=2))
    return 0


def _download(url: str, dest: str, log=print) -> None:
    """Resumable download with a progress line."""
    tmp = dest + ".part"
    have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    req = urllib.request.Request(url, headers={"User-Agent": "CAD-Agent launcher"})
    if have:
        req.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(req, timeout=60) as r:
        if have and r.status != 206:
            have = 0
        total = have + int(r.headers.get("Content-Length") or 0)
        mode = "ab" if have else "wb"
        done = have
        last = time.time()
        with open(tmp, mode) as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if time.time() - last > 2:
                    pct = f"{100.0 * done / total:.0f}%" if total else f"{done >> 20} MB"
                    log(f"  {os.path.basename(dest)}: {pct}")
                    last = time.time()
    os.replace(tmp, dest)


def cmd_fetch_models(args) -> int:
    os.makedirs(models_dir(), exist_ok=True)
    failures = 0
    for name, spec in models_config().items():
        if args.only and args.only != name:
            continue
        for key, url_key in (("file", "url"), ("mmproj", "mmproj_url")):
            fn, url = spec.get(key), spec.get(url_key)
            if not fn:
                continue
            dest = os.path.join(models_dir(), fn)
            if os.path.isfile(dest):
                print(f"have {fn}")
                continue
            if not url:
                print(f"no URL for {fn}; put the file in {models_dir()} by hand")
                failures += 1
                continue
            print(f"fetching {fn} from {url}")
            try:
                _download(url, dest)
            except Exception as e:
                print(f"  failed: {e}")
                failures += 1
    return 1 if failures else 0


def cmd_install(args) -> int:
    """Writes cad-agent.json at the package root and install.json in the
    plugin bundle, with this machine's real paths. Idempotent.

    The package root is --root, else the folder above this file when it looks
    like a package (has python\\ or version.txt). A developer checkout has
    neither, and refusing there keeps a stray cad-agent.json from appearing
    above the repo, where every process would pick it up."""
    root = os.path.abspath(args.root) if args.root else package_root()
    looks_packaged = os.path.isdir(os.path.join(root, "python")) or os.path.isfile(os.path.join(root, "version.txt"))
    if not args.root and not looks_packaged:
        print(f"{root} is not a package root (no python\\ or version.txt); pass --root to install elsewhere")
        return 2
    python_dir = os.path.join(root, "python")
    py = os.path.join(python_dir, "python.exe") if os.path.isfile(os.path.join(python_dir, "python.exe")) else sys.executable
    pyw = os.path.join(python_dir, "pythonw.exe") if os.path.isfile(os.path.join(python_dir, "pythonw.exe")) else py
    version = None
    try:
        with open(os.path.join(root, "version.txt"), encoding="utf-8") as f:
            version = f.read().strip()
    except OSError:
        pass
    template = {}
    tpl = os.path.join(root, "cad-agent.template.json")
    if os.path.isfile(tpl):
        with open(tpl, encoding="utf-8") as f:
            template = json.load(f)
    conf = dict(template)
    conf.update({"packaged": True, "version": version or conf.get("version"),
                 "data_dir": args.data_dir or conf.get("data_dir") or "%LOCALAPPDATA%\\CAD-Agent",
                 "models_dir": os.path.join(root, "models"), "python": py})
    conf.setdefault("models", DEFAULT_MODELS)
    conf_path = os.path.join(root, "cad-agent.json")
    with open(conf_path, "w", encoding="utf-8") as f:
        json.dump(conf, f, indent=2)
    print(f"wrote {conf_path}")
    if args.bundle:
        contents = os.path.join(args.bundle, "Contents")
        os.makedirs(contents, exist_ok=True)
        app_dir = os.path.join(root, "app") if os.path.isdir(os.path.join(root, "app")) else HERE
        manifest = {"program_root": app_dir, "python": py, "pythonw": pyw,
                    "launcher": os.path.join(app_dir, "launcher.py"),
                    "autostart": not args.no_autostart, "version": version}
        mpath = os.path.join(contents, "install.json")
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        print(f"wrote {mpath}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="launcher.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("start"); p.add_argument("--no-browser", action="store_true"); p.add_argument("--no-models", action="store_true")
    sub.add_parser("stop"); sub.add_parser("status"); sub.add_parser("doctor")
    p = sub.add_parser("open"); p.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS); p.add_argument("--no-models", action="store_true", help=argparse.SUPPRESS)
    p = sub.add_parser("fetch-models"); p.add_argument("--only")
    p = sub.add_parser("install"); p.add_argument("--bundle"); p.add_argument("--data-dir", dest="data_dir")
    p.add_argument("--root", help="package root holding python\\ and app\\ (default: the folder above app\\)")
    p.add_argument("--no-autostart", action="store_true")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    return {"start": cmd_start, "stop": cmd_stop, "status": cmd_status, "open": cmd_open, "doctor": cmd_doctor,
            "fetch-models": cmd_fetch_models, "install": cmd_install}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
