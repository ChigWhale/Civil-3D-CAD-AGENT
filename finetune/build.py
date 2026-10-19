"""`python -m finetune build`: the labels on disk -> train.jsonl / holdout.jsonl.

Sources (both under the review folder redline.config resolves):
  - the label set: <review dir>/labelset/labelset.json, text typed by a person
    and the chain they chose (kind "note", confidence 1.0, what a typed
    comment gets in `redline review --text`);
  - every run's training.jsonl: a row counts when a person gave a chain
    (human_tools, whatever the verdict) or marked the model's chain Right
    (human_verdict "good"); unsure, bad-without-a-chain, unlabelled and
    model-error rows do not.
Targets are checked against the tool vocabulary; duplicates by normalised
text are resolved label set > human chain > Right verdict; the held-out
split is by hash of the text, so a row stays on its side as data grows.
The prompt of every row is rendered by redline.sequence in short mode.
System Python: stdlib + redline (the vocabulary needs cad_mcp_server; a
tools.txt snapshot or the bin tables stand in when it cannot be imported).
"""
import hashlib
import os
import time
from collections import Counter

from . import paths, prompts
from redline import config, labelset, metrics, sequence, tools

HOLDOUT_DEFAULT = 0.2
MIN_PER_CLASS = 10
MIN_PROJECTS = 2
RANK = {"labelset": 0, "run:human": 1, "run:good": 2}
FILES = ("train.jsonl", "holdout.jsonl", "rows.jsonl", "manifest.json", "tools.txt")


def norm_key(text: str) -> str:
    """Upper-cased, punctuation stripped, whitespace collapsed: the key
    metrics.group_key uses to say two comments are the same."""
    return metrics.group_key({"text": text}, None, None)[0]


# ── vocabulary ───────────────────────────────────────────────────────────────

def parse_tools_txt(path: str) -> tuple:
    """(names, text) from a tools.txt: lines "- name: description"; header
    lines starting with # are skipped."""
    names, lines = [], []
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("- "):
                names.append(s[2:].split(":", 1)[0].strip())
                lines.append(s)
    return names, "\n".join(lines)


def _version_of(names: list) -> str:
    return hashlib.sha1("\n".join(names).encode("utf-8")).hexdigest()[:12]


def vocabulary(tools_file: str | None = None, labelset_dir: str | None = None, log=print) -> dict:
    """{names, text, version, source}: the registry when cad_mcp_server
    imports, else a tools.txt (given, or the label set's snapshot), else
    every name in the bin tables."""
    if tools_file:
        names, text = parse_tools_txt(tools_file)
        return {"names": names, "text": text, "version": _version_of(names), "source": f"file:{tools_file}"}
    try:
        names = tools.names()
        return {"names": names, "text": tools.text(), "version": tools.version(), "source": "registry"}
    except ImportError as e:
        log(f"  (tool registry not importable: {str(e)[:120]})")
    snap = os.path.join(labelset_dir, "tools.txt") if labelset_dir else ""
    if snap and os.path.isfile(snap):
        names, text = parse_tools_txt(snap)
        return {"names": names, "text": text, "version": _version_of(names), "source": f"snapshot:{snap}"}
    names = sorted(set().union(*tools.BINS.values()))
    return {"names": names, "text": "\n".join(f"- {n}" for n in names), "version": _version_of(names), "source": "bins"}


# ── candidates ───────────────────────────────────────────────────────────────

def labelset_candidates(ls_dir: str) -> list:
    out = []
    for e in labelset.rows(labelset.load(ls_dir)):
        out.append({"id": f"ls:{e['id']}", "text": str(e.get("text") or "").strip(), "kind": prompts.DEFAULT_KIND,
                    "confidence": prompts.DEFAULT_CONFIDENCE, "raw_tools": list(e.get("tools") or []),
                    "source": "labelset", "review_id": None, "project": "labelset", "labeller": e.get("labeller"),
                    "updated": float(e.get("updated") or 0), "ts": float(e.get("created") or 0)})
    return out


def training_files(review_dir: str) -> list:
    """Every training.jsonl one or two levels under the review folder
    (the label set folder is not a run)."""
    out = []
    if not os.path.isdir(review_dir):
        return out
    for name in sorted(os.listdir(review_dir)):
        if name == labelset.SUBDIR:
            continue
        d = os.path.join(review_dir, name)
        if not os.path.isdir(d):
            continue
        p = os.path.join(d, "training.jsonl")
        if os.path.isfile(p):
            out.append(p)
        for sub in sorted(os.listdir(d)):
            p2 = os.path.join(d, sub, "training.jsonl")
            if os.path.isfile(p2):
                out.append(p2)
    return out


def _project_of(row: dict, rid: str) -> str:
    sheet = row.get("sheet") if isinstance(row.get("sheet"), dict) else {}
    if sheet.get("dwg_path"):
        return os.path.basename(str(sheet["dwg_path"]).replace("\\", "/"))
    if sheet.get("printed_file"):
        return str(sheet["printed_file"])
    return rid


def run_candidates(review_dir: str) -> tuple:
    """(candidates, counts, decisions, files)"""
    cands, counts, decisions = [], Counter(), []
    files = training_files(review_dir)
    for p in files:
        rid = os.path.relpath(os.path.dirname(p), review_dir).replace(os.sep, "/")
        for row in paths.read_jsonl(p):
            counts["run_rows"] += 1
            c = row.get("comment") if isinstance(row.get("comment"), dict) else {}
            text = str(c.get("text") or "").strip()
            key = row.get("key") or c.get("id") or f"row{counts['run_rows']}"
            rid_key = f"run:{rid}:{key}"
            ht, verdict = row.get("human_tools"), row.get("human_verdict")
            if not text:
                counts["skipped_empty_text"] += 1
                decisions.append({"id": rid_key, "decision": "skipped_empty_text"})
                continue
            if isinstance(ht, list) and ht:
                target, source = ht, "run:human"
                counts["from_human_tools"] += 1
            elif verdict == "good":
                if row.get("error"):
                    counts["skipped_error"] += 1
                    decisions.append({"id": rid_key, "decision": "skipped_error", "text": text[:80]})
                    continue
                if not row.get("tools"):
                    counts["skipped_empty_tools"] += 1
                    decisions.append({"id": rid_key, "decision": "skipped_empty_tools", "text": text[:80]})
                    continue
                target, source = list(row["tools"]), "run:good"
                counts["from_good_verdict"] += 1
            else:
                why = {"unsure": "skipped_unsure", "bad": "skipped_bad_nofix"}.get(verdict, "skipped_unlabelled")
                counts[why] += 1
                decisions.append({"id": rid_key, "decision": why, "text": text[:80]})
                continue
            cands.append({"id": rid_key, "text": text, "kind": str(c.get("kind") or prompts.DEFAULT_KIND),
                          "confidence": float(c.get("confidence") if c.get("confidence") is not None else 1.0),
                          "raw_tools": [str(t) for t in target], "source": source, "review_id": rid,
                          "project": _project_of(row, rid), "labeller": row.get("human_labeller"),
                          "updated": float(row.get("annotated_at") or 0), "ts": float(row.get("ts") or 0)})
    return cands, counts, decisions, files


# ── selection, dedupe, split ─────────────────────────────────────────────────

def clean_target(raw: list, names: list) -> tuple:
    """(tools, unknown): names normalised against the vocabulary, consecutive
    repeats folded."""
    found, unknown = [], []
    for t in raw or []:
        n = tools.normalise(t, names)
        if n is None:
            unknown.append(str(t))
        elif not found or found[-1] != n:
            found.append(n)
    return found, unknown


def select(cands: list, names: list) -> tuple:
    """(rows, counts, decisions, unknown_names): targets cleaned; rows with
    an unknown name, too many tools or no tools dropped and counted."""
    rows, counts, decisions, unknown_names = [], Counter(), [], Counter()
    for c in cands:
        found, unknown = clean_target(c["raw_tools"], names)
        if unknown:
            counts["dropped_unknown_vocab"] += 1
            for u in unknown:
                unknown_names[u] += 1
            decisions.append({"id": c["id"], "decision": "dropped_unknown_vocab", "unknown": unknown, "text": c["text"][:80]})
            continue
        if not found:
            counts["dropped_empty_tools"] += 1
            decisions.append({"id": c["id"], "decision": "dropped_empty_tools", "text": c["text"][:80]})
            continue
        if len(found) > sequence.MAX_TOOLS:
            counts["dropped_too_long"] += 1
            decisions.append({"id": c["id"], "decision": "dropped_too_long", "n": len(found), "text": c["text"][:80]})
            continue
        r = dict(c, tools=found, key=norm_key(c["text"]))
        rows.append(r)
    return rows, counts, decisions, dict(unknown_names)


def dedupe(rows: list) -> tuple:
    """(kept, duplicates, conflicts): one row per normalised text; the label
    set beats a person's chain on a run row, which beats a Right verdict;
    ties go to the latest label."""
    order = sorted(rows, key=lambda r: (RANK.get(r["source"], 9), -float(r.get("updated") or 0), -float(r.get("ts") or 0)))
    kept, dups, conflicts = {}, [], []
    for r in order:
        w = kept.get(r["key"])
        if w is None:
            kept[r["key"]] = r
            continue
        dups.append({"id": r["id"], "decision": "duplicate", "kept": w["id"]})
        if w["tools"] != r["tools"]:
            conflicts.append({"kept": w["id"], "kept_tools": w["tools"], "dropped": r["id"], "dropped_tools": r["tools"],
                              "text": r["text"][:120]})
    return list(kept.values()), dups, conflicts


def hash_of(key: str) -> int:
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16) % 10000


def example_keys() -> set:
    return {norm_key(e["comment"]) for e in sequence.load_examples()}


def split(rows: list, fraction: float, pinned: set | None = None) -> tuple:
    """(train, holdout) by hash of the text. Rows whose text is a few-shot
    example are pinned to train. At least one holdout row when n >= 5 and
    the fraction is positive; always at least one train row."""
    pinned = pinned or set()
    for r in rows:
        r["norm_hash"] = hash_of(r["key"])
        r["in_examples"] = r["key"] in pinned
    eligible = [r for r in rows if not r["in_examples"]]
    hold = []
    if fraction > 0:
        hold = [r for r in eligible if r["norm_hash"] < int(round(fraction * 10000))]
        if not hold and len(rows) >= 5 and eligible:
            hold = [min(eligible, key=lambda r: r["norm_hash"])]
    while hold and len(rows) - len(hold) < 1:
        hold.remove(max(hold, key=lambda r: r["norm_hash"]))
    hold_ids = {r["id"] for r in hold}
    train = [r for r in rows if r["id"] not in hold_ids]
    return train, hold


def class_table(train: list, hold: list) -> dict:
    out = {}
    for name, rows in (("train", train), ("holdout", hold)):
        for r in rows:
            cls = r["class"]
            e = out.setdefault(cls, {"train": 0, "holdout": 0, "total": 0, "projects": set()})
            e[name] += 1
            e["total"] += 1
            e["projects"].add(r["project"])
    for e in out.values():
        e["projects"] = sorted(e["projects"])
    return out


def warnings_for(classes: dict, min_per_class: int, min_projects: int = MIN_PROJECTS) -> list:
    out = []
    for cls in sorted(classes):
        e = classes[cls]
        if e["total"] < min_per_class or len(e["projects"]) < min_projects:
            out.append(f"{cls}: {e['total']} row(s) from {len(e['projects'])} project(s); "
                       f"the README asks for {min_per_class} or more across {min_projects} projects")
    return out


def out_row(r: dict) -> dict:
    msgs = prompts.messages(r["text"], r["kind"], r["confidence"])
    return {"id": r["id"], "prompt": msgs, "completion": prompts.completion_messages(r["tools"]),
            "meta": {"text": r["text"], "kind": r["kind"], "confidence": r["confidence"], "tools": r["tools"],
                     "class": r["class"], "source": r["source"], "review_id": r["review_id"], "project": r["project"],
                     "labeller": r.get("labeller"), "norm_hash": r["norm_hash"], "in_examples": r["in_examples"],
                     "prompt_chars": len(msgs[1]["content"])}}


# ── the command ──────────────────────────────────────────────────────────────

def build(review_dir: str | None = None, labelset_dir: str | None = None, tools_file: str | None = None,
          holdout: float = HOLDOUT_DEFAULT, min_per_class: int = MIN_PER_CLASS, out_dir: str | None = None,
          log=print) -> dict:
    """Writes the dataset files and returns the manifest (with "exit": 0, or 1
    when no labelled row was found)."""
    settings = config.load()
    review_dir = review_dir or settings.review_dir
    labelset_dir = labelset_dir or labelset.folder(settings)
    out_dir = out_dir or paths.data_dir()
    voc = vocabulary(tools_file, labelset_dir, log=log)
    names = voc["names"]

    ls = labelset_candidates(labelset_dir)
    runs, counts, decisions, files = run_candidates(review_dir)
    counts["labelset"] = len(ls)
    counts["runs_scanned"] = len(files)
    rows, sel_counts, sel_dec, unknown_names = select(ls + runs, names)
    counts.update(sel_counts)
    decisions += sel_dec
    rows, dups, conflicts = dedupe(rows)
    counts["duplicates"] = len(dups)
    counts["conflicts"] = len(conflicts)
    decisions += dups
    for r in rows:
        r["class"] = tools.chain_class(r["tools"])
    train, hold = split(rows, float(holdout), example_keys())
    counts["in_examples"] = sum(1 for r in rows if r["in_examples"])
    counts["selected"] = len(rows)
    counts["train"] = len(train)
    counts["holdout"] = len(hold)
    for r in train:
        decisions.append({"id": r["id"], "decision": "train", "source": r["source"], "class": r["class"]})
    for r in hold:
        decisions.append({"id": r["id"], "decision": "holdout", "source": r["source"], "class": r["class"]})

    train_rows = [out_row(r) for r in sorted(train, key=lambda r: r["norm_hash"])]
    hold_rows = [out_row(r) for r in sorted(hold, key=lambda r: r["norm_hash"])]
    paths.write_jsonl(os.path.join(out_dir, "train.jsonl"), train_rows)
    if hold_rows or holdout > 0:
        paths.write_jsonl(os.path.join(out_dir, "holdout.jsonl"), hold_rows)
    elif os.path.isfile(os.path.join(out_dir, "holdout.jsonl")):
        os.remove(os.path.join(out_dir, "holdout.jsonl"))
    paths.write_jsonl(os.path.join(out_dir, "rows.jsonl"), decisions)
    paths.write_text(os.path.join(out_dir, "tools.txt"),
                     f"# {len(names)} tools (version {voc['version']}, {voc['source']})\n{voc['text']}\n")
    with open(os.path.join(out_dir, "train.jsonl"), "rb") as f:
        h = hashlib.sha1(f.read())
    if os.path.isfile(os.path.join(out_dir, "holdout.jsonl")):
        with open(os.path.join(out_dir, "holdout.jsonl"), "rb") as f:
            h.update(f.read())
    classes = class_table(train, hold)
    seen = sorted({t for r in rows for t in r["tools"]})
    warns = warnings_for(classes, min_per_class)
    examples = sequence.load_examples()
    manifest = {
        "created": time.time(), "dataset_id": h.hexdigest()[:12], "review_dir": review_dir, "labelset_dir": labelset_dir,
        "out_dir": out_dir, "holdout_fraction": float(holdout), "min_per_class": min_per_class, "prompt_mode": prompts.MODE,
        "counts": dict(counts), "unknown_names": unknown_names, "conflicts": conflicts, "classes": classes,
        "warnings": warns, "tools_version": voc["version"], "tools_count": len(names), "tools_source": voc["source"],
        "coverage": {"names_seen": len(seen), "names_total": len(names), "unseen": [n for n in names if n not in seen]},
        "examples_count": len(examples), "system_sha1": prompts.system_sha1(),
        "max_prompt_chars": max([r["meta"]["prompt_chars"] for r in train_rows + hold_rows] + [0]),
        "exit": 0 if rows else 1,
    }
    paths.write_json(os.path.join(out_dir, "manifest.json"), manifest)
    _report(manifest, log)
    return manifest


def _report(m: dict, log) -> None:
    c = m["counts"]
    log(f"build: {c.get('labelset', 0)} label-set example(s), {c.get('run_rows', 0)} run row(s) in {c.get('runs_scanned', 0)} file(s)"
        f" -> {c.get('selected', 0)} selected ({c.get('train', 0)} train, {c.get('holdout', 0)} holdout)")
    skipped = {k: v for k, v in c.items() if k.startswith(("skipped_", "dropped_"))}
    if skipped or c.get("duplicates"):
        log("  not used: " + ", ".join(f"{k} {v}" for k, v in sorted(skipped.items()))
            + (f", duplicates {c['duplicates']} (conflicting {c['conflicts']})" if c.get("duplicates") else ""))
    if m["unknown_names"]:
        log("  unknown tool names: " + ", ".join(f"{k}×{v}" for k, v in sorted(m["unknown_names"].items())))
    log(f"  vocabulary: {m['tools_count']} tools, version {m['tools_version']} ({m['tools_source']}); "
        f"{m['coverage']['names_seen']} of them appear in a target")
    if m["classes"]:
        log("  class       train  holdout  projects")
        for cls in sorted(m["classes"]):
            e = m["classes"][cls]
            log(f"  {cls:<10} {e['train']:>6} {e['holdout']:>8}  {len(e['projects'])}")
    for w in m["warnings"]:
        log(f"  warning: {w}")
    log(f"  dataset {m['dataset_id']} in {m['out_dir']}")
    if m["exit"]:
        log("  no labelled rows: label some comments in the results window or the label set first")
