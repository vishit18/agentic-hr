"""Smoke test: the app starts and the health endpoint responds."""
from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint_responds() -> None:
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in {"ok", "degraded"}
    assert len(body["checks"]) == 5


def test_status_page_renders() -> None:
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "System status" in resp.text