"""Quantities in a prompt -> numbers the tools take (feet, degrees, counts).

Pure regex over the prompt with its quoted spans blanked out, so the 8" in
"put a note '8\" PVC' 10 ft north" is never read as a distance. Every
function returns floats or None and never raises; the filler's coercion
only strips ' and ft, so "6 inch" has to become 0.5 here.

Drawing units are feet. "10 ft circle" is read as a diameter (civil habit:
pipe and circle sizes are diameters); "radius"/"r =" wins when present.
"""
import re
from dataclasses import dataclass

_NUM = r"(?:\d+(?:\.\d+)?|\.\d+)"
_UNIT_FT = r"(?:'(?!')|ft\b|feet\b|foot\b)"
_UNIT_IN = r"(?:\"|''|in\b|inch(?:es)?\b)"

# a ' or " that follows a digit is a unit mark, not a quote
_QUOTED = re.compile(r"(?<![0-9A-Za-z])\"([^\"]{1,200}?)\"(?![0-9A-Za-z])|(?<![0-9A-Za-z])'([^']{1,200}?)'(?![0-9A-Za-z])"
                     r"|“([^”]{1,200}?)”")
_FT_IN = re.compile(rf"(?<![\w.])(\d+)\s*'\s*-?\s*({_NUM})\s*{_UNIT_IN}", re.I)
_FEET = re.compile(rf"(?<![\w.])({_NUM})\s*{_UNIT_FT}", re.I)
_INCHES = re.compile(rf"(?<![\w.])({_NUM})\s*{_UNIT_IN}", re.I)
_DIR_WORDS = r"(?:north|south|east|west|ne|nw|se|sw|n|s|e|w|left|right|up|down|towards?|away|along)"
_BARE_DIR = re.compile(rf"(?<![\w.])({_NUM})\s+(?={_DIR_WORDS}\b)", re.I)

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "twice": 2, "double": 2, "half": 0.5, "quarter": 0.25}


@dataclass
class Measure:
    value: float       # feet (distances), or whatever the function says
    start: int
    end: int
    raw: str


def strip_quoted(text: str) -> str:
    """The text with every quoted span replaced by spaces of equal length."""
    def blank(m):
        return " " * len(m.group(0))
    return _QUOTED.sub(blank, str(text or ""))


def _f(s) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def to_feet(s: str) -> float | None:
    """One length with its unit -> feet. "10'", "10 ft", "10 feet", "10'-6\"",
    "1'6\"", "6\"", "6 in", "6 inch", "2.5'". None for anything else."""
    t = str(s or "").strip()
    m = _FT_IN.fullmatch(t)
    if m:
        return round(float(m.group(1)) + float(m.group(2)) / 12.0, 4)
    m = _FEET.fullmatch(t)
    if m:
        return _f(m.group(1))
    m = _INCHES.fullmatch(t)
    if m:
        v = _f(m.group(1))
        return round(v / 12.0, 4) if v is not None else None
    return None


def distances(text: str) -> list:
    """Every length in reading order, as Measures in feet. A bare number
    right before a direction word counts as feet."""
    s = strip_quoted(text)
    found = []
    taken = []

    def free(a, b):
        return all(b <= x or a >= y for x, y in taken)

    for m in _FT_IN.finditer(s):
        found.append(Measure(round(float(m.group(1)) + float(m.group(2)) / 12.0, 4), m.start(), m.end(), m.group(0)))
        taken.append((m.start(), m.end()))
    for rx, scale in ((_FEET, 1.0), (_INCHES, 1.0 / 12.0)):
        for m in rx.finditer(s):
            if not free(m.start(), m.end()):
                continue
            v = _f(m.group(1))
            if v is None:
                continue
            found.append(Measure(round(v * scale, 4), m.start(), m.end(), m.group(0)))
            taken.append((m.start(), m.end()))
    for m in _BARE_DIR.finditer(s):
        if not free(m.start(), m.end()):
            continue
        v = _f(m.group(1))
        if v is not None:
            found.append(Measure(v, m.start(), m.end(), m.group(0)))
            taken.append((m.start(), m.end()))
    found.sort(key=lambda d: d.start)
    return found


def first_distance(text: str, near: tuple | None = None) -> Measure | None:
    """The length nearest a span of the text (a relation phrase), else the first."""
    ds = distances(text)
    if not ds:
        return None
    if near is None:
        return ds[0]
    a = near[0]
    return min(ds, key=lambda d: min(abs(d.start - a), abs(d.end - a)))


def _length_at(s: str, span_end: int) -> float | None:
    """A length (with or without a unit) starting at or right after span_end."""
    tail = s[span_end:]
    m = re.match(rf"\s*(?:of\s+|=\s*|:\s*)?({_NUM}\s*(?:{_UNIT_FT}|{_UNIT_IN})|{_NUM})", tail, re.I)
    if not m:
        return None
    return to_feet(m.group(1)) if to_feet(m.group(1)) is not None else _f(m.group(1))


def circle_size(text: str) -> tuple:
    """(radius in feet, note). Radius words win, then diameter words, then a
    bare size which is read as a diameter."""
    s = strip_quoted(text).lower()
    for word, factor, what in (("radius", 1.0, "radius"), (r"\brad\b", 1.0, "radius"), (r"\br\s*=", 1.0, "radius"),
                               ("diameter", 0.5, "diameter"), (r"\bdia\b", 0.5, "diameter")):
        m = re.search(word, s)
        if m:
            v = _length_at(s, m.end())
            if v is None:          # "10 ft radius": the length before the word
                before = [d for d in distances(s) if d.end <= m.start()]
                v = before[-1].value if before else None
            if v is not None:
                r = round(v * factor, 4)
                return r, (f"radius {v:g} ft" if what == "radius" else f"diameter {v:g} ft -> radius {r:g}")
    ds = distances(s)
    if ds:
        return round(ds[0].value / 2.0, 4), f"{ds[0].value:g} ft circle read as a diameter (radius {ds[0].value / 2.0:g})"
    return None, ""


def text_height(text: str) -> float | None:
    """"text height 2.5", "2.5' text", "3 inch text", "height 0.1", "0.1' tall"."""
    s = strip_quoted(text).lower()
    m = re.search(r"\bheight\b", s)
    if m:
        v = _length_at(s, m.end())
        if v is not None:
            return v
    m = re.search(rf"({_NUM}\s*(?:{_UNIT_FT}|{_UNIT_IN})|{_NUM})\s*(?:tall|high|text\b|letters?)", s, re.I)
    if m:
        v = to_feet(m.group(1))
        return v if v is not None else _f(m.group(1))
    return None


def rectangle_size(text: str) -> tuple | None:
    """"20 x 30", "20x30", "20' x 30'", "20 by 30", "20 ft by 30 ft" -> (width, height) in feet."""
    s = strip_quoted(text).lower()
    m = re.search(rf"({_NUM}\s*(?:{_UNIT_FT}|{_UNIT_IN})?)\s*(?:x|by|×)\s*({_NUM}\s*(?:{_UNIT_FT}|{_UNIT_IN})?)", s, re.I)
    if not m:
        return None
    vals = []
    for g in (m.group(1), m.group(2)):
        v = to_feet(g.strip())
        if v is None:
            v = _f(g.strip())
        if v is None:
            return None
        vals.append(v)
    return vals[0], vals[1]


def count(text: str) -> int | None:
    """"3 copies", "two copies", "twice", "copy ... 4 times". None when absent."""
    s = strip_quoted(text).lower()
    if re.search(r"\btwice\b", s):
        return 2
    m = re.search(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:copies|copy|times)\b", s)
    if m:
        w = m.group(1)
        return int(w) if w.isdigit() else int(NUMBER_WORDS.get(w, 0)) or None
    return None


def degrees(text: str) -> float | None:
    """"45 degrees", "45 deg", "90°", "rotate ... by 30"; clockwise is negative."""
    s = strip_quoted(text).lower()
    m = re.search(rf"({_NUM})\s*(?:degrees?|deg\b|°)", s)
    if not m:
        m = re.search(rf"\brotate\b(?:\s+\w+){{0,4}}?\s+(?:by\s+)?({_NUM})\b", s)
    if not m:
        return None
    v = _f(m.group(1))
    if v is None:
        return None
    if re.search(r"\b(?:clockwise|cw)\b", s) and not re.search(r"\b(?:counter[- ]?clockwise|anti[- ]?clockwise|ccw)\b", s):
        v = -v
    return v


def scale_factor(text: str) -> float | None:
    """"by 2", "2x", "double"/"twice as big" (2), "half" (0.5), "150%" (1.5), "factor 1.25"."""
    s = strip_quoted(text).lower()
    m = re.search(rf"({_NUM})\s*%", s)
    if m:
        v = _f(m.group(1))
        return round(v / 100.0, 4) if v is not None else None
    m = re.search(rf"\b(?:factor(?:\s+of)?|by)\s+({_NUM})\b", s)
    if m:
        return _f(m.group(1))
    m = re.search(rf"\b({_NUM})\s*x\b", s)
    if m:
        return _f(m.group(1))
    if re.search(r"\b(?:double|twice)\b", s):
        return 2.0
    if re.search(r"\bhalf\b", s):
        return 0.5
    return None
