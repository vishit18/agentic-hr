"""All settings come from environment variables (loaded from .env by Docker Compose)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # App
    app_env: str = "local"
    app_secret_key: str = "dev-only-change-me"
    max_upload_mb: int = 15
    max_pdf_pages: int = 15

    # Sessions and login
    session_cookie: str = "agentic_hr_session"
    session_max_age_seconds: int = 60 * 60 * 12
    login_max_failures_per_email: int = 5
    login_max_failures_per_ip: int = 20
    login_window_seconds: int = 600

    # Database
    database_url: str = "postgresql+psycopg://hub:hub@db:5432/hub"

    # Folders (paths inside the container)
    cvs_dir: Path = Path("/cvs")
    inbox_dir: Path = Path("/inbox")
    frontend_dir: Path = Path("/srv/frontend")
    poll_seconds: int = 15
    intake_max_files: int = 0  # 0 = no limit; used for timing runs
    ingest_batch_size: int = 50  # CVs processed before a cool-down pause; 0 = never pause
    ingest_batch_pause_seconds: int = 60
    default_phone_region: str = "BH"
    max_zip_uncompressed_mb: int = 100
    max_image_pixels: int = 40_000_000
    llm_max_cv_chars: int = 7000  # must fit in llm_num_ctx together with the prompt and the answer

    # Local AI (Ollama)
    ollama_base_url: str = "http://host.docker.internal:11434"
    llm_model_extract: str = "qwen2.5:7b"
    llm_model_rerank: str = "qwen2.5:7b"
    llm_num_ctx: int = 4096
    llm_timeout_seconds: int = 120
    llm_max_retries: int = 2
    llm_max_concurrent: int = 2  # simultaneous model calls; the GPU runs them one after another anyway
    worker_concurrency: int = 2
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 768

    # Matching (workflow B). Weights are relative; components that do not apply are left out.
    score_w_must: float = 0.45
    score_w_nice: float = 0.10
    score_w_years: float = 0.10
    score_w_title: float = 0.10
    score_w_similarity: float = 0.25
    retrieval_top_k: int = 50
    shortlist_explain_top: int = 15

    # Job queue
    job_max_attempts: int = 3
    job_stale_seconds: int = 600

    # Demo accounts (seed only creates users whose password is set)
    seed_recruiter_email: str = "recruiter@agentic-hr.local"
    seed_recruiter_password: str = ""
    seed_admin_email: str = "admin@agentic-hr.local"
    seed_admin_password: str = ""
    seed_engineer_email: str = "engineer@agentic-hr.local"
    seed_engineer_password: str = ""

    # Email
    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    mail_from: str = "hub@agentic-hr.local"

    @property
    def psycopg_dsn(self) -> str:
        """Plain psycopg connection string (SQLAlchemy URL without the driver suffix)."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


settings = Settings()