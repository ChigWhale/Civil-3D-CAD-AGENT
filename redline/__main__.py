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
    p = sub.add_parser("directives", help="list the sheet-wide reviewer notes on file, or close one")
    p.add_argument("--dwg", help="only this drawing (path or file name)")
    p.add_argument("--done", help="mark this directive id done")
    p.add_argument("--reopen", help="mark this directive id open again")
    p.add_argument("--all", action="store_true", help="include closed ones")
    _common(p)
    p = sub.add_parser("show", help="the actions planned for every comment of a review, with their parameters")
    p.add_argument("review", help="review id (folder under the review dir) or a path to its report.json")
    p.add_argument("--comment", help="only this comment id, e.g. p1-c3 (all of its targets)")
    p.add_argument("--json", action="store_true", help="print the chains as JSON instead of text")
    _common(p)
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
    _isolate_learned_file(settings)

    if args.cmd == "doctor":
        return _doctor(settings)
    if args.cmd == "directives":
        return _directives(settings, args)
    if args.cmd == "show":
        return _show(settings, args)

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
        return 1 if out.get("model_errors") else 0
    if args.cmd == "annotate":
        from . import report
        row = report.annotate(settings.review_dir, args.review_id, args.comment_id, args.chain, args.verdict)
        print(json.dumps({"comment": row["comment"]["id"], "human_verdict": row.get("human_verdict"),
                          "human_chain": row.get("human_chain")}, default=str))
        return 0
    return 2


def _show(settings, args) -> int:
    """What was planned for each comment, in order, readable by a person."""
    path = args.review
    if not path.lower().endswith(".json"):
        path = os.path.join(settings.review_dir, args.review, "report.json")
    if not os.path.isfile(path):
        print(f"no report at {path}")
        return 1
    with open(path, "r", encoding="utf-8") as f:
        r = json.load(f)
    located = {l.get("key", l.get("comment_id")): l for l in r.get("located") or []}
    proposals = {p["comment_id"]: p for p in r.get("proposals") or []}
    outcomes = {o["comment_id"]: o for o in r.get("outcomes") or []}
    sheets = {s["page_no"]: s for s in r.get("sheets") or []}
    want = args.comment
    out = []
    for c in r.get("comments") or []:
        cid = c["id"]
        if want and cid != want:
            continue
        if c.get("scope") == "sheet":
            out.append({"key": cid, "text": c.get("text"), "scope": "sheet", "status": "directive", "blocks": []})
            continue
        keys = sorted((k for k in located if k == cid or k.startswith(cid + "#")),
                      key=lambda k: int(k.split("#")[1]) if "#" in k else 0) or [cid]
        for key in keys:
            loc = located.get(key) or {}
            prop = proposals.get(key) or {}
            outc = outcomes.get(key) or {}
            rationale = None
            try:
                from . import openai_compat
                rationale = openai_compat.parse_json(prop.get("raw_reply") or "").get("rationale")
            except (ValueError, AttributeError):
                pass
            out.append({"key": key, "text": c.get("text"), "kind": c.get("kind"), "scope": c.get("scope"),
                        "page": c.get("page_no"), "layout": (sheets.get(c.get("page_no")) or {}).get("layout_name"),
                        "space": loc.get("space"), "model_xy": loc.get("model_xy"), "paper_xy": loc.get("paper_xy"),
                        "status": outc.get("status") or ("skipped" if prop.get("skip") else "planned"),
                        "reason": outc.get("reason") or prop.get("reason"), "rationale": rationale,
                        "blocks": prop.get("blocks") or [], "run_id": outc.get("run_id"),
                        "results_preview": outc.get("results_preview")})
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    print(f"review {r.get('review_id')}  ({len(out)} item(s))")
    for it in out:
        where = ""
        if it.get("model_xy"):
            where = f"model ({it['model_xy'][0]:.1f}, {it['model_xy'][1]:.1f}) ft"
        elif it.get("paper_xy"):
            where = f"paper ({it['paper_xy'][0]:.2f}, {it['paper_xy'][1]:.2f}) in"
        head = f"\n{it['key']}  \"{it.get('text')}\""
        if it.get("scope") == "sheet":
            print(head + "\n   sheet-wide note: stored as a directive, nothing planned")
            continue
        print(head + f"  [{it.get('kind')}] {it.get('layout') or ''} {where}  -> {it['status']}")
        if it.get("rationale"):
            print(f"   why: {it['rationale']}")
        if it.get("reason"):
            print(f"   reason: {it['reason']}")
        for i, b in enumerate(it["blocks"], start=1):
            params = ", ".join(f"{k}={json.dumps(v, default=str)}" for k, v in (b.get("params") or {}).items())
            print(f"   {i}. {b.get('type')}  {params}")
        if it.get("results_preview"):
            print(f"   result: {str(it['results_preview'])[:200]}")
    return 0


def _directives(settings, args) -> int:
    from . import directives as directives_mod
    if args.done or args.reopen:
        hit = directives_mod.mark_done(settings.review_dir, args.done or args.reopen, done=bool(args.done))
        if hit is None:
            print(f"no directive {args.done or args.reopen!r} under {settings.review_dir}")
            return 1
        print(json.dumps(hit.to_dict(), default=str))
        return 0
    rows = directives_mod.load(settings.review_dir, None, include_done=args.all)
    if args.dwg:
        want = os.path.normcase(os.path.basename(args.dwg))
        rows = [d for d in rows if d.dwg_path and os.path.normcase(os.path.basename(d.dwg_path)) == want]
    if not rows:
        print("no open sheet-wide notes" + (f" for {args.dwg}" if args.dwg else ""))
        return 0
    for d in rows:
        print(f"{d.status:5s} {d.id:28s} {os.path.basename(d.dwg_path or '(unmatched)')} p{d.page_no}: {d.text}")
    return 0


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
