"""Command line for the fine-tune package. `train` and `merge` run inside
the fine-tuning venv (the command re-executes itself there); everything
else runs in the Python that started it."""
import argparse
import sys

from . import paths, venv

VENV_COMMANDS = ("train", "merge")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m finetune",
                                 description="Fine-tune a small model on the comment -> tool-chain labels and serve it through Ollama.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("setup", help="create .venv-finetune with torch (CUDA), transformers, peft, trl; download llama.cpp's converter")
    p.add_argument("--force", action="store_true", help="recreate the environment")
    p.add_argument("--torch-index", dest="torch_index", default="cu130", choices=sorted(venv.TORCH_INDEX), help="PyTorch wheel index (default cu130)")
    p.add_argument("--skip-llama-cpp", dest="skip_llama_cpp", action="store_true")
    p.add_argument("--llama-cpp-tag", dest="llama_cpp_tag", default=venv.LLAMA_CPP_TAG, help=f"llama.cpp release tag (default {venv.LLAMA_CPP_TAG})")
    p.add_argument("--check", action="store_true", help="report the environment and exit")

    p = sub.add_parser("build", help="label set + labelled run rows -> train.jsonl / holdout.jsonl")
    p.add_argument("--holdout", type=float, default=None, help="held-out fraction by text hash (default 0.2; 0 = none)")
    p.add_argument("--min-per-class", dest="min_per_class", type=int, default=None, help="warn below this many rows per class (default 10)")
    p.add_argument("--review-dir", dest="review_dir", help="the review folder (default from the redline config)")
    p.add_argument("--labelset-dir", dest="labelset_dir", help="the label set folder (default <review dir>/labelset)")
    p.add_argument("--tools-file", dest="tools_file", help="a tools.txt to take the vocabulary from instead of the MCP registry")
    p.add_argument("--out", help="write the dataset here instead of <finetune dir>/data")

    p = sub.add_parser("train", help="LoRA fine-tune (runs in the venv)")
    p.add_argument("--base", default="Qwen/Qwen2.5-Coder-1.5B-Instruct", help="Hugging Face id or local folder of the base model")
    p.add_argument("--dataset", help="dataset folder (default <finetune dir>/data)")
    p.add_argument("--epochs", type=float, default=3.0)
    p.add_argument("--lr", type=float, default=1e-4)
    p.add_argument("--rank", type=int, default=16)
    p.add_argument("--alpha", type=int, default=32)
    p.add_argument("--dropout", type=float, default=0.05)
    p.add_argument("--batch", type=int, default=4)
    p.add_argument("--accum", type=int, default=2)
    p.add_argument("--max-length", dest="max_length", default="auto", help="token budget per row (default auto from the data)")
    p.add_argument("--max-length-cap", dest="max_length_cap", type=int, default=2048)
    p.add_argument("--seed", type=int, default=1234)
    p.add_argument("--run-id", dest="run_id", help="run folder name (default a timestamp)")
    p.add_argument("--offline", action="store_true", help="never reach Hugging Face (the base model must be cached)")
    p.add_argument("--max-steps", dest="max_steps", type=int, default=None, help="stop after this many steps (a smoke test)")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="pre-checks and token statistics only")
    p.add_argument("--allow-big", dest="allow_big", action="store_true", help="allow a base model above 2.2B parameters")
    p.add_argument("--allow-cpu", dest="allow_cpu", action="store_true", help="train without a CUDA device (slow)")
    p.add_argument("--keep-checkpoints", dest="keep_checkpoints", action="store_true")

    p = sub.add_parser("merge", help="merge the adapter into the base and convert to GGUF (runs in the venv)")
    p.add_argument("--run", default="latest")
    p.add_argument("--name", default="cad-sequencer")
    p.add_argument("--outtype", default="q8_0", choices=["q8_0", "f16", "bf16"])
    p.add_argument("--quant", default=None, help="e.g. q4_K_M; needs llama-quantize.exe")
    p.add_argument("--llama-cpp-tag", dest="llama_cpp_tag", default=None)

    p = sub.add_parser("deploy", help="merge -> GGUF -> ollama create; then the model is served under --name")
    p.add_argument("--run", default="latest")
    p.add_argument("--name", default="cad-sequencer")
    p.add_argument("--outtype", default="q8_0", choices=["q8_0", "f16", "bf16"])
    p.add_argument("--quant", default=None)
    p.add_argument("--skip-merge", dest="skip_merge", action="store_true", help="reuse the run's GGUF")
    p.add_argument("--no-ollama", dest="no_ollama", action="store_true", help="stop after the Modelfile")
    p.add_argument("--no-smoke", dest="no_smoke", action="store_true")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="write the Modelfile and print the commands only")
    p.add_argument("--model-base", dest="model_base")

    p = sub.add_parser("eval", help="score a served model on the held-out rows")
    p.add_argument("--llm", help="model name at the server (default: the pipeline's llm)")
    p.add_argument("--prompt", choices=["full", "short"], help="prompt shape (default: short for a deployed cad-sequencer, else full)")
    p.add_argument("--set", choices=["holdout", "train", "all"], default="holdout")
    p.add_argument("--dataset")
    p.add_argument("--model-base", dest="model_base")
    p.add_argument("--timeout", type=float)
    p.add_argument("--planner", choices=["openai", "fake"], default="openai")
    p.add_argument("--compare", action="store_true", help="print the side-by-side table and exit")

    sub.add_parser("status", help="folders, environment, dataset, runs, served models")
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    if args.cmd in VENV_COMMANDS and not paths.inside_venv():
        return venv.reexec(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if args.cmd == "setup":
        return venv.setup(force=args.force, torch_index=args.torch_index, skip_llama_cpp=args.skip_llama_cpp,
                          llama_cpp_tag=args.llama_cpp_tag, check_only=args.check)
    if args.cmd == "build":
        from . import build
        kw = {}
        if args.holdout is not None:
            kw["holdout"] = args.holdout
        if args.min_per_class is not None:
            kw["min_per_class"] = args.min_per_class
        m = build.build(review_dir=args.review_dir, labelset_dir=args.labelset_dir, tools_file=args.tools_file, out_dir=args.out, **kw)
        return int(m.get("exit", 0))
    if args.cmd == "train":
        from . import train
        return train.main(args)
    if args.cmd == "merge":
        from . import merge
        return merge.main(args)
    if args.cmd == "deploy":
        from . import deploy
        return deploy.main(args)
    if args.cmd == "eval":
        from . import evaluate
        return evaluate.main(args)
    if args.cmd == "status":
        from . import status
        return status.main(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
