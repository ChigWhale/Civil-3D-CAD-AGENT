# redline: scanned reviewer comments to tool-name sequences

A reviewer marks up plotted plan sheets by hand. The scans come back as a
PDF, one page per sheet. This package reads the handwriting with a local
vision model, then asks a local language model, one comment at a time, for
the ordered list of **tool names** that would address it: the MCP tools
Claude Desktop sees (`find_entities`, `move_entities`, `draw_leader`,
`delete_objects` ...). Names only. No parameters, nothing opened or drawn,
and no comment is skipped: every comment gets a row, and an unusable answer
is recorded as empty with the model's reply.

That is the current stage. The earlier full run (match the sheet to its
drawing, open it, locate each comment through the viewport, plan
parameterised block chains, run them on the dashboard) is parked, not
deleted; see "Parked for the parameter stage" below.

No paid API is involved. Models are served over HTTP by Ollama during
development and by a bundled llama-server in a packaged install; both speak
the same OpenAI-compatible endpoint, so the code does not change.

## Install (once, on the machine running Civil 3D)

1. `pip install -r redline/requirements.txt` (pypdfium2, pypdf, Pillow, numpy, requests).
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
3. Both commands check the model server and the models they need before they
   start and stop with one line saying what is missing. A `review` of typed
   comments only needs the language model.

## Use

Three commands:

    python -m redline transcribe scans.pdf [--page N]      # read the handwriting only; nothing planned
    python -m redline review scans.pdf [more.pdf ...]       # handwriting: read, then name the tools per comment
    python -m redline review --text "MOVE HYD 20' N" --text "DELETE THIS LINE"
                                                            # typed comments, no scan: tries the model alone
    python -m redline markups marked.pdf [more.pdf ...]     # Bluebeam text boxes: no vision model, same output

`ocr` (optical character recognition, reading text out of an image) is
accepted as another name for `transcribe`; `bluebeam` for `markups`. The
command line is the only way to run the comment automation for now.

Output goes to `dashboard/uploads/reviews/<review_id>/`: `report.md` (one
row per comment with its tool sequence), `report.json`, `training.jsonl`,
`tools.txt` (the names the model could choose from), the page renders and
the crops the model read. A process aimed at the stub plugin
(`CAD_PLUGIN_BASE` set) writes under `dashboard/uploads-test/` instead.
`CAD_REDLINE_DIR` overrides both.

## How a comment travels

1. `pdf.py` renders the page at 150 dpi.
2. `marks.py` finds the reviewer's ink (a red mask pooled onto a coarse
   grid; connected cells are one comment). No model. A grey photocopy falls
   back to the vision model on page tiles, which is coarse.
3. `ocr.py` asks the vision model, one crop at a time with a margin, for the
   text, the kind of mark (note, arrow, circle, cloud, strike, dimension),
   whether it points at something and where, and a confidence. It also
   transcribes the title strip.
4. `tools.py` lists the vocabulary: every MCP tool registered by
   `cad_mcp_server.py` and `drafting_tools.py`, minus the ones that manage
   the agent (dashboards, runs, exemplars, playbooks, learning). Each gets its
   first docstring sentence. The list is written to `tools.txt`, and its
   hash is the `tools_version` in every training row.
5. `sequence.py` prompts the language model with the rules, the vocabulary,
   the worked examples in `examples/sequences.json`, and the comment
   (`text`, `kind`, `confidence`). The answer is `{"tools": [...]}`; the
   JSON schema sent with the request enumerates the names, so a model served
   by Ollama cannot invent one. Names are normalised, unknown ones dropped
   and recorded, repeats folded, at most 8 kept. An answer with no usable
   name is retried once with the problem stated, then recorded as empty
   (`no_valid_tools`). A model error is recorded as `model_error`. Nothing
   raises and nothing is skipped.
6. `report.py` writes `report.md` / `report.json` and appends one training
   row per comment with `human_tools` and `human_verdict` left null.

## Bluebeam text boxes (`markups`)

A PDF marked up in Bluebeam carries its markups as PDF annotations, not
ink. `markups.py` opens the file with pypdf and keeps the **FreeText**
annotations only: Bluebeam's Text Box, and Callout (a text box with a
leader). Each gives the comment's exact text (confidence 1.0), its author,
subject and date, its rectangle, and for a callout the point the leader's
arrowhead sits on (`targets_px`, kind `arrow`). Everything else (sticky
notes, clouds, arrows, pen, highlights, their pop-ups) is counted per kind
and ignored; the count appears in `report.md` under each page as "other
markups ignored". The pages are rendered into `pages/` so a person can look
at them, then the same sequencer, report and training rows run as for
handwriting. The report table has an extra `author` column.

A **flattened** PDF has no annotations left: the command reports zero text
boxes and says so. Ask reviewers for the unflattened file, or for Bluebeam's
"allow markup recovery" when they flatten.

## Before trusting a review

1. `python -m redline review --text "..."` with a few comments you know the
   answer to, and read the sequences the model gives.
2. `python -m redline transcribe <pdf>` on a real scan and read 10 to 20
   transcriptions by eye against the crops in the output folder.
3. `python -m redline review <pdf>` and read `report.md`: every comment with
   its tool sequence, or `(none: ...)` with the reason, and any names the
   model offered that were dropped.

## Parked for the parameter stage

`pipeline.review_located()` is the earlier full run and is not reachable from
the command line. It and its modules are kept, unchanged, for when the
tool-name stage is good enough to add parameters: `sheet.py` (which drawing
and layout the sheet is), `transform.py` (page pixels to paper inches to
model feet through the viewport), `locate.py` and `paper_index.py` (what is
at the point, with handles), `planner.py` (parameterised block chains
validated by the dashboard), `runner.py` (running them with the conflict
policy), `directives.py` (sheet-wide starred notes). `tests/run_redline.py`
still exercises all of it against the stub plugin, so it does not rot.

## Tests

    python tests/test_redline_sequence.py   # vocabulary, parsing, retry, fake backend, typed review
    python tests/test_redline_markups.py    # Bluebeam FreeText reader, rotation mapping, markups review, CLI
    python tests/test_redline_transform.py
    python tests/test_redline_marks.py
    python tests/test_redline_sheet.py
    python tests/test_redline_planner.py
    python tests/run_redline.py        # end to end on the stub plugin and a test dashboard

The end-to-end test starts its own servers on 5399 and 8799, builds a
two-page PDF from the stub's layouts, runs the current flow with fake OCR
and a fake sequencer (one row per comment, no skips, dashboard untouched),
and runs the parked flow too. Everything is forced under `%TEMP%`; the live
`runs.json` is checked untouched.

## Fine-tuning

Not yet. `training.jsonl` accumulates, per comment, the text, the kind, the
tool sequence the model gave, its raw reply and the vocabulary version, with
`human_tools` / `human_verdict` to be filled in by a person. Consider a LoRA
on the planner model once there are ten or more verified sequences per
comment class across two or more projects and the few-shot examples in
`examples/sequences.json` are clearly short of that. Until then, add
examples there; it is free and reversible.

## Settings

| variable | default | meaning |
|---|---|---|
| `CAD_REDLINE_DIR` | `dashboard/uploads/reviews` | reports and training rows |
| `CAD_REDLINE_MODEL_BASE` | `http://127.0.0.1:11434/v1` | OpenAI-compatible endpoint |
| `CAD_REDLINE_VLM` | `qwen3-vl:4b-instruct` | vision model (the plain `qwen3-vl:4b` tag thinks by default and returns empty answers) |
| `CAD_REDLINE_LLM` | `qwen2.5-coder:7b` | the model that names the tools |
| `CAD_REDLINE_OCR` / `CAD_REDLINE_PLANNER` | `openai` | or `fake` for tests |
| `CAD_REDLINE_MODEL_TIMEOUT` | 600 | seconds to wait for one model answer |
| `CAD_REDLINE_DPI` / `CAD_REDLINE_CROP_DPI` | 150 / 300 | page render, crop render |
| `CAD_REDLINE_TITLE_RECT` | `0.84,0,1,1;0,0.9,1,1` | page fractions holding the title text |
| `CAD_REDLINE_SWEEP` | `1` | the model takes a second look for handwriting on page tiles; `0` to skip |
| `CAD_REDLINE_MIN_AREA` | 0.04 | ink boxes smaller than this (square inches) are specks |

Read only by the parked flow: `CAD_PLUGIN_BASE`, `CAD_DASHBOARD_BASE`,
`CAD_PROJECT_ROOT`, `CAD_REDLINE_BORDER_RECT`, `CAD_REDLINE_MIN_CONFIDENCE`,
`CAD_REDLINE_ON_CONFLICT`, `CAD_REDLINE_CONTEXT_RADIUS`.

## What the ink detector records

The ink detector (`marks.py`) separates writing from connectors before it
groups anything. Long thin strokes (arrows, leaders) and rings (circles,
clouds) are traced on a fine grid to their far ends; a connector whose near
end sits beside a note gives that note one target per far end, and a ring
gives its centre. The crops the model reads hold the writing only, never
the arrows, which is what keeps them legible. In the current stage the
targets and the star that marks a sheet-wide note are recorded on the
comment (`targets_px`, `starred`, `scope`) but not acted on: every comment
is sequenced the same way. The parked flow uses them to locate a
three-arrow note three times and to file starred notes as directives.

`crops/p<N>_regions.png` in every review folder shows what the detector
found: green boxes are notes, orange are their connectors, blue dots their
targets, magenta are long connectors with no writing beside them. Those, and
tiles where the model counts clearly more marks than the detector boxed, are
listed in the report under "Possible missed comments".

A `redline.json` beside the repo root (or the file `CAD_REDLINE_CONFIG`
names) supplies the same keys for an install with no environment to set.
