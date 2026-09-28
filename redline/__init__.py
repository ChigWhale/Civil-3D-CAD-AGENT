"""redline: scanned reviewer redlines on plotted plan sheets -> dashboard block chains.

A redlined sheet is a PDF page: the plotted plan with handwriting on top and
the drawing's name printed in the title block. The pipeline reads the
handwriting with a local vision-language model, opens the drawing the title
block names, maps each comment through the sheet's viewport into model space,
asks a local language model for a chain of existing dashboard blocks, runs the
chains it can and reports the ones it cannot with a reason.

Stages (one module each, every one testable without Civil 3D):
    pdf        rasterise pages
    marks      find the reviewer's ink (deterministic, no model)
    ocr        read the ink: text, kind of mark, what it points at
    transform  page pixels -> paper inches -> model feet
    sheet      which drawing and which layout a page is
    paper_index / locate   what sits where the comment lands
    palette / planner      the block chain, or a reason to skip
    runner / report        run it on the dashboard, write it down

Model calls go to an OpenAI-compatible endpoint (Ollama during development,
a bundled llama-server in a packaged install); nothing here imports torch.
"""
__version__ = "0.1.0"


def review(*args, **kwargs):
    """See redline.pipeline.review; imported on first use so the light
    modules (transform, marks) load without the whole pipeline."""
    from .pipeline import review as _review
    return _review(*args, **kwargs)
