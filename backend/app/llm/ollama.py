"""Ollama client: schema-validated chat, embeddings, health.

Errors are split so callers can pick the right fallback:
- LLMUnavailable: Ollama is down, slow or erroring -> deterministic fallback, mark "AI pending", requeue.
- LLMValidationError: the model answered but not in the required shape -> route to review.
"""
from __future__ import annotations

import time
from functools import lru_cache
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import settings

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


class LLMUnavailable(LLMError):
    pass


class LLMValidationError(LLMError):
    pass


@dataclass
class ChatResult(Generic[T]):
    parsed: T
    model: str
    latency_ms: int


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    model: str
    latency_ms: int


@dataclass
class OllamaHealth:
    ok: bool
    detail: str
    missing_models: list[str]


class OllamaClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
        num_ctx: int | None = None,
        transport: httpx.BaseTransport | None = None,
        backoff_base: float = 0.5,
    ) -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.llm_timeout_seconds
        self.max_retries = max_retries if max_retries is not None else settings.llm_max_retries
        self.num_ctx = num_ctx or settings.llm_num_ctx
        self.backoff_base = backoff_base
        self._http = httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=transport)

    def _post(self, path: str, body: dict) -> dict:
        """POST with bounded retries on connection errors, timeouts and 5xx."""
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.post(path, json=body)
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError("server error", request=resp.request, response=resp)
                if resp.status_code >= 400:
                    raise LLMError(f"Ollama rejected the request ({resp.status_code})")
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last = exc
                if attempt < self.max_retries:
                    time.sleep(self.backoff_base * 2**attempt)
        raise LLMUnavailable(f"Ollama unavailable after {self.max_retries + 1} attempts ({last.__class__.__name__})")

    def chat(self, model: str, system: str, user: str, schema: type[T]) -> ChatResult[T]:
        """Ask for JSON matching the Pydantic model. Temperature 0, num_ctx always set."""
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": schema.model_json_schema(),
            "stream": False,
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
        }
        started = time.monotonic()
        data = self._post("/api/chat", body)
        latency = int((time.monotonic() - started) * 1000)
        content = (data.get("message") or {}).get("content", "")
        try:
            parsed = schema.model_validate_json(content)
        except ValidationError as exc:
            raise LLMValidationError(f"Model output did not match {schema.__name__}: {exc.error_count()} error(s)") from exc
        return ChatResult(parsed=parsed, model=model, latency_ms=latency)

    def embed(
        self,
        texts: list[str],
        kind: Literal["document", "query"],
        model: str | None = None,
    ) -> EmbedResult:
        """Embed with the nomic prefixes: "search_document: " or "search_query: "."""
        model = model or settings.embedding_model
        prefix = "search_document: " if kind == "document" else "search_query: "
        started = time.monotonic()
        data = self._post("/api/embed", {"model": model, "input": [prefix + t for t in texts]})
        latency = int((time.monotonic() - started) * 1000)
        vectors = data.get("embeddings") or []
        if len(vectors) != len(texts) or any(len(v) != settings.embedding_dim for v in vectors):
            raise LLMValidationError(
                f"Embedding response has wrong shape (expected {len(texts)} x {settings.embedding_dim})"
            )
        return EmbedResult(vectors=vectors, model=model, latency_ms=latency)

    def health(self) -> OllamaHealth:
        required = [settings.llm_model_extract, settings.llm_model_rerank, settings.embedding_model]
        try:
            resp = self._http.get("/api/tags", timeout=5)
            resp.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            return OllamaHealth(False, f"Not reachable ({exc.__class__.__name__})", required)
        installed = {m.get("name", "") for m in resp.json().get("models", [])}
        missing = [m for m in required if m not in installed and f"{m}:latest" not in installed]
        if missing:
            return OllamaHealth(False, "Missing models: " + ", ".join(missing), missing)
        return OllamaHealth(True, "Running, all models installed", [])


@lru_cache(maxsize=1)
def get_client() -> OllamaClient:
    """Shared client for the worker and api. Tests patch this name where it is used."""
    return OllamaClient()
