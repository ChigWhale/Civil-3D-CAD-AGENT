"""What happened, written down: report.md for a person, report.json for a
program, training.jsonl for the fine-tune that is not being done yet.

Current stage (report.sequences set): one row per comment with the tool
names the model gave; `sequence_rows` writes the training rows with
`human_tools` / `human_verdict` left null for a person to fill in.

Parked stage (review_located): every located target gets a training row,
skips and failures included; `training_rows` and `annotate` belong to it.
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


def _keys_for(comment_id: str, located: dict) -> list:
    """The located keys of one comment: itself, or id#1, id#2 ... for a
    multi-target comment, in order."""
    keys = [k for k in located if k == comment_id or k.startswith(comment_id + "#")]
    return sorted(keys, key=lambda k: int(k.split("#")[1]) if "#" in k else 0)


def _label(key: str, n: int) -> str:
    if "#" not in key:
        return key
    cid, idx = key.split("#")
    return f"{cid} ({idx}/{n})"


def _md_cell(s) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def _markdown_sequences(r: dict) -> str:
    """The text-to-text stage: one row per comment with the tool names."""
    counts = r.get("counts") or {}
    lines = [f"# Redline review {r['review_id']}", ""]
    if r.get("pdfs"):
        lines += [f"PDFs: {', '.join(r['pdfs'])}", ""]
    lines += ["| comments | sequenced | empty | model errors |", "|---|---|---|---|",
              "| {comments} | {sequenced} | {empty} | {model_errors} |".format(
                  **{k: counts.get(k, 0) for k in ("comments", "sequenced", "empty", "model_errors")}), ""]
    seqs = {s["comment_id"]: s for s in r.get("sequences") or []}
    sheets = {s["page_no"]: s for s in r.get("sheets") or []}
    with_author = any(c.get("author") for c in r["comments"])
    from_markups = (r.get("settings") or {}).get("source") == "markups"
    pages = sorted({c["page_no"] for c in r["comments"]} | ({s["page_no"] for s in sheets.values()} if from_markups else set()))
    for pn in pages:
        lines.append("## Typed comments" if pn == 0 else f"## Page {pn}")
        sheet = sheets.get(pn) or {}
        tb = sheet.get("title_block")
        mk = sheet.get("markups")
        if mk:
            other = mk.get("other") or {}
            n = mk.get("textboxes", 0)
            line = f"- markups: {n} text box{'es' if n != 1 else ''} read"
            if other:
                line += "; other markups ignored: " + ", ".join(f"{k} {v}" for k, v in sorted(other.items()))
            lines.append(line)
        elif tb:
            lines.append(f"- title block read: sheet {tb.get('sheet_no')} project {tb.get('project_no')} "
                         f"file {tb.get('printed_file')}")
        lines.append("")
        page_comments = [c for c in r["comments"] if c["page_no"] == pn]
        if not page_comments:
            lines.append("_no text boxes on this page_\n")
            continue
        head = "| id | comment | kind |" + (" author |" if with_author else "") + " confidence | tools |"
        lines.append(head)
        lines.append("|---|---|---|" + ("---|" if with_author else "") + "---|---|")
        for c in page_comments:
            s = seqs.get(c["id"]) or {}
            shown = " → ".join(s.get("tools") or []) or f"(none: {s.get('error') or 'no answer'})"
            if s.get("unknown_tools"):
                shown += f" — dropped: {', '.join(s['unknown_tools'][:4])}"
            author = f" {_md_cell(c.get('author'))} |" if with_author else ""
            lines.append(f"| {c['id']} | {_md_cell(c.get('text'))} | {c.get('kind')} |{author} {float(c.get('confidence') or 0):.2f} | {_md_cell(shown)} |")
        lines.append("")
    if r.get("recall_warnings"):
        lines.append("## Possible missed comments")
        lines.append("")
        lines.append("Check these spots on the page renders by eye (crops/p<N>_regions.png shows what was found).")
        lines.append("")
        for w in r["recall_warnings"]:
            lines.append(f"- page {w.get('page_no')}: {w.get('detail')}")
        lines.append("")
    if r.get("errors"):
        lines.append("## Errors")
        lines += [f"- {e}" for e in r["errors"]]
        lines.append("")
    voc = r.get("vocabulary") or {}
    lines.append(f"_tool vocabulary {voc.get('version')} ({voc.get('count')} names; the list offered is in tools.txt). "
                 f"written {time.strftime('%Y-%m-%d %H:%M:%S')}_")
    return "\n".join(lines) + "\n"


def markdown(report: Report) -> str:
    r = report.to_dict()
    if r.get("sequences") or r.get("vocabulary"):      # the current text-to-text flows, even with no comments
        return _markdown_sequences(r)
    counts = r["counts"]
    lines = [f"# Redline review {r['review_id']}", "",
             f"PDFs: {', '.join(r['pdfs'])}", "",
             "| done | skipped | failed | needs a person | dry run | sheet-wide notes |", "|---|---|---|---|---|---|",
             "| {done} | {skipped} | {failed} | {needs_human} | {dry_run} | {directives} |".format(
                 **{k: counts.get(k, 0) for k in ("done", "skipped", "failed", "needs_human", "dry_run", "directives")}), ""]
    located = _by(r["located"], "key")
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
            text = str(c.get("text", "")).replace("|", "\\|")
            if c.get("scope") == "sheet":
                lines.append(f"| {cid} | {text} | {c.get('kind')} | whole sheet | directive | stored as a sheet-wide note (see below) |")
                continue
            keys = _keys_for(cid, located) or [cid]
            for key in keys:
                loc = located.get(key) or {}
                where = "?"
                if loc.get("space") == "model":
                    where = f"model ({loc['model_xy'][0]:.0f}, {loc['model_xy'][1]:.0f})"
                elif loc.get("space") == "paper":
                    where = f"paper ({loc['paper_xy'][0]:.1f}, {loc['paper_xy'][1]:.1f}) in"
                prop = proposals.get(key) or {}
                outc = outcomes.get(key) or {}
                status = outc.get("status") or ("skipped" if prop.get("skip") else "?")
                detail = outc.get("reason") or prop.get("reason") or ""
                if prop.get("blocks"):
                    chain = " → ".join(b["type"] for b in prop["blocks"])
                    detail = (chain + (f" — {detail}" if detail else ""))
                lines.append(f"| {_label(key, len(keys))} | {text} | {c.get('kind')} | {where} | {status} | {str(detail).replace('|', '/')[:160]} |")
        lines.append("")
    for u in r.get("unmatched_pages") or []:
        if u["page_no"] not in sheets:
            lines.append(f"## Page {u['page_no']}\n- **not matched:** {u.get('reason')}\n")
    if r.get("directives"):
        lines.append("## Sheet-wide notes (a person must apply these)")
        lines.append("")
        lines.append("| id | page | note | status |")
        lines.append("|---|---|---|---|")
        for d in r["directives"]:
            lines.append(f"| {d['id']} | {d.get('page_no')} | {str(d['text']).replace('|', '/')} | {d.get('status')} |")
        lines.append("")
        lines.append("Once applied, mark it done in the drawing's file under the review dir's `directives` folder "
                     "(status \"done\"); until then it is shown to the planner as a standing note.")
        lines.append("")
    if r.get("recall_warnings"):
        lines.append("## Possible missed comments")
        lines.append("")
        lines.append("Check these spots on the page renders by eye (crops/p<N>_regions.png shows what was found).")
        lines.append("")
        for w in r["recall_warnings"]:
            lines.append(f"- page {w.get('page_no')}: {w.get('detail')}")
        lines.append("")
    if r.get("errors"):
        lines.append("## Errors")
        lines += [f"- {e}" for e in r["errors"]]
        lines.append("")
    lines.append(f"_written {time.strftime('%Y-%m-%d %H:%M:%S')}_")
    return "\n".join(lines) + "\n"


def training_rows(report: Report, palette_version: str) -> list:
    r = report.to_dict()
    located = _by(r["located"], "key")
    proposals = _by(r["proposals"])
    outcomes = _by(r["outcomes"])
    sheets = {s["page_no"]: s for s in r["sheets"]}
    rows = []
    for c in r["comments"]:
        cid = c["id"]
        sheet = sheets.get(c["page_no"]) or {}
        base = {"ts": time.time(), "review_id": r["review_id"], "comment": c,
                "sheet": {"dwg_path": sheet.get("dwg_path"), "layout_name": sheet.get("layout_name")},
                "palette_version": palette_version, "human_chain": None, "human_verdict": None}
        if c.get("scope") == "sheet":
            rows.append(dict(base, key=cid, located=None, proposal=None, outcome={"comment_id": cid, "status": "directive"}))
            continue
        for key in _keys_for(cid, located) or [cid]:
            rows.append(dict(base, key=key, located=located.get(key), proposal=proposals.get(key),
                             outcome=outcomes.get(key)))
    return rows


def sequence_rows(report: Report, sheet_of: dict | None = None) -> list:
    """One training row per comment for the text-to-text stage: the comment,
    the tool names the model gave, and empty slots for a person's answer."""
    r = report.to_dict()
    seqs = {s["comment_id"]: s for s in r.get("sequences") or []}
    sheet_of = sheet_of or {}
    rows = []
    for c in r["comments"]:
        s = seqs.get(c["id"]) or {}
        tb = sheet_of.get(c["page_no"]) or {}
        rows.append({"ts": time.time(), "review_id": r["review_id"], "key": c["id"], "comment": c,
                     "sheet": {"sheet_no": tb.get("sheet_no"), "printed_file": tb.get("printed_file")},
                     "tools_version": (r.get("vocabulary") or {}).get("version"),
                     "tools": s.get("tools") or [], "unknown_tools": s.get("unknown_tools") or [],
                     "raw_reply": s.get("raw_reply", ""), "model": s.get("model", ""), "error": s.get("error"),
                     "human_tools": None, "human_verdict": None})
    return rows


def append_training(path: str, rows: list) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
    return len(rows)


def annotate(review_dir: str, review_id: str, comment_id: str, chain_path: str | None, verdict: str | None) -> dict:
    """Records a person's chain and/or verdict for one comment (or one
    target, `p1-c3#2`) of one review, in that review's training.jsonl (rows
    rewritten in place)."""
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
        if row.get("key", (row.get("comment") or {}).get("id")) == comment_id:
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
