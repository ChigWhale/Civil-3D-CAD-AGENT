"""`python -m finetune train` (inside the venv): a LoRA on the base model,
completion-only loss on the short prompt rows build.py rendered.

Never imports redline: the prompts are already in the dataset. Pre-checks
run before anything touches the GPU: the tokenizer's chat template must
render ChatML exactly (what Ollama renders at inference), every row must
fit the token budget (TRL would otherwise cut the completion silently), and
the base must be small enough for the card.
"""
import math
import os
import shutil
import subprocess
import sys
import time

from . import paths

MAX_PARAMS = 2.2e9
TARGET_MODULES = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
EOS = "<|im_end|>"


def chatml(prompt: list) -> str:
    out = []
    for m in prompt:
        out.append(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n")
    return "".join(out) + "<|im_start|>assistant\n"


def _ids(enc):
    if isinstance(enc, dict) or hasattr(enc, "keys"):
        return list(enc["input_ids"])
    return list(enc)


def token_lengths(tok, rows: list) -> list:
    return [len(_ids(tok.apply_chat_template(r["prompt"] + r["completion"], tokenize=True))) for r in rows]


def auto_max_length(max_total: int, cap: int) -> int:
    return min(cap, int(math.ceil((max_total + 32) / 64.0) * 64))


def git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=str(paths.ROOT),
                              timeout=10).stdout.strip() or None
    except Exception:
        return None


def main(args, log=print) -> int:
    os.environ.setdefault("HF_HOME", paths.hf_home())
    if getattr(args, "offline", False):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch
    import transformers
    from transformers import AutoModelForCausalLM, AutoTokenizer, TrainerCallback
    import peft
    from peft import LoraConfig
    import trl
    from trl import SFTConfig, SFTTrainer
    from datasets import Dataset

    data_dir = getattr(args, "dataset", None) or paths.data_dir(False)
    manifest = paths.read_json(os.path.join(data_dir, "manifest.json")) or {}
    train_rows = paths.read_jsonl(os.path.join(data_dir, "train.jsonl"))
    hold_rows = paths.read_jsonl(os.path.join(data_dir, "holdout.jsonl"))
    if not train_rows:
        log(f"no train rows in {data_dir}; run: python -m finetune build")
        return 1
    base = args.base
    run_id = getattr(args, "run_id", None) or paths.stamp()
    rd = paths.run_dir(run_id, create=not getattr(args, "dry_run", False))
    log(f"train: run {run_id}, base {base}, {len(train_rows)} train / {len(hold_rows)} holdout rows from {data_dir}")

    tok = AutoTokenizer.from_pretrained(base)
    if tok.pad_token is None:
        tok.pad_token = "<|endoftext|>"
    p0 = train_rows[0]["prompt"]
    rendered = tok.apply_chat_template(p0, tokenize=False, add_generation_prompt=True)
    if rendered != chatml(p0):
        log("the tokenizer's chat template does not render ChatML the way Ollama will; this base cannot be used as is")
        log(f"  got:      {rendered[:160]!r}")
        log(f"  expected: {chatml(p0)[:160]!r}")
        return 1
    lens = token_lengths(tok, train_rows + hold_rows)
    max_total, mean_total = max(lens), sum(lens) / len(lens)
    comp_lens = [len(_ids(tok(r["completion"][0]["content"] + EOS))) for r in train_rows]
    cap = int(getattr(args, "max_length_cap", 2048))
    ml = getattr(args, "max_length", "auto")
    max_length = auto_max_length(max_total, cap) if str(ml) == "auto" else int(ml)
    over = [r["id"] for r, n in zip(train_rows + hold_rows, lens) if n > max_length]
    if over:
        log(f"{len(over)} row(s) exceed max_length {max_length} (longest {max_total} tokens): {', '.join(over[:8])}; raise --max-length-cap or shorten them")
        return 1
    tokens = {"max_total": max_total, "mean_total": round(mean_total, 1), "max_length": max_length,
              "mean_completion": round(sum(comp_lens) / len(comp_lens), 1)}
    log(f"  tokens: longest row {max_total}, mean {mean_total:.0f}, completion mean {tokens['mean_completion']}; max_length {max_length}")

    cuda = torch.cuda.is_available()
    if not cuda and not getattr(args, "allow_cpu", False):
        log("no CUDA device in this environment; pass --allow-cpu to train on the CPU (slow)")
        return 1
    model = AutoModelForCausalLM.from_pretrained(base, dtype=torch.bfloat16, attn_implementation="sdpa")
    n_params = int(model.num_parameters())
    if n_params > MAX_PARAMS and not getattr(args, "allow_big", False):
        log(f"{base} has {n_params / 1e9:.2f}B parameters; above {MAX_PARAMS / 1e9:.1f}B it will not fit beside the desktop on an 8 GB card (--allow-big to try)")
        return 1
    if getattr(args, "dry_run", False):
        log(f"  dry run: {n_params / 1e9:.2f}B parameters, cuda {cuda}; nothing trained")
        return 0

    batch, accum, epochs = int(args.batch), int(args.accum), float(args.epochs)
    steps_per_epoch = math.ceil(len(train_rows) / (batch * accum))
    total_steps = int(math.ceil(steps_per_epoch * epochs))
    if getattr(args, "max_steps", None):
        total_steps = min(total_steps, int(args.max_steps))
    warmup = max(1, int(round(0.1 * total_steps)))
    lora = LoraConfig(task_type="CAUSAL_LM", r=int(args.rank), lora_alpha=int(args.alpha), lora_dropout=float(args.dropout),
                      bias="none", target_modules=TARGET_MODULES)
    cfg_kw = dict(output_dir=os.path.join(rd, "checkpoints"), max_length=max_length, completion_only_loss=True,
                  assistant_only_loss=False, bf16=cuda, gradient_checkpointing=True,
                  gradient_checkpointing_kwargs={"use_reentrant": False}, per_device_train_batch_size=batch,
                  gradient_accumulation_steps=accum, learning_rate=float(args.lr), num_train_epochs=epochs,
                  lr_scheduler_type="cosine", warmup_steps=warmup, logging_steps=1,
                  eval_strategy="epoch" if hold_rows else "no", per_device_eval_batch_size=batch, save_strategy="no",
                  report_to="none", seed=int(args.seed), data_seed=int(args.seed), dataloader_num_workers=0,
                  packing=False, padding_free=False, use_cache=False, eos_token=EOS, disable_tqdm=True)
    if getattr(args, "max_steps", None):
        cfg_kw["max_steps"] = int(args.max_steps)
    cfg = SFTConfig(**cfg_kw)
    ds_train = Dataset.from_list([{"prompt": r["prompt"], "completion": r["completion"]} for r in train_rows])
    ds_eval = Dataset.from_list([{"prompt": r["prompt"], "completion": r["completion"]} for r in hold_rows]) if hold_rows else None

    log_path = os.path.join(rd, "train_log.jsonl")
    eval_losses = []

    class JsonlLogger(TrainerCallback):
        def on_log(self, a, state, control, logs=None, **kw):
            logs = dict(logs or {})
            if "eval_loss" in logs:
                eval_losses.append(float(logs["eval_loss"]))
            with open(log_path, "a", encoding="utf-8") as f:
                import json
                f.write(json.dumps({"ts": time.time(), "step": state.global_step, "epoch": state.epoch, **logs}, default=str) + "\n")
            keys = [k for k in ("loss", "eval_loss", "learning_rate", "grad_norm", "mean_token_accuracy") if k in logs]
            log("  step " + f"{state.global_step:>4}" + "  " + "  ".join(f"{k} {float(logs[k]):.4g}" for k in keys))

    if os.path.isfile(log_path):
        os.remove(log_path)
    model.enable_input_require_grads()
    trainer = SFTTrainer(model=model, args=cfg, train_dataset=ds_train, eval_dataset=ds_eval, processing_class=tok,
                         peft_config=lora, callbacks=[JsonlLogger()])
    try:
        trainer.remove_callback(transformers.PrinterCallback)   # JsonlLogger prints the one line per step
    except Exception:
        pass
    trainable = sum(p.numel() for p in trainer.model.parameters() if p.requires_grad)
    log(f"  LoRA r={args.rank} alpha={args.alpha}: {trainable / 1e6:.1f}M trainable of {n_params / 1e9:.2f}B; "
        f"{total_steps} step(s) ({steps_per_epoch}/epoch × {epochs:g}), warmup {warmup}, batch {batch}×{accum}, lr {args.lr}")
    if cuda:
        torch.cuda.reset_peak_memory_stats()
    t0 = time.time()
    out = trainer.train()
    wall = time.time() - t0
    adapter = os.path.join(rd, "adapter")
    trainer.model.save_pretrained(adapter)
    tok.save_pretrained(adapter)
    peak = int(torch.cuda.max_memory_allocated() // (1024 * 1024)) if cuda else None
    run = {"run_id": run_id, "created": time.time(), "status": "trained", "base_model": base,
           "base_revision": getattr(getattr(model, "config", None), "_commit_hash", None), "dataset_id": manifest.get("dataset_id"),
           "dataset_dir": data_dir, "manifest_counts": manifest.get("counts"), "classes": manifest.get("classes"),
           "lora": {"r": int(args.rank), "alpha": int(args.alpha), "dropout": float(args.dropout), "target_modules": TARGET_MODULES,
                    "trainable_params": trainable, "total_params": n_params},
           "sft": {k: (v if isinstance(v, (int, float, str, bool, type(None))) else str(v)) for k, v in cfg_kw.items()},
           "tokens": tokens,
           "versions": {"python": sys.version.split()[0], "torch": torch.__version__, "cuda": torch.version.cuda,
                        "transformers": transformers.__version__, "peft": peft.__version__, "trl": trl.__version__,
                        "gpu": torch.cuda.get_device_name(0) if cuda else None, "git": git_commit()},
           "result": {"steps": int(getattr(out, "global_step", 0) or trainer.state.global_step), "epochs": epochs,
                      "train_loss_final": float(getattr(out, "training_loss", float("nan"))), "eval_loss_by_epoch": eval_losses,
                      "wall_s": round(wall, 1), "peak_vram_mb": peak},
           "prompt": {"mode": manifest.get("prompt_mode", "short"), "tools_version": manifest.get("tools_version"),
                      "system_sha1": manifest.get("system_sha1")},
           "adapter_dir": adapter, "deploy": None}
    paths.write_json(os.path.join(rd, "run.json"), run)
    paths.set_latest_run(run_id)
    if not getattr(args, "keep_checkpoints", False):
        shutil.rmtree(os.path.join(rd, "checkpoints"), ignore_errors=True)
    log(f"train: done in {wall:.0f} s, final loss {run['result']['train_loss_final']:.4f}"
        + (f", eval loss {eval_losses}" if eval_losses else "") + (f", peak VRAM {peak} MB" if peak else ""))
    log(f"  adapter: {adapter}\n  next: python -m finetune deploy")
    return 0
