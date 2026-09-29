# redline: scanned reviewer comments to dashboard block chains

A reviewer marks up plotted plan sheets by hand. The scans come back as a
PDF, one page per sheet, with the drawing's file path printed in the title
block. This package reads the handwriting with a local vision model, opens
the drawing the sheet names, maps each comment through the sheet's viewport
into model space, asks a local language model for a chain of existing
dashboard blocks, runs the chains it can and reports the rest with a reason.

No paid API is involved. Models are served over HTTP by Ollama during
development and by a bundled llama-server in a packaged install; both speak
the same OpenAI-compatible endpoint, so the code does not change.

## Install (once, on the machine running Civil 3D)

1. `pip install -r redline/requirements.txt` (pypdfium2, Pillow, numpy, requests).
2. Install Ollama for Windows from https://ollama.com/download, then pull the
   two models. Both are Apache-2.0; no Hugging Face account is needed.

       ollama pull qwen3-vl:4b-instruct # reads the handwriting (about 3.5 GB)
       ollama pull qwen2.5-coder:7b     # writes the block chains (about 4.7 GB)

   Ollama's default context window is 4096 tokens, which is too small for a
   crop plus the prompt, and far too small for the planner's palette. Set the
   user environment variable `OLLAMA_CONTEXT_LENGTH=16384` (then quit and
   restart Ollama from the tray) before the first run; `doctor` reports the
   window once a model has been loaded.

   Alternatives: `qwen2.5vl:7b` for the vision model, `qwen3:8b` for the
   planner. Any Hugging Face GGUF works too: `ollama pull hf.co/<user>/<repo>:<quant>`.
   On an 8 GB card only one model is resident at a time; the pipeline reads
   every page first, then plans, so the server swaps once per review.
3. Set `CAD_PROJECT_ROOT` to the folder(s) that hold the project drawings,
   `;` separated. Claude Desktop strips the user environment, so for the MCP
   tool put it in the env block of the Claude Desktop config, as
   `CAD_EXEMPLAR_DIR` is.
4. `python -m redline doctor` reports what is reachable and what is missing.

## Use

    python -m redline review scans.pdf [more.pdf ...] [--dry-run] [--on-conflict pause|warn]
    python -m redline ocr scans.pdf                # transcriptions only, nothing opened or run
    python -m redline phase0 scans.pdf --page 1 --click 2410,1180
    python -m redline annotate <review_id> <comment_id> --chain chain.json --verdict good|bad
    python -m redline palette                      # what the planner is shown

From Claude Desktop: `review_redlines(pdf_path)` starts a review in the
background; `review_status(review_id)` reads the report. The dashboard serves
the same reports at `GET /reviews` and `GET /reviews/{id}`.

Output goes to `dashboard/uploads/reviews/<review_id>/`: `report.md`,
`report.json`, `training.jsonl`, the page renders and the crops the model
read. A process aimed at the stub plugin (`CAD_PLUGIN_BASE` set) writes
under `dashboard/uploads-test/` instead. `CAD_REDLINE_DIR` overrides both.

## How a comment travels

1. `pdf.py` renders the page at 150 dpi.
2. `marks.py` finds the reviewer's ink (a red mask pooled onto a coarse
   grid; connected cells are one comment). No model. A grey photocopy falls
   back to the vision model on page tiles, which is coarse.
3. `ocr.py` asks the vision model, one crop at a time with a margin, for the
   text, the kind of mark (note, arrow, circle, cloud, strike, dimension),
   whether it points at something and where, and a confidence. It also
   transcribes the title strip.
4. `sheet.py` parses the sheet number, project number and `.dwg` name, scores
   candidate drawings (open in Civil 3D, under `CAD_PROJECT_ROOT`, the printed
   path itself), opens the best by asking the plugin for its snapshot with the
   file named, and picks the layout whose title block `#` matches. Two
   candidates within 0.05 of each other is `ambiguous_drawing` and the page
   is skipped rather than guessed.
5. `transform.py` maps page pixels to paper inches (from the layout's plot
   paper size and rotation; refined by a border fit when
   `CAD_REDLINE_BORDER_RECT` gives the border's paper rectangle) and paper
   inches to model feet through the viewport:
   `model = model_center + R(-twist) . ((paper - paper_center) / scale)`,
   the same arithmetic as the plugin's `ViewportWindow`.
6. `locate.py` writes the digest the planner reads: the index's sentence for
   the cell, the alignment station and side, the nearest entity of each
   useful tag with its handle, everything within 100 ft grouped by tag,
   closed shapes the point is inside, nearby labels and text. Entities on
   layers frozen in that viewport are left out. A comment outside every
   viewport gets the paper-space text around it instead (`paper_index.py`).
7. `planner.py` prompts the language model with the rules, the palette
   (from `dashboard.blocks.BLOCK_SPECS`, options included), the plan type's
   playbook steps and recipe, the worked examples in `examples/`, then
   the comment and its digest. The reply is `{"blocks": [...]}` or
   `{"skip": true, "reason": ...}`. A chain is validated by the dashboard's
   own `compile_blocks` and by a handle guard (every handle must be in the
   digest). One retry with the error, then a skip.
8. `runner.py` runs each chain on the dashboard assigned to the drawing
   (created with source `redline` when none is), one at a time, with
   `on_conflict=pause`. A pause is stopped and recorded as skipped with the
   conflict summary; `--on-conflict warn` draws through instead.
9. `report.py` writes the report and appends one training row per comment.

## Phase 0: prove the two hard links before trusting a review

1. Offline: `python tests/test_redline_transform.py` reproduces the plugin's
   viewport ring at 0 and 30 degrees and recovers a dot on a shrunk scan.
2. Live: `python -m redline phase0 <pdf> --page 1 --click X,Y` on a pixel
   where the reviewer circled something. The output shows the title block
   read, the drawing and layout chosen, the paper inches, the viewport, the
   model feet and the index's sentence there. The sentence should name what
   the reviewer circled. Repeat on a twisted sheet. The output also lists
   the layout's paper texts, which is how to confirm which one carries the
   printed path, and the border's nominal paper rectangle for
   `CAD_REDLINE_BORDER_RECT`.
3. Live: `python -m redline ocr <pdf>` and read 10 to 20 transcriptions by
   eye. `CAD_REDLINE_MIN_CONFIDENCE` (default 0.5) sends anything below it to
   a person instead of the planner.

## Tests

    python tests/test_redline_transform.py
    python tests/test_redline_marks.py
    python tests/test_redline_sheet.py
    python tests/test_redline_planner.py
    python tests/run_redline.py        # end to end on the stub plugin and a test dashboard

The end-to-end test starts its own servers on 5399 and 8799, builds a
two-page PDF from the stub's layouts (one untwisted sheet, one twisted 30
degrees and plotted rotated 90), and runs with fake OCR and a fake planner.
The transform, sheet match, digest, validation, dashboard runs and report
are real. Everything is forced under `%TEMP%`; the live `runs.json` is
checked untouched.

## Fine-tuning

Not yet. `training.jsonl` accumulates prompt context, comment, proposal and
outcome, and `annotate` adds a person's chain and verdict. Consider a LoRA
on the planner model once there are ten or more verified examples per
comment class across two or more projects and few-shot prompting is clearly
short of that. Until then, add worked examples to `examples/` and improve
the digest; both are free and reversible.

## Settings

| variable | default | meaning |
|---|---|---|
| `CAD_PLUGIN_BASE` | `http://127.0.0.1:5199` | the Civil 3D plugin |
| `CAD_DASHBOARD_BASE` | `http://127.0.0.1:8765` | the dashboard server |
| `CAD_PROJECT_ROOT` | unset | folders searched for `.dwg` files, `;` separated |
| `CAD_REDLINE_DIR` | `dashboard/uploads/reviews` | reports and training rows |
| `CAD_REDLINE_MODEL_BASE` | `http://127.0.0.1:11434/v1` | OpenAI-compatible endpoint |
| `CAD_REDLINE_VLM` | `qwen3-vl:4b-instruct` | vision model (the plain `qwen3-vl:4b` tag thinks by default and returns empty answers) |
| `CAD_REDLINE_LLM` | `qwen2.5-coder:7b` | planner model |
| `CAD_REDLINE_OCR` / `CAD_REDLINE_PLANNER` | `openai` | or `fake` for tests |
| `CAD_REDLINE_DPI` / `CAD_REDLINE_CROP_DPI` | 150 / 300 | page render, crop render |
| `CAD_REDLINE_TITLE_RECT` | `0.84,0,1,1;0,0.9,1,1` | page fractions holding the title text |
| `CAD_REDLINE_BORDER_RECT` | unset | border's paper rectangle, inches, for the fit |
| `CAD_REDLINE_MIN_CONFIDENCE` | 0.5 | below it, a comment goes to a person |
| `CAD_REDLINE_ON_CONFLICT` | `pause` | or `warn` |

A `redline.json` beside the repo root (or the file `CAD_REDLINE_CONFIG`
names) supplies the same keys for an install with no environment to set.
