from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    debug: bool = False

    database_url: str = "postgresql+asyncpg://simumarket:simumarket@localhost:5433/simumarket"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "development-only-change-me"
    jwt_issuer: str = "simumarket-ai"
    jwt_audience: str = "simumarket-ai-web"
    access_token_minutes: int = 15
    refresh_token_days: int = 30
    auth_cookie_secure: bool = False

    # NoDecode stops pydantic-settings from JSON-parsing the raw value, so the
    # validator below sees the comma-separated string an env file actually holds.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    gemini_api_key: str = ""

    # --- Celery ---------------------------------------------------------
    # Broker and result backend default to the same Redis as the cache. Redis is
    # transport only; PostgreSQL stays the system of record for every run.
    celery_broker_url: str = ""
    celery_result_backend: str = ""
    celery_task_always_eager: bool = False
    celery_analysis_queue: str = "analysis"
    celery_artifact_queue: str = "artifacts"
    celery_analysis_soft_time_limit_seconds: int = 600
    celery_analysis_time_limit_seconds: int = 900
    celery_analysis_max_retries: int = 3
    celery_artifact_max_retries: int = 2

    # --- Stuck-run recovery ---------------------------------------------
    # A worker holds a lease while it executes a run and renews it at every
    # stage. Nothing renews the lease of a dead worker, so an expired lease is
    # the signal docs/11 asks for: find the stuck job and requeue it.
    analysis_lease_seconds: int = 960
    analysis_queue_grace_seconds: int = 120
    analysis_max_attempts: int = 3
    analysis_recovery_interval_seconds: int = 60
    analysis_recovery_batch_size: int = 50

    # --- OASIS ----------------------------------------------------------
    oasis_enabled: bool = True
    oasis_provider: str = "gemini"
    oasis_model_id: str = "gemini-3.1-flash-lite"
    oasis_package_version: str = "0.2.5"
    camel_package_version: str = "0.2.78"
    oasis_prompt_version: str = "oasis-council-v2"
    oasis_cohort_version: str = "jabodetabek-fnb-v1"
    oasis_seed: int = 42
    oasis_cohort_size: int = 16
    oasis_round_limit: int = 4
    oasis_token_budget: int = 120_000
    oasis_max_output_tokens_per_stage: int = 1_024
    oasis_concurrency_limit: int = 4
    oasis_wall_clock_seconds: int = 240
    oasis_retry_limit: int = 1
    oasis_trace_root: str = "var/oasis-traces"
    oasis_trace_retention_days: int = 30

    # --- Private object storage, receipt OCR, and exports ---------------
    object_storage_provider: Literal["s3", "memory"] = "s3"
    object_storage_endpoint_url: str = "http://localhost:9000"
    object_storage_public_endpoint_url: str = ""
    object_storage_region: str = "us-east-1"
    object_storage_bucket: str = "simumarket-private"
    object_storage_access_key: str = "simumarket"
    object_storage_secret_key: str = "development-object-storage-secret"
    object_storage_signed_url_seconds: int = 600
    receipt_max_size_bytes: int = 10 * 1024 * 1024
    receipt_max_pixels: int = 24_000_000
    receipt_image_retention_days: int = 30
    receipt_ocr_provider: Literal["paddle", "unavailable"] = "paddle"
    receipt_ocr_engine_version: str = "PP-StructureV3"
    receipt_preprocessing_version: str = "receipt-image-v1"
    export_retention_days: int = 7
    retention_interval_seconds: int = 3600

    # --- SSE ------------------------------------------------------------
    sse_heartbeat_seconds: int = 15
    sse_poll_interval_seconds: int = 2
    sse_max_duration_seconds: int = 900

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def result_backend_url(self) -> str:
        return self.celery_result_backend or self.redis_url

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        # Environment variables carry a single string; an explicit allowlist is required
        # by docs/07, so a bare "*" must never survive parsing.
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("jwt_secret")
    @classmethod
    def require_production_secret(cls, value: str, info: object) -> str:
        if not value:
            raise ValueError("JWT_SECRET tidak boleh kosong")
        return value

    @model_validator(mode="after")
    def validate_auth_settings(self) -> "Settings":
        if self.environment in {"staging", "production"}:
            if self.jwt_secret == "development-only-change-me" or len(self.jwt_secret) < 32:
                raise ValueError("JWT_SECRET harus unik dan minimal 32 karakter")
            if not self.auth_cookie_secure:
                raise ValueError("AUTH_COOKIE_SECURE wajib aktif di staging dan production")
        return self

    @model_validator(mode="after")
    def validate_oasis_settings(self) -> "Settings":
        # docs/14 forbids preview-tagged models on the demo path because their
        # behaviour can change without notice, which would silently invalidate
        # every comparison recorded against a run manifest.
        if "-preview" in self.oasis_model_id:
            raise ValueError("Model berlabel -preview tidak boleh dipakai pada jalur demo")
        if not 12 <= self.oasis_cohort_size <= 24:
            raise ValueError("OASIS_COHORT_SIZE harus berada di rentang 12 sampai 24")
        return self

    @model_validator(mode="after")
    def validate_recovery_settings(self) -> "Settings":
        # A lease shorter than the task hard limit would let the reconciler
        # declare a healthy worker dead before Celery has stopped it.
        if self.analysis_lease_seconds <= self.celery_analysis_time_limit_seconds:
            raise ValueError(
                "ANALYSIS_LEASE_SECONDS harus lebih besar dari CELERY_ANALYSIS_TIME_LIMIT_SECONDS"
            )
        if self.analysis_max_attempts < 1:
            raise ValueError("ANALYSIS_MAX_ATTEMPTS minimal 1")
        return self

    @model_validator(mode="after")
    def validate_artifact_settings(self) -> "Settings":
        if self.object_storage_signed_url_seconds < 60:
            raise ValueError("OBJECT_STORAGE_SIGNED_URL_SECONDS minimal 60")
        if self.environment in {"staging", "production"} and self.object_storage_provider != "s3":
            raise ValueError("Object storage S3-compatible wajib dipakai di staging/production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
