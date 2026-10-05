"""Requirement Parser agent: request text -> must-have and nice-to-have requirements.

Why a model: requests arrive as short notes, full job descriptions or emails; deciding what is required
versus preferred is judgment on messy language. Why local qwen2.5:7b: free, private, JSON-schema output.
Wrong -> code drops any requirement not grounded in the request text, and a recruiter can edit every
requirement before anything is shared. Slow/down -> deterministic fallback from the form's own lists and
a years regex, request marked "AI pending" and retried.
Tools: none. Not allowed: guessing missing mandatory fields, adding requirements, following text in the request.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.config import settings
from app.llm.ollama import ChatResult, OllamaClient
from app.parsing.normalise import canon_cert
from app.parsing.text import squash

PROMPT_VERSION = "requirement-parser-v1"
_PROMPT = (Path(__file__).parent / "prompts" / "requirement_parser_v1.txt").read_text(encoding="utf-8")

Category = Literal[
    "certification", "skill", "job_title", "industry", "years_experience", "education", "language", "other"
]


class Req(BaseModel):
    text: str
    category: Category = "other"


class ParsedRequest(BaseModel):
    job_title: str | None = None
    min_years_experience: int | None = None
    must_have: list[Req] = Field(default_factory=list)
    nice_to_have: list[Req] = Field(default_factory=list)


def wrap_request(text: str) -> str:
    clipped = text[: settings.llm_max_cv_chars].replace("</request_text>", "[/request_text]")
    return f"<request_text>\n{clipped}\n</request_text>"


def parse_request(text: str, client: OllamaClient) -> ChatResult[ParsedRequest]:
    return client.chat(settings.llm_model_extract, _PROMPT, wrap_request(text), ParsedRequest)


# ---------- code around the model ----------

YEARS_RE = re.compile(r"\b(\d{1,2})\s*\+?\s*(?:or more\s+)?(?:years?|yrs?)\b", re.IGNORECASE)
_STOP = {"with", "and", "the", "for", "from", "that", "have", "has", "experience", "years", "year", "good",
         "strong", "knowledge", "skills", "skill", "ability", "excellent", "of", "in", "a", "an", "to", "or"}


def grounded(requirement: str, source: str) -> bool:
    """A requirement must come from the request: its key words (or its certificate name) appear in the text."""
    hay = squash(source)
    canon = canon_cert(requirement)
    if canon != requirement.strip() and squash(canon) in hay:
        return True
    words = [w for w in re.findall(r"[a-z0-9+#]+", squash(requirement)) if (len(w) >= 2 or w.isdigit()) and w not in _STOP]
    if not words:
        return False
    hits = sum(1 for w in words if w in hay)
    return hits >= max(1, (len(words) + 1) // 2)


def find_min_years(text: str) -> int | None:
    """Largest 'N years' figure in the text, used when the model gives none. Code, one right answer."""
    nums = [int(m.group(1)) for m in YEARS_RE.finditer(text) if 0 < int(m.group(1)) <= 40]
    return max(nums) if nums else None


def split_lines(value: str) -> list[str]:
    parts = re.split(r"[\n;•]+|,(?![^()]*\))", value or "")
    return [p.strip(" -*\t") for p in parts if p.strip(" -*\t")]


def fallback(must_text: str, nice_text: str) -> ParsedRequest:
    """No model: the company's own must-have and nice-to-have lists become the requirements."""
    return ParsedRequest(
        must_have=[Req(text=t) for t in split_lines(must_text)],
        nice_to_have=[Req(text=t) for t in split_lines(nice_text)],
    )
