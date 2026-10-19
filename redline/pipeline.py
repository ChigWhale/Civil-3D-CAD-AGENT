"""The stages in order, for one or more PDFs.

    review(pdfs, texts)     the current full run: transcribe every comment,
                            then ask the language model for the ordered tool
                            names that would address each one. Text to text;
                            nothing is opened, located or run, and no comment
                            is skipped.
    review_markups(pdfs)    the same, for Bluebeam text boxes (FreeText
                            annotations) instead of handwriting: no detector,
                            no vision model
    ocr_only(pdf)           regions and transcriptions, nothing planned

    review_located(pdfs)    PARKED. The earlier full run: match the sheet to a
                            drawing, open it, locate every comment in model
                            space, plan parameterised block chains, run them on
                            the dashboard. Kept, with its modules (sheet,
                            transform, locate, planner, runner, directives),
                            for the parameter stage that comes after the
                            tool-name stage works. tests/run_redline.py still
                            exercises it against the stub.

Order of model use: every page is read first (vision model), then every
comment is sequenced (text model). On an 8 GB card only one of the two fits
at a time; grouping the calls means the server swaps models once per review.
"""
import os
import sys
import time

from . import config, directives as directives_mod, locate as locate_mod, markups as markups_mod, marks, ocr as ocr_mod, palette
from . import pdf as pdf_mod, planner as planner_mod, report as report_mod, sequence as sequence_mod, sheet as sheet_mod
from . import tools as tools_mod
from .paper_index import PaperIndex
from .runner import DashboardClient, DashboardError, outcome_of
from .types import Comment, CommentOutcome, Located, Report

if str(config.ROOT) not in sys.path:
    sys.path.insert(0, str(config.ROOT))


def _log(msg):
    print(msg, flush=True)


def _open_drawing(settings):
    """spatial_cache.get(path) opens the drawing in Civil 3D when the plugin
    does not have it, and returns the snapshot + index."""
    import spatial_cache
    spatial_cache.PLUGIN_BASE = settings.plugin_base

    def opener(path):
        return spatial_cache.get(path)
    return opener


def _review_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def detect(page, rgb, ocr, settings, crops_dir: str, log=_log) -> tuple:
    """The reviewer's ink on one page: (regions, warnings, used_model).

    The ink detector finds the writing and traces its arrows. When it finds
    nothing at all (a grey photocopy) the model is asked for boxes instead.
    Otherwise, with the sweep on, the model takes a second look and any mark
    it sees that the detector has no region for is added; a tile where it
    counts clearly more marks than were detected becomes a warning. Long
    arrows with no writing beside them are warnings too. The overlay PNG
    shows all of it."""
    regions, orphans = marks.analyse(page, rgb=rgb, min_area_in2=settings.min_area_in2)
    warnings = []
    used_model = False
    if not regions and not orphans:
        regions = ocr.find_marks(page)
        used_model = True
        log(f"  no red ink; model found {len(regions)} mark(s)")
    elif settings.sweep:
        extra, warns = ocr_mod.sweep(page, regions, ocr, rgb=rgb)
        if extra:
            for k, r in enumerate(extra, start=len(regions) + 1):
                r.id = f"{page.id}-r{k}"
            regions = regions + extra
            log(f"  sweep added {len(extra)} region(s) the ink detector had not boxed")
        warnings.extend(warns)
    for o in orphans:
        warnings.append({"page_no": page.page_no, "kind": "orphan_connector", "bbox_px": o["bbox_px"],
                         "detail": f"a {o['length_in']} in {o['kind']} with no writing beside it, at {o['bbox_px']}"})
    try:
        os.makedirs(crops_dir, exist_ok=True)
        marks.debug_image(page, regions, orphans).save(os.path.join(crops_dir, f"{page.id}_regions.png"))
    except Exception:
        pass
    return regions, warnings, used_model


def _describe(c) -> str:
    if c.scope == "sheet":
        return " (sheet-wide note)"
    if c.scope == "multi":
        return f" -> {len(c.targets_px)} targets"
    return ""


def _read_pages(pdfs, settings, ocr, out_dir: str, crops_dir: str, pages, log=_log, errors: list | None = None) -> dict:
    """Every page of every PDF read: {page_no: {page, comments, title, border,
    regions, regions_from_model, warnings}}. The same work for transcribe
    and for both review flows. A PDF that will not rasterise is recorded in
    `errors` and skipped."""
    wanted_pages = pages_filter(pages)
    per_page = {}
    for pdf in pdfs:
        try:
            pgs = pdf_mod.rasterise(pdf, dpi=settings.dpi, out_dir=os.path.join(out_dir, "pages"), pages=wanted_pages)
        except Exception as e:
            if errors is None:
                raise
            errors.append(f"{pdf}: could not rasterise ({e})")
            continue
        for page in pgs:
            log(f"page {page.page_no} of {os.path.basename(pdf)}: {page.width_px}x{page.height_px} px")
            rgb = marks._array(page)
            regions, warnings, used_model = detect(page, rgb, ocr, settings, crops_dir, log=log)
            comments = ocr.read_comments(page, regions, out_dir=crops_dir)
            log(f"  {len(regions)} region(s), {len(comments)} comment(s) read")
            title = sheet_mod.read_title_block(page, ocr, settings, out_dir=crops_dir)
            border = marks.find_border(page, rgb=rgb) if settings.border_rect else None
            per_page[page.page_no] = {"page": page, "comments": comments, "title": title, "border": border,
                                      "regions": regions, "regions_from_model": used_model, "warnings": warnings}
    return per_page


def _text_comments(texts) -> list:
    """Comments typed on the command line, so the sequencer can be exercised
    without a scan. Page 0, full confidence, kind note."""
    return [Comment(id=f"t{i}", page_no=0, bbox_px=[0, 0, 0, 0], text=str(t).strip(), kind="note", confidence=1.0)
            for i, t in enumerate(texts or [], start=1) if str(t).strip()]


def review(pdfs: list | None = None, texts: list | None = None, settings=None, ocr=None, backend=None, log=_log,
           review_id: str | None = None, pages: list | None = None, locate: bool = False, act: str | None = None,
           on_conflict: str | None = None) -> Report:
    """Transcribe, then name the tools. One model call per comment; every
    comment gets a row. With `locate`, the drawing printed on each sheet is
    opened in Civil 3D first and every comment is placed in it. With `act`
    ("dry" or "run", which implies locate), the parameters of every tool in
    the sequence are filled and the tools run: a dry run fills and runs the
    read-only steps only; "run" draws."""
    locate = locate or bool(act)
    pdfs = list(pdfs or [])
    settings = settings or config.load()
    review_id = review_id or _review_id()
    out_dir = os.path.join(settings.review_dir, review_id)
    os.makedirs(out_dir, exist_ok=True)
    report = Report(review_id=review_id, created=time.time(), pdfs=[os.path.abspath(p) for p in pdfs], out_dir=out_dir,
                    settings={k: v for k, v in settings.raw.items() if k not in ("fixture",)})
    crops_dir = os.path.join(out_dir, "crops")

    # ── 1. read every page ────────────────────────────────────────────────
    comments = _text_comments(texts)
    sheet_of = {0: {}}
    per_page = {}
    if pdfs:
        ocr = ocr or ocr_mod.backend(settings)
        per_page = _read_pages(pdfs, settings, ocr, out_dir, crops_dir, pages, log=log, errors=report.errors)
        for pn, rec in per_page.items():
            report.recall_warnings.extend(rec["warnings"])
            sheet_of[pn] = rec["title"].to_dict()
            comments.extend(rec["comments"])
    located, comment_of, match_of = {}, {}, {}
    if locate and per_page:
        located, comment_of, match_of, _ = _locate_all(per_page, settings, report, log=log)   # fills report.sheets / located / unmatched_pages
    else:
        for pn, rec in per_page.items():
            report.sheets.append({"page_no": pn, "title_block": rec["title"].to_dict()})
    return _sequence_all(report, comments, sheet_of, settings, backend, out_dir, log, act=act, located=located,
                         comment_of=comment_of, match_of=match_of, on_conflict=on_conflict)


def _sequence_all(report: Report, comments: list, sheet_of: dict, settings, backend, out_dir: str, log=_log,
                  act: str | None = None, located: dict | None = None, comment_of: dict | None = None,
                  match_of: dict | None = None, on_conflict: str | None = None) -> Report:
    """The shared tail of every current flow: one sequencer call per comment,
    then (with act = "dry" | "run") the parameters filled and the tools run
    for every located target, then counts, tools.txt, the report files and
    the training rows."""
    report.comments = [c.to_dict() for c in comments]
    backend = backend or _backend(settings)
    names = tools_mod.names()
    tools_text = tools_mod.text()
    examples = sequence_mod.load_examples()
    mode = getattr(settings, "prompt", "full") or "full"
    report.vocabulary = {"version": tools_mod.version(), "count": len(names), "excluded": sorted(tools_mod.EXCLUDE),
                         "prompt": mode}
    with open(os.path.join(out_dir, "tools.txt"), "w", encoding="utf-8") as f:
        f.write(f"# {len(names)} tools offered to the model (version {report.vocabulary['version']})\n{tools_text}\n")
    sequences = []
    for c in comments:
        seq = sequence_mod.sequence(c, backend, tools_text, names, examples=examples, log=log, mode=mode)
        sequences.append(seq)
        shown = " -> ".join(seq.tools) if seq.tools else f"(none: {seq.error})"
        log(f"  {c.id} '{c.text[:40]}' -> {shown}")
    report.sequences = [s.to_dict() for s in sequences]
    report.counts = {"comments": len(sequences), "sequenced": sum(1 for s in sequences if s.tools),
                     "empty": sum(1 for s in sequences if not s.tools),
                     "model_errors": sum(1 for s in sequences if str(s.error or "").startswith("model_error"))}

    if act:
        _act_all(report, comments, sequences, settings, backend, act, located or {}, comment_of or {}, match_of or {},
                 on_conflict=on_conflict, log=log)

    md, js = report_mod.write(report, out_dir)
    rows = report_mod.sequence_rows(report, sheet_of)
    report_mod.append_training(os.path.join(out_dir, report_mod.TRAINING_FILE), rows)
    log(f"report: {md}")
    return report


def _act_all(report: Report, comments: list, sequences: list, settings, backend, mode: str, located: dict,
             comment_of: dict, match_of: dict, on_conflict: str | None = None, log=_log) -> None:
    """Parameters filled and tools run (mode "run") or only filled (mode
    "dry": read-only steps run for real, writes never) for every located
    target, in comment order. Fills report.actions and report.mode."""
    from . import act as act_mod
    report.mode = mode
    seq_of = {s.comment_id: s.tools for s in sequences}
    unk_of = {s.comment_id: s.unknown_tools for s in sequences}
    ex = act_mod.Executor(settings, mode=mode, on_conflict=on_conflict or settings.on_conflict or "pause",
                          backend=backend, log=log)
    actions = []
    for c in comments:
        keys = sorted((k for k in located if k == c.id or k.startswith(c.id + "#")),
                      key=lambda k: int(k.split("#")[1]) if "#" in k else 0)
        if not keys:
            reason = "sheet-wide note" if c.scope == "sheet" else "not located (run with --locate against Civil 3D)"
            actions.append(act_mod.Action(key=c.id, comment_id=c.id, status="not_located", reason=reason))
            continue
        for key in keys:
            loc = located[key]
            m = match_of.get(c.page_no)
            log(f"  {key} '{c.text[:40]}': {' -> '.join(seq_of.get(c.id) or []) or '(no sequence)'}")
            a = ex.act(key, c, loc, m, seq_of.get(c.id) or [], unknown_tools=unk_of.get(c.id) or [])
            log(f"  {key}: {a.status}" + (f" ({a.reason})" if a.reason else ""))
            actions.append(a)
    report.actions = [a.to_dict() for a in actions]
    counts = {}
    for a in actions:
        counts[a.status] = counts.get(a.status, 0) + 1
    report.counts.update({"acted": len(actions), **{f"act_{k}": v for k, v in sorted(counts.items())}})


def review_markups(pdfs: list, settings=None, backend=None, log=_log, review_id: str | None = None,
                   pages: list | None = None, locate: bool = False, act: str | None = None,
                   on_conflict: str | None = None) -> Report:
    """Bluebeam text boxes (FreeText annotations) instead of handwriting: no
    detector, no vision model. The pages are rendered so a person can look
    at them; then the same sequencer, report and training rows. With
    `locate`, the title block is read from the PDF's text layer, the drawing
    it names is opened in Civil 3D and every text box is placed in it. With
    `act` ("dry" | "run") the tools are filled and run, as in review()."""
    locate = locate or bool(act)
    pdfs = list(pdfs or [])
    settings = settings or config.load()
    review_id = review_id or _review_id()
    out_dir = os.path.join(settings.review_dir, review_id)
    os.makedirs(out_dir, exist_ok=True)
    report = Report(review_id=review_id, created=time.time(), pdfs=[os.path.abspath(p) for p in pdfs], out_dir=out_dir,
                    settings=dict({k: v for k, v in settings.raw.items() if k not in ("fixture",)}, source="markups"))
    comments = []
    sheet_of = {}
    per_page = {}
    for pdf in pdfs:
        try:
            found = markups_mod.read(pdf, dpi=settings.dpi, pages=pages_filter(pages))
        except Exception as e:
            report.errors.append(f"{pdf}: could not read annotations ({e})")
            continue
        total = 0
        summaries = {}
        for pn, rec in sorted(found.items()):
            total += len(rec["comments"])
            log(f"page {pn} of {os.path.basename(pdf)}: {markups_mod.summary(rec)}")
            summaries[pn] = {"textboxes": len(rec["comments"]), "other": rec["annots_other"],
                             "rotate": rec["rotate"], "size_pt": list(rec["size_pt"])}
            comments.extend(rec["comments"])
        if total == 0:
            report.errors.append(f"{pdf}: no Bluebeam text boxes found (flattened PDF, or markups of other kinds only)")
        pages_rendered = {}
        try:
            for page in pdf_mod.rasterise(pdf, dpi=settings.dpi, out_dir=os.path.join(out_dir, "pages"), pages=pages_filter(pages)):
                pages_rendered[page.page_no] = page
        except Exception as e:
            log(f"  (pages not rendered: {e})")
        for pn, rec in sorted(found.items()):
            title = sheet_mod.title_from_text_layer(pdf, pn)
            sheet_of[pn] = title.to_dict()
            if locate and pn in pages_rendered:
                per_page[pn] = {"page": pages_rendered[pn], "comments": rec["comments"], "title": title, "border": None,
                                "markups": summaries[pn]}
            else:
                report.sheets.append({"page_no": pn, "pdf": os.path.abspath(pdf), "markups": summaries[pn],
                                      "title_block": title.to_dict()})
    located, comment_of, match_of = {}, {}, {}
    if locate and per_page:
        located, comment_of, match_of, _ = _locate_all(per_page, settings, report, log=log)
        by_page = {s["page_no"]: s for s in report.sheets}
        for pn, rec in per_page.items():           # keep the markup counts beside the match
            if pn in by_page:
                by_page[pn]["markups"] = rec["markups"]
    return _sequence_all(report, comments, sheet_of, settings, backend, out_dir, log, act=act, located=located,
                         comment_of=comment_of, match_of=match_of, on_conflict=on_conflict)


def _locate_all(per_page: dict, settings, report: Report, log=_log) -> tuple:
    """Which drawing and layout each page is, opened in Civil 3D through the
    plugin, and where every comment lands in it.

    per_page: {page_no: {"page": Page, "comments": [Comment], "title": TitleBlockRead, "border": rect|None}}.
    Returns (located {key: Located}, comment_of {key: Comment}, match_of
    {page_no: SheetMatch}, sheet_wide [(page_no, Comment)]). A key is the
    comment id, or id#n for the n-th target of a multi-target comment.
    Sheet-wide notes are not located; they come back in `sheet_wide`. A page
    that cannot be matched (no candidate, plugin unreachable, no layout)
    is recorded in report.unmatched_pages and its comments get
    Located(space="none", reason=...). Nothing here raises."""
    opener = _open_drawing(settings)
    entries = {}
    match_of = {}
    for pn, rec in per_page.items():
        page, title = rec["page"], rec["title"]

        def open_drawing(path):
            entry = opener(path)
            entries[os.path.normcase(os.path.abspath(path))] = entry
            return entry.data
        try:
            m = sheet_mod.match(page, title, settings, open_drawing, border_rect_px=rec.get("border"))
        except Exception as e:
            m = sheet_mod.SheetMatch(page_no=pn, title_block=title.to_dict(), reason=f"could_not_match: {e}")
        rec["match"] = m
        match_of[pn] = m
        report.sheets.append(m.to_dict())
        if not m.ok or m.reason:
            report.unmatched_pages.append({"page_no": pn, "reason": m.reason or "unmatched"})
            log(f"page {pn}: not matched ({m.reason}); {len(rec['comments'])} comment(s) will be reported only")
        else:
            log(f"page {pn}: {m.dwg_path} / {m.layout_name} ({m.how})"
                + (" opened by the pipeline" if m.opened_by_pipeline else ""))

    located = {}          # key -> Located
    comment_of = {}       # key -> Comment
    sheet_wide = []       # (page_no, Comment)
    for pn, rec in per_page.items():
        m = rec.get("match")
        matched = m is not None and m.ok and not m.reason
        for c in rec["comments"]:
            if c.scope == "sheet":
                sheet_wide.append((pn, c))
                continue
            if not matched:
                loc = Located(comment_id=c.id, space="none", reason=m.reason if m else "unmatched")
                located[loc.key] = loc
                comment_of[loc.key] = c
                continue
            entry = entries[os.path.normcase(os.path.abspath(m.dwg_path))]
            layout = next((l for l in entry.data.get("layouts") or [] if l.get("name") == m.layout_name), {})
            pidx = PaperIndex(layout)
            p2p, vps = sheet_mod.transforms_for(m)
            frozen = {v["handle"]: v.get("frozen_layers") or [] for v in m.viewports}
            layers = locate_mod.layer_map(entry.data)
            for loc in locate_mod.locate(c, p2p, vps, entry.index, entry.data, pidx, frozen_by_handle=frozen,
                                         radius=settings.context_radius, layers=layers, layout_name=m.layout_name):
                located[loc.key] = loc
                comment_of[loc.key] = c
                log(f"  {loc.key} '{c.text[:40]}' -> {loc.space} {loc.model_xy or loc.paper_xy}")
    report.located = [l.to_dict() for l in located.values()]
    return located, comment_of, match_of, sheet_wide


def _backend(settings):
    kind = (settings.planner_backend or "openai").lower()
    if kind == "fake":
        return sequence_mod.FakeSequencer()
    if kind in ("openai", "ollama", "llm"):
        return sequence_mod.OpenAICompatPlanner(settings.model_base, settings.llm, timeout=settings.model_timeout)
    raise ValueError(f"unknown planner backend {kind!r} (openai | fake)")


# ── PARKED: the located, parameterised flow ──────────────────────────────────

def review_located(pdfs: list, settings=None, dry_run: bool = False, ocr=None, planner=None, on_conflict: str | None = None,
                   log=_log, review_id: str | None = None, pages: list | None = None) -> Report:
    """The earlier full run, kept for the parameter stage: match each sheet
    to a drawing, open it, locate every comment through the viewport, plan
    parameterised block chains with handles, run them on the dashboard.
    Not reachable from the command line for now."""
    settings = settings or config.load()
    review_id = review_id or _review_id()
    out_dir = os.path.join(settings.review_dir, review_id)
    os.makedirs(out_dir, exist_ok=True)
    report = Report(review_id=review_id, created=time.time(), pdfs=[os.path.abspath(p) for p in pdfs], out_dir=out_dir,
                    settings={k: v for k, v in settings.raw.items() if k not in ("fixture",)})
    ocr = ocr or ocr_mod.backend(settings)
    on_conflict = (on_conflict or settings.on_conflict or "pause").lower()
    crops_dir = os.path.join(out_dir, "crops")

    # ── 1. read every page ────────────────────────────────────────────────
    per_page = _read_pages(pdfs, settings, ocr, out_dir, crops_dir, pages, log=log, errors=report.errors)
    for rec in per_page.values():
        report.recall_warnings.extend(rec["warnings"])
        report.comments.extend(c.to_dict() for c in rec["comments"])

    # ── 2 + 3. which drawing, which layout; locate every comment ──────────
    located, comment_of, match_of, sheet_wide = _locate_all(per_page, settings, report, log=log)

    # file the sheet-wide notes as directives (the parked flow's rule)
    found_directives = {} # dwg path (or None) -> [Directive] found in this review
    for pn, c in sheet_wide:
        m = match_of.get(pn)
        dwg = m.dwg_path if m is not None else None
        d = directives_mod.add(settings.review_dir, review_id, c, dwg, m.layout_name if m else None,
                               sheet_no=per_page[pn]["title"].sheet_no)
        found_directives.setdefault(dwg, []).append(d)
        report.directives.append(d.to_dict())
        log(f"  {c.id} '{c.text[:40]}' -> sheet-wide note, stored as {d.id}")

    # ── 4. plan ───────────────────────────────────────────────────────────
    planner = planner or _planner(settings)
    pal_text = palette.palette_text()
    ftypes = palette.field_types()
    examples = palette.load_examples()
    notes_cache = {}
    proposals = {}
    for key, loc in located.items():
        c = comment_of[key]
        m = match_of.get(c.page_no)
        if loc.space == "none":
            proposals[key] = planner_mod.ChainProposal(comment_id=key, skip=True,
                                                       reason=f"not_located: {loc.reason or 'no sheet match'}")
            continue
        if c.confidence < settings.min_confidence or not c.text.strip():
            proposals[key] = planner_mod.ChainProposal(comment_id=key, skip=True,
                                                       reason=f"needs_human: transcription confidence {c.confidence:.2f}")
            continue
        dwg = m.dwg_path if m is not None else None
        if dwg not in notes_cache:
            notes_cache[dwg] = directives_mod.notes_for(settings.review_dir, dwg, extra=found_directives.get(dwg))
        plan_type = (loc.context or {}).get("plan_type_guess")
        prop = planner_mod.plan(c, loc, m, planner, pal_text, ftypes, steps=palette.playbook_steps(plan_type),
                                recipe=palette.recipe_blocks(plan_type), examples=examples, log=log,
                                notes=notes_cache[dwg])
        proposals[key] = prop
        log(f"  {key}: " + ("skip: " + str(prop.reason) if prop.skip else "chain " + " -> ".join(b["type"] for b in prop.blocks)))
    report.proposals = [p.to_dict() for p in proposals.values()]

    # ── 5. run ────────────────────────────────────────────────────────────
    outcomes = {}
    if dry_run:
        for key, prop in proposals.items():
            if prop.blocks:
                outcomes[key] = CommentOutcome(comment_id=key, status="dry_run")
            else:
                reason = prop.reason or "no chain"
                status = "needs_human" if str(reason).startswith("needs_human") else "skipped"
                outcomes[key] = CommentOutcome(comment_id=key, status=status, reason=reason)
    else:
        client = DashboardClient(settings.dashboard_base)
        by_drawing = {}
        for key, prop in proposals.items():
            c = comment_of[key]
            m = match_of.get(c.page_no)
            if prop.blocks and m is not None and m.dwg_path:
                by_drawing.setdefault(m.dwg_path, []).append((key, c, prop, m))
            else:
                reason = prop.reason or "no chain"
                status = "needs_human" if str(reason).startswith("needs_human") else "skipped"
                outcomes[key] = CommentOutcome(comment_id=key, status=status, reason=reason)
        for dwg, items in by_drawing.items():
            try:
                dash = client.ensure_dashboard(dwg)
            except DashboardError as e:
                for key, c, prop, m in items:
                    outcomes[key] = CommentOutcome(comment_id=key, status="failed", reason=f"dashboard: {e}")
                report.errors.append(f"{dwg}: {e}")
                continue
            if client.busy(dash):
                for key, c, prop, m in items:
                    outcomes[key] = CommentOutcome(comment_id=key, status="failed", reason="dashboard_busy", dashboard=dash)
                continue
            for key, c, prop, m in items:
                label = f"{m.layout_name} {key}: {c.text[:50]}"
                try:
                    started = client.run(dash, prop.blocks, label, on_conflict=on_conflict)
                    digest = client.wait_run(dash, started["run"])
                    results = client.results(dash) if digest.get("status") == "finished" else None
                    outcomes[key] = outcome_of(key, dash, digest, results)
                except DashboardError as e:
                    outcomes[key] = CommentOutcome(comment_id=key, status="failed", reason=str(e)[:400], dashboard=dash)
                log(f"  {key}: {outcomes[key].status} {outcomes[key].reason or ''}")
    report.outcomes = [o.to_dict() for o in outcomes.values()]
    counts = {}
    for o in outcomes.values():
        counts[o.status] = counts.get(o.status, 0) + 1
    if report.directives:
        counts["directives"] = len(report.directives)
    report.counts = counts

    # ── 6. write ──────────────────────────────────────────────────────────
    md, js = report_mod.write(report, out_dir)
    rows = report_mod.training_rows(report, palette.palette_version())
    report_mod.append_training(os.path.join(out_dir, report_mod.TRAINING_FILE), rows)
    log(f"report: {md}")
    return report


def pages_filter(pages):
    return [int(p) for p in pages] if pages else None


def _planner(settings):
    kind = (settings.planner_backend or "openai").lower()
    if kind == "fake":
        return planner_mod.FakePlanner()
    if kind in ("openai", "ollama", "llm"):
        return planner_mod.OpenAICompatPlanner(settings.model_base, settings.llm, timeout=settings.model_timeout)
    raise ValueError(f"unknown planner backend {kind!r} (openai | fake)")


def ocr_only(pdf: str, settings=None, ocr=None, log=_log, pages: list | None = None) -> dict:
    settings = settings or config.load()
    ocr = ocr or ocr_mod.backend(settings)
    out_dir = os.path.join(settings.review_dir, "ocr-only", _review_id())
    crops_dir = os.path.join(out_dir, "crops")
    out = {"pdf": os.path.abspath(pdf), "out_dir": out_dir, "pages": [], "recall_warnings": []}
    per_page = _read_pages([pdf], settings, ocr, out_dir, crops_dir, pages, log=log)
    for pn, rec in per_page.items():
        comments, title, warnings = rec["comments"], rec["title"], rec["warnings"]
        out["recall_warnings"].extend(warnings)
        out["pages"].append({"page_no": pn, "regions_from_model": rec["regions_from_model"],
                             "regions": [r.to_dict() for r in rec["regions"]], "comments": [c.to_dict() for c in comments],
                             "title_block": title.to_dict()})
        log(f"page {pn}: {len(comments)} comment(s); sheet {title.sheet_no} file {title.printed_file}")
        for c in comments:
            log(f"  {c.id} [{c.kind} {c.confidence:.2f}] {c.text}{_describe(c)}")
        for w in warnings:
            log(f"  POSSIBLE MISS: {w['detail']}")
    # Every failed read carries its error, but eight "unknown" comments in a row
    # read like bad handwriting when the truth is that no model answered. Say so.
    failed = [c for pg in out["pages"] for c in pg["comments"] if str(c.get("raw_model_reply", "")).startswith("error")]
    total = sum(len(pg["comments"]) for pg in out["pages"])
    if failed:
        first = str(failed[0]["raw_model_reply"]).split(" | reply:", 1)[0]
        out["model_errors"] = len(failed)
        log(f"MODEL ERRORS: {len(failed)} of {total} comment(s) were not read; {first}")
        if len(failed) == total:
            log("Nothing was transcribed. Check that Ollama is running and the vision model is pulled, then run this again.")
    return out
