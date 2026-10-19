"""`python -m finetune merge` (inside the venv): the adapter merged into the
base weights, saved as safetensors, converted to GGUF with llama.cpp's
convert_hf_to_gguf.py (q8_0 straight from the converter; q4_K_M only when
llama-quantize.exe is available). The CPU is enough for a 1.5B model."""
import os
import shutil
import subprocess
import sys
import time

from . import paths, venv


def find_quantizer() -> str | None:
    env = os.environ.get("CAD_LLAMA_QUANTIZE", "").strip()
    if env and os.path.isfile(env):
        return env
    found = shutil.which("llama-quantize")
    if found:
        return found
    try:
        import launcher
        p = os.path.join(launcher.models_dir(), "llama-quantize.exe")
        if os.path.isfile(p):
            return p
    except Exception:
        pass
    return None


def main(args, log=print) -> int:
    os.environ.setdefault("HF_HOME", paths.hf_home())
    run_id = paths.resolve_run(getattr(args, "run", None))
    if not run_id:
        log("no trained run; run: python -m finetune train")
        return 1
    rd = paths.run_dir(run_id)
    runj = paths.read_json(os.path.join(rd, "run.json")) or {}
    adapter = runj.get("adapter_dir") or os.path.join(rd, "adapter")
    base = runj.get("base_model")
    if not base or not os.path.isdir(adapter):
        log(f"run {run_id} has no adapter at {adapter}")
        return 1
    conv = venv.converter(getattr(args, "llama_cpp_tag", None))
    if not conv:
        log("llama.cpp's convert_hf_to_gguf.py is not downloaded; run: python -m finetune setup (or --llama-cpp-tag)")
        return 1
    name = getattr(args, "name", None) or "cad-sequencer"
    outtype = getattr(args, "outtype", None) or "q8_0"
    quant = getattr(args, "quant", None) or None
    quantizer = find_quantizer() if quant else None
    if quant and not quantizer:
        log(f"--quant {quant} needs llama-quantize.exe (set CAD_LLAMA_QUANTIZE or put it on PATH)")
        return 1

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import PeftModel

    merged_dir = os.path.join(rd, "merged")
    t0 = time.time()
    log(f"merge: {base} + {adapter}")
    model = AutoModelForCausalLM.from_pretrained(base, dtype=torch.bfloat16)
    tok = AutoTokenizer.from_pretrained(adapter if os.path.isfile(os.path.join(adapter, "tokenizer_config.json")) else base)
    pm = PeftModel.from_pretrained(model, adapter)
    merged = pm.merge_and_unload(safe_merge=True)
    if os.path.isdir(merged_dir):
        shutil.rmtree(merged_dir, ignore_errors=True)
    merged.save_pretrained(merged_dir, safe_serialization=True)
    tok.save_pretrained(merged_dir)
    log(f"  merged weights at {merged_dir} ({time.time() - t0:.0f} s)")
    del merged, pm, model

    gguf_dir = os.path.join(rd, "gguf")
    os.makedirs(gguf_dir, exist_ok=True)
    conv_type = "f16" if quant else outtype
    out = os.path.join(gguf_dir, f"{paths.safe_name(name)}-{run_id}-{conv_type}.gguf")
    cmd = [sys.executable, conv, merged_dir, "--outfile", out, "--outtype", conv_type]
    log("  $ " + " ".join(cmd))
    rc = subprocess.call(cmd, cwd=rd)
    if rc or not os.path.isfile(out):
        log(f"the converter failed (exit {rc})")
        return 1
    final = out
    if quant:
        final = os.path.join(gguf_dir, f"{paths.safe_name(name)}-{run_id}-{quant}.gguf")
        cmd = [quantizer, out, final, quant.upper()]
        log("  $ " + " ".join(cmd))
        rc = subprocess.call(cmd, cwd=rd)
        if rc or not os.path.isfile(final):
            log(f"llama-quantize failed (exit {rc})")
            return 1
    size = os.path.getsize(final)
    runj["deploy"] = dict(runj.get("deploy") or {}, merged_dir=merged_dir, gguf=final, outtype=outtype, quant=quant,
                          converter=conv, gguf_sha1=paths.sha1_file(final), gguf_bytes=size, merged_at=time.time())
    paths.write_json(os.path.join(rd, "run.json"), runj)
    log(f"merge: GGUF {final} ({size / 1e9:.2f} GB) in {time.time() - t0:.0f} s")
    return 0
