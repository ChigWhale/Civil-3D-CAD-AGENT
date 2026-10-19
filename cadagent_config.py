"""One configuration for every CAD-Agent process.

Order of precedence for a setting: the environment variable, then
`cad-agent.json`, then the default the caller passes. Developers keep
setting variables as before; a packaged install ships a `cad-agent.json`
and sets nothing in the environment (Claude Desktop strips it anyway).

Where the file is looked for, first hit wins:
    CAD_AGENT_CONFIG            an explicit path
    <this folder>\\cad-agent.json           the repo root, or app\\ in a package
    <this folder>\\..\\cad-agent.json       the package root (app\\ sits under it)

Two kinds of folder:
    program root   where this file is: the code and the read-only data
                   (layer_tags.json, playbooks/, standards/, Layout Templates/).
    data dir       everything a process writes: dashboard state, learned layer
                   families, exemplars, fits, reviews, logs. In development it
                   is unset and each module keeps its old repo-relative
                   default; a package sets it to %LOCALAPPDATA%\\CAD-Agent.
                   Folders the user edits (playbooks, standards) are seeded
                   there from the program root on first use, so the program
                   folder can stay read-only.
"""
import json
import os
import shutil
import threading

PROGRAM_ROOT = os.path.dirname(os.path.abspath(__file__))

# setting name -> (environment variable, default)
KEYS = {
    "plugin_base": ("CAD_PLUGIN_BASE", "http://127.0.0.1:5199"),
    "dashboard_base": ("CAD_DASHBOARD_BASE", "http://127.0.0.1:8765"),
    "dashboard_state": ("CAD_DASHBOARD_STATE", None),
    "data_dir": ("CAD_DATA_DIR", None),
    "exemplar_dir": ("CAD_EXEMPLAR_DIR", None),
    "learn_dir": ("CAD_LEARN_DIR", None),
    "standards_dir": ("CAD_STANDARDS_DIR", None),
    "playbooks_dir": ("CAD_PLAYBOOKS_DIR", None),
    "project_roots": ("CAD_PROJECT_ROOT", None),
    "review_dir": ("CAD_REDLINE_DIR", None),
    "model_base": ("CAD_REDLINE_MODEL_BASE", "http://127.0.0.1:11434/v1"),
    "vlm": ("CAD_REDLINE_VLM", "qwen3-vl:4b-instruct"),
    "llm": ("CAD_REDLINE_LLM", "qwen2.5-coder:7b"),
    # the dashboard prompt block's model; unset = llm. The slot a fine-tuned model drops into.
    "prompt_llm": ("CAD_PROMPT_LLM", None),
    "models_dir": ("CAD_MODELS_DIR", None),
    "python": ("CAD_PYTHON", None),
    "packaged": (None, False),
    "version": (None, None),
}

_lock = threading.Lock()
_cache = {"path": None, "mtime": None, "data": {}}


def config_path() -> str | None:
    explicit = os.environ.get("CAD_AGENT_CONFIG", "").strip()
    candidates = [explicit] if explicit else []
    candidates += [os.path.join(PROGRAM_ROOT, "cad-agent.json"),
                   os.path.join(os.path.dirname(PROGRAM_ROOT), "cad-agent.json")]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def load(force: bool = False) -> dict:
    """The file's contents (empty when there is none), re-read when it changes."""
    path = config_path()
    with _lock:
        if path is None:
            _cache.update(path=None, mtime=None, data={})
            return {}
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        if not force and _cache["path"] == path and _cache["mtime"] == mtime:
            return dict(_cache["data"])
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        _cache.update(path=path, mtime=mtime, data=data)
        return dict(data)


def get(key: str, default=None):
    """Environment first, then the file, then `default` (or KEYS' default)."""
    env, key_default = KEYS.get(key, (None, None))
    if env:
        val = os.environ.get(env)
        if val is not None and val != "":
            return val
    data = load()
    if key in data and data[key] not in (None, ""):
        return data[key]
    return default if default is not None else key_default


def packaged() -> bool:
    return bool(get("packaged", False))


def program_root() -> str:
    return PROGRAM_ROOT


def package_root() -> str | None:
    """The folder above app\\ in a package (holds python\\, models\\, cad-agent.json)."""
    path = config_path()
    return os.path.dirname(path) if path else None


def data_dir(create: bool = True) -> str | None:
    """Where processes write, or None in development (modules keep their old
    defaults). A package sets `data_dir`; `%LOCALAPPDATA%` and friends expand."""
    raw = get("data_dir")
    if not raw:
        return None
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(str(raw))))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def writable(name: str, dev_default: str, create: bool = True) -> str:
    """A writable folder or file path: <data dir>\\name when a data dir is set,
    else the development default the caller has always used."""
    base = data_dir(create=create)
    if base is None:
        return dev_default
    path = os.path.join(base, name)
    if create and not os.path.splitext(name)[1]:
        os.makedirs(path, exist_ok=True)
    return path


def seeded(name: str, dev_default: str) -> str:
    """A folder the user edits (playbooks, standards): in development the
    repo's own; with a data dir, <data dir>\\name, copied from the program
    root the first time so the program folder stays read-only."""
    base = data_dir()
    if base is None:
        return dev_default
    target = os.path.join(base, name)
    if not os.path.isdir(target):
        source = os.path.join(PROGRAM_ROOT, name)
        if os.path.isdir(source):
            shutil.copytree(source, target)
        else:
            os.makedirs(target, exist_ok=True)
    return target


def folder(key: str, name: str, dev_default: str | None = None, seed: bool = False) -> str | None:
    """A folder setting: its own key (env or file) wins; else <data dir>\\name;
    else dev_default (None means 'unset', which callers may treat as an error)."""
    explicit = get(key)
    if explicit:
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(str(explicit))))
        os.makedirs(path, exist_ok=True)
        return path
    if data_dir(create=False) is None:
        return dev_default
    return seeded(name, dev_default or "") if seed else writable(name, dev_default or "")


def project_roots() -> list:
    raw = get("project_roots")
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        parts = [str(p) for p in raw]
    else:
        parts = str(raw).split(";")
    return [os.path.expandvars(p.strip()) for p in parts if p.strip()]


def describe() -> dict:
    """What every process would resolve right now; for `doctor` commands."""
    return {"config_path": config_path(), "packaged": packaged(), "program_root": PROGRAM_ROOT,
            "package_root": package_root(), "data_dir": data_dir(create=False),
            **{k: get(k) for k in ("plugin_base", "dashboard_base", "model_base", "vlm", "llm", "prompt_llm", "version")},
            "project_roots": project_roots()}
