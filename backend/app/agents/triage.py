"""Ticket Triage agent: one-line summary of an HR problem report.

Why a model: reports are free text. Wrong or slow -> the summary is only a convenience; the full message and the
logs are always attached. Down -> the first words of the message are used. Tools: none. Not allowed: changing
any data, guessing a cause.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.config import settings
from app.llm.ollama import ChatResult, OllamaClient

PROMPT_VERSION = "triage-v1"
_PROMPT = (Path(__file__).parent / "prompts" / "triage_v1.txt").read_text(encoding="utf-8")


class Summary(BaseModel):
    summary: str


def summarise(message: str, client: OllamaClient) -> ChatResult[Summary]:
    text = message[:2000].replace("</report>", "[/report]")
    return client.chat(settings.llm_model_extract, _PROMPT, f"<report>\n{text}\n</report>", Summary)


def fallback(message: str) -> str:
    one_line = " ".join(message.split())
    return one_line if len(one_line) <= 110 else one_line[:107].rstrip() + "..."
