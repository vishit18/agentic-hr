import json

import httpx
import pytest
from pydantic import BaseModel

from app.config import settings
from app.llm.ollama import LLMError, LLMUnavailable, LLMValidationError, OllamaClient


class Person(BaseModel):
    name: str
    years: int


def client_with(handler, **kwargs) -> OllamaClient:
    return OllamaClient("http://ollama.test", transport=httpx.MockTransport(handler), backoff_base=0, **kwargs)


def chat_reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})


def test_chat_sends_schema_temperature_zero_and_num_ctx() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return chat_reply('{"name": "Jane", "years": 8}')

    result = client_with(handler).chat("qwen2.5:7b", "sys", "user text", Person)

    body = seen["body"]
    assert seen["path"] == "/api/chat"
    assert body["model"] == "qwen2.5:7b"
    assert body["stream"] is False
    assert body["format"] == Person.model_json_schema()
    assert body["options"] == {"temperature": 0, "num_ctx": settings.llm_num_ctx}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert result.parsed == Person(name="Jane", years=8)
    assert result.model == "qwen2.5:7b"


def test_chat_rejects_output_that_does_not_match_schema() -> None:
    client = client_with(lambda r: chat_reply('{"name": "Jane", "years": "lots"}'))
    with pytest.raises(LLMValidationError):
        client.chat("m", "s", "u", Person)


def test_chat_rejects_non_json_output() -> None:
    client = client_with(lambda r: chat_reply("Sure! Here is the answer"))
    with pytest.raises(LLMValidationError):
        client.chat("m", "s", "u", Person)


def test_unreachable_ollama_raises_unavailable_after_retries() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("down")

    with pytest.raises(LLMUnavailable):
        client_with(handler, max_retries=2).chat("m", "s", "u", Person)
    assert calls["n"] == 3  # first try + 2 retries


def test_server_error_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return chat_reply('{"name": "Jane", "years": 1}')

    assert client_with(handler, max_retries=2).chat("m", "s", "u", Person).parsed.name == "Jane"
    assert calls["n"] == 2


def test_client_error_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, json={"error": "model not found"})

    with pytest.raises(LLMError) as exc:
        client_with(handler, max_retries=2).chat("m", "s", "u", Person)
    assert not isinstance(exc.value, LLMUnavailable)
    assert calls["n"] == 1


def test_embed_adds_nomic_prefixes() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"embeddings": [[0.1] * settings.embedding_dim for _ in body["input"]]})

    client = client_with(handler)
    doc = client.embed(["a CV", "another CV"], "document")
    query = client.embed(["cfa banking"], "query")

    assert seen[0]["input"] == ["search_document: a CV", "search_document: another CV"]
    assert seen[1]["input"] == ["search_query: cfa banking"]
    assert seen[0]["model"] == settings.embedding_model
    assert len(doc.vectors) == 2 and len(doc.vectors[0]) == settings.embedding_dim
    assert len(query.vectors) == 1


def test_embed_rejects_wrong_dimension() -> None:
    client = client_with(lambda r: httpx.Response(200, json={"embeddings": [[0.1, 0.2]]}))
    with pytest.raises(LLMValidationError):
        client.embed(["x"], "query")


def test_health_reports_missing_models_and_down() -> None:
    def tags(models: list[str]):
        return lambda r: httpx.Response(200, json={"models": [{"name": m} for m in models]})

    ok = client_with(tags([settings.llm_model_extract, settings.llm_model_rerank, f"{settings.embedding_model}:latest"]))
    assert ok.health().ok is True

    partial = client_with(tags([settings.llm_model_extract])).health()
    assert partial.ok is False
    assert settings.embedding_model in partial.missing_models

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert client_with(down).health().ok is False
