"""Matcher agent: explains why a candidate fits a request, with quotes from the CV.

Why a model: turning evidence into a short, readable argument is language work. Ranking itself is code
(transparent weighted score), so the model cannot change the order. Why a local model: free, private.
Wrong -> every quote is checked against the CV text in code and unverified claims are dropped.
Slow/down -> a plain template summary built from the met and unmet requirements; marked as such.
Tools: none (it only sees the evidence pack). Not allowed: changing ranks, uncited claims, protected attributes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.config import settings
from app.llm.ollama import ChatResult, OllamaClient
from app.parsing.text import squash

PROMPT_VERSION = "matcher-v1"
_PROMPT = (Path(__file__).parent / "prompts" / "matcher_v1.txt").read_text(encoding="utf-8")


class Strength(BaseModel):
    text: str
    quote: str = ""


class Explanation(BaseModel):
    summary: str
    strengths: list[Strength] = Field(default_factory=list, max_length=6)
    gaps: list[str] = Field(default_factory=list, max_length=5)


def evidence_pack(data: dict[str, Any], must: list[str], nice: list[str], met: list[str], unmet: list[str]) -> str:
    """What the model sees: structured CV facts plus the lines it may quote. No contact details, no nationality."""
    lines = ["Experience:"]
    for e in (data.get("experience") or [])[:8]:
        lines.append(f"- {e.get('title')} at {e.get('company')} ({(e.get('start') or '?')[:7]} to "
                     f"{'present' if e.get('is_current') else (e.get('end') or '?')[:7]})"
                     + (f" | {e['snippet']}" if e.get("snippet") else ""))
    lines.append("Education:")
    for ed in (data.get("education") or [])[:4]:
        lines.append("- " + " ".join(filter(None, [ed.get("degree"), ed.get("field"), ed.get("institution")])))
    if data.get("skills"):
        lines.append("Skills: " + ", ".join(data["skills"][:25]))
    if data.get("certifications"):
        lines.append("Certificates: " + ", ".join(data["certifications"]))
    if data.get("years_experience") is not None:
        lines.append(f"Total years of experience (calculated): {data['years_experience']}")
    cand = "\n".join(lines)
    reqs = (
        "Must have: " + "; ".join(must) + "\nNice to have: " + "; ".join(nice)
        + "\nAlready checked by code, met: " + ("; ".join(met) or "none")
        + "\nAlready checked by code, not met: " + ("; ".join(unmet) or "none")
    )
    return f"<requirements>\n{reqs}\n</requirements>\n<candidate>\n{cand}\n</candidate>"


def explain(pack: str, client: OllamaClient) -> ChatResult[Explanation]:
    return client.chat(settings.llm_model_rerank, _PROMPT, pack, Explanation)


def verify(expl: Explanation, cv_text: str) -> tuple[Explanation, int]:
    """Keep a strength only if its quote exists in the CV. Returns the cleaned explanation and how many were dropped."""
    hay = squash(cv_text)
    kept, dropped = [], 0
    for s in expl.strengths:
        q = squash(s.quote)
        if len(q) >= 8 and q in hay:
            kept.append(s)
        else:
            dropped += 1
    return Explanation(summary=expl.summary, strengths=kept, gaps=expl.gaps), dropped


def template_summary(name: str | None, met: list[str], unmet: list[str], years: float | None) -> Explanation:
    """Used when the model is unavailable. Built from facts only."""
    bits = []
    if met:
        bits.append("Meets: " + ", ".join(met[:5]) + ".")
    if unmet:
        bits.append("Not shown in the CV: " + ", ".join(unmet[:4]) + ".")
    if years is not None:
        bits.append(f"About {years:g} years of experience.")
    return Explanation(summary=" ".join(bits) or "Matched on overall similarity to the request.",
                       strengths=[], gaps=unmet[:3])
