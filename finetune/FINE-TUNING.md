# Fine-tuning the comment sequencer: what you need to know before you start

This is the one document to read before running `python -m finetune`. It
covers what is being tuned and why, what the machine and the data must
look like, every step with what to expect from it, how to read the
results, and what to do when something is off. The command reference is
in `README.md` beside this file.

---

## 1. What is being fine-tuned, in one paragraph

The redline pipeline's text stage takes one reviewer comment ("MOVE HYD
20' N") and answers with the ordered list of MCP tool names that address
it (`["object_at", "move_entities"]`). Today a stock model,
`qwen2.5-coder:7b` through Ollama, does that from a ~5,000-token prompt
holding all 137 tool descriptions and 17 hand-written examples.
Fine-tuning replaces that with a small model (Qwen2.5-Coder 1.5B) that has
*learnt* the vocabulary and the house style from your labels, answers from
a ~250-token prompt, runs ten times faster, and can be measured against the
stock model on comments it never saw. The pipeline itself does not change;
two environment variables point it at the tuned model.

What it is **not**: it does not teach the model Civil 3D, geometry or your
drawings. It teaches the mapping *comment text → tool names*. Parameter
filling, locating and running stay exactly as they are.

---

## 2. Machine and software

| | needed | this laptop (verified 2026-10-19) |
|---|---|---|
| GPU | NVIDIA, 6 GB+ VRAM, driver supporting CUDA 13 | RTX PRO 2000 Blackwell, 8 GB, driver 596.98 |
| Python | 3.12–3.14 system interpreter (the repo's) | 3.14.5 |
| Disk | ~12 GB free: venv 6 GB, base model 3 GB, merged + GGUF 5 GB per run | |
| Ollama | 0.40+ on PATH, serving at `http://127.0.0.1:11434` | 0.40.1 |
| Network | once: PyTorch wheel (2 GB), HF libraries, llama.cpp source zip, the base model from Hugging Face (3 GB) | |
| Time | setup 10–15 min; build seconds; train minutes; deploy 2–5 min; eval 5 s per comment for the 7B, 0.5 s for the tuned model | |

The training stack never touches the repo's Python. `python -m finetune
setup` creates `.venv-finetune` beside the repo (gitignored) and installs
`torch 2.14.1+cu130`, `transformers 5.20.1`, `peft 0.21.2`, `trl 1.16.0`,
`datasets`, `accelerate`, `safetensors`, then downloads llama.cpp's source
for its GGUF converter. It ends by proving the GPU works: CUDA available,
`sm_120` in the kernel list, a bf16 matmul succeeds. If that check fails,
`setup --torch-index cu128 --force` is the fallback.

Why the venv: `redline/` must stay torch-free (the packaged product ships
without torch), and the training libraries are large and version-sensitive.

Model size rule: 1.5B in bf16 is 3.1 GB of weights and peaks around 3.5 GB
during training here. 3B would be 6.2 GB of weights alone and does not fit
beside the Windows desktop's share of 8 GB; `train` refuses anything above
2.2B parameters unless you pass `--allow-big`.

---

## 3. The data: where labels come from and how many you need

`build` reads two sources from the review folder (`CAD_REDLINE_DIR`, else
`dashboard\uploads\reviews`):

1. **The label set** (`python -m redline.labelset`): you type the comment
   and drag tool blocks into its chain. Cleanest source; no reading step
   to go wrong. Each example gets `kind=note, confidence=1.0`, the same as
   a typed comment in `redline review --text`.
2. **Labelled run rows**: every `training.jsonl` row from the results
   window where a person either gave a chain (`human_tools`, counted
   whatever the verdict was) or marked the model's chain Right
   (`human_verdict: good`). Rows marked Can't tell, Wrong without a
   corrected chain, unlabelled, or ending in a model error are skipped and
   counted.

What `build` does with them:

- drops any target naming a tool the MCP server no longer offers (listed in
  the manifest so you can fix the label);
- folds repeated consecutive tools, drops chains over 8 tools;
- deduplicates by normalised text (case and punctuation ignored): label set
  beats a human chain on a run row beats a Right verdict; newest label wins
  a tie; conflicting chains for the same text are listed;
- pins any text that is already a few-shot example to the train side;
- splits train / held-out by a hash of the text (default 20%), so a
  comment stays on its side forever and later evals stay comparable.

**How many labels.** The README's rule from the start: ten or more per
class across two or more projects before trusting a tune. Classes are the
chain's risk tier (read, draw, modify, delete, design). `build` prints a
table and a warning for every class under that bar; it never blocks.
Practical guidance:

| rows | what you can expect |
|---|---|
| under 20 | the tuned model will lose to the stock 7B; useful only to prove the pipeline works (that is where this machine is today: 3 rows) |
| 50–100 | the model picks up the common patterns (move, delete, label, relabel); judgement comments stay wrong |
| 200+ with 2+ projects | a fair fight with the 7B on your own comment style; the eval table decides |
| 500+ | diminishing returns on this task; spend labels on the comment types the eval table shows as weak |

Label the *long tail*, not the same "DELETE" fifty times: deduplication
keeps one row per distinct text, so repeats add nothing.

**Coverage.** The tuned model never sees the tool list at inference, so it
can only emit names that appear in its training targets. `build` prints
how many of the 137 names appear in a target and lists the unseen ones.
The schema enum at inference stops it inventing names, but a tool with no
training example is a tool it will never choose.

---

## 4. The prompt the model learns

Exactly this, rendered by the pipeline's own `redline.sequence` code in
short mode (one source of truth, asserted by the tests):

```
system: <the pipeline's SYSTEM_PROMPT, unchanged>
user:   COMMENT: text="MOVE HYD 20' N" kind=note confidence=1.00
        ANSWER as JSON: {"tools": [...]}
assistant: {"tools": ["object_at", "move_entities"]}
```

No TOOLS block, no EXAMPLES block. `kind` and `confidence` are part of the
prompt, so run rows carry their real values (circle, cloud, callout, 0.83)
and label-set rows get `note` / `1.00`. If you later change
`SYSTEM_PROMPT`, retrain: the manifest records its hash and `eval` warns.

---

## 5. The training recipe and why

- **Loss**: completion-only causal-LM cross-entropy. The model is scored
  only on the assistant tokens; prompt tokens are masked (`-100`). This is
  the standard SFT setup; it is what TRL's `SFTTrainer` does for
  prompt/completion data with `completion_only_loss=True`.
- **LoRA**, not full fine-tuning: rank 16, alpha 32, dropout 0.05 on all
  attention and MLP projections (18.5 M trainable parameters of 1.54 B).
  The base weights never change; the adapter is 70 MB and merges back in
  at deploy time.
- **bf16**, no 4-bit quantisation: bitsandbytes on Windows + Blackwell is
  a risk the 1.5B model does not need.
- **3 epochs, lr 1e-4, cosine schedule, 10% warm-up, batch 4 × 2
  accumulation**. Small data overfits fast; three passes is the right
  starting point. `--epochs`, `--lr`, `--rank`, `--batch` are flags.
- **Pre-checks before the GPU is touched**: the tokenizer must render
  ChatML exactly as Ollama will; every row must fit the token budget (TRL
  would otherwise cut the completion silently and the row would teach
  nothing); the base must fit the card.
- **Eval loss every epoch** on the held-out rows, logged to
  `train_log.jsonl`. Train loss falling while eval loss rises is
  overfitting: fewer epochs or more data.

---

## 6. The steps, with what to expect

```
python -m finetune status
```
Folders, environment, dataset, runs, served models. Run it first and last.

```
python -m finetune setup
```
Once per machine (10–15 min, mostly the 2 GB torch download). Must end
with `environment ready` and a line naming your GPU and `sm_120`.

```
python -m finetune build
```
Seconds. Read the table: train / holdout per class, projects per class,
warnings, "N of 137 tools appear in a target", the dataset id. Fix label
problems it lists (unknown names, conflicts) in the label set or the
results window and rebuild.

```
python -m finetune eval --llm qwen2.5-coder:7b --prompt full
```
The baseline on the held-out rows, about 5 s per comment. This is the
number to beat; if the stock model already scores near 100%, fine-tuning
buys speed, not accuracy, and you may not need it yet.

```
python -m finetune train
```
First time: downloads the 1.5B base (3 GB) into the finetune folder's
`hf-cache`. Then one line per step: `loss`, `learning_rate`, `grad_norm`,
`mean_token_accuracy`, and `eval_loss` at each epoch end. Ends with the
final loss, eval losses, peak VRAM and the adapter path. Use
`--dry-run` to see token statistics without training; `--max-steps 2` for
a smoke test.

```
python -m finetune deploy
```
Merges the adapter into the base on the CPU, converts to a q8_0 GGUF
(1.6 GB, 2–4 min), writes the Modelfile, runs `ollama create
cad-sequencer:<run>` and `ollama cp` to `cad-sequencer:latest`, checks the
name is served, and sends one real comment through the pipeline's own
sequencer (the "smoke" line). `--dry-run` writes the Modelfile only.

```
python -m finetune eval
```
The tuned model on the same held-out rows, then the side-by-side table.

```
set CAD_REDLINE_LLM=cad-sequencer
set CAD_REDLINE_PROMPT=short
python -m redline markups marked.pdf --dry-run
```
The pipeline on the tuned model. `report.json` shows `vocabulary.prompt:
"short"` and `prompt_chars` around 90. Unset both variables to go back;
`ollama rm cad-sequencer:<run>` removes a model.

---

## 7. Reading the results

The side-by-side table (`eval/<dataset id>/compare.md`):

| row | meaning | what good looks like |
|---|---|---|
| exact (collapsed) | the predicted chain equals the labelled one, with all lookup tools counted as one class | the headline; compare the Wilson ranges, not the point values, when n is small |
| exact (raw) | the same without collapsing lookups | lower is fine if only the lookup choice differs |
| precision / recall / F1 | the right tools present, regardless of order | recall below precision = the model leaves tools out; the reverse = it adds spurious ones |
| order | 1 − edit distance over the longer chain | catches right-tools-wrong-order |
| first wrong step | histogram of where chains diverge | "0" dominating means the model misreads the comment; later positions mean it loses the thread |
| per class | exact per risk tier | delete and design classes matter most; a wrong delete costs more than a wrong label |
| latency | seconds per comment | the tuned 1.5B is about ten times faster than the 7B |

Decision rule: switch the pipeline to the tuned model when its
*collapsed exact* interval is not below the baseline's and its
delete-class accuracy is at least as good. Until then keep the 7B and keep
labelling. With n under 20 the intervals are so wide that nothing is
decided either way; the table will tell you that honestly.

The per-row section of each `.md` lists gold vs predicted for every
held-out comment: read it. The failures are the labels to add next.

---

## 8. Knobs, in the order to try them

1. **More labels**, especially for the classes and comment types the eval
   shows as weak. Nothing else comes close.
2. `--epochs 5` when eval loss is still falling at epoch 3; `--epochs 2`
   when it rises.
3. `--lr 5e-5` if training is unstable (loss jumping); `2e-4` if it
   barely moves.
4. `--rank 32` once you have a few hundred rows; below that it only
   overfits faster.
5. `--holdout 0.3` on `build` to get tighter intervals at the cost of
   training rows; `--holdout 0` only for a final model after the decision
   is made.
6. A different base (`--base`) only within the 2.2B limit; `Qwen2.5-Coder-
   0.5B-Instruct` is the next smaller option if speed matters more.

---

## 9. When to retrain

- The tool vocabulary changed (a tool added, renamed or removed):
  `eval` and `status` warn that the live vocabulary hash differs from the
  dataset's. Rebuild and retrain; the enum the model is judged against has
  moved and a renamed tool cannot be chosen.
- `SYSTEM_PROMPT` changed: same warning, same answer.
- A batch of new labels landed: rebuild (the holdout stays stable by
  design), retrain, eval, compare with the previous run's table.

Each run keeps its own folder (`runs/<stamp>/` with `run.json` recording
every setting, version, loss and VRAM figure) and its own Ollama tag, so
you can keep two models side by side and point the pipeline at either.

---

## 10. Trouble

| symptom | cause and fix |
|---|---|
| `no fine-tuning environment ... run: python -m finetune setup` | `train`/`merge` need the venv; run setup |
| setup ends with "torch sees no CUDA device" | driver too old for CUDA 13, or wrong index; update the driver or `setup --torch-index cu128 --force` |
| `the GPU is sm_120 but this torch build has kernels for ...` | wheel without Blackwell kernels; `--torch-index cu128` |
| `N row(s) exceed max_length` | a very long comment; raise `--max-length-cap`, or shorten the label |
| `the tokenizer's chat template does not render ChatML` | a different base family; stay with Qwen2.5 Instruct models |
| CUDA out of memory | another GPU process (Civil 3D, a browser); close it, or `--batch 2` |
| deploy: `llama.cpp's convert_hf_to_gguf.py is not downloaded` | setup's download failed; rerun `setup` (it skips the installed parts) |
| deploy: `/models does not list cad-sequencer` | Ollama not running or a different `CAD_REDLINE_MODEL_BASE`; `ollama serve`, then `deploy --skip-merge` |
| eval: `model 'cad-sequencer' is not served` | deploy did not finish; `ollama list` |
| eval: `the live tool vocabulary differs from the dataset's` | rebuild and retrain (section 9) |
| `prompt_mismatches` in the eval summary | the dataset was built with an older `build_user_prompt`; rebuild |
| the tuned model answers nonsense | too little data; see the table in section 3; the baseline 7B is still the right model for now |

---

## 11. Files and folders

```
<finetune dir>              CAD_FINETUNE_DIR, else dashboard\uploads\finetune (gitignored)
  data\                     train.jsonl, holdout.jsonl, rows.jsonl (every decision), tools.txt, manifest.json
  runs\<stamp>\             run.json, train_log.jsonl, adapter\, merged\, gguf\, Modelfile
  eval\<dataset id>\        <model>-<prompt>.json|.md, compare.md
  hf-cache\                 the base model download
  tools\llama.cpp-<tag>\    the converter source
.venv-finetune\             the training environment (CAD_FINETUNE_VENV)
finetune\                   the package: build.py, train.py, merge.py, deploy.py, evaluate.py, venv.py, README.md, tests\
```

Nothing under `dashboard\uploads` is committed. The GGUF is the one
artefact worth keeping or copying: a packaged install serves it through
llama-swap by dropping it into its `models\` folder and adding a
`cad-sequencer` entry to `cad-agent.json`.

---

## 12. The honest state on 2026-10-19

The whole loop has run once on this machine: setup verified CUDA and
`sm_120`; build found 3 labelled rows (1 train, 2 held out) and warned
accordingly; the stock 7B scored 2 of 2 on the held-out rows; training on
the single row took 3 s at 3.5 GB peak VRAM; deploy produced a 1.6 GB
GGUF served as `cad-sequencer`; the tuned model scored 0 of 2 at 0.5 s per
comment against the 7B's 5 s. The machinery is proven; the labels are the
missing ingredient. Start in the label set window, aim for the table in
section 3, and let the eval decide.
