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
    session_cookie: str = "cadence_session"
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
    default_phone_region: str = "BH"
    max_zip_uncompressed_mb: int = 100
    max_image_pixels: int = 40_000_000
    llm_max_cv_chars: int = 16000

    # Local AI (Ollama)
    ollama_base_url: str = "http://host.docker.internal:11434"
    llm_model_extract: str = "qwen2.5:7b"
    llm_model_rerank: str = "qwen2.5:14b"
    llm_num_ctx: int = 8192
    llm_timeout_seconds: int = 120
    llm_max_retries: int = 2
    worker_concurrency: int = 4
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 768

    # Job queue
    job_max_attempts: int = 3
    job_stale_seconds: int = 600

    # Demo accounts (seed only creates users whose password is set)
    seed_recruiter_email: str = "recruiter@cadence.local"
    seed_recruiter_password: str = ""
    seed_admin_email: str = "admin@cadence.local"
    seed_admin_password: str = ""
    seed_engineer_email: str = "engineer@cadence.local"
    seed_engineer_password: str = ""

    # Email
    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    mail_from: str = "hub@cadence.local"

    @property
    def psycopg_dsn(self) -> str:
        """Plain psycopg connection string (SQLAlchemy URL without the driver suffix)."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


settings = Settings()