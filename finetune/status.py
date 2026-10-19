"""`python -m finetune status`: where things are and what state they are in."""
import os
import time

from . import paths, venv
from redline import config, openai_compat


def _when(t):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(t)))
    except (TypeError, ValueError):
        return "?"


def main(args, log=print) -> int:
    settings = config.load()
    fd = paths.finetune_dir(False)
    log(f"finetune dir: {fd}" + ("" if os.path.isdir(fd) else "  (not created yet)"))
    log(f"review dir:   {settings.review_dir}")
    m = venv.marker()
    if not venv.exists():
        log(f"environment:  none at {paths.venv_dir()}  -> python -m finetune setup")
    elif not m:
        log(f"environment:  {paths.venv_dir()} exists but setup never finished  -> python -m finetune setup")
    else:
        log(f"environment:  {paths.venv_dir()}  python {m.get('python')}, torch {m.get('torch')} "
            f"({'CUDA ' + str(m.get('cuda')) + ', ' + str(m.get('device')) if m.get('cuda_available') else 'no CUDA'}), "
            f"transformers {m.get('transformers')}, peft {m.get('peft')}, trl {m.get('trl')}")
        conv = venv.converter()
        log(f"converter:    {conv or 'llama.cpp not downloaded (setup downloads it)'}")
    man = paths.read_json(os.path.join(paths.data_dir(False), "manifest.json"))
    if not man:
        log("dataset:      none  -> python -m finetune build")
    else:
        c = man.get("counts") or {}
        log(f"dataset:      {man.get('dataset_id')} built {_when(man.get('created'))}: {c.get('train', 0)} train, {c.get('holdout', 0)} holdout "
            f"(label set {c.get('labelset', 0)}, run rows {c.get('run_rows', 0)}); vocabulary {man.get('tools_version')} ({man.get('tools_count')} tools)")
        for cls in sorted(man.get("classes") or {}):
            e = man["classes"][cls]
            log(f"              {cls:<8} train {e['train']:>3} holdout {e['holdout']:>3} projects {len(e['projects'])}")
        for w in man.get("warnings") or []:
            log(f"              warning: {w}")
        try:
            from redline import tools
            live = tools.version()
            if live != man.get("tools_version"):
                log(f"              warning: the live vocabulary is {live}; the dataset was built on {man.get('tools_version')}")
        except ImportError:
            pass
    runs = paths.list_runs()
    if not runs:
        log("runs:         none  -> python -m finetune train")
    for rid in runs:
        r = paths.read_json(os.path.join(paths.run_dir(rid), "run.json")) or {}
        res, dep = r.get("result") or {}, r.get("deploy") or {}
        log(f"run {rid}: {r.get('base_model')} on dataset {r.get('dataset_id')}, {r.get('status')}, "
            f"loss {res.get('train_loss_final')}, eval {res.get('eval_loss_by_epoch')}, "
            f"{'deployed as ' + dep['name'] if dep.get('name') else ('GGUF ready' if dep.get('gguf') else 'not deployed')}"
            + ("  (latest)" if rid == paths.latest_run() else ""))
    ed = paths.eval_dir(False)
    if os.path.isdir(ed):
        for ds in sorted(os.listdir(ed)):
            names = [n[:-5] for n in sorted(os.listdir(os.path.join(ed, ds))) if n.endswith(".json")]
            if names:
                log(f"evals on {ds}: {', '.join(names)}")
    if openai_compat.is_up(settings.model_base):
        have = openai_compat.models(settings.model_base)
        tuned = [h for h in have if h.startswith("cad-sequencer")]
        log(f"model server: {settings.model_base} up; pipeline llm {settings.llm}, prompt {settings.prompt}; tuned models: {', '.join(tuned) or 'none'}")
    else:
        log(f"model server: {settings.model_base} not reachable")
    return 0
