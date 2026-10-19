"""The fine-tune package without a GPU, a venv or a network: the dataset
builder's rules on synthetic labels, the prompt identity with the pipeline,
the held-out split, the manifest, the Modelfile, a fake-backend eval and
the command line's exit paths.

    python finetune/tests/test_finetune.py
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

TMP = os.path.join(tempfile.gettempdir(), "cad-finetune-test")
shutil.rmtree(TMP, ignore_errors=True)
REVIEWS = os.path.join(TMP, "reviews")
os.makedirs(REVIEWS)
os.environ["CAD_REDLINE_DIR"] = REVIEWS
os.environ["CAD_FINETUNE_DIR"] = os.path.join(TMP, "finetune")
os.environ["CAD_FINETUNE_VENV"] = os.path.join(TMP, "no-venv")
os.environ["CAD_DASHBOARD_BASE"] = "http://127.0.0.1:1"
os.environ["CAD_PLUGIN_BASE"] = "http://127.0.0.1:1"
os.environ["CAD_REDLINE_MODEL_BASE"] = "http://127.0.0.1:1"
os.environ["CAD_REDLINE_VIEW"] = "0"
os.environ["CAD_REDLINE_PLANNER"] = "fake"
for k in ("CAD_REDLINE_LABELSET_DIR", "CAD_REDLINE_PROMPT", "CAD_FINETUNE_INSIDE"):
    os.environ.pop(k, None)

from finetune import build, deploy, evaluate, paths, prompts, venv  # noqa: E402
from redline import config, labelset, metrics, sequence, tools  # noqa: E402
from redline.types import Comment  # noqa: E402

failures = 0
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def check(name, cond, detail=""):
    global failures
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -- {detail}"))
    if not cond:
        failures += 1


check("the system-side modules never import torch", "torch" not in sys.modules and "transformers" not in sys.modules)
settings = config.load()
check("finetune dir honours CAD_FINETUNE_DIR", paths.finetune_dir() == os.path.join(TMP, "finetune"), paths.finetune_dir())

# ── vocabulary and prompt rendering ─────────────────────────────────────────
NAMES = sorted(set().union(*tools.BINS.values()))
USED = ["object_at", "move_entities", "delete_objects", "draw_polyline", "edit_text", "draw_leader", "describe_location",
        "nearby", "space_labels", "move_to_layer", "draw_shape"]
check("the test's tool names are in the bin tables", all(n in NAMES for n in USED), str([n for n in USED if n not in NAMES]))
TOOLS_FILE = os.path.join(TMP, "tools.txt")
with open(TOOLS_FILE, "w", encoding="utf-8") as f:
    f.write("# header line\n" + "\n".join(f"- {n}: does {n}" for n in NAMES) + "\n")
voc = build.vocabulary(TOOLS_FILE)
check("vocabulary from a tools file", voc["names"] == NAMES and voc["source"].startswith("file:") and len(voc["version"]) == 12
      and "- object_at: does object_at" in voc["text"])

system, user = prompts.render("MOVE HYD 20' N", "circle", 0.9)
check("short prompt: system is the pipeline's", system == sequence.SYSTEM_PROMPT)
check("short prompt: comment line and ANSWER only", user == 'COMMENT: text="MOVE HYD 20\' N" kind=circle confidence=0.90\nANSWER as JSON: {"tools": [...]}', user)
check("completion is the literal JSON shape", prompts.completion(["object_at", "move_entities"]) == '{"tools": ["object_at", "move_entities"]}')
check("completion parses back through the pipeline", sequence.clean(prompts.completion(["object_at", "move_entities"]), NAMES)[0] == ["object_at", "move_entities"])

# ── synthetic labels ─────────────────────────────────────────────────────────
ls_dir = labelset.folder(settings)
check("label set folder sits under the test review dir", ls_dir == os.path.join(REVIEWS, "labelset"), ls_dir)
USED_TEXTS = {build.norm_key(t) for t in ("MOVE HYD 20' N", "SOMETHING NEW", "DELETE THIS LINE", "CHANGE DR14 TO DR18", "FIX LABELS", "WHATEVER",
                                           "ERR ROW", "ERR WITH HUMAN", "BAD NO FIX", "UNKNOWN TOOL", "TOO LONG", "REPEATS", "MOVE TO LAYER X",
                                           "ANOTHER ONE", "AND ANOTHER", "DEEP ROW")}
EXAMPLE = next(e for e in sequence.load_examples() if build.norm_key(e["comment"]) not in USED_TEXTS)
labelset.add(ls_dir, "MOVE HYD 20' N", ["object_at", "move_entities"], labeller="a")
labelset.add(ls_dir, EXAMPLE["comment"], list(EXAMPLE["tools"]), labeller="a")
labelset.add(ls_dir, "SOMETHING NEW", ["draw_polyline", "draw_leader"], labeller="b")


def row(key, text, model_tools=None, human_tools=None, verdict=None, error=None, ts=1.0, annotated_at=None,
        kind="circle", confidence=0.9, dwg="A.dwg"):
    return {"ts": ts, "review_id": "x", "key": key, "comment": {"id": key, "page_no": 1, "text": text, "kind": kind, "confidence": confidence},
            "sheet": {"sheet_no": "C-1", "printed_file": "P.dwg", "dwg_path": f"C:\\proj\\{dwg}" if dwg else None, "layout_name": "L"},
            "located": None, "mode": None, "actions": None, "human_params": None, "tools_version": "v1",
            "tools": model_tools or [], "unknown_tools": [], "raw_reply": "", "model": "m", "error": error,
            "human_tools": human_tools, "human_verdict": verdict, "human_labeller": "a" if human_tools or verdict else None,
            "annotated_at": annotated_at}


def write_rows(folder, rows):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "training.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    with open(os.path.join(folder, "report.json"), "w", encoding="utf-8") as f:
        f.write("{}")


write_rows(os.path.join(REVIEWS, "20261001-100000"), [
    row("r1", "DELETE THIS LINE", ["object_at", "delete_objects"], human_tools=["object_at", "delete_objects"], verdict="bad", annotated_at=2.0),
    row("r2", "MOVE HYD 20' N", ["object_at", "move_entities", "draw_leader"], verdict="good"),
    row("r3", "CHANGE DR14 TO DR18", ["object_at", "edit_text"], verdict="good", kind="note", confidence=0.7),
    row("r4", "FIX LABELS", ["space_labels"], verdict="unsure"),
    row("r5", "WHATEVER", ["object_at"]),
    row("r6", "ERR ROW", [], verdict="good", error="model_error: boom"),
    row("r7", "ERR WITH HUMAN", [], human_tools=["nearby", "draw_polyline"], error="model_error: boom"),
    row("r8", "BAD NO FIX", ["object_at"], verdict="bad"),
    row("r9", "UNKNOWN TOOL", [], human_tools=["object_at", "teleport_hydrant"]),
    row("r10", "TOO LONG", [], human_tools=["object_at", "move_entities", "edit_text", "draw_leader", "draw_polyline", "nearby", "space_labels", "move_to_layer", "draw_shape"]),
    row("r11", "   ", [], human_tools=["object_at"]),
    row("r12", "REPEATS", [], human_tools=["object_at", "object_at", "move_entities"]),
])
write_rows(os.path.join(REVIEWS, "20261002-100000"), [
    row("s1", "delete this line", [], human_tools=["object_at", "delete_objects", "draw_leader"], annotated_at=5.0, dwg="B.dwg"),
    row("s2", "MOVE TO LAYER X", [], human_tools=["object_at", "move_to_layer"], dwg="B.dwg"),
    row("s3", "ANOTHER ONE", [], human_tools=["object_at", "delete_objects"], dwg="B.dwg"),
    row("s4", "AND ANOTHER", [], human_tools=["draw_polyline"], dwg=None),
])
write_rows(os.path.join(REVIEWS, "ocr-only", "20261003-100000"), [
    row("s5", "DEEP ROW", [], human_tools=["nearby"], dwg="B.dwg"),
])

logged = []
m = build.build(tools_file=TOOLS_FILE, log=logged.append)
c = m["counts"]
check("build: sources counted", c["labelset"] == 3 and c["run_rows"] == 17 and c["runs_scanned"] == 3, json.dumps(c))
check("build: selection rules", c["from_human_tools"] == 10 and c["from_good_verdict"] == 2 and c["skipped_unsure"] == 1
      and c["skipped_unlabelled"] == 1 and c["skipped_error"] == 1 and c["skipped_bad_nofix"] == 1 and c["skipped_empty_text"] == 1, json.dumps(c))
check("build: vocabulary and length drops", c["dropped_unknown_vocab"] == 1 and c["dropped_too_long"] == 1
      and m["unknown_names"] == {"teleport_hydrant": 1}, json.dumps(c) + str(m["unknown_names"]))
# candidates: 3 label set + 8 human chains kept (r1 r7 r12 s1..s5) + 2 Right verdicts (r2 r3) = 13; r2 duplicates the
# label set's MOVE HYD and s1 duplicates r1 -> 11 selected
check("build: duplicates and conflicts", c["duplicates"] == 2 and c["conflicts"] == 2 and c["selected"] == 11
      and c["train"] + c["holdout"] == 11, json.dumps(c))
kept_ids = {x["kept"] for x in m["conflicts"]}
dropped_ids = {x["dropped"] for x in m["conflicts"]}
check("build: the label set beats a Right verdict, the newer human chain beats the older",
      "ls:ex-0001" in kept_ids and "run:20261001-100000:r2" in dropped_ids
      and "run:20261002-100000:s1" in kept_ids and "run:20261001-100000:r1" in dropped_ids, str(m["conflicts"]))
check("build: exit 0 with rows", m["exit"] == 0)
check("build: a warning per thin class", m["warnings"] and all("the README asks for 10 or more" in w for w in m["warnings"]), str(m["warnings"]))
data_dir = paths.data_dir()
train_rows = paths.read_jsonl(os.path.join(data_dir, "train.jsonl"))
hold_rows = paths.read_jsonl(os.path.join(data_dir, "holdout.jsonl"))
all_rows = train_rows + hold_rows
seen = {t for r in all_rows for t in r["meta"]["tools"]}
check("build: coverage reported", m["coverage"]["names_total"] == len(NAMES) and m["coverage"]["names_seen"] == len(seen)
      and set(m["coverage"]["unseen"]) == set(NAMES) - seen, str(m["coverage"]["names_seen"]))
check("build: files written", len(train_rows) == c["train"] and len(hold_rows) == c["holdout"] and len(hold_rows) >= 1
      and os.path.isfile(os.path.join(data_dir, "tools.txt")) and os.path.isfile(os.path.join(data_dir, "rows.jsonl")))
by_id = {r["id"]: r for r in all_rows}
check("build: repeats folded", by_id["run:20261001-100000:r12"]["meta"]["tools"] == ["object_at", "move_entities"])
check("build: kind and confidence come from the run row; label set rows get note / 1.0",
      by_id["run:20261001-100000:r3"]["meta"]["kind"] == "note" and by_id["run:20261001-100000:r3"]["meta"]["confidence"] == 0.7
      and by_id["ls:ex-0003"]["meta"]["kind"] == "note" and by_id["ls:ex-0003"]["meta"]["confidence"] == 1.0)
check("build: deep training.jsonl found and projects named", "run:ocr-only/20261003-100000:s5" in by_id
      and by_id["run:ocr-only/20261003-100000:s5"]["meta"]["project"] == "B.dwg"
      and by_id["run:20261002-100000:s4"]["meta"]["project"] == "P.dwg")
ex_row = by_id["ls:ex-0002"]
check("build: a few-shot example text is pinned to train", ex_row["meta"]["in_examples"] and ex_row["id"] in {r["id"] for r in train_rows})
check("build: no row on both sides", not ({r["id"] for r in train_rows} & {r["id"] for r in hold_rows}))
check("build: every row has class, prompt and completion", all(r["meta"]["class"] in tools.TIER_ORDER and len(r["prompt"]) == 2
                                                               and r["completion"][0]["role"] == "assistant" for r in all_rows))
decisions = paths.read_jsonl(os.path.join(data_dir, "rows.jsonl"))
check("build: rows.jsonl records every decision", sum(1 for d in decisions if d["decision"] in ("train", "holdout")) == c["selected"]
      and any(d["decision"] == "dropped_unknown_vocab" for d in decisions) and any(d["decision"] == "duplicate" for d in decisions))

# prompt identity with the pipeline
r0 = train_rows[0]
mt = r0["meta"]
fake = sequence.FakeSequencer()
s = sequence.sequence(Comment(id="q", page_no=0, bbox_px=[0, 0, 0, 0], text=mt["text"], kind=mt["kind"], confidence=mt["confidence"]),
                      fake, "", NAMES, None, mode="short")
check("prompt identity: the dataset's user prompt is what the pipeline sends in short mode", fake.calls[0] == r0["prompt"][1]["content"], fake.calls[0])
check("prompt identity: system prompt", r0["prompt"][0]["content"] == sequence.SYSTEM_PROMPT)
check("prompt identity: completion parses to the target", sequence.clean(r0["completion"][0]["content"], NAMES)[0] == mt["tools"])
check("manifest: id, mode, tools version, system sha", len(m["dataset_id"]) == 12 and m["prompt_mode"] == "short"
      and m["tools_version"] == voc["version"] and m["system_sha1"] == prompts.system_sha1() and m["max_prompt_chars"] < 200)

# determinism and the holdout rules
m2 = build.build(tools_file=TOOLS_FILE, log=lambda s: None)
check("build twice: the same split and dataset id", m2["dataset_id"] == m["dataset_id"]
      and [r["id"] for r in paths.read_jsonl(os.path.join(data_dir, "holdout.jsonl"))] == [r["id"] for r in hold_rows])
m0 = build.build(tools_file=TOOLS_FILE, holdout=0, out_dir=os.path.join(TMP, "ds0"), log=lambda s: None)
check("--holdout 0: nothing held out", m0["counts"]["holdout"] == 0 and m0["counts"]["train"] == c["selected"]
      and not os.path.isfile(os.path.join(TMP, "ds0", "holdout.jsonl")))
m1 = build.build(tools_file=TOOLS_FILE, holdout=1.0, out_dir=os.path.join(TMP, "ds1"), log=lambda s: None)
check("--holdout 1.0: everything eligible held out, the pinned example keeps train non-empty",
      m1["counts"]["train"] >= 1 and m1["counts"]["train"] == m1["counts"]["in_examples"]
      and m1["counts"]["holdout"] == c["selected"] - m1["counts"]["train"], json.dumps(m1["counts"]))
tiny = build.split([{"id": str(i), "key": f"K{i}", "tools": ["object_at"]} for i in range(5)], 0.0001)
check("n >= 5 with a fraction that picks nothing: exactly one holdout row", len(tiny[1]) == 1 and len(tiny[0]) == 4)
tiny = build.split([{"id": str(i), "key": f"K{i}", "tools": ["object_at"]} for i in range(3)], 0.0001)
check("n < 5: no forced holdout", len(tiny[1]) == 0)

# vocabulary fallbacks
_names = tools.names
tools.names = lambda: (_ for _ in ()).throw(ImportError("no server"))
v = build.vocabulary(None, ls_dir, log=lambda s: None)
check("vocabulary: bin tables when nothing else", v["source"] == "bins" and v["names"] == NAMES)
shutil.copy(TOOLS_FILE, os.path.join(ls_dir, "tools.txt"))
v = build.vocabulary(None, ls_dir, log=lambda s: None)
check("vocabulary: the label set's snapshot when present", v["source"].startswith("snapshot:") and v["names"] == NAMES)
tools.names = _names

# ── Modelfile and a dry deploy ──────────────────────────────────────────────
mf = deploy.modelfile_text(os.path.join(TMP, "x.gguf"))
check("Modelfile: FROM, TEMPLATE, parameters, no SYSTEM", mf.startswith(f"FROM {os.path.abspath(os.path.join(TMP, 'x.gguf'))}\n")
      and 'TEMPLATE """' in mf and "<|im_start|>assistant" in mf and "PARAMETER temperature 0.1" in mf
      and "PARAMETER num_ctx 4096" in mf and 'PARAMETER stop "<|im_end|>"' in mf and "\nSYSTEM" not in mf, mf)
rid = "20261019-000000"
rd = paths.run_dir(rid, create=True)
paths.write_json(os.path.join(rd, "run.json"), {"run_id": rid, "base_model": "Qwen/x", "adapter_dir": os.path.join(rd, "adapter"), "status": "trained", "deploy": None})
paths.set_latest_run(rid)
check("latest run resolves", paths.resolve_run("latest") == rid and paths.resolve_run(None) == rid and paths.resolve_run("nope") is None)
args = argparse.Namespace(run="latest", name="cad-sequencer", outtype="q8_0", quant=None, skip_merge=False, no_ollama=False,
                          no_smoke=True, dry_run=True, model_base=None)
logged = []
rc = deploy.main(args, log=logged.append)
check("deploy --dry-run: Modelfile written, commands printed, nothing run", rc == 0 and os.path.isfile(os.path.join(rd, "Modelfile"))
      and any("ollama create cad-sequencer:" + rid in s for s in logged) and deploy.gguf_path_for(rid, "cad-sequencer", "q8_0", None) in open(os.path.join(rd, "Modelfile"), encoding="utf-8").read(),
      str(logged))
check("deploy: no run -> 1", deploy.main(argparse.Namespace(run="nope", dry_run=True), log=lambda s: None) == 1)

# ── eval with a fake backend ────────────────────────────────────────────────
def ds_row(i, text, gold, kind="note"):
    return {"id": f"t{i}", "prompt": prompts.messages(text, kind, 1.0), "completion": prompts.completion_messages(gold),
            "meta": {"text": text, "kind": kind, "confidence": 1.0, "tools": gold, "class": tools.chain_class(gold)}}


rows3 = [ds_row(1, "MOVE HYD 20' N", ["object_at", "move_entities"]),
         ds_row(2, "DELETE THIS LINE", ["object_at", "delete_objects", "draw_leader"]),
         ds_row(3, "ZZZ", ["describe_location"])]
recs = evaluate.run(rows3, sequence.FakeSequencer(), "short", NAMES, "", None, log=lambda s: None, progress=False)
summ = evaluate.summarise(recs)
lo, hi = metrics.wilson(2, 3)
check("eval: exact 2 of 3 with Wilson bounds", summ["exact"]["k"] == 2 and summ["exact"]["n"] == 3 and abs(summ["exact"]["low"] - lo) < 1e-9
      and abs(summ["exact"]["high"] - hi) < 1e-9, json.dumps(summ["exact"]))
check("eval: per-row scores", recs[1]["precision"] == 1.0 and abs(recs[1]["recall"] - 2 / 3) < 1e-9 and abs(recs[1]["f1"] - 0.8) < 1e-9
      and abs(recs[1]["order"] - 2 / 3) < 1e-9 and recs[1]["first_wrong"] == 2 and recs[0]["exact"] and recs[2]["exact"], json.dumps(recs[1]))
check("eval: aggregates", abs(summ["f1"] - (1 + 0.8 + 1) / 3) < 1e-9 and summ["first_wrong"] == {"none": 2, "2": 1}
      and summ["model_errors"] == 0 and summ["empty"] == 0 and summ["prompt_mismatches"] == 0 and summ["by_class"]["modify"]["n"] == 1, json.dumps(summ))

ev_args = argparse.Namespace(model_base=None, dataset=None, set="holdout", compare=False, planner="fake", llm="fake-a", prompt="short", timeout=None)
rc = evaluate.main(ev_args, log=lambda s: None)
folder = os.path.join(paths.eval_dir(), m["dataset_id"])
check("eval main: result written per dataset", rc == 0 and os.path.isfile(os.path.join(folder, "fake-a-short.json")) and os.path.isfile(os.path.join(folder, "fake-a-short.md")))
check("eval main: no compare with one result", evaluate.compare(folder) is None)
rc = evaluate.main(argparse.Namespace(**dict(vars(ev_args), llm="fake-b", prompt="full")), log=lambda s: None)
cmp_text = evaluate.compare(folder)
check("eval main: two results -> side-by-side table", rc == 0 and cmp_text and "fake-a (short)" in cmp_text and "fake-b (full)" in cmp_text
      and os.path.isfile(os.path.join(folder, "compare.md")), str(cmp_text)[:200])
check("eval main: --compare exits 0", evaluate.main(argparse.Namespace(**dict(vars(ev_args), compare=True)), log=lambda s: None) == 0)
check("eval main: unreachable server -> 1", evaluate.main(argparse.Namespace(**dict(vars(ev_args), planner="openai", llm="x")), log=lambda s: None) == 1)
check("eval: default mode is short only for a deployed name", evaluate.default_mode("cad-sequencer", settings) == "short"
      and evaluate.default_mode("qwen2.5-coder:7b", settings) == "full")

# ── the command line ────────────────────────────────────────────────────────
env = dict(os.environ)


def cli(*a):
    return subprocess.run([sys.executable, "-m", "finetune", *a], capture_output=True, text=True, cwd=ROOT, env=env)


r = cli("train")
check("cli: train without a venv exits 2 and names setup", r.returncode == 2 and "python -m finetune setup" in r.stdout, r.stdout + r.stderr)
r = cli("merge")
check("cli: merge without a venv exits 2", r.returncode == 2 and "setup" in r.stdout, r.stdout + r.stderr)
r = cli("status")
check("cli: status", r.returncode == 0 and "environment:  none" in r.stdout and m["dataset_id"] in r.stdout and rid in r.stdout, r.stdout + r.stderr)
r = cli("build", "--tools-file", TOOLS_FILE)
check("cli: build", r.returncode == 0 and f"{c['selected']} selected" in r.stdout, r.stdout + r.stderr)
r = cli("eval", "--llm", "x")
check("cli: eval against a dead server exits 1", r.returncode == 1 and "no model server" in r.stdout, r.stdout + r.stderr)
r = cli("--help")
check("cli: --help", r.returncode == 0 and "deploy" in r.stdout)
r = cli()
check("cli: no command prints help, exits 2", r.returncode == 2)
r = cli("setup", "--check")
check("cli: setup --check without a venv exits 2", r.returncode == 2 and "setup" in r.stdout, r.stdout + r.stderr)
check("venv helpers: not usable, converter absent", not venv.usable() and venv.converter() is None)

print(f"\n{failures} failure(s)")
sys.exit(failures)
