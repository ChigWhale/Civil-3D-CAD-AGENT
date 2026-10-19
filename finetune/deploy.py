"""`python -m finetune deploy`: the trained adapter into Ollama.

    merge (in the venv): base + adapter -> merged safetensors -> GGUF (q8_0) via llama.cpp's converter
    then here (system Python): Modelfile -> ollama create NAME:<run> -> ollama cp NAME:latest
    -> the name is listed at {model_base}/models -> one smoke request through the pipeline's sequencer.

Why GGUF: Ollama on Windows (0.40) imports safetensors only through its MLX
runtime, which is not shipped for Windows and has no Qwen2 converter; GGUF
import is the route that works, and the same file serves a packaged install
through llama-swap. The Modelfile pins the ChatML template so a request
renders exactly as training did; no SYSTEM line, the pipeline sends its own.
"""
import os
import shutil
import subprocess
import time

from . import paths, prompts, venv
from redline import config, openai_compat, sequence

DEFAULT_NAME = "cad-sequencer"
NUM_CTX = 4096
TEMPERATURE = 0.1

# The Messages branch of Ollama's own qwen2.5-coder template (FIM and tools parts left out):
# system block, then user / assistant turns, then the assistant header for the reply.
TEMPLATE = (
    '{{- if .System }}<|im_start|>system\n{{ .System }}<|im_end|>\n{{ end }}'
    '{{- range $i, $_ := .Messages }}\n'
    '{{- $last := eq (len (slice $.Messages $i)) 1 -}}\n'
    '{{- if eq .Role "user" }}<|im_start|>user\n{{ .Content }}<|im_end|>\n'
    '{{ if $last }}<|im_start|>assistant\n{{ end }}\n'
    '{{- else if eq .Role "assistant" }}<|im_start|>assistant\n{{ .Content }}{{ if not $last }}<|im_end|>\n{{ end }}\n'
    '{{- end }}\n'
    '{{- end }}'
)


def modelfile_text(gguf_path: str, num_ctx: int = NUM_CTX, temperature: float = TEMPERATURE) -> str:
    return (f"FROM {os.path.abspath(gguf_path)}\n"
            f'TEMPLATE """{TEMPLATE}"""\n'
            f"PARAMETER temperature {temperature}\n"
            f"PARAMETER num_ctx {int(num_ctx)}\n"
            'PARAMETER stop "<|im_end|>"\n')


def gguf_path_for(run_id: str, name: str, outtype: str, quant: str | None) -> str:
    return os.path.join(paths.run_dir(run_id), "gguf", f"{paths.safe_name(name)}-{run_id}-{quant or outtype}.gguf")


def ollama_exe() -> str | None:
    return shutil.which("ollama")


def _ollama(args: list, log=print) -> int:
    exe = ollama_exe()
    if not exe:
        log("ollama is not on PATH")
        return 1
    log("  $ ollama " + " ".join(args))
    return subprocess.call([exe, *args])


def smoke(model_base: str, name: str, data_dir: str, log=print) -> list | None:
    rows = paths.read_jsonl(os.path.join(data_dir, "holdout.jsonl")) or paths.read_jsonl(os.path.join(data_dir, "train.jsonl"))
    if not rows:
        return None
    from . import build
    names, _ = build.parse_tools_txt(os.path.join(data_dir, "tools.txt"))
    m = rows[0].get("meta") or {}
    comment = prompts.comment_for(m.get("text", ""), m.get("kind"), m.get("confidence"), cid="smoke")
    backend = sequence.OpenAICompatPlanner(model_base, name, timeout=120)
    seq = sequence.sequence(comment, backend, "", names, examples=None, mode="short")
    log(f"  smoke: {str(m.get('text'))[:50]!r} -> {' -> '.join(seq.tools) or '(none: ' + str(seq.error) + ')'}  ({seq.latency_s:.1f} s)")
    return seq.tools


def main(args, log=print) -> int:
    settings = config.load()
    model_base = str(getattr(args, "model_base", None) or settings.model_base).rstrip("/")
    run_id = paths.resolve_run(getattr(args, "run", None))
    if not run_id:
        log("no trained run; run: python -m finetune train")
        return 1
    rd = paths.run_dir(run_id)
    runj = paths.read_json(os.path.join(rd, "run.json")) or {}
    name = getattr(args, "name", None) or DEFAULT_NAME
    outtype = getattr(args, "outtype", None) or "q8_0"
    quant = getattr(args, "quant", None) or None
    dry = bool(getattr(args, "dry_run", False))
    dep = runj.get("deploy") or {}
    gguf = dep.get("gguf") if getattr(args, "skip_merge", False) and dep.get("gguf") else gguf_path_for(run_id, name, outtype, quant)

    if not dry and not (getattr(args, "skip_merge", False) and os.path.isfile(gguf)):
        argv = ["merge", "--run", run_id, "--name", name, "--outtype", outtype] + (["--quant", quant] if quant else [])
        rc = venv.reexec(argv, log=log)
        if rc:
            return rc
        runj = paths.read_json(os.path.join(rd, "run.json")) or runj
        gguf = (runj.get("deploy") or {}).get("gguf") or gguf

    modelfile = os.path.join(rd, "Modelfile")
    paths.write_text(modelfile, modelfile_text(gguf))
    tag = f"{name}:{run_id}"
    log(f"deploy: Modelfile written to {modelfile}")
    if dry:
        log(f"  would run: ollama create {tag} -f {modelfile}")
        log(f"             ollama cp {tag} {name}:latest")
        return 0
    if not os.path.isfile(gguf):
        log(f"no GGUF at {gguf}; the merge step did not produce it")
        return 1
    if getattr(args, "no_ollama", False):
        return 0
    if _ollama(["create", tag, "-f", modelfile], log):
        return 1
    if _ollama(["cp", tag, f"{name}:latest"], log):
        return 1
    have = openai_compat.models(model_base)
    if tag not in have or f"{name}:latest" not in have:
        log(f"  warning: {model_base}/models does not list {tag} and {name}:latest (have: {', '.join(have)[:200]})")
        return 1
    tools_out = None
    if not getattr(args, "no_smoke", False):
        try:
            tools_out = smoke(model_base, name, paths.data_dir(False), log)
        except Exception as e:
            log(f"  smoke request failed: {e}")
    dep = dict(runj.get("deploy") or {})
    dep.update({"name": name, "tag": tag, "ollama_tags": [tag, f"{name}:latest"], "created_at": time.time(),
                "modelfile": modelfile, "smoke_tools": tools_out})
    runj["deploy"] = dep
    paths.write_json(os.path.join(rd, "run.json"), runj)
    log(f"deploy: {name} is served at {model_base}. To use it in the pipeline:")
    log(f"    set CAD_REDLINE_LLM={name}")
    log("    set CAD_REDLINE_PROMPT=short")
    log(f'  or python -m redline review --llm {name} --text "..." with CAD_REDLINE_PROMPT=short in the environment;')
    log(f'  redline.json: {{"llm": "{name}", "prompt": "short"}}. Roll back by unsetting both (ollama rm {tag} removes the model).')
    return 0
