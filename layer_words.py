"""Naming a layer from the words in its name.

A layer name here follows [xref|]D-MAJR-MINR[-MINR...][-FUTR]: a
discipline letter, a major category, minors, an optional future flag. Read
against the abbreviation dictionary in layer_words.json, that grammar gives
every layer a tag with no rule written for it: C-WATR-HYDR is water_hydrant in
the water family, V-WATR-VALV is water_valve and existing, C-SSWR-PIPE-FUTR is
sanitary_pipe and future. A token the dictionary does not know is kept as
itself, so an unfamiliar layer still gets a stable name (c-misc-stuff ->
misc_stuff) and is reported as unverified rather than lost as "other".

The first time a layer family is seen its derivation is written to
layer_tags.learned.json, where a person can read it, correct it (an entry with
"edited": true overrides the derivation from then on) or leave it. Hand-written
rules in layer_tags.json always win over both.

Nothing here reads a drawing; spatial.Vocabulary calls in.
"""

import json
import os
import re
import threading
import time
from dataclasses import dataclass, asdict

WORDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "layer_words.json")
LEARNED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "layer_tags.learned.json")

STATES = ("proposed", "existing", "future", "demolition", "abandoned", "temporary", "civil3d")
CONFIDENCE = ("full", "partial", "none")
_NUMERIC = re.compile(r"^\d+$")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").strip().lower()).strip("_")


@dataclass(frozen=True)
class Derivation:
    layer: str
    core: str                 # the layer minus its xref prefix, lower-cased
    family_key: str           # the learned-file key: core minus numeric tokens and "futr"
    tokens: tuple
    words: tuple
    unknown: tuple
    discipline: str           # civil | survey | landscape | civil 3d | ""
    tag: str
    family: str
    state: str
    confidence: str           # full | partial | none
    derivable: bool           # False: no discipline and no known major -- caller falls through

    def as_dict(self) -> dict:
        return asdict(self)


def _write_atomic(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ── The dictionary ────────────────────────────────────────────────────────────


class Words:
    """layer_words.json in memory: tokenizer plus derivation, editable and saved back."""

    def __init__(self, path: str = WORDS_FILE):
        self.path = path
        self.version = 0
        self.about: list = []
        self.separators = "-_ ."
        self.disciplines: dict = {}
        self.majors: dict = {}
        self.minors: dict = {}
        self.states: dict = {}
        self.annotation: set = set()
        self.ignore: set = set()
        self.family_glyphs: dict = {}
        self._memo: dict = {}
        self._tag_words = None
        self.load()

    def load(self) -> None:
        raw = {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        self.about = raw.get("_about") or []
        self.version = int(raw.get("version") or 0)
        self.separators = str(raw.get("separators") or "-_ .")
        self.disciplines = {str(k).lower(): {"word": str(v.get("word") or k), "state": str(v.get("state") or "proposed")}
                            for k, v in (raw.get("disciplines") or {}).items() if isinstance(v, dict)}
        self.majors = {}
        for k, v in (raw.get("majors") or {}).items():
            if isinstance(v, str):
                v = {"word": v}
            if not isinstance(v, dict):
                continue
            word = str(v.get("word") or k)
            self.majors[str(k).lower()] = {"word": word, "tag": _slug(v.get("tag") or word)}
        self.minors = {str(k).lower(): str(v) for k, v in (raw.get("minors") or {}).items()}
        self.states = {str(k).lower(): {"state": str(v.get("state") or "proposed"), "word": str(v.get("word") or k)}
                       for k, v in (raw.get("states") or {}).items() if isinstance(v, dict)}
        self.annotation = {str(t).lower() for t in raw.get("annotation") or []}
        self.ignore = {str(t).lower() for t in raw.get("ignore") or []}
        self.family_glyphs = {str(k).lower(): str(v)[:1] for k, v in (raw.get("family_glyphs") or {}).items() if str(v)}
        self._memo.clear()
        self._tag_words = None

    def save(self) -> None:
        self.version += 1
        data = {
            "_about": self.about,
            "version": self.version,
            "separators": self.separators,
            "disciplines": self.disciplines,
            "majors": {k: ({"word": v["word"], "tag": v["tag"]} if v["tag"] != _slug(v["word"]) else {"word": v["word"]})
                       for k, v in sorted(self.majors.items())},
            "minors": dict(sorted(self.minors.items())),
            "states": self.states,
            "annotation": sorted(self.annotation),
            "ignore": sorted(self.ignore),
            "family_glyphs": self.family_glyphs,
        }
        _write_atomic(self.path, data)
        self._memo.clear()
        self._tag_words = None

    # -- reading a name -----------------------------------------------------

    def tokenize(self, layer) -> tuple:
        """(core, tokens): the layer minus its xref prefix, split on the
        separators, without empty, numeric or ignored tokens."""
        core = str(layer or "").rsplit("|", 1)[-1].strip().lower()
        parts = re.split("[" + re.escape(self.separators) + "]+", core)
        tokens = [t for t in parts if t and not _NUMERIC.match(t) and t not in self.ignore]
        return core, tokens

    def family_key(self, layer) -> str:
        _, tokens = self.tokenize(layer)
        return "-".join(t for t in tokens if t != "futr")

    def derive(self, layer) -> Derivation:
        key = str(layer or "").lower()
        hit = self._memo.get(key)
        if hit is not None:
            return hit
        d = self._derive(layer)
        if len(self._memo) > 50_000:
            self._memo.clear()
        self._memo[key] = d
        return d

    def _derive(self, layer) -> Derivation:
        core, tokens = self.tokenize(layer)
        family_key = "-".join(t for t in tokens if t != "futr")
        rest = list(tokens)
        discipline, state = "", "proposed"
        if rest and rest[0] in self.disciplines:
            d = self.disciplines[rest[0]]
            discipline, state = d["word"], d["state"]
            rest = rest[1:]
        content, words, unknown = [], [], []
        for t in rest:
            if t in self.states:
                state = self.states[t]["state"]
                continue
            content.append(t)
        if not content:
            return Derivation(str(layer or ""), core, family_key, tuple(tokens), tuple(), tuple(),
                              discipline, "", "", state, "none", False)
        major, minors = content[0], content[1:]
        known_major = major in self.majors
        derivable = bool(discipline) or known_major
        if known_major:
            family = self.majors[major]["tag"]
            words.append(self.majors[major]["word"])
        else:
            family = _slug(major)
            words.append(major)
            unknown.append(major)
        parts = [family]
        for m in minors:
            if m in self.minors:
                words.append(self.minors[m])
                parts.append(_slug(self.minors[m]))
            else:
                words.append(m)
                unknown.append(m)
                parts.append(_slug(m))
        tag = "_".join(p for p in parts if p)
        if any(m in self.annotation for m in minors) or major in self.annotation:
            family = "annotation"
        if discipline:
            words.insert(0, discipline)
        n_known = len(content) - len(unknown)
        confidence = "full" if n_known == len(content) else ("partial" if n_known else "none")
        return Derivation(str(layer or ""), core, family_key, tuple(tokens), tuple(words), tuple(unknown),
                          discipline, tag, family, state, confidence, derivable)

    # -- editing ----------------------------------------------------------------

    def set_word(self, abbr: str, word: str, kind: str = "major", tag_key: str = "", glyph: str = "") -> dict:
        abbr = str(abbr or "").strip().lower()
        word = str(word or "").strip()
        kind = str(kind or "major").strip().lower()
        if not abbr or not word:
            raise ValueError("abbr and word are both required")
        if kind == "major":
            self.majors[abbr] = {"word": word, "tag": _slug(tag_key or word)}
            if glyph:
                self.family_glyphs[self.majors[abbr]["tag"]] = str(glyph)[:1]
        elif kind == "minor":
            self.minors[abbr] = word
        elif kind == "state":
            st = _slug(tag_key or word)
            if st not in STATES:
                raise ValueError(f"state must be one of {STATES}")
            self.states[abbr] = {"state": st, "word": word}
        elif kind == "discipline":
            st = _slug(tag_key or "proposed")
            if st not in STATES:
                raise ValueError(f"discipline state must be one of {STATES}")
            self.disciplines[abbr] = {"word": word, "state": st}
        elif kind == "annotation":
            self.annotation.add(abbr)
            self.minors.setdefault(abbr, word)
        else:
            raise ValueError("kind must be major, minor, state, discipline or annotation")
        self.save()
        return {"abbr": abbr, "word": word, "kind": kind, "tag_key": _slug(tag_key or word) if kind == "major" else None,
                "version": self.version}

    def forget_word(self, abbr: str, kind: str) -> bool:
        abbr = str(abbr or "").strip().lower()
        table = {"major": self.majors, "minor": self.minors, "state": self.states, "discipline": self.disciplines}.get(kind)
        if table is None or abbr not in table:
            return False
        del table[abbr]
        self.save()
        return True

    # -- what the vocabulary needs -----------------------------------------------

    def tag_key(self, major_abbr: str) -> str:
        m = self.majors.get(str(major_abbr or "").lower())
        return m["tag"] if m else _slug(major_abbr)

    def family_glyph(self, family: str) -> str:
        return self.family_glyphs.get(str(family or "").lower(), "")

    def families(self) -> list:
        return sorted({m["tag"] for m in self.majors.values()})

    def all_tag_words(self) -> frozenset:
        """Every family key and every family_minor combination the dictionary can
        produce: what a check may name without it being a typo."""
        if self._tag_words is None:
            out = set(self.families())
            minor_slugs = {_slug(w) for w in self.minors.values()}
            for fam in list(out):
                for m in minor_slugs:
                    out.add(f"{fam}_{m}")
            out.add("annotation")
            self._tag_words = frozenset(out)
        return self._tag_words

    def state_word(self, state: str) -> str:
        for v in self.states.values():
            if v["state"] == state:
                return v["word"]
        return str(state or "")

    def describe(self) -> dict:
        return {"file": self.path, "version": self.version, "disciplines": self.disciplines,
                "majors": len(self.majors), "minors": len(self.minors), "states": {k: v["state"] for k, v in self.states.items()},
                "annotation": sorted(self.annotation), "family_glyphs": self.family_glyphs,
                "families": self.families()}


# ── What the index has seen ───────────────────────────────────────────────────


class Learned:
    """layer_tags.learned.json: one entry per layer family, written the first
    time the family is seen. classify() only notes a derivation in `pending`;
    the index build flushes them, so the hot path never touches the disk."""

    def __init__(self, path: str = LEARNED_FILE):
        self.path = path
        self.about = ["Layer families the index named from their words, first time seen.",
                      "source auto = derived from layer_words.json. Set \"edited\": true (or use",
                      "edit_learned) and change tag / family / state to override the derivation",
                      "for that family. Hand rules in layer_tags.json still win over both."]
        self.entries: dict = {}
        self.pending: dict = {}
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = {}
        self.entries = raw.get("entries") if isinstance(raw, dict) and isinstance(raw.get("entries"), dict) else {}
        if isinstance(raw, dict) and raw.get("_about"):
            self.about = raw["_about"]

    def save(self) -> None:
        _write_atomic(self.path, {"_about": self.about, "entries": dict(sorted(self.entries.items()))})

    def get(self, family_key: str):
        return self.entries.get(str(family_key or "").lower())

    def note(self, family_key: str, d: Derivation) -> None:
        key = str(family_key or "").lower()
        if not key or key in self.entries or key in self.pending:
            return
        self.pending[key] = {
            "tag": d.tag, "family": d.family, "state": d.state,
            "tokens": list(d.tokens), "words": list(d.words), "confidence": d.confidence,
            "unknown": list(d.unknown), "first_seen": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "example": d.layer, "source": "auto", "edited": False,
        }

    def flush(self) -> int:
        with self._lock:
            if not self.pending:
                return 0
            added = 0
            for key, entry in self.pending.items():
                if key not in self.entries:
                    self.entries[key] = entry
                    added += 1
            self.pending.clear()
            if added:
                self.save()
            return added

    def edit(self, family_key: str, tag=None, family=None, state=None, note=None) -> dict:
        key = str(family_key or "").strip().lower()
        if not key:
            raise ValueError("family is required (the layer name without xref prefix, e.g. c-misc-stuff)")
        if state is not None and state != "" and state not in STATES:
            raise ValueError(f"state must be one of {STATES}")
        with self._lock:
            entry = dict(self.entries.get(key) or self.pending.get(key) or
                         {"tag": None, "family": None, "state": None, "tokens": [], "words": [],
                          "confidence": "none", "unknown": [], "first_seen": time.strftime("%Y-%m-%dT%H:%M:%S")})
            if tag:
                entry["tag"] = _slug(tag)
            if family:
                entry["family"] = _slug(family)
            if state:
                entry["state"] = state
            if note is not None:
                entry["note"] = str(note)
            entry["edited"] = True
            entry["source"] = "hand"
            entry["edited_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.entries[key] = entry
            self.pending.pop(key, None)
            self.save()
        return entry

    def tags(self) -> set:
        return {e.get("tag") for e in self.entries.values() if e.get("tag")}

    def describe(self, limit: int = 25) -> dict:
        edited = [k for k, e in self.entries.items() if e.get("edited")]
        low = [{"family": k, "tag": e.get("tag"), "unknown": e.get("unknown"), "example": e.get("example")}
               for k, e in self.entries.items() if e.get("confidence") == "none" and not e.get("edited")]
        return {"file": self.path, "entries": len(self.entries), "edited": edited[:limit],
                "unverified": low[:limit], "unverified_count": len(low)}
