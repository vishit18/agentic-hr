"""Code-only location guess for CVs where the model left the location empty.

Deliberately conservative: a wrong location hides a good candidate from a location filter, so when nothing clearly
looks like a place this returns None and the profile shows the location as unknown.
"""
from __future__ import annotations

import re

HEADER_LINES = 15
LABEL_LINES = 40
MAX_LEN = 80

_LABEL = re.compile(r"^\s*(?:current\s+)?(?:location|address|based\s+in|city|residence)\s*[:\-–]\s*(?P<v>.+)$", re.I)
_SPLIT = re.compile(r"\s*[|•·●▪◦]\s*|\s{3,}")
# "City, ST" or "City, Country" (one to three capitalised words each side, no digits)
_WORD = r"[A-Z][A-Za-z.'\-]+"
_PLACE = re.compile(rf"^(?P<city>{_WORD}(?: {_WORD}){{0,2}}),\s*(?P<region>[A-Z]{{2}}|{_WORD}(?: {_WORD}){{0,2}})$")
# Segments that look like a place but are really a job or a heading
_NOT_PLACE = re.compile(
    r"\b(manager|engineer|analyst|intern|developer|director|consultant|associate|officer|student|assistant|"
    r"limited|ltd|llc|inc|bank|university|college|school|capital|partners|group|summary|profile|education|"
    r"experience|skills|objective)\b", re.I)


def _clean(value: str) -> str:
    first = _SPLIT.split(value.strip())[0]
    return re.sub(r"\s+", " ", first).strip(" ,;-–")


def guess_location(text: str) -> str | None:
    lines = [ln.strip() for ln in text.splitlines()]
    for ln in lines[:LABEL_LINES]:
        m = _LABEL.match(ln)
        if m:
            value = _clean(m.group("v"))
            if value and len(value) <= MAX_LEN and not re.search(r"\d{3,}|@", value):
                return value
    for ln in lines[:HEADER_LINES]:
        if not ln:
            continue
        for seg in _SPLIT.split(ln):
            seg = seg.strip()
            if "@" in seg or re.search(r"\d", seg) or len(seg) > MAX_LEN or _NOT_PLACE.search(seg):
                continue
            head, comma, tail = seg.partition(",")
            if head.islower():  # "berkeley, CA"
                seg = head.title() + comma + tail
            if _PLACE.match(seg):
                return seg
    return None
