"""The records the stages pass along. Plain dataclasses, JSON-safe via to_dict()."""
from dataclasses import asdict, dataclass, field
from typing import Optional


def _clean(d):
    if isinstance(d, dict):
        return {k: _clean(v) for k, v in d.items() if not str(k).startswith("_")}
    if isinstance(d, (list, tuple)):
        return [_clean(v) for v in d]
    return d


class Record:
    def to_dict(self) -> dict:
        return _clean(asdict(self))


@dataclass
class Page(Record):
    pdf: str
    page_no: int                 # 1-based
    dpi: int
    width_px: int
    height_px: int
    image_path: str = ""         # PNG on disk (empty for an in-memory page)
    width_in: float = 0.0        # the PDF page's own size, when known
    height_in: float = 0.0
    _image: object = field(default=None, repr=False, compare=False)

    @property
    def id(self) -> str:
        return f"p{self.page_no}"

    def image(self):
        if self._image is None:
            from PIL import Image
            self._image = Image.open(self.image_path).convert("RGB")
        return self._image


@dataclass
class Region(Record):
    id: str
    bbox_px: list                # [x0, y0, x1, y1] on the page image: the writing, not its arrows
    ink_fraction: float = 0.0
    color: str = "red"
    kind_hint: str = "text"      # text | arrow | ring: what the ink detector saw
    targets_px: list = field(default_factory=list)      # arrow tips / ring centres, page px
    connector_boxes: list = field(default_factory=list)  # the arrows and rings that belong to it


@dataclass
class Comment(Record):
    id: str                      # "p1-c3"
    page_no: int
    bbox_px: list
    text: str
    kind: str = "note"           # note | arrow | circle | cloud | strike | dimension | unknown
    targets_geometry: bool = False
    target_offset: Optional[str] = None      # above | below | left | right | on
    target_bbox_px: Optional[list] = None
    confidence: float = 0.0
    crop_path: str = ""
    raw_model_reply: str = ""
    targets_px: list = field(default_factory=list)   # arrow tips / ring centres from the ink detector
    starred: bool = False                             # begins with a drawn star / asterisk
    is_directive: bool = False                        # the model read it as a general instruction
    scope: str = "point"                              # point | multi | sheet

    def anchor_px(self) -> tuple:
        """Where this comment is about, for a single-spot comment."""
        if self.targets_px:
            return (float(self.targets_px[0][0]), float(self.targets_px[0][1]))
        b = self.target_bbox_px or self.bbox_px
        return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)

    def anchors_px(self) -> list:
        """Every spot this comment is about: one per arrow tip for a
        multi-target comment, one otherwise, none for a sheet-wide note."""
        if self.scope == "sheet":
            return []
        if self.scope == "multi":
            return [(float(t[0]), float(t[1])) for t in self.targets_px]
        return [self.anchor_px()]


def scope_of(targets_px: list, is_directive: bool, starred: bool = False, in_top_band: bool = False) -> str:
    """A star makes a note sheet-wide outright. Without one, the model's
    opinion that a note is a general instruction counts only for a note in
    the top margin that points at nothing: reviewers write imperatives
    beside things all the time ("connect to existing"), and those are spots."""
    if starred or (is_directive and in_top_band and not targets_px):
        return "sheet"
    return "multi" if len(targets_px or []) >= 2 else "point"


@dataclass
class TitleBlockRead(Record):
    raw_text: str = ""
    sheet_no: Optional[str] = None
    sheet_no_candidates: list = field(default_factory=list)
    project_no: Optional[str] = None
    sheet_title: Optional[str] = None
    printed_path: Optional[str] = None
    printed_file: Optional[str] = None       # the .dwg name without folders
    crops: list = field(default_factory=list)  # paths of the crops read


@dataclass
class ViewportRef(Record):
    handle: str
    paper_center: list
    paper_size: list
    scale: float
    twist_deg: float
    model_center: list
    on: bool = True
    frozen_layers: list = field(default_factory=list)


@dataclass
class SheetMatch(Record):
    page_no: int
    dwg_path: Optional[str] = None
    how: Optional[str] = None                # open_document | project_root | printed_path
    score: float = 0.0
    layout_name: Optional[str] = None
    layout_handle: Optional[str] = None
    page_to_paper: Optional[dict] = None
    viewports: list = field(default_factory=list)
    title_block: Optional[dict] = None
    candidates_considered: list = field(default_factory=list)
    reason: Optional[str] = None             # why the page could not be matched
    opened_by_pipeline: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.dwg_path and self.layout_name and self.page_to_paper)


@dataclass
class Located(Record):
    comment_id: str
    space: str                               # model | paper | none
    paper_xy: Optional[list] = None
    viewport_handle: Optional[str] = None
    model_xy: Optional[list] = None
    context: dict = field(default_factory=dict)
    reason: Optional[str] = None
    target_index: int = 0                    # which of the comment's targets this is
    target_count: int = 1
    target_px: Optional[list] = None         # the page pixel that was located

    @property
    def key(self) -> str:
        """What proposals and outcomes are filed under: the comment id, with
        `#n` appended for the n-th target of a multi-target comment."""
        return self.comment_id if self.target_count <= 1 else f"{self.comment_id}#{self.target_index + 1}"

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["key"] = self.key
        return d


@dataclass
class Directive(Record):
    """A sheet-wide reviewer note (starred, or general): stored per drawing
    and shown to a person and to the planner, never acted on by itself."""
    id: str
    text: str
    review_id: str
    page_no: int
    created: float
    dwg_path: Optional[str] = None
    layout_name: Optional[str] = None
    sheet_no: Optional[str] = None
    comment_id: Optional[str] = None
    status: str = "open"                     # open | done
    done_at: Optional[float] = None


@dataclass
class ChainProposal(Record):
    comment_id: str
    attempt: int = 1
    blocks: Optional[list] = None
    skip: bool = False
    reason: Optional[str] = None
    model: str = ""
    latency_s: float = 0.0
    compile_error: Optional[str] = None
    raw_reply: str = ""
    prompt_chars: int = 0


@dataclass
class CommentOutcome(Record):
    comment_id: str
    status: str                              # done | skipped | failed | paused | dry_run | needs_human
    reason: Optional[str] = None
    run_id: Optional[str] = None
    dashboard: Optional[str] = None
    conflicts: int = 0
    warnings: list = field(default_factory=list)
    results_preview: Optional[str] = None
    verification: Optional[dict] = None


@dataclass
class Report(Record):
    review_id: str
    created: float
    pdfs: list
    out_dir: str
    sheets: list = field(default_factory=list)
    comments: list = field(default_factory=list)
    located: list = field(default_factory=list)
    proposals: list = field(default_factory=list)
    outcomes: list = field(default_factory=list)
    unmatched_pages: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    directives: list = field(default_factory=list)        # sheet-wide notes found in this review
    recall_warnings: list = field(default_factory=list)   # hints that a comment may have been missed
