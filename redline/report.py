"""What happened, written down: report.md for a person, report.json for a
program, training.jsonl for the fine-tune that is not being done yet.

Every comment gets a training row, skips and failures included, with
`human_chain` and `human_verdict` left null. `annotate` fills those in
later from the command line, which is how the dataset earns its labels.
"""
import json
import os
import time

from .types import Report

TRAINING_FILE = "training.jsonl"


def write(report: Report, out_dir: str | None = None) -> tuple:
    out_dir = out_dir or report.out_dir
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "report.json")
    md_path = os.path.join(out_dir, "report.md")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, default=str)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(markdown(report))
    return md_path, json_path


def _by(items: list, key: str = "comment_id") -> dict:
    return {str(it.get(key)): it for it in items if isinstance(it, dict)}


def markdown(report: Report) -> str:
    r = report.to_dict()
    lines = [f"# Redline review {r['review_id']}", "",
             f"PDFs: {', '.join(r['pdfs'])}", "",
             "| done | skipped | failed | needs a person | dry run |", "|---|---|---|---|---|",
             "| {done} | {skipped} | {failed} | {needs_human} | {dry_run} |".format(
                 **{k: r["counts"].get(k, 0) for k in ("done", "skipped", "failed", "needs_human", "dry_run")}), ""]
    located = _by(r["located"])
    proposals = _by(r["proposals"])
    outcomes = _by(r["outcomes"])
    sheets = {s["page_no"]: s for s in r["sheets"]}
    for sheet in r["sheets"]:
        pn = sheet["page_no"]
        lines.append(f"## Page {pn}")
        if sheet.get("dwg_path"):
            lines.append(f"- drawing: `{sheet['dwg_path']}` ({sheet.get('how')}, score {sheet.get('score')})"
                         + (" opened by the pipeline" if sheet.get("opened_by_pipeline") else ""))
            p2p = sheet.get("page_to_paper") or {}
            lines.append(f"- layout: {sheet.get('layout_name')}; page->paper {p2p.get('method')}"
                         f" rmse {p2p.get('rmse_in')} in; viewports {len(sheet.get('viewports') or [])}")
        tb = sheet.get("title_block") or {}
        lines.append(f"- title block read: sheet {tb.get('sheet_no')} project {tb.get('project_no')} "
                     f"file {tb.get('printed_file')}")
        if sheet.get("reason"):
            lines.append(f"- **not matched:** {sheet['reason']}")
            if sheet.get("candidates_considered"):
                lines.append("- candidates: " + "; ".join(f"{c['path']} ({c['score']})" for c in sheet["candidates_considered"][:4]))
        lines.append("")
        comments = [c for c in r["comments"] if c["page_no"] == pn]
        if not comments:
            lines.append("_no comments found on this page_\n")
            continue
        lines.append("| id | comment | kind | where | outcome | reason / chain |")
        lines.append("|---|---|---|---|---|---|")
        for c in comments:
            cid = c["id"]
            loc = located.get(cid) or {}
            where = "?"
            if loc.get("space") == "model":
                where = f"model ({loc['model_xy'][0]:.0f}, {loc['model_xy'][1]:.0f})"
            elif loc.get("space") == "paper":
                where = f"paper ({loc['paper_xy'][0]:.1f}, {loc['paper_xy'][1]:.1f}) in"
            prop = proposals.get(cid) or {}
            outc = outcomes.get(cid) or {}
            status = outc.get("status") or ("skipped" if prop.get("skip") else "?")
            detail = outc.get("reason") or prop.get("reason") or ""
            if prop.get("blocks"):
                chain = " → ".join(b["type"] for b in prop["blocks"])
                detail = (chain + (f" — {detail}" if detail else ""))
            text = str(c.get("text", "")).replace("|", "\\|")
            lines.append(f"| {cid} | {text} | {c.get('kind')} | {where} | {status} | {str(detail).replace('|', '/')[:160]} |")
        lines.append("")
    for u in r.get("unmatched_pages") or []:
        if u["page_no"] not in sheets:
            lines.append(f"## Page {u['page_no']}\n- **not matched:** {u.get('reason')}\n")
    if r.get("errors"):
        lines.append("## Errors")
        lines += [f"- {e}" for e in r["errors"]]
        lines.append("")
    lines.append(f"_written {time.strftime('%Y-%m-%d %H:%M:%S')}_")
    return "\n".join(lines) + "\n"


def training_rows(report: Report, palette_version: str) -> list:
    r = report.to_dict()
    located = _by(r["located"])
    proposals = _by(r["proposals"])
    outcomes = _by(r["outcomes"])
    sheets = {s["page_no"]: s for s in r["sheets"]}
    rows = []
    for c in r["comments"]:
        cid = c["id"]
        sheet = sheets.get(c["page_no"]) or {}
        rows.append({"ts": time.time(), "review_id": r["review_id"], "comment": c,
                     "sheet": {"dwg_path": sheet.get("dwg_path"), "layout_name": sheet.get("layout_name")},
                     "located": located.get(cid), "palette_version": palette_version,
                     "proposal": proposals.get(cid), "outcome": outcomes.get(cid),
                     "human_chain": None, "human_verdict": None})
    return rows


def append_training(path: str, rows: list) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
    return len(rows)


def annotate(review_dir: str, review_id: str, comment_id: str, chain_path: str | None, verdict: str | None) -> dict:
    """Records a person's chain and/or verdict for one comment of one
    review, in that review's training.jsonl (rows rewritten in place)."""
    path = os.path.join(review_dir, review_id, TRAINING_FILE)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    chain = None
    if chain_path:
        with open(chain_path, "r", encoding="utf-8") as f:
            chain = json.load(f)
        if isinstance(chain, dict) and "blocks" in chain:
            chain = chain["blocks"]
    with open(path, "r", encoding="utf-8") as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]
    hit = None
    for row in rows:
        if (row.get("comment") or {}).get("id") == comment_id:
            if chain is not None:
                row["human_chain"] = chain
            if verdict:
                row["human_verdict"] = verdict
            row["annotated_at"] = time.time()
            hit = row
    if hit is None:
        raise KeyError(f"no comment {comment_id} in {path}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
    os.replace(tmp, path)
    return hit
