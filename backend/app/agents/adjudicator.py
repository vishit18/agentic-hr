"""Adjudicator agent: suggests same / different / unsure for ambiguous pairs. Suggestion only.

Why a model: weighing partial evidence (spelling variants, overlapping careers) is judgment on messy data.
Why local qwen2.5:7b: free, private, adequate for a short two-record comparison.
Wrong -> it only adds a suggestion to a review item; a named person decides, so the cost is a slower review.
Slow/down -> the review item is created without a suggestion. Tools: read two profile views.
Not allowed: merging, seeing protected attributes (views contain no location, nationality or contact details).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.config import settings
from app.llm.ollama import ChatResult, OllamaClient

PROMPT_VERSION = "adjudicator-v1"
_PROMPT = (Path(__file__).parent / "prompts" / "adjudicator_v1.txt").read_text(encoding="utf-8")


class Verdict(BaseModel):
    verdict: Literal["same", "different", "unsure"]
    reasons: list[str] = Field(default_factory=list, max_length=8)


def profile_view(data: dict[str, Any]) -> str:
    """Text view with protected-attribute-prone fields left out."""
    lines = [f"Name: {data.get('name')}"]
    for e in (data.get("experience") or [])[:8]:
        lines.append(f"Job: {e.get('title')} at {e.get('company')} ({e.get('start')} to {e.get('end')})")
    for ed in (data.get("education") or [])[:5]:
        lines.append(f"Education: {ed.get('degree')} {ed.get('field')} at {ed.get('institution')}")
    if data.get("skills"):
        lines.append("Skills: " + ", ".join(data["skills"][:15]))
    if data.get("certifications"):
        lines.append("Certificates: " + ", ".join(data["certifications"]))
    return "\n".join(lines)


def adjudicate(a: dict[str, Any], b: dict[str, Any], client: OllamaClient) -> ChatResult[Verdict]:
    user = f"<record_a>\n{profile_view(a)}\n</record_a>\n<record_b>\n{profile_view(b)}\n</record_b>"
    return client.chat(settings.llm_model_extract, _PROMPT, user, Verdict)
