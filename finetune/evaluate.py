"""`python -m finetune eval`: the held-out rows through the pipeline's own
sequencer against a model served at the OpenAI-compatible endpoint, scored
with redline.metrics. Results are kept per dataset, so the baseline (the
stock model on the full prompt) and the tuned model (short prompt) sit side
by side. System Python: stdlib + redline + requests.
"""
import os
import statistics
import time

from . import build, paths, prompts
from redline import config, metrics, openai_compat, sequence, tools

DEPLOY_NAME = "cad-sequencer"
SETS = ("holdout", "train", "all")


def load_rows(data_dir: str, which: str = "holdout") -> list:
    rows = []
    if which in ("holdout", "all"):
        rows += paths.read_jsonl(os.path.join(data_dir, "holdout.jsonl"))
    if which in ("train", "all"):
        rows += paths.read_jsonl(os.path.join(data_dir, "train.jsonl"))
    return rows


def backend_for(planner: str, model_base: str, llm: str, timeout: float = 600.0):
    if planner == "fake":
        return sequence.FakeSequencer()
    return sequence.OpenAICompatPlanner(model_base, llm, timeout=timeout)


def preflight(model_base: str, llm: str) -> str | None:
    if not openai_compat.is_up(model_base):
        return f"no model server at {model_base} (is Ollama running? `ollama serve`)"
    have = openai_compat.models(model_base)
    if llm not in have and f"{llm}:latest" not in have:
        return f"model {llm!r} is not served at {model_base}; have: {', '.join(have) or 'nothing'}"
    return None


def deployed_name() -> str | None:
    """The model name the latest deployed run registered, if any."""
    rid = paths.latest_run()
    dep = (paths.read_json(os.path.join(paths.run_dir(rid), "run.json")) or {}).get("deploy") if rid else None
    return (dep or {}).get("name") or None


def default_mode(llm: str, settings) -> str:
    """short for a model this package deployed, full for anything else."""
    if llm == DEPLOY_NAME or str(llm).startswith(DEPLOY_NAME + ":"):
        return "short"
    for rid in paths.list_runs():
        dep = (paths.read_json(os.path.join(paths.run_dir(rid), "run.json")) or {}).get("deploy") or {}
        if dep.get("name") and (llm == dep["name"] or str(llm).startswith(dep["name"] + ":")):
            return "short"
    return "full"


def vocabulary_for(data_dir: str, mode: str, log=print) -> tuple:
    """(names, tools_text, live_version): the enum is the dataset's
    vocabulary; the full prompt's tool text is the live one when the registry
    imports, else the dataset snapshot."""
    names, snap_text = build.parse_tools_txt(os.path.join(data_dir, "tools.txt"))
    live_version, text = None, snap_text
    try:
        live_names = tools.names()
        live_version = tools.version()
        if mode == "full":
            text = tools.text()
            names = live_names
    except ImportError:
        pass
    return names, text, live_version


def run(rows: list, backend, mode: str, names: list, tools_text: str, examples, log=print, progress=True) -> list:
    out = []
    for i, r in enumerate(rows, 1):
        m = r.get("meta") or {}
        comment = prompts.comment_for(m.get("text", ""), m.get("kind"), m.get("confidence"), cid=str(r.get("id")))
        mismatch = False
        if mode == "short":
            expected = (r.get("prompt") or [{}, {}])[1].get("content")
            mismatch = expected is not None and sequence.build_user_prompt(comment, "", None, mode="short") != expected
        t0 = time.time()
        seq = sequence.sequence(comment, backend, tools_text, names, examples=examples, mode=mode)
        gold, pred = list(m.get("tools") or []), list(seq.tools or [])
        cm = metrics.chain_metrics(pred, gold)
        rec = {"id": r.get("id"), "text": m.get("text"), "kind": m.get("kind"), "class": m.get("class") or tools.chain_class(gold),
               "gold": gold, "pred": pred, "exact": bool(cm["exact"]), "raw_exact": pred == gold,
               "precision": cm["scores"]["precision"], "recall": cm["scores"]["recall"], "f1": cm["scores"]["f1"],
               "tp": cm["scores"]["tp"], "fp": cm["scores"]["fp"], "fn": cm["scores"]["fn"],
               "order": cm["order"], "first_wrong": cm["first_wrong"], "error": seq.error, "unknown": list(seq.unknown_tools or []),
               "attempt": seq.attempt, "latency_s": round(seq.latency_s or (time.time() - t0), 3),
               "raw_reply": str(seq.raw_reply or "")[:200], "prompt_chars": seq.prompt_chars, "prompt_mismatch": mismatch}
        out.append(rec)
        if progress:
            mark = "ok " if rec["exact"] else ("ERR" if seq.error else "no ")
            log(f"  {i:>3}/{len(rows)} {mark} {str(m.get('text', ''))[:40]!r:44} gold {' -> '.join(gold)} | got {' -> '.join(pred) or '(none)'}")
    return out


def summarise(records: list) -> dict:
    n = len(records)
    pairs = [(r["pred"], r["gold"]) for r in records]
    hist = {}
    for r in records:
        k = "none" if r["first_wrong"] is None else str(r["first_wrong"])
        hist[k] = hist.get(k, 0) + 1
    tp, fp, fn = sum(r["tp"] for r in records), sum(r["fp"] for r in records), sum(r["fn"] for r in records)
    pooled_p = tp / (tp + fp) if (tp + fp) else 0.0
    pooled_r = tp / (tp + fn) if (tp + fn) else 0.0
    pooled_f = 2 * pooled_p * pooled_r / (pooled_p + pooled_r) if (pooled_p + pooled_r) else 0.0
    lat = sorted(r["latency_s"] for r in records)
    by_class = {}
    for r in records:
        e = by_class.setdefault(r["class"], {"n": 0, "exact_k": 0, "f1s": []})
        e["n"] += 1
        e["exact_k"] += int(r["exact"])
        e["f1s"].append(r["f1"])
    classes = {c: {"n": e["n"], "exact": metrics.rate(e["exact_k"], e["n"]), "f1": statistics.fmean(e["f1s"]) if e["f1s"] else None}
               for c, e in by_class.items()}
    return {"n": n, "exact": metrics.rate(sum(r["exact"] for r in records), n), "raw_exact": metrics.rate(sum(r["raw_exact"] for r in records), n),
            "precision": statistics.fmean(r["precision"] for r in records) if n else None,
            "recall": statistics.fmean(r["recall"] for r in records) if n else None,
            "f1": statistics.fmean(r["f1"] for r in records) if n else None,
            "pooled": {"precision": pooled_p, "recall": pooled_r, "f1": pooled_f, "tp": tp, "fp": fp, "fn": fn},
            "order": statistics.fmean(r["order"] for r in records) if n else None,
            "first_wrong": hist, "step_accuracy": metrics.step_accuracy_by_position(pairs),
            "model_errors": sum(1 for r in records if str(r["error"] or "").startswith("model_error")),
            "empty": sum(1 for r in records if not r["pred"]), "prompt_mismatches": sum(1 for r in records if r["prompt_mismatch"]),
            "latency_mean_s": statistics.fmean(lat) if lat else None, "latency_p95_s": lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else None,
            "by_class": classes}


def _pct(v):
    return "—" if v is None else f"{100 * v:.0f}%"


def _rate(r):
    r = r or {}
    return "—" if not r.get("n") else f"{_pct(r['rate'])} ({_pct(r['low'])}–{_pct(r['high'])}, {r['k']}/{r['n']})"


def markdown(result: dict) -> str:
    s = result["summary"]
    lines = [f"# Eval: {result['llm']} ({result['prompt_mode']} prompt) on {result['set']} of dataset {result['dataset_id']}", "",
             f"{s['n']} comment(s), model base {result['model_base']}, {time.strftime('%Y-%m-%d %H:%M', time.localtime(result['created']))}", "",
             "| metric | value |", "|---|---|",
             f"| exact match (lookups collapsed) | {_rate(s['exact'])} |", f"| exact match (raw) | {_rate(s['raw_exact'])} |",
             f"| tool-set precision / recall / F1 (mean) | {_pct(s['precision'])} / {_pct(s['recall'])} / {_pct(s['f1'])} |",
             f"| tool-set P / R / F1 (pooled) | {_pct(s['pooled']['precision'])} / {_pct(s['pooled']['recall'])} / {_pct(s['pooled']['f1'])} |",
             f"| order score | {_pct(s['order'])} |",
             f"| first wrong step | {', '.join(f'{k}: {v}' for k, v in sorted(s['first_wrong'].items()))} |",
             f"| model errors / empty answers | {s['model_errors']} / {s['empty']} |",
             f"| latency mean / p95 | {s['latency_mean_s'] and round(s['latency_mean_s'], 2)} s / {s['latency_p95_s'] and round(s['latency_p95_s'], 2)} s |", ""]
    if s["by_class"]:
        lines += ["| class | n | exact | mean F1 |", "|---|---|---|---|"]
        for c in sorted(s["by_class"]):
            e = s["by_class"][c]
            lines.append(f"| {c} | {e['n']} | {_rate(e['exact'])} | {_pct(e['f1'])} |")
        lines.append("")
    lines += ["| id | comment | gold | predicted | |", "|---|---|---|---|---|"]
    for r in result["rows"]:
        mark = "✓" if r["exact"] else ("error" if r["error"] else "✕")
        lines.append(f"| {r['id']} | {str(r['text']).replace('|', '/')[:60]} | {' → '.join(r['gold'])} | {' → '.join(r['pred']) or '(none)'} | {mark} |")
    return "\n".join(lines) + "\n"


def result_name(llm: str, mode: str, which: str) -> str:
    return paths.safe_name(llm) + f"-{mode}" + ("" if which == "holdout" else f"-{which}")


def write(result: dict, folder: str) -> tuple:
    base = os.path.join(folder, result_name(result["llm"], result["prompt_mode"], result["set"]))
    paths.write_json(base + ".json", result)
    paths.write_text(base + ".md", markdown(result))
    return base + ".json", base + ".md"


def compare(folder: str, which: str = "holdout") -> str | None:
    """A side-by-side table of every result in the folder for one set, or
    None with fewer than two."""
    results = []
    if not os.path.isdir(folder):
        return None
    for name in sorted(os.listdir(folder)):
        if name.endswith(".json") and name != "compare.json":
            d = paths.read_json(os.path.join(folder, name))
            if isinstance(d, dict) and d.get("summary") and d.get("set") == which:
                results.append(d)
    if len(results) < 2:
        return None
    cols = [f"{r['llm']} ({r['prompt_mode']})" for r in results]
    lines = ["| metric | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]

    def row(label, fn):
        lines.append(f"| {label} | " + " | ".join(fn(r["summary"]) for r in results) + " |")
    row("n", lambda s: str(s["n"]))
    row("exact (collapsed)", lambda s: _rate(s["exact"]))
    row("exact (raw)", lambda s: _rate(s["raw_exact"]))
    row("precision / recall / F1", lambda s: f"{_pct(s['precision'])} / {_pct(s['recall'])} / {_pct(s['f1'])}")
    row("order", lambda s: _pct(s["order"]))
    row("first wrong step", lambda s: ", ".join(f"{k}: {v}" for k, v in sorted(s["first_wrong"].items())))
    row("model errors / empty", lambda s: f"{s['model_errors']} / {s['empty']}")
    row("latency mean", lambda s: "—" if s["latency_mean_s"] is None else f"{s['latency_mean_s']:.2f} s")
    classes = sorted({c for r in results for c in r["summary"]["by_class"]})
    for c in classes:
        row(f"exact: {c}", lambda s, c=c: _rate((s["by_class"].get(c) or {}).get("exact")))
    text = "\n".join(lines) + "\n"
    paths.write_text(os.path.join(folder, "compare.md"), text)
    return text


def main(args, log=print) -> int:
    settings = config.load()
    model_base = str(getattr(args, "model_base", None) or settings.model_base).rstrip("/")
    data_dir = getattr(args, "dataset", None) or paths.data_dir(False)
    manifest = paths.read_json(os.path.join(data_dir, "manifest.json"))
    if not manifest:
        log(f"no dataset at {data_dir}; run: python -m finetune build")
        return 1
    which = getattr(args, "set", None) or "holdout"
    folder = os.path.join(paths.eval_dir(), manifest["dataset_id"])
    if getattr(args, "compare", False):
        text = compare(folder, which)
        log(text or f"fewer than two results for the {which} set in {folder}")
        return 0
    rows = load_rows(data_dir, which)
    if not rows:
        log(f"no {which} rows in {data_dir}" + (" (build with --holdout > 0, or use --set train)" if which == "holdout" else ""))
        return 1
    planner = getattr(args, "planner", None) or "openai"
    llm = getattr(args, "llm", None) or deployed_name() or settings.llm
    mode = getattr(args, "prompt", None) or default_mode(llm, settings)
    if planner != "fake":
        reason = preflight(model_base, llm)
        if reason:
            log(reason)
            return 1
    names, tools_text, live_version = vocabulary_for(data_dir, mode, log)
    if live_version and live_version != manifest.get("tools_version"):
        log(f"  warning: the live tool vocabulary ({live_version}) differs from the dataset's ({manifest.get('tools_version')}); rebuild and retrain when it settles")
    examples = sequence.load_examples() if mode == "full" else None
    backend = backend_for(planner, model_base, llm, timeout=float(getattr(args, "timeout", None) or settings.model_timeout))
    log(f"eval: {llm} ({mode} prompt) on {len(rows)} {which} row(s) of dataset {manifest['dataset_id']}")
    records = run(rows, backend, mode, names, tools_text, examples, log=log)
    result = {"llm": llm, "model_base": model_base, "prompt_mode": mode, "planner": planner, "set": which,
              "dataset_id": manifest["dataset_id"], "tools_version": manifest.get("tools_version"), "created": time.time(),
              "n": len(records), "summary": summarise(records), "rows": records}
    jp, mp = write(result, folder)
    s = result["summary"]
    log(f"  exact {_rate(s['exact'])}, F1 {_pct(s['f1'])}, order {_pct(s['order'])}, errors {s['model_errors']}, empty {s['empty']}")
    if s["prompt_mismatches"]:
        log(f"  warning: {s['prompt_mismatches']} row(s) rendered a different short prompt than the dataset holds; rebuild")
    log(f"  written: {mp}")
    text = compare(folder, which)
    if text:
        log("")
        log(text)
    return 0
