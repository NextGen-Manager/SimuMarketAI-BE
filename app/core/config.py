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


@lru_cache
def get_settings() -> Settings:
    return Settings()
