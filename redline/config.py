"""Settings for the redline pipeline.

Order of precedence: environment variable, then a `redline.json` beside the
repo root (or the file CAD_REDLINE_CONFIG names), then the default here. The
JSON file exists for a packaged install with no environment to set; a
developer just sets variables, as the rest of the repo does.

The review folder follows the dashboard's rule: a process aimed at a stub
plugin (CAD_PLUGIN_BASE set) keeps its output under dashboard/uploads-test,
never beside the live server's runs.
"""
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_ENV = {
    "plugin_base": ("CAD_PLUGIN_BASE", "http://127.0.0.1:5199"),
    "dashboard_base": ("CAD_DASHBOARD_BASE", "http://127.0.0.1:8765"),
    "project_roots": ("CAD_PROJECT_ROOT", ""),
    "review_dir": ("CAD_REDLINE_DIR", ""),
    "ocr_backend": ("CAD_REDLINE_OCR", "openai"),
    "planner_backend": ("CAD_REDLINE_PLANNER", "openai"),
    "model_base": ("CAD_REDLINE_MODEL_BASE", "http://127.0.0.1:11434/v1"),
    "vlm": ("CAD_REDLINE_VLM", "qwen3-vl:4b-instruct"),
    "llm": ("CAD_REDLINE_LLM", "qwen2.5-coder:7b"),
    "dpi": ("CAD_REDLINE_DPI", "150"),
    "crop_dpi": ("CAD_REDLINE_CROP_DPI", "300"),
    # the title strips are long and thin; they are read at this DPI in tiles
    # no longer than max_image_px on a side, so printed 9 pt text stays legible
    # without a single crop swallowing the model's context window.
    "title_dpi": ("CAD_REDLINE_TITLE_DPI", "150"),
    "max_image_px": ("CAD_REDLINE_MAX_IMAGE_PX", "1280"),
    # fractions of the page (origin top-left) that hold the title block text;
    # several rectangles separated by ';'. Fractions, not inches, so a scan
    # that was shrunk to fit a smaller page still crops the right strip.
    "title_rects": ("CAD_REDLINE_TITLE_RECT", "0.84,0,1,1;0,0.9,1,1"),
    # paper-inch rectangle of the plotted border [x0,y0,x1,y1]; when set, the
    # border found in the scan is fitted to it and the page->paper transform
    # is refined. Unset = trust the page size.
    "border_rect": ("CAD_REDLINE_BORDER_RECT", ""),
    "min_confidence": ("CAD_REDLINE_MIN_CONFIDENCE", "0.5"),
    "context_radius": ("CAD_REDLINE_CONTEXT_RADIUS", "100"),
    "model_timeout": ("CAD_REDLINE_MODEL_TIMEOUT", "600"),
    "on_conflict": ("CAD_REDLINE_ON_CONFLICT", "pause"),
    "fixture": ("CAD_REDLINE_FIXTURE", ""),
}


def _file_settings() -> dict:
    path = os.environ.get("CAD_REDLINE_CONFIG") or str(ROOT / "redline.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _rects(raw: str) -> list:
    out = []
    for part in str(raw or "").split(";"):
        nums = [p for p in part.replace(" ", "").split(",") if p]
        if len(nums) == 4:
            try:
                out.append(tuple(float(n) for n in nums))
            except ValueError:
                pass
    return out


@dataclass
class Settings:
    plugin_base: str
    dashboard_base: str
    project_roots: list
    review_dir: str
    ocr_backend: str
    planner_backend: str
    model_base: str
    vlm: str
    llm: str
    dpi: int
    crop_dpi: int
    title_dpi: int
    max_image_px: int
    title_rects: list
    border_rect: tuple | None
    min_confidence: float
    context_radius: float
    model_timeout: float
    on_conflict: str
    fixture: str
    raw: dict = field(default_factory=dict)


def load() -> Settings:
    """Environment, then redline.json, then the shared cad-agent.json
    (cadagent_config), then the defaults above."""
    import cadagent_config
    file_cfg = _file_settings()
    shared = cadagent_config.load()
    raw = {}
    for key, (env, default) in _ENV.items():
        val = os.environ.get(env)
        if val is None or val == "":
            val = file_cfg.get(key)
        if val is None or val == "":
            val = shared.get(key)
        if val is None or val == "":
            val = default
        raw[key] = val
    review_dir = str(raw["review_dir"] or "").strip()
    if not review_dir:
        sub = "uploads-test" if os.environ.get("CAD_PLUGIN_BASE") else "uploads"
        review_dir = cadagent_config.writable("reviews", str(ROOT / "dashboard" / sub / "reviews"))
    if isinstance(raw["project_roots"], (list, tuple)):
        roots = [os.path.expandvars(str(r).strip()) for r in raw["project_roots"] if str(r).strip()]
    else:
        roots = [os.path.expandvars(r.strip()) for r in str(raw["project_roots"] or "").split(";") if r.strip()]
    border = _rects(raw["border_rect"])
    return Settings(
        plugin_base=str(raw["plugin_base"]).rstrip("/"),
        dashboard_base=str(raw["dashboard_base"]).rstrip("/"),
        project_roots=roots,
        review_dir=review_dir,
        ocr_backend=str(raw["ocr_backend"]).strip().lower(),
        planner_backend=str(raw["planner_backend"]).strip().lower(),
        model_base=str(raw["model_base"]).rstrip("/"),
        vlm=str(raw["vlm"]),
        llm=str(raw["llm"]),
        dpi=int(float(raw["dpi"])),
        crop_dpi=int(float(raw["crop_dpi"])),
        title_dpi=int(float(raw["title_dpi"])),
        max_image_px=int(float(raw["max_image_px"])),
        title_rects=_rects(raw["title_rects"]) or [(0.84, 0, 1, 1), (0, 0.9, 1, 1)],
        border_rect=border[0] if border else None,
        min_confidence=float(raw["min_confidence"]),
        context_radius=float(raw["context_radius"]),
        model_timeout=float(raw["model_timeout"]),
        on_conflict=str(raw["on_conflict"]).strip().lower() or "pause",
        fixture=str(raw["fixture"] or ""),
        raw=raw,
    )
