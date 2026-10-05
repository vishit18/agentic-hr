"""Web server entry point.

Phase 0 starter: serves the frontend and a status page that checks every
dependency (database, Ollama models, Mailpit, CV folder, inbox).
Claude Code adds routers under app/api/ and includes them here.
"""
from __future__ import annotations

import smtplib
from pathlib import Path
from typing import Any

import httpx
import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import activity as activity_api
from app.api import admin as admin_api
from app.api import auth as auth_api
from app.api import candidates as candidates_api
from app.api import home as home_api
from app.api import public_form as public_form_api
from app.api import requests as requests_api
from app.api import review as review_api
from app.api import tickets as tickets_api
from app.config import settings
from app.core.auth import NotAuthenticated, get_current_user_optional, read_session
from app.core.csrf import verify_csrf
from app.core.db import SessionLocal
from app.core.models import User
from app.core.web import render, templates

# verify_csrf only acts on unsafe methods (POST/PUT/PATCH/DELETE); GET pages such as "/" are untouched.
app = FastAPI(title="Cadence HR Intelligence Hub", dependencies=[Depends(verify_csrf)])

app.mount(
    "/static",
    StaticFiles(directory=settings.frontend_dir / "static"),
    name="static",
)
# Tests swap this for a factory bound to the temporary test database.
app.state.session_factory = SessionLocal

app.include_router(auth_api.router)
app.include_router(home_api.router)
app.include_router(activity_api.router)
app.include_router(admin_api.router)
app.include_router(review_api.router)
app.include_router(tickets_api.router)
app.include_router(candidates_api.router)
app.include_router(requests_api.router)
app.include_router(public_form_api.router)


@app.exception_handler(NotAuthenticated)
async def not_authenticated(request: Request, exc: NotAuthenticated) -> Response:
    if request.headers.get("hx-request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
    """Plain-English error page for browsers; the status code is preserved."""
    if exc.status_code in {403, 404}:
        messages = {
            403: "You do not have access to this page, or the security check failed.",
            404: "We could not find that page.",
        }
        user: User | None = None
        try:
            with request.app.state.session_factory() as db:
                user = get_current_user_optional(request, db)
                return render(
                    request,
                    "error.html",
                    {"code": exc.status_code, "message": messages[exc.status_code]},
                    user=user,
                    db=db if user else None,
                    status_code=exc.status_code,
                )
        except Exception:  # noqa: BLE001 - never let the error page itself fail
            pass
    return Response(content=str(exc.detail), status_code=exc.status_code, headers=getattr(exc, "headers", None))


def check_database() -> dict[str, Any]:
    try:
        with psycopg.connect(settings.psycopg_dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
            vector = conn.execute(
                "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
            ).fetchone()
        if vector:
            return {"name": "Database", "ok": True, "detail": "Connected, pgvector available"}
        return {"name": "Database", "ok": False, "detail": "Connected, but pgvector is missing"}
    except Exception as exc:  # noqa: BLE001
        return {"name": "Database", "ok": False, "detail": f"Not reachable ({exc.__class__.__name__})"}


def check_ollama() -> dict[str, Any]:
    required = [settings.llm_model_extract, settings.llm_model_rerank, settings.embedding_model]
    try:
        resp = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5)
        resp.raise_for_status()
        installed = {m.get("name", "") for m in resp.json().get("models", [])}

        def present(model: str) -> bool:
            return model in installed or f"{model}:latest" in installed

        missing = [m for m in required if not present(m)]
        if missing:
            return {
                "name": "Ollama (local AI)",
                "ok": False,
                "detail": "Running, but missing: " + ", ".join(missing) + ". Run: ollama pull <name>",
            }
        return {"name": "Ollama (local AI)", "ok": True, "detail": "Running, all models installed"}
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "Ollama (local AI)",
            "ok": False,
            "detail": f"Not reachable at {settings.ollama_base_url} ({exc.__class__.__name__}). Is Ollama open?",
        }


def check_mail() -> dict[str, Any]:
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=3) as smtp:
            smtp.noop()
        return {"name": "Mail catcher (Mailpit)", "ok": True, "detail": "Connected. Inbox: http://localhost:8025"}
    except Exception as exc:  # noqa: BLE001
        return {"name": "Mail catcher (Mailpit)", "ok": False, "detail": f"Not reachable ({exc.__class__.__name__})"}


def check_folder(label: str, path: Path) -> dict[str, Any]:
    """Counts files only. Never opens or reads them."""
    if not path.exists():
        return {"name": label, "ok": False, "detail": f"Folder not found: {path}"}
    count = sum(1 for p in path.iterdir() if p.is_file())
    return {"name": label, "ok": True, "detail": f"{count} files"}


def run_checks() -> list[dict[str, Any]]:
    return [
        check_database(),
        check_ollama(),
        check_mail(),
        check_folder("CV folder (read-only)", settings.cvs_dir),
        check_folder("Inbox folder", settings.inbox_dir),
    ]


@app.get("/", response_class=HTMLResponse)
@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request) -> Response:
    # Signed-in users landing on "/" go to their home; "/status" always shows this page. No database needed.
    if request.url.path == "/" and read_session(request).get("uid"):
        return RedirectResponse("/home", status_code=303)
    checks = run_checks()
    all_ok = all(c["ok"] for c in checks)
    return templates.TemplateResponse(
        request,
        "status.html",
        {"checks": checks, "all_ok": all_ok},
    )


@app.get("/health")
def health() -> dict[str, Any]:
    checks = run_checks()
    return {"status": "ok" if all(c["ok"] for c in checks) else "degraded", "checks": checks}