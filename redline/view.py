"""The results window: what the dashboard serves for a review folder, and
how the command line opens it.

This module never imports cad_mcp_server (a second of start-up the
dashboard's request path cannot afford); the tool tables it needs are the
pure ones in redline.tools.

    payload(review_dir, review_id)   -> everything the page draws for one run
    aggregate_payload(review_dir)    -> the metrics over every run
    show(settings, review_id)        -> make sure the dashboard is up, open the window
"""
import json
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from . import config, labels as labels_mod, tools

PAGE = "/redline.html"
ALLOWED_SUFFIXES = (".png", ".jpg", ".jpeg", ".json", ".txt", ".md", ".jsonl")
VIEW_ENV = "CAD_REDLINE_VIEW"
OCR_FOLDER = "ocr-only"
_AGG_CACHE: dict = {}


# ── folders ──────────────────────────────────────────────────────────────────

def review_dir() -> str:
    return config.load().review_dir


def _is_segment(s: str) -> bool:
    return bool(s) and s not in (".", "..") and "/" not in s and "\\" not in s and ":" not in s and not s.startswith(".")


def resolve(review_root: str, review_id: str) -> str | None:
    """The folder of a review id ("20261012-173308" or "ocr-only/20261012-1200"),
    or None for anything that is not one of those shapes or does not sit under
    the review root with a report.json / ocr.json inside."""
    rid = str(review_id or "").strip().replace("\\", "/")
    parts = rid.split("/")
    if not (1 <= len(parts) <= 2) or not all(_is_segment(p) for p in parts):
        return None
    root = os.path.realpath(review_root)
    folder = os.path.realpath(os.path.join(root, *parts))
    try:
        if os.path.commonpath([root, folder]) != root:
            return None
    except ValueError:
        return None
    if not (os.path.isfile(os.path.join(folder, "report.json")) or os.path.isfile(os.path.join(folder, "ocr.json"))):
        return None
    return folder


def safe_file(folder: str, rel: str) -> str | None:
    """A file under the review folder by its relative path, or None."""
    rel = str(rel or "").replace("\\", "/")
    if not rel or rel.startswith("/") or ":" in rel or any(p in ("..", "") for p in rel.split("/")):
        return None
    if not rel.lower().endswith(ALLOWED_SUFFIXES):
        return None
    root = os.path.realpath(folder)
    path = os.path.realpath(os.path.join(root, *rel.split("/")))
    try:
        if os.path.commonpath([root, path]) != root:
            return None
    except ValueError:
        return None
    return path if os.path.isfile(path) else None


def png_size(path: str) -> tuple | None:
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
            return None
        w, h = struct.unpack(">II", head[16:24])
        return int(w), int(h)
    except (OSError, struct.error):
        return None


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _source_of(report: dict) -> str:
    if report.get("kind") == "ocr":
        return "transcription"
    if (report.get("settings") or {}).get("source") == "markups":
        return "markups"
    if report.get("pdfs"):
        return "handwriting"
    return "typed"


def _summary(folder: str, rid: str, kind: str, report: dict) -> dict:
    counts = report.get("counts") or {}
    labs = labels_mod.load(folder)
    return {"id": rid, "kind": kind, "created": report.get("created") or os.path.getmtime(folder),
            "mode": report.get("mode"), "source": _source_of(report),
            "pdfs": [os.path.basename(p) for p in report.get("pdfs") or []],
            "comments": counts.get("comments", len(report.get("comments") or [])),
            "acted": counts.get("acted", 0),
            "labelled": labels_mod.counts(labs, len(report.get("comments") or []))["labelled"]}


def _folders(review_root: str) -> list:
    """[(id, folder, kind)] for every run under the root, unsorted."""
    out = []
    if not os.path.isdir(review_root):
        return out
    for name in os.listdir(review_root):
        folder = os.path.join(review_root, name)
        if not os.path.isdir(folder):
            continue
        if name == OCR_FOLDER:
            for sub in os.listdir(folder):
                f2 = os.path.join(folder, sub)
                if os.path.isfile(os.path.join(f2, "ocr.json")):
                    out.append((f"{OCR_FOLDER}/{sub}", f2, "ocr"))
        elif os.path.isfile(os.path.join(folder, "report.json")):
            out.append((name, folder, "review"))
    return out


def list_reviews(review_root: str, limit: int = 200) -> list:
    rows = []
    for rid, folder, kind in _folders(review_root):
        report = load_report(folder)
        if report is None:
            continue
        rows.append(_summary(folder, rid, kind, report))
    rows.sort(key=lambda r: -float(r.get("created") or 0))
    return rows[:limit]


def latest_review_id(review_root: str) -> str | None:
    best, best_t = None, -1.0
    for rid, folder, kind in _folders(review_root):
        f = os.path.join(folder, "report.json" if kind == "review" else "ocr.json")
        t = os.path.getmtime(f)
        if t > best_t:
            best, best_t = rid, t
    return best


def load_report(folder: str) -> dict | None:
    """report.json, or ocr.json normalised to the same shape (kind "ocr")."""
    rp = os.path.join(folder, "report.json")
    if os.path.isfile(rp):
        rep = _read_json(rp)
        if isinstance(rep, dict):
            rep.setdefault("kind", "review")
            return rep
        return None
    op = os.path.join(folder, "ocr.json")
    ocr = _read_json(op)
    if not isinstance(ocr, dict):
        return None
    comments, sheets, warnings = [], [], list(ocr.get("recall_warnings") or [])
    for pg in ocr.get("pages") or []:
        pn = pg.get("page_no")
        for c in pg.get("comments") or []:
            c = dict(c)
            c.setdefault("page_no", pn)
            comments.append(c)
        sheets.append({"page_no": pn, "title_block": pg.get("title_block") or {}, "regions": pg.get("regions") or [],
                       "regions_from_model": pg.get("regions_from_model")})
    return {"kind": "ocr", "review_id": os.path.basename(folder), "created": os.path.getmtime(op),
            "pdfs": [ocr.get("pdf")] if ocr.get("pdf") else [], "out_dir": ocr.get("out_dir") or folder,
            "sheets": sheets, "comments": comments, "located": [], "sequences": [], "actions": [],
            "unmatched_pages": [], "recall_warnings": warnings, "errors": [], "mode": None,
            "counts": {"comments": len(comments), "model_errors": ocr.get("model_errors", 0)}, "settings": {}, "vocabulary": {}}


def vocabulary(folder: str) -> list:
    p = os.path.join(folder, "tools.txt")
    names = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln.startswith("- "):
                    names.append(ln[2:].split(":", 1)[0].strip())
    except OSError:
        return []
    return names


def _file_url(review_id: str, rel: str) -> str:
    return f"/redline/reviews/{urllib.parse.quote(review_id, safe='/')}/file/{urllib.parse.quote(rel.replace(os.sep, '/'), safe='/')}"


def images(folder: str, report: dict, review_id: str) -> dict:
    out = {"pages": {}, "crops": {}, "regions": {}, "duplicates": []}
    pages_dir = os.path.join(folder, "pages")
    if os.path.isdir(pages_dir):
        for name in sorted(os.listdir(pages_dir)):
            if not name.lower().endswith(".png") or "_p" not in name:
                continue
            try:
                pn = int(name.rsplit("_p", 1)[1].split(".")[0])
            except ValueError:
                continue
            key = str(pn)
            if key in out["pages"]:
                out["duplicates"].append(name)
                continue
            size = png_size(os.path.join(pages_dir, name)) or (None, None)
            out["pages"][key] = {"url": _file_url(review_id, f"pages/{name}"), "width_px": size[0], "height_px": size[1], "file": name}
    crops_dir = os.path.join(folder, "crops")
    if os.path.isdir(crops_dir):
        have = set(os.listdir(crops_dir))
        for c in report.get("comments") or []:
            cp = str(c.get("crop_path") or "")
            base = os.path.basename(cp.replace("/", os.sep)) if cp else ""
            if base and base in have:
                out["crops"][c["id"]] = _file_url(review_id, f"crops/{base}")
        for name in have:
            if name.endswith("_regions.png") and name.startswith("p"):
                try:
                    pn = int(name[1:].split("_", 1)[0])
                except ValueError:
                    continue
                out["regions"][str(pn)] = _file_url(review_id, f"crops/{name}")
    return out


def _tools_block(folder: str) -> dict:
    names = vocabulary(folder) or sorted(set().union(*tools.BINS.values()))
    descriptions = {}
    p = os.path.join(folder, "tools.txt")
    try:
        with open(p, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln.startswith("- ") and ":" in ln:
                    n, d = ln[2:].split(":", 1)
                    descriptions[n.strip()] = d.strip()
    except OSError:
        pass
    return {"names": names, "bins": {n: tools.bin_of(n) for n in names}, "risks": {n: tools.risk_of(n) for n in names},
            "bin_order": list(tools.BIN_ORDER), "tier_order": list(tools.TIER_ORDER), "labels": dict(tools.BIN_LABELS),
            "descriptions": descriptions}


def _stamp_bins(report: dict) -> None:
    for a in report.get("actions") or []:
        for s in a.get("steps") or []:
            if not s.get("bin"):
                s["bin"] = tools.bin_of(s.get("tool"))
            if not s.get("risk"):
                s["risk"] = tools.risk_of(s.get("tool"))


def payload(review_root: str, review_id: str) -> dict | None:
    from . import metrics
    folder = resolve(review_root, review_id)
    if folder is None:
        return None
    report = load_report(folder)
    if report is None:
        return None
    _stamp_bins(report)
    labs = labels_mod.load(folder)
    total = len(report.get("comments") or [])
    return {"id": str(review_id).replace("\\", "/"), "kind": report.get("kind", "review"), "report": report,
            "labels": labs.get("comments") or {}, "counts": labels_mod.counts(labs, total),
            "metrics": metrics.review_metrics(report, labs.get("comments") or {}),
            "images": images(folder, report, str(review_id).replace("\\", "/")), "tools": _tools_block(folder),
            "review_dir": review_root, "folder": folder, "whoami": labels_mod.whoami()}


def aggregate_payload(review_root: str, limit: int = 50) -> dict:
    from . import metrics
    runs = []
    stamp = []
    for rid, folder, kind in sorted(_folders(review_root), key=lambda t: -os.path.getmtime(t[1]))[:limit]:
        if kind != "review":
            continue
        stamp.append((folder, os.path.getmtime(os.path.join(folder, "report.json")),
                      os.path.getmtime(labels_mod.path(folder)) if os.path.isfile(labels_mod.path(folder)) else 0))
    key = (review_root, tuple(stamp))
    if key in _AGG_CACHE:
        return _AGG_CACHE[key]
    for folder, _, _ in stamp:
        report = load_report(folder)
        if report is None:
            continue
        _stamp_bins(report)
        runs.append((report, labels_mod.load(folder).get("comments") or {}))
    out = metrics.aggregate(runs)
    out["review_dir"] = review_root
    _AGG_CACHE.clear()
    _AGG_CACHE[key] = out
    return out


# ── the window ───────────────────────────────────────────────────────────────

def is_enabled(settings, no_view_flag: bool = False) -> bool:
    if no_view_flag:
        return False
    env = os.environ.get(VIEW_ENV)
    if env is not None and env.strip().lower() in ("0", "false", "no", "off", ""):
        return False
    return bool(getattr(settings, "view", True))


def url_for(base: str, review_id: str) -> str:
    return f"{str(base).rstrip('/')}{PAGE}?review={urllib.parse.quote(str(review_id).replace(chr(92), '/'), safe='')}"


def _get(url: str, timeout: float = 2.0):
    """(status, body) or (None, error)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:
        return None, str(e)


def _launcher():
    if str(config.ROOT) not in sys.path:
        sys.path.insert(0, str(config.ROOT))
    import launcher
    return launcher


def ensure_dashboard(base: str, log=print, probe: str = "/redline/reviews") -> str | None:
    """None when a dashboard that answers `probe` (the review page's route by
    default) is up at `base`; otherwise the reason it could not be made to.
    Starts or restarts a server only when `base` is the one the launcher is
    configured for."""
    base = str(base).rstrip("/")
    try:
        launcher = _launcher()
        mine = urllib.parse.urlparse(base).port == launcher.dashboard_port()
    except Exception as e:
        launcher, mine = None, False
        log(f"  (launcher not importable: {e})")
    status, _ = _get(f"{base}/version")
    if status == 200:
        got, _ = _get(f"{base}{probe}")
        if got == 200:
            return None
        if got == 404 and launcher is not None and mine:
            pid = launcher._read_pid("dashboard")
            if launcher._alive(pid):
                log("  the running dashboard is an older version; restarting it")
                launcher._kill(pid)
                time.sleep(1.0)
                os.makedirs(launcher.run_dir(), exist_ok=True)
                os.makedirs(launcher.log_dir(), exist_ok=True)
                launcher.start_dashboard()
                got, _ = _get(f"{base}{probe}")
                return None if got == 200 else f"the restarted dashboard at {base} still does not serve {probe}"
        return (f"the dashboard at {base} is an older version without this page; "
                "run: python launcher.py stop  then  python launcher.py start --no-browser")
    if launcher is None or not mine:
        return f"no dashboard at {base}" + (f" (the launcher is configured for {launcher.dashboard_url()})" if launcher else "")
    try:
        os.makedirs(launcher.run_dir(), exist_ok=True)
        os.makedirs(launcher.log_dir(), exist_ok=True)
        note = launcher.start_dashboard()
        log(f"  dashboard: {note}")
    except Exception as e:
        return f"could not start the dashboard at {base}: {e}"
    got, _ = _get(f"{base}{probe}")
    return None if got == 200 else f"the dashboard at {base} did not come up with {probe}"


def find_edge() -> str | None:
    import shutil
    found = shutil.which("msedge")
    if found:
        return found
    for root in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if root:
            p = os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.isfile(p):
                return p
    return None


def open_window(url: str, dry: bool = False, log=print):
    """Opens the page in its own window: an Edge app window on Windows,
    else the default browser, else prints the URL. dry=True returns the
    command that would run (or None) without running anything."""
    edge = find_edge() if sys.platform == "win32" else None
    cmd = [edge, f"--app={url}", "--new-window", "--window-size=1480,960"] if edge else None
    if dry:
        return cmd
    if cmd:
        try:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags, close_fds=True)
            return "edge"
        except OSError as e:
            log(f"  (Edge did not start: {e})")
    try:
        if webbrowser.open(url, new=1):
            return "browser"
    except Exception:
        pass
    log(f"open this in a browser: {url}")
    return "none"


def show(settings, review_id: str, no_view_flag: bool = False, log=print) -> bool:
    """The command line's exit: make sure the dashboard serves the page,
    open the window on the run. Never raises; returns whether it opened."""
    if not is_enabled(settings, no_view_flag):
        return False
    try:
        base = str(settings.dashboard_base).rstrip("/")
        reason = ensure_dashboard(base, log=log)
        url = url_for(base, review_id)
        if reason:
            log(f"view: {reason}\n      the run is at {url}")
            return False
        how = open_window(url, log=log)
        log(f"view: {url}" + ("" if how != "none" else "  (no browser could be started)"))
        return how != "none"
    except Exception as e:
        log(f"view: could not open the results window ({e})")
        return False
