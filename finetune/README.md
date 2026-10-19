# finetune: from comment labels to a tuned sequencer model

The redline pipeline's text stage turns one reviewer comment into an ordered
list of MCP tool names. This package fine-tunes a small open model on the
labels that stage collects and puts the result back in front of the
pipeline. It is separate from `redline/` (which never imports torch) and
from `learn/` (numpy only), and it is not shipped in the packaged product.

New to this? Read `FINE-TUNING.md` first: machine and data
requirements, what the model learns, how to read the eval table, when to
retrain, and the trouble list.

## The loop

    python -m finetune setup                                      # once: .venv-finetune with torch (CUDA), transformers, peft, trl; llama.cpp's converter
    python -m finetune build                                      # the labels -> data/train.jsonl, data/holdout.jsonl
    python -m finetune eval --llm qwen2.5-coder:7b --prompt full  # the baseline: the stock model on the held-out rows
    python -m finetune train                                      # a LoRA on Qwen2.5-Coder-1.5B-Instruct
    python -m finetune deploy                                     # merge -> GGUF -> ollama create cad-sequencer
    python -m finetune eval                                       # the tuned model on the same rows; side-by-side table
    python -m finetune status

Then, to run the pipeline on it:

    set CAD_REDLINE_LLM=cad-sequencer
    set CAD_REDLINE_PROMPT=short
    python -m redline markups marked.pdf --dry-run

Unset both to go back to the stock model. `ollama rm cad-sequencer:<run>`
removes a model.

## Where the labels come from

`build` reads the review folder the pipeline writes to (`CAD_REDLINE_DIR`,
else `dashboard/uploads/reviews`):

- the label set (`labelset/labelset.json`, the standalone window): text a
  person typed and the chain they chose (kind `note`, confidence 1.0, what
  a typed comment gets in `redline review --text`);
- every run's `training.jsonl`: a row is used when a person gave a chain in
  the results window (`human_tools`, whatever the verdict) or marked the
  model's chain Right (`human_verdict` "good"); unsure, Wrong without a
  chain, unlabelled and model-error rows are not.

Targets are checked against the tool vocabulary (a name the MCP server no
longer offers drops the row and is listed), duplicates by normalised text
are resolved label set > human chain > Right verdict (newest label on a
tie), and a text that is one of the few-shot examples is pinned to the
train side. The held-out split is by hash of the text (default 20%,
`--holdout`), so a comment stays on its side as the data grows and eval
numbers stay comparable. A class with fewer than ten rows or from one
project only gets a warning; nothing blocks.

## The prompt the tuned model sees

The pipeline's stock prompt carries all 137 tool descriptions and 17
examples (~5,000 tokens). A tuned model does not need them: it is trained
and served on the **short prompt**, the same system prompt plus only

    COMMENT: text="MOVE HYD 20' N" kind=note confidence=1.00
    ANSWER as JSON: {"tools": [...]}

rendered by `redline.sequence.build_user_prompt(..., mode="short")`, the
code the pipeline itself runs when `CAD_REDLINE_PROMPT=short`. The JSON
schema enum the pipeline sends still guarantees every name is a real tool.
The completion is the literal `{"tools": ["object_at", "move_entities"]}`.

## Training

`train` runs inside `.venv-finetune`: a LoRA (r 16, alpha 32, dropout 0.05,
all attention and MLP projections) on `Qwen/Qwen2.5-Coder-1.5B-Instruct` in
bf16, completion-only cross-entropy (prompt tokens masked), 3 epochs, lr
1e-4, cosine schedule, batch 4 × 2. Before touching the GPU it checks that
the tokenizer renders ChatML exactly as Ollama will, that every row fits
the token budget (TRL would otherwise cut the completion silently), and
that the base fits the card (above 2.2B parameters needs `--allow-big`; a
3B model in bf16 does not fit beside the desktop on 8 GB). Outputs under
`runs/<stamp>/`: `adapter/`, `train_log.jsonl` (loss per step, eval loss
per epoch), `run.json` (everything used, versions, peak VRAM).

Budget on the RTX PRO 2000 (8 GB): 1.5B bf16 weights ≈ 3.1 GB, LoRA and
optimiser ≈ 0.3 GB, activations at ~300 tokens small, CUDA context ≈ 0.7 GB;
about 5 GB peak, minutes per run on a few hundred rows.

## Deploy: why GGUF

Ollama on Windows (0.40) imports safetensors only through its MLX runtime,
which is not shipped for Windows and has no Qwen2 converter, and it does
not quantize GGUF on import. So `deploy` merges the adapter into the base
weights, runs llama.cpp's `convert_hf_to_gguf.py` (`--outtype q8_0`, about
1.6 GB, no extra binary; `--quant q4_K_M` needs `llama-quantize.exe` via
`CAD_LLAMA_QUANTIZE` or PATH), writes a Modelfile (`FROM` the GGUF, the
ChatML template pinned, temperature 0.1, `num_ctx` 4096, no SYSTEM line
because the pipeline sends its own) and runs `ollama create
cad-sequencer:<run>` plus `ollama cp` to `:latest`. It then checks the name
is listed at `{model_base}/models` and sends one smoke request through the
pipeline's own sequencer. `--dry-run` writes the Modelfile and prints the
commands only.

A packaged install serves GGUF files through llama-swap: copy the file into
its `models\` folder and add a `cad-sequencer` entry to `cad-agent.json`'s
`models` map (file, context 4096).

## Eval

`eval` sends the held-out rows through `redline.sequence.sequence()` with
the pipeline's own OpenAI-compatible backend (temperature 0.1, schema enum)
and scores them with `redline.metrics`: exact match (lookups collapsed, and
raw), tool-set precision / recall / F1, order score, first wrong step, step
accuracy by position, per class with Wilson intervals, errors and latency.
Results live in `eval/<dataset id>/<model>-<prompt>.json|.md`; with two or
more, `compare.md` holds the side-by-side table. Run the baseline before
training: it says whether the dataset is worth a run. `eval` warns when the
live tool vocabulary differs from the one the dataset was built on.

## Settings

| variable | default | meaning |
|---|---|---|
| `CAD_FINETUNE_DIR` | data dir `finetune`, else `dashboard/uploads/finetune` | datasets, runs, evals, HF cache, converter |
| `CAD_FINETUNE_VENV` | `<repo>/.venv-finetune` | the training environment |
| `CAD_FINETUNE_HF_HOME` | `<finetune dir>/hf-cache` | where the base model downloads |
| `CAD_LLAMA_QUANTIZE` | unset | `llama-quantize.exe` for `--quant` |
| `CAD_REDLINE_MODEL_BASE` | `http://127.0.0.1:11434/v1` | the model server (Ollama) |
| `CAD_REDLINE_PROMPT` | `full` | the pipeline's prompt shape; `short` for the tuned model |

`setup --torch-index cu128` is the fallback if the cu130 build lacks the
GPU's kernels (`setup` checks `sm_120` and a bf16 matmul). Retrain when
the tool vocabulary changes: the enum the model is judged against moves.

## Tests

    python finetune/tests/test_finetune.py    # builder rules, prompt identity, split, manifest, Modelfile, fake eval, CLI (no GPU, no venv, no network)
