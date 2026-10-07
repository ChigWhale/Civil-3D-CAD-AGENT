"""Command line: two commands, nothing else.

    python -m redline transcribe scan.pdf [--page N]
        Read the handwriting and the title strips only. Nothing is opened or
        run. Output: <review dir>/ocr-only/<timestamp>/ocr.json plus the crops
        the model read and a regions overlay per page.

    python -m redline review scan.pdf [more.pdf ...] [--text "COMMENT" ...]
        The full run for handwritten redlines: read every comment, then ask
        the language model for the ordered TOOL NAMES that would address it.
        Text in, tool names out; no parameters, nothing opened or drawn, no
        comment skipped. --text adds a typed comment (repeatable) and works
        with no PDF at all, so the model can be tried without a scan.
        Output: <review dir>/<review id>/report.md, report.json, training.jsonl,
        tools.txt (the names the model could choose from).

    python -m redline markups marked.pdf [more.pdf ...]
        The same run for Bluebeam text boxes and callouts (FreeText
        annotations) instead of handwriting. No vision model; the text,
        author and leader come straight from the PDF. Other markup kinds
        (clouds, arrows, pen, sticky notes) are counted and ignored. A
        flattened PDF has no annotations and reports zero text boxes.

`ocr` is accepted as another name for `transcribe`; `bluebeam` for `markups`.

Both commands first check that the model server answers and that the needed
models are present, and stop with one line saying what is missing when they
are not (skipped when the fake backends are selected).

Common flags: --ocr openai|fake, --planner openai|fake, --fixture file.json (for fake),
--project-root DIR, --model-base URL, --dpi N, --vlm NAME, --llm NAME, --review-dir DIR.
Environment variables in redline/config.py set the same things.
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
    ap = argparse.ArgumentParser(prog="python -m redline", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("transcribe", aliases=["ocr"], help="read the handwriting and title strips only; nothing opened or run")
    p.add_argument("pdf")
    p.add_argument("--page", type=int, action="append", help="only these pages (repeatable)")
    _common(p)

    p = sub.add_parser("review", help="handwritten redlines: read every comment, then name the tools that would address it")
    p.add_argument("pdfs", nargs="*", help="scanned redline PDFs")
    p.add_argument("--text", action="append", help="a typed comment to sequence (repeatable; no PDF needed)")
    p.add_argument("--page", type=int, action="append", help="only these pages (repeatable)")
    p.add_argument("--review-id", dest="review_id", help="folder name under the review dir (default: a timestamp)")
    _common(p)

    p = sub.add_parser("markups", aliases=["bluebeam"],
                       help="Bluebeam text boxes and callouts: read their text from the PDF, then name the tools")
    p.add_argument("pdfs", nargs="+", help="PDFs marked up in Bluebeam (not flattened)")
    p.add_argument("--page", type=int, action="append", help="only these pages (repeatable)")
    p.add_argument("--review-id", dest="review_id", help="folder name under the review dir (default: a timestamp)")
    _common(p)

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    if args.cmd == "bluebeam":
        args.cmd = "markups"
    if args.cmd == "review" and not args.pdfs and not args.text:
        ap.error("review needs at least one PDF or one --text comment")

    settings = _apply_flags(args)
    _isolate_learned_file(settings)
    need_vlm = args.cmd == "transcribe" or args.cmd == "ocr" or (args.cmd == "review" and bool(args.pdfs))
    problem = _preflight(settings, need_vlm=need_vlm)
    if problem:
        print(problem)
        return 1

    from . import pipeline
    if args.cmd in ("review", "markups"):
        if args.cmd == "review":
            rep = pipeline.review(args.pdfs, texts=args.text, settings=settings, pages=args.page, review_id=args.review_id)
            echo = bool(args.text)
        else:
            rep = pipeline.review_markups(args.pdfs, settings=settings, pages=args.page, review_id=args.review_id)
            echo = True
        if echo:
            comments = {c["id"]: c for c in rep.comments}
            for s in rep.sequences:
                c = comments.get(s["comment_id"]) or {}
                shown = " -> ".join(s["tools"]) if s["tools"] else f"(none: {s.get('error')})"
                print(f"{s['comment_id']} {c.get('text', '')!r}: {shown}")
        for e in rep.errors:
            print(f"ERROR: {e}")
        print(json.dumps(rep.counts))
        print(f"report: {os.path.join(rep.out_dir, 'report.md')}")
        return 0 if not rep.errors else 1
    # transcribe / ocr
    out = pipeline.ocr_only(args.pdf, settings, pages=args.page)
    path = os.path.join(out["out_dir"], "ocr.json")
    os.makedirs(out["out_dir"], exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"written {path}")
    return 1 if out.get("model_errors") else 0


def _preflight(settings, need_vlm: bool = True) -> str | None:
    """One line naming what is missing before any page is read, or None.
    Only the model side is checked. A review of typed comments needs the
    language model alone."""
    need = []
    if need_vlm and settings.ocr_backend != "fake":
        need.append(settings.vlm)
    if settings.planner_backend != "fake":
        need.append(settings.llm)
    if not need:
        return None
    from . import openai_compat
    if not openai_compat.is_up(settings.model_base):
        return (f"No model server at {settings.model_base}. Install Ollama (ollama.com/download) and make sure it "
                "is running, then try again.")
    have = openai_compat.models(settings.model_base)
    missing = [n for n in need if n not in have and (n + ":latest") not in have]
    if missing:
        return "Missing model(s): " + ", ".join(missing) + ". Pull them with: " + " ; ".join(f"ollama pull {n}" for n in missing)
    return None


def _isolate_learned_file(settings) -> None:
    """A run aimed at the stub plugin (CAD_PLUGIN_BASE set) must not write the
    stub's made-up layer families into the repo's layer_tags.learned.json;
    the dashboard server keeps the same rule. The file goes beside the reviews."""
    if not os.environ.get("CAD_PLUGIN_BASE"):
        return
    try:
        import layer_words
        import spatial
        os.makedirs(settings.review_dir, exist_ok=True)
        spatial.TAGS.learned = layer_words.Learned(os.path.join(settings.review_dir, "layer_tags.learned.json"))
        spatial.TAGS._bump()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
