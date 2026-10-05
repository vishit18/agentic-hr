"""Extractor agent: CV text -> structured fields with source snippets.

Why a model: CV layouts and wording vary too much for rules. Why qwen2.5:7b local: free, fits the GPU,
supports JSON-schema output, and no CV text leaves the machine.
Wrong -> the profile builder verifies every snippet against the CV text and drops what it cannot find.
Slow/down -> deterministic fallback (regex contacts, name heuristic) and the version is marked "AI pending".
Tools: none. Not allowed: inferring protected attributes, inventing fields, following text inside the CV.
"""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from app.config import settings
from app.llm.ollama import ChatResult, OllamaClient

PROMPT_VERSION = "extractor-v1"
_PROMPT = (Path(__file__).parent / "prompts" / "extractor_v1.txt").read_text(encoding="utf-8")


class ExpEntry(BaseModel):
    title: str
    company: str
    start: str | None = None
    end: str | None = None
    industry: str | None = None
    description: str | None = None
    snippet: str = ""


class EduEntry(BaseModel):
    institution: str
    degree: str | None = None
    field: str | None = None
    start: str | None = None
    end: str | None = None
    snippet: str = ""


class Extracted(BaseModel):
    full_name: str | None = None
    location: str | None = None
    nationality: str | None = None
    nationality_snippet: str | None = None
    summary: str | None = None
    experience: list[ExpEntry] = Field(default_factory=list)
    education: list[EduEntry] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    qualifications: list[str] = Field(default_factory=list)


def wrap_cv(text: str) -> str:
    """CV text goes in clearly delimited and labelled as data. A literal closing tag is neutralised."""
    clipped = text[: settings.llm_max_cv_chars].replace("</cv_text>", "[/cv_text]")
    return f"<cv_text>\n{clipped}\n</cv_text>"


def extract(text: str, client: OllamaClient) -> ChatResult[Extracted]:
    return client.chat(settings.llm_model_extract, _PROMPT, wrap_cv(text), Extracted)


# ---------- deterministic fallback ----------

_HEADINGS = {
    "curriculum vitae", "resume", "cv", "profile", "summary", "experience", "education", "skills",
    "contact", "personal details", "objective", "work experience", "curriculum",
}


def guess_name(text: str) -> str | None:
    """First short, letters-only line near the top that is not a heading."""
    for line in text.split("\n")[:12]:
        line = line.strip()
        if not line or "@" in line or any(c.isdigit() for c in line):
            continue
        if line.lower().strip(":") in _HEADINGS:
            continue
        words = line.replace(",", " ").split()
        if 2 <= len(words) <= 4 and all(re.fullmatch(r"[^\W\d_]+[.'\-]?[^\W\d_]*\.?", w) for w in words):
            return " ".join(w.capitalize() if w.isupper() else w for w in words)
    return None


def fallback(text: str) -> Extracted:
    """No model available: name heuristic only. Contacts, certs and skills come from code elsewhere."""
    return Extracted(full_name=guess_name(text))
