"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Only backend/.env is read (relative to the working directory). The repository-root .env is for
    # docker compose, which injects variables into the containers itself; reading both made the root
    # file silently override local-development settings.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- general -------------------------------------------------------
    app_name: str = "AgentReady"
    environment: Literal["development", "test", "production"] = "development"
    api_prefix: str = "/api/v1"
    frontend_url: str = "http://localhost:5173"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    log_level: str = "INFO"
    # text (human readable) | json (one JSON object per line, for log shippers)
    log_format: Literal["text", "json"] = "text"
    # Interactive API docs at /docs. Default: on outside production, off in production.
    api_docs_enabled: bool | None = None
    # Requests with a larger body are refused with 413 before they are read (nginx enforces 1 MB too).
    max_request_body_bytes: int = 2_000_000
    # Directory with the built frontend (index.html + assets/) to serve from the API process.
    # Empty: the frontend is served elsewhere (nginx in Docker Compose).
    static_dir: str = ""

    # --- database / queue --------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./agentready.db"
    redis_url: str = "redis://localhost:6379/0"
    # "celery" dispatches scans to the Celery worker; "inline" runs them as
    # asyncio tasks inside the API process (local development without Redis).
    job_backend: Literal["celery", "inline"] = "inline"
    # Single-instance hosting (e.g. one free Render web service, no Redis/worker): permits
    # JOB_BACKEND=inline with ENVIRONMENT=production. Jobs interrupted by a restart are recovered
    # as failed by the stale-job sweep. Run exactly ONE API process (WEB_CONCURRENCY=1) with it.
    allow_inline_jobs_in_production: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_recycle_seconds: int = 1800
    db_pool_timeout_seconds: int = 30
    # Background jobs (feed snapshots, bulk operations, integration syncs) that have not finished after
    # this long are considered interrupted (worker crash/restart) and are marked failed with a reason.
    job_stale_after_minutes: int = 30
    # /health asks the Celery worker for a ping (JOB_BACKEND=celery only)
    health_check_worker: bool = True

    # --- auth --------------------------------------------------------------
    secret_key: str = "dev-insecure-change-me"
    access_token_expire_minutes: int = 30
    password_reset_expire_minutes: int = 30
    auth_rate_limit_per_minute: int = 10
    # Failed-login throttle per account (any IP): protects against distributed password guessing
    login_failures_per_account: int = 10
    login_failure_window_seconds: int = 900

    # --- AI ----------------------------------------------------------------
    ai_provider: Literal["groq", "mock", "none"] = "groq"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # auto | json_schema | json_object
    groq_response_format: Literal["auto", "json_schema", "json_object"] = "auto"
    groq_timeout_seconds: float = 60.0
    groq_max_retries: int = 3
    groq_max_completion_tokens: int = 4096
    # USD per 1M tokens. Defaults reflect Groq's published price for
    # openai/gpt-oss-120b at the time of writing — verify and override.
    groq_input_price_per_mtok: float = 0.15
    groq_output_price_per_mtok: float = 0.60
    # when: "fallback" (only if deterministic extraction is insufficient),
    # "always" (every product/listing page) or "never".
    ai_extraction_mode: Literal["fallback", "always", "never"] = "fallback"
    max_ai_content_chars: int = 12000
    max_ai_calls_per_scan: int = 60

    # --- crawler -----------------------------------------------------------
    max_pages_per_scan: int = 100
    max_products_per_scan: int = 500
    max_crawl_depth: int = 4
    max_active_scans_per_business: int = 2
    # A queued/running scan whose row has not been updated for this long is considered dead
    # (worker crash/restart) and is marked failed so the website can be rescanned.
    scan_stale_after_minutes: int = 15
    crawl_delay_seconds: float = 1.0
    crawl_timeout_seconds: float = 15.0
    crawl_max_retries: int = 2
    max_response_bytes: int = 5_000_000
    max_redirects: int = 5
    crawler_user_agent: str = "AgentReadyBot/0.1 (catalog extraction; respects robots.txt)"
    allowed_ports: str = "80,443"
    # Only for local development against fixture servers. Never enable in production.
    allow_private_networks: bool = False
    playwright_enabled: bool = False

    # --- Phase 2: background scheduling ---------------------------------------
    # In-process scheduler loop for JOB_BACKEND=inline (Celery Beat is used with JOB_BACKEND=celery).
    scheduler_enabled: bool = True
    scheduler_interval_seconds: int = 60

    # --- Phase 2: integrations -------------------------------------------------
    # Fernet key for integration credentials at rest. Required in production; generate with
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    credentials_encryption_key: str = ""
    integration_max_retries: int = 2
    integration_retry_base_seconds: float = 1.0
    # Public base URL of this API (used for OAuth redirect URIs)
    api_base_url: str = "http://localhost:8000"
    # Shopify app credentials (from the Shopify Partner/Dev Dashboard). Without them only direct
    # Admin API access-token connections are possible.
    shopify_api_key: str = ""
    shopify_api_secret: str = ""
    shopify_api_version: str = "2026-10"
    shopify_scopes: str = "read_products,write_products,read_inventory,write_inventory,read_locations"

    # --- Phase 2: public catalog API ---------------------------------------------
    api_key_default_rate_limit: int = 120  # requests per minute per key

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        # Hosting providers (Render, Heroku, ...) hand out postgres:// or postgresql:// URLs;
        # the app needs the asyncpg driver.
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix):]
        return v

    @field_validator("secret_key")
    @classmethod
    def _secret_present(cls, v: str) -> str:
        if not v:
            raise ValueError("SECRET_KEY must be set")
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_port_set(self) -> set[int]:
        return {int(p) for p in self.allowed_ports.split(",") if p.strip()}

    @property
    def docs_enabled(self) -> bool:
        return self.api_docs_enabled if self.api_docs_enabled is not None else self.environment != "production"

    @property
    def ai_enabled(self) -> bool:
        if self.ai_provider == "mock":
            return True
        return self.ai_provider == "groq" and bool(self.groq_api_key)

def production_problems(s: Settings) -> list[str]:
    """Configuration that is acceptable for development but unsafe in production."""
    problems: list[str] = []
    if s.secret_key == "dev-insecure-change-me" or len(s.secret_key) < 32:
        problems.append("SECRET_KEY must be a random value of at least 32 characters")
    if s.allow_private_networks:
        problems.append("ALLOW_PRIVATE_NETWORKS must be false")
    if not s.credentials_encryption_key:
        problems.append("CREDENTIALS_ENCRYPTION_KEY must be set (integration credentials are encrypted at rest)")
    else:
        try:
            from cryptography.fernet import Fernet

            Fernet(s.credentials_encryption_key.encode())
        except Exception:
            problems.append("CREDENTIALS_ENCRYPTION_KEY is not a valid Fernet key")
    if s.ai_provider == "mock":
        problems.append("AI_PROVIDER=mock is a local test double and cannot be used in production")
    if any(o == "*" for o in s.cors_origin_list):
        problems.append("CORS_ORIGINS must list explicit origins, not *")
    if s.job_backend != "celery" and not s.allow_inline_jobs_in_production:
        problems.append("JOB_BACKEND must be celery (inline jobs die with the API process)")
    if s.database_url.startswith("sqlite"):
        problems.append("DATABASE_URL must point to PostgreSQL")
    return problems


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.environment == "production":
        problems = production_problems(s)
        if problems:
            raise RuntimeError("Refusing to start in production: " + "; ".join(problems))
    if s.environment == "test":
        s.scheduler_enabled = False
    return s
