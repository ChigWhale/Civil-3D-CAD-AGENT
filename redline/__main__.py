"""Command line.

    python -m redline review a.pdf [b.pdf ...] [--dry-run] [--on-conflict pause|warn]
    python -m redline phase0 a.pdf --page 1 --click X,Y
    python -m redline ocr a.pdf [--page N]
    python -m redline annotate <review_id> <comment_id> [--chain chain.json] [--verdict good|bad]
    python -m redline palette            # what the planner is shown
    python -m redline doctor             # is everything reachable

Common flags: --ocr openai|fake, --planner openai|fake, --fixture file.json (for fake),
--project-root DIR, --model-base URL. Environment variables in redline/config.py
set the same things.
"""
import argparse
import json
import os
import sys

from . import config


def _apply_flags(args):
    m = {"ocr": "CAD_REDLINE_OCR", "planner": "CAD_REDLINE_PLANNER", "fixture": "CAD_REDLINE_FIXTURE",
         "project_root": "CAD_PROJECT_ROOT", "model_base": "CAD_REDLINE_MODEL_BASE", "dpi": "CAD_REDLINE_DPI",
         "vlm": "CAD_REDLINE_VLM", "llm": "CAD_REDLINE_LLM", "review_dir": "CAD_REDLINE_DIR"}
    for attr, env in m.items():
        val = getattr(args, attr, None)
        if val not in (None, ""):
            os.environ[env] = str(val)
    return config.load()


def _common(p):
    p.add_argument("--ocr", choices=["openai", "fake"])
    p.add_argument("--planner", choices=["openai", "fake"])
    p.add_argument("--fixture", help="fixture JSON for the fake OCR backend")
    p.add_argument("--project-root", dest="project_root", help="folder(s) searched for .dwg files (';' separated)")
    p.add_argument("--model-base", dest="model_base", help="OpenAI-compatible endpoint, default Ollama's /v1")
    p.add_argument("--dpi", type=int)
    p.add_argument("--vlm")
    p.add_argument("--llm")
    p.add_argument("--review-dir", dest="review_dir")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m redline", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("review", help="read, locate, plan and run the comments on redlined sheets")
    p.add_argument("pdfs", nargs="+")
    p.add_argument("--dry-run", action="store_true", help="plan but do not run")
    p.add_argument("--on-conflict", dest="on_conflict", choices=["pause", "warn"])
    p.add_argument("--page", type=int, action="append", help="only these pages (repeatable)")
    p.add_argument("--review-id", dest="review_id", help="folder name under the review dir (default: a timestamp)")
    _common(p)

    p = sub.add_parser("phase0", help="one page pixel through the transform, with evidence")
    p.add_argument("pdf")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--click", required=True, help="X,Y page pixel at the page's render DPI")
    _common(p)

    p = sub.add_parser("ocr", help="regions and transcriptions only; nothing opened or run")
    p.add_argument("pdf")
    p.add_argument("--page", type=int, action="append")
    _common(p)

    p = sub.add_parser("annotate", help="record a person's chain / verdict for one comment")
    p.add_argument("review_id")
    p.add_argument("comment_id")
    p.add_argument("--chain", help="JSON file with the blocks a person would run")
    p.add_argument("--verdict", choices=["good", "bad"])
    _common(p)

    p = sub.add_parser("palette", help="the block palette text the planner is shown")
    p = sub.add_parser("doctor", help="check the plugin, dashboard and model server")
    _common(p)

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2

    if args.cmd == "palette":
        from . import palette
        print(palette.palette_text())
        print(f"\n# {len(palette.compact_specs())} blocks, version {palette.palette_version()}")
        return 0

    settings = _apply_flags(args)

    if args.cmd == "doctor":
        return _doctor(settings)

    from . import pipeline
    if args.cmd == "review":
        rep = pipeline.review(args.pdfs, settings, dry_run=args.dry_run, on_conflict=args.on_conflict, pages=args.page,
                              review_id=args.review_id)
        print(json.dumps(rep.counts))
        return 0 if not rep.errors else 1
    if args.cmd == "phase0":
        x, y = (float(v) for v in args.click.split(","))
        out = pipeline.phase0(args.pdf, args.page, x, y, settings)
        print(json.dumps(out, indent=2, default=str))
        return 0 if "error" not in out else 1
    if args.cmd == "ocr":
        out = pipeline.ocr_only(args.pdf, settings, pages=args.page)
        path = os.path.join(out["out_dir"], "ocr.json")
        os.makedirs(out["out_dir"], exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, default=str)
        print(f"written {path}")
        return 0
    if args.cmd == "annotate":
        from . import report
        row = report.annotate(settings.review_dir, args.review_id, args.comment_id, args.chain, args.verdict)
        print(json.dumps({"comment": row["comment"]["id"], "human_verdict": row.get("human_verdict"),
                          "human_chain": row.get("human_chain")}, default=str))
        return 0
    return 2


def _doctor(settings) -> int:
    import requests
    from . import openai_compat
    ok = True

    def line(name, good, detail):
        nonlocal ok
        ok = ok and good
        print(("OK   " if good else "MISSING ") + f"{name}: {detail}")

    try:
        r = requests.get(f"{settings.plugin_base}/documents", timeout=3)
        docs = [d.get("path") for d in r.json().get("drawings") or []]
        line("plugin", True, f"{settings.plugin_base}, {len(docs)} drawing(s) open")
    except Exception as e:
        line("plugin", False, f"{settings.plugin_base} ({e.__class__.__name__}); open Civil 3D with the CADAgent plugin loaded")
    try:
        r = requests.get(f"{settings.dashboard_base}/version", timeout=3)
        if r.status_code == 200:
            line("dashboard", True, f"{settings.dashboard_base} {r.json()}")
        else:
            line("dashboard", False, f"{settings.dashboard_base} answers but has no /version route: an old "
                                     f"server is running on that port; stop it and start the current one")
    except Exception as e:
        line("dashboard", False, f"{settings.dashboard_base} ({e.__class__.__name__}); start it: python -m uvicorn dashboard.server:app --port 8765")
    if settings.ocr_backend == "fake" and settings.planner_backend == "fake":
        line("models", True, "fake backends selected")
    else:
        up = openai_compat.is_up(settings.model_base)
        have = openai_compat.models(settings.model_base) if up else []
        line("model server", up, f"{settings.model_base}" + ("" if up else "; install Ollama and run `ollama serve`"))
        for label, name in (("vision model", settings.vlm), ("planner model", settings.llm)):
            found = name in have or (name + ":latest") in have
            line(label, found, name + ("" if found else f"; pull it: ollama pull {name}"))
        # Ollama reports the context window of whatever is loaded right now.
        # 4096 (its default) is too small for a crop plus the prompt.
        try:
            r = requests.get(settings.model_base.removesuffix("/v1") + "/api/ps", timeout=3)
            loaded = [(m.get("name"), int(m.get("context_length") or 0)) for m in r.json().get("models") or []]
        except Exception:
            loaded = []
        if loaded:
            small = [n for n, c in loaded if c and c < 8192]
            line("context window", not small,
                 ", ".join(f"{n} {c}" for n, c in loaded)
                 + ("" if not small else "; set OLLAMA_CONTEXT_LENGTH=16384 (user variable) and restart Ollama"))
        else:
            print("     context window: no model loaded yet (checked after the first run)")
    try:
        from . import pdf as pdf_mod
        line("pdf renderer", True, pdf_mod.backend().name)
    except RuntimeError as e:
        line("pdf renderer", False, str(e))
    line("project roots", bool(settings.project_roots), "; ".join(settings.project_roots) or "CAD_PROJECT_ROOT unset (only open drawings and printed paths are searched)")
    line("review dir", True, settings.review_dir)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
