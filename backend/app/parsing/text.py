"""Text cleaning. All CV text is untrusted data: strip markup and control characters."""
from __future__ import annotations

import re
import unicodedata

_TAGS = re.compile(r"<[^>]{0,200}>")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")


def clean_text(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", "\n")
    text = _CONTROL.sub(" ", text)
    text = _TAGS.sub(" ", text)  # HTML stripped from extracted text (XSS defence in depth)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", text).strip()


def is_garbage(text: str, min_alnum: int = 80) -> bool:
    """True when there is no usable text layer (empty, a few stray characters, or mojibake)."""
    stripped = text.strip()
    alnum = sum(c.isalnum() for c in stripped)
    if alnum < min_alnum:
        return True
    letters = sum(c.isalpha() for c in stripped)
    replacement = stripped.count("�")
    return letters / max(len(stripped), 1) < 0.4 or replacement / max(len(stripped), 1) > 0.05


def squash(text: str) -> str:
    """Lower-case, whitespace-collapsed form used to check that a quoted snippet exists in the CV."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip().lower()
