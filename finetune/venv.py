"""The fine-tuning environment: a virtual environment beside the repo with
torch (CUDA), transformers, peft, trl and friends, plus llama.cpp's GGUF
converter source. Stdlib only, so the CLI can decide where to run before
importing anything heavy.

    python -m finetune setup [--force] [--torch-index cu130|cu128|cpu] [--skip-llama-cpp] [--llama-cpp-tag TAG]
    python -m finetune setup --check
"""
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
import zipfile

from . import paths

TORCH_VERSION = "2.14.1"
TORCH_INDEX = {"cu130": "https://download.pytorch.org/whl/cu130", "cu128": "https://download.pytorch.org/whl/cu128",
               "cpu": "https://download.pytorch.org/whl/cpu"}
LLAMA_CPP_TAG = "v0.7.0"
LLAMA_CPP_ZIP = "https://github.com/ggml-org/llama.cpp/archive/refs/tags/{tag}.zip"
REQUIREMENTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements-venv.txt")

VERIFY = r"""
import json, sys
out = {"python": sys.version.split()[0]}
try:
    import torch
    out["torch"] = torch.__version__
    out["cuda_available"] = bool(torch.cuda.is_available())
    out["cuda"] = torch.version.cuda
    out["arch_list"] = list(torch.cuda.get_arch_list()) if torch.cuda.is_available() else []
    if torch.cuda.is_available():
        out["device"] = torch.cuda.get_device_name(0)
        cap = torch.cuda.get_device_capability(0)
        out["capability"] = f"sm_{cap[0]}{cap[1]}"
        out["vram_mb"] = int(torch.cuda.get_device_properties(0).total_memory // (1024 * 1024))
        a = torch.randn(64, 64, device="cuda", dtype=torch.bfloat16)
        out["bf16_matmul_ok"] = bool(torch.isfinite((a @ a).float().sum()).item())
except Exception as e:
    out["torch_error"] = str(e)[:300]
for mod in ("transformers", "peft", "trl", "datasets", "accelerate", "safetensors"):
    try:
        m = __import__(mod)
        out[mod] = getattr(m, "__version__", "?")
    except Exception as e:
        out[mod + "_error"] = str(e)[:200]
print(json.dumps(out))
"""


def exists() -> bool:
    return os.path.isfile(paths.venv_python())


def marker() -> dict | None:
    return paths.read_json(paths.venv_marker())


def usable() -> bool:
    m = marker()
    return exists() and bool(m) and not m.get("torch_error")


def reexec(argv: list, log=print) -> int:
    """Runs `python -m finetune <argv>` with the venv's interpreter; 2 when
    there is no venv."""
    py = paths.venv_python()
    if not os.path.isfile(py):
        log(f"no fine-tuning environment at {paths.venv_dir()}; run: python -m finetune setup")
        return 2
    env = dict(os.environ)
    env[paths.ENV_INSIDE] = "1"
    env["PYTHONUTF8"] = "1"
    env.setdefault("HF_HOME", paths.hf_home())
    env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")   # Windows without developer mode: the cache just copies
    return subprocess.call([py, "-m", "finetune", *argv], cwd=str(paths.ROOT), env=env)


def _run(cmd: list, log=print, **kw) -> int:
    log("  $ " + " ".join(str(c) for c in cmd))
    return subprocess.call(cmd, cwd=str(paths.ROOT), **kw)


def verify(log=print) -> dict:
    py = paths.venv_python()
    r = subprocess.run([py, "-c", VERIFY], capture_output=True, text=True, cwd=str(paths.ROOT))
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {"torch_error": (r.stderr or r.stdout)[-400:]}
    return out


def llama_cpp_dir(tag: str = LLAMA_CPP_TAG) -> str:
    """The converter folder for a tag. GitHub's archive names the top folder
    without the tag's leading "v", so both spellings are accepted."""
    root = paths.tools_dir(False)
    for t in (tag, tag.lstrip("v")):
        p = os.path.join(root, f"llama.cpp-{t}")
        if os.path.isfile(os.path.join(p, "convert_hf_to_gguf.py")):
            return p
    return os.path.join(root, f"llama.cpp-{tag}")


def converter(tag: str | None = None) -> str | None:
    """Path of convert_hf_to_gguf.py, or None when not downloaded."""
    tags = [tag] if tag else [LLAMA_CPP_TAG]
    td = paths.tools_dir(False)
    if os.path.isdir(td) and not tag:
        tags += sorted((n[len("llama.cpp-"):] for n in os.listdir(td) if n.startswith("llama.cpp-")), reverse=True)
    for t in tags:
        p = os.path.join(llama_cpp_dir(t), "convert_hf_to_gguf.py")
        if os.path.isfile(p):
            return p
    return None


def download_llama_cpp(tag: str = LLAMA_CPP_TAG, log=print) -> str:
    """Fetches the tagged source zip and unpacks it under <finetune>/tools.
    Downloaded code stays in its own folder and is only ever run by path."""
    dest = llama_cpp_dir(tag)
    if os.path.isfile(os.path.join(dest, "convert_hf_to_gguf.py")):
        log(f"  llama.cpp {tag} already at {dest}")
        return dest
    url = LLAMA_CPP_ZIP.format(tag=tag)
    log(f"  downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read()
    tools_root = paths.tools_dir()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        top = z.namelist()[0].split("/")[0]
        z.extractall(tools_root)
    extracted = os.path.join(tools_root, top)
    if not os.path.isfile(os.path.join(extracted, "convert_hf_to_gguf.py")):
        raise RuntimeError(f"the archive for {tag} has no convert_hf_to_gguf.py")
    if os.path.abspath(extracted) != os.path.abspath(dest) and not os.path.isdir(dest):
        try:
            os.replace(extracted, dest)
        except OSError:
            dest = extracted          # the extracted name is fine; llama_cpp_dir accepts both spellings
    elif os.path.abspath(extracted) != os.path.abspath(dest):
        dest = extracted
    return dest


def setup(force: bool = False, torch_index: str = "cu130", skip_llama_cpp: bool = False,
          llama_cpp_tag: str = LLAMA_CPP_TAG, check_only: bool = False, log=print) -> int:
    vd, py = paths.venv_dir(), paths.venv_python()
    if check_only:
        if not exists():
            log(f"no fine-tuning environment at {vd}; run: python -m finetune setup")
            return 2
        out = verify(log)
        log(json.dumps(out, indent=2))
        return 0 if out.get("cuda_available") or torch_index == "cpu" else 1
    if force and os.path.isdir(vd):
        import shutil
        log(f"  removing {vd}")
        shutil.rmtree(vd, ignore_errors=True)
    if not exists():
        log(f"setup: creating {vd} from {sys.executable}")
        if _run([sys.executable, "-m", "venv", vd], log):
            return 1
    else:
        log(f"setup: {vd} exists (use --force to recreate)")
    index = TORCH_INDEX.get(torch_index)
    if not index:
        log(f"unknown torch index {torch_index!r}; one of {', '.join(TORCH_INDEX)}")
        return 1
    if _run([py, "-m", "pip", "install", "--upgrade", "pip"], log):
        return 1
    if _run([py, "-m", "pip", "install", f"torch=={TORCH_VERSION}", "--index-url", index], log):
        log("torch did not install; check the index name and the Python version")
        return 1
    if _run([py, "-m", "pip", "install", "-r", REQUIREMENTS], log):
        return 1
    out = verify(log)
    out.update({"created": time.time(), "torch_index": torch_index, "python_exe": py, "llama_cpp_tag": None})
    if out.get("torch_error"):
        log(f"setup: torch does not import in the venv: {out['torch_error']}")
        paths.write_json(paths.venv_marker(), out)
        return 1
    if torch_index != "cpu":
        if not out.get("cuda_available"):
            log("setup: torch sees no CUDA device; training needs --allow-cpu, or try --torch-index cu128")
        elif out.get("capability") and out["capability"] not in out.get("arch_list", []):
            log(f"setup: the GPU is {out['capability']} but this torch build has kernels for {out.get('arch_list')}; "
                "try --torch-index cu128")
        elif not out.get("bf16_matmul_ok"):
            log("setup: a bf16 matmul on the GPU failed")
    if not skip_llama_cpp:
        try:
            download_llama_cpp(llama_cpp_tag, log)
            out["llama_cpp_tag"] = llama_cpp_tag
        except Exception as e:
            log(f"setup: llama.cpp converter not downloaded ({e}); deploy will need --llama-cpp-tag or a manual download")
    paths.write_json(paths.venv_marker(), out)
    log("setup: " + ", ".join(f"{k} {out[k]}" for k in ("python", "torch", "cuda", "device", "capability", "transformers", "peft", "trl")
                             if k in out))
    log(f"setup: environment ready at {vd}" if out.get("cuda_available") or torch_index == "cpu" else "setup: finished with warnings")
    return 0
