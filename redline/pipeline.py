"""The stages in order, for one or more PDFs.

    review(pdfs)            the whole thing
    phase0(pdf, page, x, y) one page pixel -> paper -> model, with the index's
                            words at the point (Phase 0 check of the transform)
    ocr_only(pdf)           regions and transcriptions, nothing opened or run

Order of model use inside review(): every page is read first (vision model),
then every comment is planned (text model). On an 8 GB card only one of
the two fits at a time; grouping the calls means the server swaps models
once per review instead of once per comment.
"""
import os
import sys
import time

from . import config, locate as locate_mod, marks, ocr as ocr_mod, palette, pdf as pdf_mod, planner as planner_mod
from . import report as report_mod, sheet as sheet_mod
from .paper_index import PaperIndex
from .runner import DashboardClient, DashboardError, outcome_of
from .transform import PageToPaper, PaperToModel
from .types import CommentOutcome, Report

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


def review(pdfs: list, settings=None, dry_run: bool = False, ocr=None, planner=None, on_conflict: str | None = None,
           log=_log, review_id: str | None = None, pages: list | None = None) -> Report:
    settings = settings or config.load()
    review_id = review_id or _review_id()
    out_dir = os.path.join(settings.review_dir, review_id)
    os.makedirs(out_dir, exist_ok=True)
    report = Report(review_id=review_id, created=time.time(), pdfs=[os.path.abspath(p) for p in pdfs], out_dir=out_dir,
                    settings={k: v for k, v in settings.raw.items() if k not in ("fixture",)})
    ocr = ocr or ocr_mod.backend(settings)
    on_conflict = (on_conflict or settings.on_conflict or "pause").lower()

    # ── 1. read every page ────────────────────────────────────────────────
    wanted_pages = pages_filter(pages)
    per_page = {}
    for pdf in pdfs:
        try:
            pgs = pdf_mod.rasterise(pdf, dpi=settings.dpi, out_dir=os.path.join(out_dir, "pages"), pages=wanted_pages)
        except Exception as e:
            report.errors.append(f"{pdf}: could not rasterise ({e})")
            continue
        for page in pgs:
            log(f"page {page.page_no} of {os.path.basename(pdf)}: {page.width_px}x{page.height_px} px")
            rgb = marks._array(page)
            regions = marks.find_regions(page, rgb=rgb)
            if not regions:
                regions = ocr.find_marks(page)
                log(f"  no red ink; model found {len(regions)} mark(s)")
            comments = ocr.read_comments(page, regions, out_dir=os.path.join(out_dir, "crops"))
            log(f"  {len(regions)} region(s), {len(comments)} comment(s) read")
            title = sheet_mod.read_title_block(page, ocr, settings, out_dir=os.path.join(out_dir, "crops"))
            border = marks.find_border(page, rgb=rgb) if settings.border_rect else None
            per_page[page.page_no] = {"page": page, "comments": comments, "title": title, "border": border}
            report.comments.extend(c.to_dict() for c in comments)

    # ── 2. which drawing, which layout ────────────────────────────────────
    opener = _open_drawing(settings)
    entries = {}
    for pn, rec in per_page.items():
        page, title = rec["page"], rec["title"]

        def open_drawing(path):
            entry = opener(path)
            entries[os.path.normcase(os.path.abspath(path))] = entry
            return entry.data
        m = sheet_mod.match(page, title, settings, open_drawing, border_rect_px=rec["border"])
        rec["match"] = m
        report.sheets.append(m.to_dict())
        if not m.ok or m.reason:
            report.unmatched_pages.append({"page_no": pn, "reason": m.reason or "unmatched"})
            log(f"page {pn}: not matched ({m.reason}); {len(rec['comments'])} comment(s) will be reported only")
        else:
            log(f"page {pn}: {m.dwg_path} / {m.layout_name} ({m.how})")

    # ── 3. locate every comment ───────────────────────────────────────────
    located = {}
    for pn, rec in per_page.items():
        m = rec.get("match")
        if m is None or not m.ok or m.reason:
            for c in rec["comments"]:
                located[c.id] = locate_mod.Located(comment_id=c.id, space="none", reason=m.reason if m else "unmatched")
            continue
        entry = entries[os.path.normcase(os.path.abspath(m.dwg_path))]
        layout = next((l for l in entry.data.get("layouts") or [] if l.get("name") == m.layout_name), {})
        pidx = PaperIndex(layout)
        p2p, vps = sheet_mod.transforms_for(m)
        frozen = {v["handle"]: v.get("frozen_layers") or [] for v in m.viewports}
        layers = locate_mod.layer_map(entry.data)
        for c in rec["comments"]:
            loc = locate_mod.locate(c, p2p, vps, entry.index, entry.data, pidx, frozen_by_handle=frozen,
                                    radius=settings.context_radius, layers=layers, layout_name=m.layout_name)
            located[c.id] = loc
            log(f"  {c.id} '{c.text[:40]}' -> {loc.space} {loc.model_xy or loc.paper_xy}")
    report.located = [l.to_dict() for l in located.values()]

    # ── 4. plan ───────────────────────────────────────────────────────────
    planner = planner or _planner(settings)
    pal_text = palette.palette_text()
    ftypes = palette.field_types()
    examples = palette.load_examples()
    proposals = {}
    for pn, rec in per_page.items():
        m = rec.get("match")
        for c in rec["comments"]:
            loc = located[c.id]
            if loc.space == "none":
                proposals[c.id] = planner_mod.ChainProposal(comment_id=c.id, skip=True,
                                                            reason=f"not_located: {loc.reason or 'no sheet match'}")
                continue
            if c.confidence < settings.min_confidence or not c.text.strip():
                proposals[c.id] = planner_mod.ChainProposal(comment_id=c.id, skip=True,
                                                            reason=f"needs_human: transcription confidence {c.confidence:.2f}")
                continue
            plan_type = (loc.context or {}).get("plan_type_guess")
            prop = planner_mod.plan(c, loc, m, planner, pal_text, ftypes, steps=palette.playbook_steps(plan_type),
                                    recipe=palette.recipe_blocks(plan_type), examples=examples, log=log)
            proposals[c.id] = prop
            log(f"  {c.id}: " + ("skip: " + str(prop.reason) if prop.skip else "chain " + " -> ".join(b["type"] for b in prop.blocks)))
    report.proposals = [p.to_dict() for p in proposals.values()]

    # ── 5. run ────────────────────────────────────────────────────────────
    outcomes = {}
    if dry_run:
        for cid, prop in proposals.items():
            if prop.blocks:
                outcomes[cid] = CommentOutcome(comment_id=cid, status="dry_run")
            else:
                reason = prop.reason or "no chain"
                status = "needs_human" if str(reason).startswith("needs_human") else "skipped"
                outcomes[cid] = CommentOutcome(comment_id=cid, status=status, reason=reason)
    else:
        client = DashboardClient(settings.dashboard_base)
        by_drawing = {}
        for pn, rec in per_page.items():
            m = rec.get("match")
            for c in rec["comments"]:
                prop = proposals[c.id]
                if prop.blocks and m is not None and m.dwg_path:
                    by_drawing.setdefault(m.dwg_path, []).append((c, prop, m))
                else:
                    reason = prop.reason or "no chain"
                    status = "needs_human" if str(reason).startswith("needs_human") else "skipped"
                    outcomes[c.id] = CommentOutcome(comment_id=c.id, status=status, reason=reason)
        for dwg, items in by_drawing.items():
            try:
                dash = client.ensure_dashboard(dwg)
            except DashboardError as e:
                for c, prop, m in items:
                    outcomes[c.id] = CommentOutcome(comment_id=c.id, status="failed", reason=f"dashboard: {e}")
                report.errors.append(f"{dwg}: {e}")
                continue
            if client.busy(dash):
                for c, prop, m in items:
                    outcomes[c.id] = CommentOutcome(comment_id=c.id, status="failed", reason="dashboard_busy", dashboard=dash)
                continue
            for c, prop, m in items:
                label = f"{m.layout_name} {c.id}: {c.text[:50]}"
                try:
                    started = client.run(dash, prop.blocks, label, on_conflict=on_conflict)
                    digest = client.wait_run(dash, started["run"])
                    results = client.results(dash) if digest.get("status") == "finished" else None
                    outcomes[c.id] = outcome_of(c.id, dash, digest, results)
                except DashboardError as e:
                    outcomes[c.id] = CommentOutcome(comment_id=c.id, status="failed", reason=str(e)[:400], dashboard=dash)
                log(f"  {c.id}: {outcomes[c.id].status} {outcomes[c.id].reason or ''}")
    report.outcomes = [o.to_dict() for o in outcomes.values()]
    counts = {}
    for o in outcomes.values():
        counts[o.status] = counts.get(o.status, 0) + 1
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


# ── Phase 0 helpers ──────────────────────────────────────────────────────────

def phase0(pdf: str, page_no: int, x_px: float, y_px: float, settings=None, ocr=None, log=_log) -> dict:
    """One page pixel through the whole transform, with evidence at every
    step: the title block read, the drawing and layout chosen, paper inches,
    the viewport, model feet and the index's sentence there."""
    settings = settings or config.load()
    ocr = ocr or ocr_mod.backend(settings)
    out_dir = os.path.join(settings.review_dir, "phase0")
    page = pdf_mod.rasterise(pdf, dpi=settings.dpi, out_dir=os.path.join(out_dir, "pages"), pages=[page_no])[0]
    rgb = marks._array(page)
    title = sheet_mod.read_title_block(page, ocr, settings, out_dir=os.path.join(out_dir, "crops"))
    border = marks.find_border(page, rgb=rgb)
    out = {"page": page.to_dict(), "title_block": title.to_dict(), "border_px": border,
           "regions": [r.to_dict() for r in marks.find_regions(page, rgb=rgb)]}
    if border:
        nominal = PageToPaper.nominal(page.width_px, page.height_px, page_in=(page.width_in, page.height_in))
        bl = nominal.to_paper(border[0], border[3])
        tr = nominal.to_paper(border[2], border[1])
        out["border_paper_in_nominal"] = [round(bl[0], 3), round(bl[1], 3), round(tr[0], 3), round(tr[1], 3)]
    opener = _open_drawing(settings)
    holder = {}

    def open_drawing(path):
        entry = opener(path)
        holder["entry"] = entry
        return entry.data
    m = sheet_mod.match(page, title, settings, open_drawing, border_rect_px=border)
    out["match"] = m.to_dict()
    if not m.ok or m.reason:
        out["error"] = m.reason
        return out
    entry = holder["entry"]
    layout = next((l for l in entry.data.get("layouts") or [] if l.get("name") == m.layout_name), {})
    out["layout_texts"] = [t.get("text") for t in (layout.get("texts") or [])][:40]
    p2p, vps = sheet_mod.transforms_for(m)
    px, py = p2p.to_paper(x_px, y_px)
    out["paper_in"] = [round(px, 3), round(py, 3)]
    from .transform import viewport_at
    vp = viewport_at(vps, px, py)
    if vp is None:
        out["viewport"] = None
        out["paper_texts_near"] = PaperIndex(layout).texts_near((px, py), 4.0)
        return out
    X, Y = vp.to_model(px, py)
    out["viewport"] = {"handle": vp.handle, "scale": vp.scale, "twist_deg": vp.twist_deg}
    out["model_ft"] = [round(X, 3), round(Y, 3)]
    out["context"] = locate_mod.comment_context(entry.index, entry.data, (X, Y), radius=settings.context_radius)
    return out


def ocr_only(pdf: str, settings=None, ocr=None, log=_log, pages: list | None = None) -> dict:
    settings = settings or config.load()
    ocr = ocr or ocr_mod.backend(settings)
    out_dir = os.path.join(settings.review_dir, "ocr-only", _review_id())
    out = {"pdf": os.path.abspath(pdf), "out_dir": out_dir, "pages": []}
    for page in pdf_mod.rasterise(pdf, dpi=settings.dpi, out_dir=os.path.join(out_dir, "pages"), pages=pages_filter(pages)):
        rgb = marks._array(page)
        regions = marks.find_regions(page, rgb=rgb)
        used_model = False
        if not regions:
            regions = ocr.find_marks(page)
            used_model = True
        comments = ocr.read_comments(page, regions, out_dir=os.path.join(out_dir, "crops"))
        title = sheet_mod.read_title_block(page, ocr, settings, out_dir=os.path.join(out_dir, "crops"))
        out["pages"].append({"page_no": page.page_no, "regions_from_model": used_model,
                             "regions": [r.to_dict() for r in regions], "comments": [c.to_dict() for c in comments],
                             "title_block": title.to_dict()})
        log(f"page {page.page_no}: {len(comments)} comment(s); sheet {title.sheet_no} file {title.printed_file}")
        for c in comments:
            log(f"  {c.id} [{c.kind} {c.confidence:.2f}] {c.text}")
    # Every failed read carries its error, but eight "unknown" comments in a row
    # read like bad handwriting when the truth is that no model answered. Say so.
    failed = [c for pg in out["pages"] for c in pg["comments"] if str(c.get("raw_model_reply", "")).startswith("error")]
    total = sum(len(pg["comments"]) for pg in out["pages"])
    if failed:
        first = str(failed[0]["raw_model_reply"]).split(" | reply:", 1)[0]
        out["model_errors"] = len(failed)
        log(f"MODEL ERRORS: {len(failed)} of {total} comment(s) were not read; {first}")
        if len(failed) == total:
            log("Nothing was transcribed. Run `python -m redline doctor` and fix what it reports, then run this again.")
    return out
