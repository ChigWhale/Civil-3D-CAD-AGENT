"""Metrics over a redline review: pure functions of report.json (Report.to_dict())
and the "comments" map of labels.json. Nothing here touches the registry or
the plugin; only the bin / tier tables of redline.tools are used.

One line per metric:
- RATE: {k, n, rate, low, high}; rate = k/n (None when n == 0) with a Wilson 95% interval (0.0..1.0 when n == 0).
- coverage: comments the pipeline attempted (an action with status done or dry_run) over every comment.
- precision: labelled comments judged good over those judged good or bad (unsure ignored, attempted or not).
- by_class: coverage and precision split by tools.chain_class of the proposed chain (read / draw / modify / delete / design / none).
- doability: four yes/no checks on a located comment (grounded, action_covered, params_determinable, unambiguous), score = passes / 4.
- risk_coverage: coverage, precision and risk (bad share) if only comments with doability score >= threshold were acted on; a missing score is never covered.
- calibration: expected calibration error of the doability score against good/bad labels, bucketed by exact score.
- chains: proposed chain vs gold chain after collapse() (lookups -> "lookup", repeats folded): set precision/recall/F1,
  order = 1 - Levenshtein / max length, first wrong step, exact match, step accuracy by position.
- pass^k: for a comment seen in several runs (same normalised text and drawing), the chance that k runs drawn at random all pass.
- kappa: Cohen's kappa between the two labellers who share the most comments, over their verdicts.
"""
import math
import os
import re
from collections import Counter
from itertools import combinations
from math import comb

from . import tools

LOOKUP = tools.LOOKUP_CLASS
ATTEMPTED = frozenset({"done", "dry_run"})
STEP_STATUSES = ("ran", "dry", "failed", "blocked", "not_filled", "planned")
CHECKS = ("grounded", "action_covered", "params_determinable", "unambiguous")
DEFAULT_THRESHOLDS = (0.0, 0.25, 0.5, 0.75, 1.0)
LOCATED_SPACES = frozenset({"model", "paper"})


# ── chains ───────────────────────────────────────────────────────────────────
def collapse(seq) -> list:
    """Lookup tools become the single class "lookup"; consecutive duplicates fold into one."""
    out = []
    for name in (seq or []):
        n = "lookup" if str(name) in LOOKUP else str(name)
        if not out or out[-1] != n:
            out.append(n)
    return out


def levenshtein(a, b) -> int:
    a, b = list(a or []), list(b or [])
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if x == y else 1)))
        prev = cur
    return prev[-1]


def tool_set_scores(pred, gold) -> dict:
    """Set precision / recall / F1 of the collapsed chains. Both empty -> 1/1/1; one side empty -> 0/0/0."""
    p, g = set(collapse(pred)), set(collapse(gold))
    if not p and not g:
        return {"tp": 0, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0}
    tp, fp, fn = len(p & g), len(p - g), len(g - p)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def order_score(pred, gold) -> float:
    """1 - Levenshtein / max length over the collapsed chains; both empty -> 1.0."""
    a, b = collapse(pred), collapse(gold)
    m = max(len(a), len(b))
    return 1.0 if m == 0 else 1.0 - levenshtein(a, b) / m


def first_wrong_step(pred, gold):
    """0-based index of the first differing collapsed step; the shorter length when one
    chain is a strict prefix of the other; None when they are equal."""
    a, b = collapse(pred), collapse(gold)
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return i
    return None if len(a) == len(b) else min(len(a), len(b))


def step_accuracy_by_position(pairs, max_pos: int = 8) -> list:
    """Per position i: over the pairs whose collapsed chains agree on steps < i and whose
    gold has a step i, how often the predicted step i is right. Positions with no pairs are left out."""
    out = []
    for i in range(max_pos):
        k = n = 0
        for pred, gold in pairs:
            a, b = collapse(pred), collapse(gold)
            if len(b) > i and a[:i] == b[:i]:
                n += 1
                if len(a) > i and a[i] == b[i]:
                    k += 1
        if n:
            out.append({"position": i, **rate(k, n)})
    return out


def chain_metrics(pred, gold) -> dict:
    return {"scores": tool_set_scores(pred, gold), "order": order_score(pred, gold),
            "first_wrong": first_wrong_step(pred, gold), "exact": collapse(pred) == collapse(gold)}


# ── rates ────────────────────────────────────────────────────────────────────
def wilson(k, n, z: float = 1.96) -> tuple:
    """Wilson score interval; (0.0, 1.0) when n == 0."""
    if not n or n <= 0:
        return (0.0, 1.0)
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def rate(k, n) -> dict:
    k, n = int(k), int(n)
    low, high = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n) if n else None, "low": low, "high": high}


def _mean(xs):
    xs = [float(x) for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


# ── doability ────────────────────────────────────────────────────────────────
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def doability(context, sequence, steps, unknown_tools=()) -> dict:
    """Could the pipeline have done this comment? Four checks on the located context, the
    proposed chain and the filled steps; score = passes / 4; reasons say which failed and why."""
    reasons = []
    ctx = context if isinstance(context, dict) else {}
    pick = ctx.get("pick")
    obj, enclosing, candidates = None, [], []
    if isinstance(pick, dict):
        obj = pick.get("object") if isinstance(pick.get("object"), dict) else None
        enclosing = [e for e in (pick.get("enclosing") or []) if isinstance(e, dict)]
        candidates = [c for c in (pick.get("candidates") or []) if isinstance(c, dict)]
        grounded = obj is not None or bool(enclosing)
        if not grounded:
            reasons.append("no object under the arrow tip")
    else:
        grounded = False
        reasons.append("not located")

    names = [str(t) for t in (sequence or [])]
    unknown = [str(t) for t in (unknown_tools or [])]
    unknown += [n for n in names if n not in unknown and tools.bin_of(n) == "other"]
    if not names:
        action_covered = False
        reasons.append("no tool sequence")
    elif unknown:
        action_covered = False
        reasons.append("unknown tools: " + ", ".join(unknown))
    else:
        action_covered = True

    steps_ = [s for s in (steps or []) if isinstance(s, dict)]
    not_filled = [s for s in steps_ if s.get("status") == "not_filled"]
    if not steps_:
        params_determinable = False
        reasons.append("no step attempted")
    elif not_filled:
        params_determinable = False
        reasons.extend(f"{s.get('tool') or '?'}: {s.get('error') or 'parameters not filled'}" for s in not_filled)
    else:
        params_determinable = True

    tol = _num(ctx.get("pick_tolerance_ft"))
    tol = 5.0 if tol is None else tol
    unambiguous = True
    if obj is not None:
        rivals = []
        for c in candidates:
            if str(c.get("type") or "") == "label":
                continue
            if c.get("handle") and c.get("handle") == obj.get("handle"):
                continue
            d = _num(c.get("distance"))
            if d is None or d <= tol:
                rivals.append((c, d))
        if rivals:
            unambiguous = False
            for c, d in rivals:
                where = "at the arrow tip" if d is None else f"{d:g} ft away"
                reasons.append(f"rival {c.get('handle') or '?'} ({c.get('type') or 'object'}) {where}")
    elif enclosing and len(enclosing) != 1:
        unambiguous = False
        reasons.append(f"{len(enclosing)} enclosing shapes: " + ", ".join(str(e.get("handle") or "?") for e in enclosing))

    flags = {"grounded": grounded, "action_covered": action_covered,
             "params_determinable": params_determinable, "unambiguous": unambiguous}
    passes = sum(1 for v in flags.values() if v)
    return {**flags, "passes": passes, "score": passes / len(CHECKS), "reasons": reasons}


# ── outcomes ─────────────────────────────────────────────────────────────────
def outcome_counts(report) -> dict:
    """Status counts of actions and steps, sequence outcomes, steps per bin and per risk tier."""
    report = report or {}
    actions = Counter()
    steps = Counter()
    bins = {}
    tiers = Counter()
    for a in report.get("actions") or []:
        if not isinstance(a, dict):
            continue
        actions[str(a.get("status") or "unknown")] += 1
        for s in a.get("steps") or []:
            if not isinstance(s, dict):
                continue
            status = str(s.get("status") or "unknown")
            steps[status] += 1
            tool = str(s.get("tool") or "")
            b = s.get("bin") or tools.bin_of(tool)
            t = s.get("risk") or tools.risk_of(tool)
            row = bins.setdefault(b, {"steps": 0, **{k: 0 for k in STEP_STATUSES}})
            row["steps"] += 1
            if status in row:
                row[status] += 1
            tiers[t] += 1
    seq = {"sequenced": 0, "empty": 0, "model_errors": 0}
    for s in report.get("sequences") or []:
        if not isinstance(s, dict):
            continue
        if s.get("error"):
            seq["model_errors"] += 1
        elif s.get("tools"):
            seq["sequenced"] += 1
        else:
            seq["empty"] += 1
    order = list(tools.BIN_ORDER) + sorted(b for b in bins if b not in tools.BIN_ORDER)
    tier_order = list(tools.TIER_ORDER) + sorted(t for t in tiers if t not in tools.TIER_ORDER)
    return {"actions": dict(actions), "steps": dict(steps), "sequences": seq,
            "bins": {b: bins[b] for b in order if b in bins},
            "tiers": {t: tiers[t] for t in tier_order if t in tiers}}


def _merge_counts(a, b):
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict):
            out[k] = _merge_counts(out.get(k) or {}, v)
        else:
            out[k] = (out.get(k) or 0) + v
    return out


# ── coverage, precision, risk ────────────────────────────────────────────────
def _cov_prec(records):
    attempted = sum(1 for r in records if r.get("attempted"))
    good = sum(1 for r in records if r.get("verdict") == "good")
    bad = sum(1 for r in records if r.get("verdict") == "bad")
    return rate(attempted, len(records)), rate(good, good + bad)


def coverage_precision(records) -> dict:
    """records [{"attempted": bool, "verdict": str|None, "action_class": str}]."""
    records = list(records or [])
    coverage, precision = _cov_prec(records)
    classes = {str(r.get("action_class") or "none") for r in records}
    order = ["none"] + list(tools.TIER_ORDER) + sorted(c for c in classes if c != "none" and c not in tools.TIER_ORDER)
    by_class = {}
    for cls in order:
        rs = [r for r in records if str(r.get("action_class") or "none") == cls]
        if rs:
            c, p = _cov_prec(rs)
            by_class[cls] = {"coverage": c, "precision": p}
    return {"coverage": coverage, "precision": precision, "by_class": by_class}


def risk_coverage(records, thresholds=DEFAULT_THRESHOLDS) -> list:
    """records [{"score": float|None, "verdict"}]: acting only on score >= threshold, the share covered,
    the precision among covered labelled ones and the risk (bad share) taken."""
    records = list(records or [])
    out = []
    for t in thresholds:
        covered = [r for r in records if _num(r.get("score")) is not None and _num(r.get("score")) >= t]
        good = sum(1 for r in covered if r.get("verdict") == "good")
        bad = sum(1 for r in covered if r.get("verdict") == "bad")
        out.append({"threshold": t, "coverage": rate(len(covered), len(records)),
                    "precision": rate(good, good + bad), "risk": rate(bad, good + bad)})
    return out


def calibration(records) -> dict:
    """ECE of the doability score against good/bad labels, one bucket per distinct score."""
    labelled = [(float(_num(r.get("score"))), r.get("verdict")) for r in (records or [])
                if r.get("verdict") in ("good", "bad") and _num(r.get("score")) is not None]
    n = len(labelled)
    buckets = []
    ece = 0.0
    for score in sorted({s for s, _ in labelled}):
        good = sum(1 for s, v in labelled if s == score and v == "good")
        bad = sum(1 for s, v in labelled if s == score and v == "bad")
        nb = good + bad
        buckets.append({"score": score, "n": nb, "accuracy": rate(good, nb)})
        ece += nb / n * abs(score - good / nb)
    return {"ece": ece if n else None, "n": n, "buckets": buckets}


# ── agreement across runs and labellers ──────────────────────────────────────
def pass_k(groups, k: int) -> dict:
    """groups [[bool, ...]]: per group with n >= k runs, C(passes, k) / C(n, k); value = mean over those groups."""
    values = []
    runs = 0
    for g in groups or []:
        g = [bool(x) for x in g]
        n = len(g)
        if n < k or k < 1:
            continue
        c = sum(1 for x in g if x)
        values.append(comb(c, k) / comb(n, k))
        runs += n
    return {"k": k, "value": _mean(values), "groups": len(values), "runs": runs}


def cohens_kappa(pairs) -> dict:
    """pairs [(a, b)] of two labellers' verdicts on the same items; kappa None when n < 2 or pe == 1."""
    pairs = [(str(a), str(b)) for a, b in (pairs or [])]
    n = len(pairs)
    if n == 0:
        return {"kappa": None, "po": None, "pe": None, "n": 0}
    po = sum(1 for a, b in pairs if a == b) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[c] / n * cb[c] / n for c in set(ca) | set(cb))
    kappa = None if (n < 2 or pe >= 1.0) else (po - pe) / (1.0 - pe)
    return {"kappa": kappa, "po": po, "pe": pe, "n": n}


_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def _basename(path) -> str:
    return os.path.basename(str(path).replace("\\", "/"))


def group_key(comment, sheet, pdfs) -> tuple:
    """(normalised text, drawing) that identifies the same comment across runs: text upper-cased,
    punctuation stripped, whitespace collapsed; drawing = basename of the sheet's dwg, else its
    title block's printed file, else the first pdf's basename, else "typed"."""
    text = _PUNCT.sub("", str((comment or {}).get("text") or ""))
    text = " ".join(text.upper().split())
    drawing = ""
    if isinstance(sheet, dict):
        if sheet.get("dwg_path"):
            drawing = _basename(sheet["dwg_path"])
        else:
            tb = sheet.get("title_block") if isinstance(sheet.get("title_block"), dict) else {}
            drawing = str(tb.get("printed_file") or "")
    if not drawing and pdfs:
        drawing = _basename(pdfs[0])
    return (text, drawing or "typed")


# ── labels and per-comment records ───────────────────────────────────────────
def _latest_label(entry):
    """(labeller, record) with the greatest `updated`; (None, None) when nothing is there."""
    best_name, best, best_t = None, None, None
    for name, rec in (entry or {}).items():
        if not isinstance(rec, dict):
            continue
        t = _num(rec.get("updated")) or 0.0
        if best_t is None or t > best_t:
            best_name, best, best_t = str(name), rec, t
    return best_name, best


def label_pairs(report, labels) -> list:
    """One row per comment: the model's chain (pred), the gold chain (the latest label's tools, else
    the model's own chain when the verdict is good, else None), that label's verdict and labeller."""
    return [{"comment_id": r["comment_id"], "pred": r["pred"], "gold": r["gold"], "verdict": r["verdict"],
             "labeller": r["labeller"]} for r in _records(report, labels)]


def _records(report, labels) -> list:
    report = report if isinstance(report, dict) else {}
    labels = labels if isinstance(labels, dict) else {}
    seqs = {}
    for s in report.get("sequences") or []:
        if isinstance(s, dict):
            seqs.setdefault(str(s.get("comment_id")), s)
    acts = {}
    for a in report.get("actions") or []:
        if isinstance(a, dict):
            acts.setdefault(str(a.get("comment_id")), []).append(a)
    sheets = {}
    for sh in report.get("sheets") or []:
        if isinstance(sh, dict):
            sheets.setdefault(sh.get("page_no"), sh)
    pdfs = [str(p) for p in (report.get("pdfs") or [])]
    out = []
    for c in report.get("comments") or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id"))
        seq = seqs.get(cid) or {}
        pred = [str(t) for t in (seq.get("tools") or [])]
        entry = labels.get(cid) if isinstance(labels.get(cid), dict) else {}
        labeller, lab = _latest_label(entry)
        verdict = lab.get("verdict") if lab else None
        verdict = str(verdict) if verdict is not None else None
        gold_tools = lab.get("tools") if lab else None
        if isinstance(gold_tools, list):
            gold = [str(t) for t in gold_tools]
        else:
            gold = list(pred) if verdict == "good" else None
        actions = acts.get(cid) or []
        first = actions[0] if actions else None
        step_tools = [s.get("tool") for a in actions for s in (a.get("steps") or []) if isinstance(s, dict)]
        action_class = tools.chain_class(pred)
        doab = first.get("doability") if first else None
        doab = doab if isinstance(doab, dict) else None
        out.append({
            "comment_id": cid,
            "pred": pred,
            "gold": gold,
            "verdict": verdict,
            "labeller": labeller,
            "labelled": bool(lab) and (verdict is not None or isinstance(gold_tools, list)),
            "attempted": any(a.get("status") in ATTEMPTED for a in actions),
            "action_class": action_class,
            "tier": tools.chain_class(step_tools) if step_tools else action_class,
            "chain": chain_metrics(pred, gold) if gold is not None else None,
            "doability": doab,
            "score": _num(doab.get("score")) if doab else None,
            "status": str(first.get("status")) if first and first.get("status") is not None else None,
            "group": group_key(c, sheets.get(c.get("page_no")), pdfs),
            "labellers": {str(n): str(r.get("verdict")) for n, r in entry.items()
                          if isinstance(r, dict) and r.get("verdict") is not None},
        })
    return out


def _located_count(report) -> int:
    return sum(1 for l in ((report or {}).get("located") or []) if isinstance(l, dict) and l.get("space") in LOCATED_SPACES)


def _chain_block(records) -> dict:
    rows = [r for r in records if r.get("chain") is not None]
    cm = [r["chain"] for r in rows]
    first_wrong = Counter("none" if c["first_wrong"] is None else str(c["first_wrong"]) for c in cm)
    fw = {"none": first_wrong.get("none", 0)}
    for k in sorted((k for k in first_wrong if k != "none"), key=int):
        fw[k] = first_wrong[k]
    return {
        "n": len(cm),
        "precision": _mean(c["scores"]["precision"] for c in cm),
        "recall": _mean(c["scores"]["recall"] for c in cm),
        "f1": _mean(c["scores"]["f1"] for c in cm),
        "order": _mean(c["order"] for c in cm),
        "pooled": {"tp": sum(c["scores"]["tp"] for c in cm), "fp": sum(c["scores"]["fp"] for c in cm),
                   "fn": sum(c["scores"]["fn"] for c in cm)},
        "exact": rate(sum(1 for c in cm if c["exact"]), len(cm)),
        "first_wrong": fw,
        "by_position": step_accuracy_by_position([(r["pred"], r["gold"]) for r in rows]),
    }


def _public(r) -> dict:
    return {k: r[k] for k in ("pred", "gold", "verdict", "labeller", "attempted", "action_class", "tier", "chain",
                              "doability", "status")}


def _summarise(records, outcomes, located, key="comment_id") -> dict:
    labelled = [r for r in records if r["labelled"]]
    doabs = [r["doability"] for r in records if r["doability"]]
    scores = [r["score"] for r in records if r["doability"] and r["score"] is not None]
    cp = coverage_precision(records)
    return {
        "n": {"comments": len(records),
              "sequenced": sum(1 for r in records if r["pred"]),
              "located": located,
              "acted": sum(1 for r in records if r["attempted"]),
              "labelled": len(labelled),
              "labelled_chains": sum(1 for r in records if r["gold"] is not None)},
        "outcomes": outcomes,
        "coverage": cp["coverage"],
        "precision": cp["precision"],
        "by_class": cp["by_class"],
        "doability": {"n": len(doabs), "mean": _mean(scores),
                      "checks": {c: rate(sum(1 for d in doabs if d.get(c)), len(doabs)) for c in CHECKS}},
        "risk_coverage": risk_coverage(records),
        "calibration": calibration(records),
        "chains": _chain_block(records),
        "per_comment": {r[key]: _public(r) for r in records},
    }


def review_metrics(report, labels) -> dict:
    """The metrics of one review: report = Report.to_dict(), labels = the "comments" map of labels.json."""
    records = _records(report, labels)
    return _summarise(records, outcome_counts(report), _located_count(report))


def _pass_k_block(records) -> dict:
    groups = {}
    for r in records:
        groups.setdefault(r["group"], []).append(r)
    bools, rows = [], []
    for (text, dwg), rs in groups.items():
        golds = [collapse(r["gold"]) for r in rs if r["gold"] is not None]
        passes = []
        for r in rs:
            if r["verdict"] == "good":
                passes.append(True)
            elif golds:
                passes.append(collapse(r["pred"]) in golds)
        if not passes:
            continue
        bools.append(passes)
        rows.append({"text": text, "dwg": dwg, "k": len(passes), "passes": sum(1 for p in passes if p)})
    rows.sort(key=lambda g: (-g["k"], g["dwg"], g["text"]))
    return {"1": pass_k(bools, 1), "2": pass_k(bools, 2), "3": pass_k(bools, 3), "groups": rows}


def _kappa_block(records) -> dict:
    shared = Counter()
    for r in records:
        for a, b in combinations(sorted(r["labellers"]), 2):
            shared[(a, b)] += 1
    if not shared:
        return {"kappa": None, "po": None, "pe": None, "n": 0, "labellers": []}
    (a, b), _ = sorted(shared.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    pairs = [(r["labellers"][a], r["labellers"][b]) for r in records if a in r["labellers"] and b in r["labellers"]]
    return {**cohens_kappa(pairs), "labellers": [a, b]}


def aggregate(runs) -> dict:
    """review_metrics pooled over runs = [(report, labels), ...] (per_comment keyed "<review_id>:<comment_id>"),
    plus a row per review, pass^k over comments seen in several runs, and kappa between labellers."""
    records, outcomes, reviews, located = [], {}, [], 0
    for report, labels in (runs or []):
        report = report if isinstance(report, dict) else {}
        rid = str(report.get("review_id") or "")
        recs = _records(report, labels)
        for r in recs:
            r["key"] = f"{rid}:{r['comment_id']}"
        records.extend(recs)
        outcomes = _merge_counts(outcomes, outcome_counts(report))
        located += _located_count(report)
        _, precision = _cov_prec(recs)
        reviews.append({"id": rid, "created": report.get("created"), "comments": len(recs),
                        "labelled": sum(1 for r in recs if r["labelled"]), "precision": precision})
    out = _summarise(records, outcomes, located, key="key")
    out["reviews"] = reviews
    out["pass_k"] = _pass_k_block(records)
    out["kappa"] = _kappa_block(records)
    return out
