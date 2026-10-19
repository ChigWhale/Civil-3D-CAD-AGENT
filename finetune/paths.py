"""Folders, stamps and small file helpers for the fine-tune package. Stdlib
only: __main__ imports this before deciding which interpreter runs the rest.

    <finetune dir>/            CAD_FINETUNE_DIR, else the data dir's "finetune", else dashboard/uploads/finetune
      data/                    train.jsonl, holdout.jsonl, rows.jsonl, tools.txt, manifest.json
      runs/<stamp>/            run.json, train_log.jsonl, adapter/, merged/, gguf/, Modelfile
      runs/latest.json         {"run": "<stamp>"}
      eval/<dataset_id>/       <llm>.json, <llm>.md, compare.md
      hf-cache/                HF_HOME for the base model download
      tools/llama.cpp-<tag>/   the converter source tree
    .venv-finetune/            CAD_FINETUNE_VENV, else beside the repo root
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENV_DIR = "CAD_FINETUNE_DIR"
ENV_VENV = "CAD_FINETUNE_VENV"
ENV_HF_HOME = "CAD_FINETUNE_HF_HOME"
ENV_INSIDE = "CAD_FINETUNE_INSIDE"
VENV_NAME = ".venv-finetune"
MARKER = "finetune-setup.json"


def finetune_dir(create: bool = True) -> str:
    env = os.environ.get(ENV_DIR, "").strip()
    if env:
        path = os.path.abspath(os.path.expandvars(env))
    else:
        import cadagent_config
        path = cadagent_config.writable("finetune", str(ROOT / "dashboard" / "uploads" / "finetune"), create=create)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def data_dir(create: bool = True) -> str:
    return _sub("data", create)


def runs_dir(create: bool = True) -> str:
    return _sub("runs", create)


def eval_dir(create: bool = True) -> str:
    return _sub("eval", create)


def tools_dir(create: bool = True) -> str:
    return _sub("tools", create)


def hf_home(create: bool = True) -> str:
    env = os.environ.get(ENV_HF_HOME, "").strip()
    if env:
        return os.path.abspath(os.path.expandvars(env))
    return _sub("hf-cache", create)


def _sub(name: str, create: bool) -> str:
    p = os.path.join(finetune_dir(create), name)
    if create:
        os.makedirs(p, exist_ok=True)
    return p


def venv_dir() -> str:
    env = os.environ.get(ENV_VENV, "").strip()
    return os.path.abspath(os.path.expandvars(env)) if env else str(ROOT / VENV_NAME)


def venv_python() -> str:
    d = venv_dir()
    if sys.platform == "win32":
        return os.path.join(d, "Scripts", "python.exe")
    return os.path.join(d, "bin", "python")


def venv_marker() -> str:
    return os.path.join(venv_dir(), MARKER)


def inside_venv() -> bool:
    return os.environ.get(ENV_INSIDE) == "1"


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def run_dir(run_id: str, create: bool = False) -> str:
    p = os.path.join(runs_dir(create), run_id)
    if create:
        os.makedirs(p, exist_ok=True)
    return p


def latest_run() -> str | None:
    data = read_json(os.path.join(runs_dir(False), "latest.json"))
    rid = (data or {}).get("run")
    if rid and os.path.isdir(run_dir(rid)):
        return rid
    runs = list_runs()
    return runs[-1] if runs else None


def set_latest_run(run_id: str) -> None:
    write_json(os.path.join(runs_dir(), "latest.json"), {"run": run_id, "updated": time.time()})


def list_runs() -> list:
    d = runs_dir(False)
    if not os.path.isdir(d):
        return []
    return sorted(n for n in os.listdir(d) if os.path.isfile(os.path.join(d, n, "run.json")))


def resolve_run(run_id: str | None) -> str | None:
    """A run stamp, or the latest when None/"latest"; None when there is none."""
    if run_id in (None, "", "latest"):
        return latest_run()
    return run_id if os.path.isfile(os.path.join(run_dir(run_id), "run.json")) else None


# ── files ────────────────────────────────────────────────────────────────────

def read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def read_jsonl(path: str) -> list:
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    out.append(json.loads(ln))
    except OSError:
        return []
    return out


def write_jsonl(path: str, rows: list) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
    os.replace(tmp, path)
    return len(rows)


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def sha1_text(text: str, n: int = 12) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:n]


def sha1_file(path: str) -> str | None:
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def safe_name(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in str(s)) or "model"
