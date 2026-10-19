"""finetune: turn the comment labels into a fine-tuned sequencer model.

    python -m finetune setup     # a venv with torch (CUDA), transformers, peft, trl; llama.cpp's converter
    python -m finetune build     # label set + labelled run rows -> train.jsonl / holdout.jsonl
    python -m finetune eval --llm qwen2.5-coder:7b --prompt full    # the baseline, before training
    python -m finetune train     # LoRA on Qwen2.5-Coder-1.5B-Instruct, short prompt, completion-only loss
    python -m finetune deploy    # merge -> GGUF -> ollama create cad-sequencer
    python -m finetune eval      # the tuned model on the same held-out rows
    python -m finetune status

The package is separate from redline/ (which stays free of torch) and from
learn/ (numpy only). Heavy libraries live in their own virtual environment
(.venv-finetune); only `train` and `merge` run inside it. Nothing here is
shipped in the packaged product.
"""
__version__ = "0.1"
