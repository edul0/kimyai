from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Kemy AI"
    version: str = "3.0.0-cloud-free"
    environment: str = Field(default="development", alias="AMBIENTE")
    free_only: bool = Field(default=True, alias="FREE_ONLY")
    cors_origins: str = Field(default="*", alias="ORIGENS_CORS")
    redis_url: str | None = Field(default=None, alias="REDIS_URL")
    job_ttl_seconds: int = Field(default=86400, alias="JOB_TTL_SECONDS")
    session_ttl_seconds: int = Field(default=604800, alias="SESSION_TTL_SECONDS")
    max_prompt_chars: int = Field(default=12000, alias="MAX_PROMPT_CHARS")
    default_model: str = Field(default="groq/openai/gpt-oss-120b", alias="DEFAULT_MODEL")
    gemini_primary_model: str = Field(default="gemini-2.5-pro", alias="GEMINI_PRIMARY_MODEL")
    gemini_fallback_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_FALLBACK_MODEL")
    llm_mode: Literal["mock", "providers"] = Field(default="mock", alias="LLM_MODE")
    auth_user: str = Field(default="admin", alias="KEMY_AUTH_USER")
    auth_password: str = Field(default="kemy-ai", alias="KEMY_AUTH_PASSWORD")
    auth_secret: str = Field(default="change-this-secret", alias="KEMY_AUTH_SECRET")

    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    groq_api_key: str | None = Field(default=None, alias="GROQ_API_KEY")
    cerebras_api_key: str | None = Field(default=None, alias="CEREBRAS_API_KEY")
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    tavily_api_key: str | None = Field(default=None, alias="TAVILY_API_KEY")
    serper_api_key: str | None = Field(default=None, alias="SERPER_API_KEY")
    e2b_api_key: str | None = Field(default=None, alias="E2B_API_KEY")
    browserless_api_key: str | None = Field(default=None, alias="BROWSERLESS_API_KEY")
    browserless_url: str | None = Field(default=None, alias="BROWSERLESS_URL")
    supabase_url: str | None = Field(default=None, alias="SUPABASE_URL")
    supabase_anon_key: str | None = Field(default=None, alias="SUPABASE_ANON_KEY")
    supabase_service_role_key: str | None = Field(default=None, alias="SUPABASE_SERVICE_ROLE_KEY")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def configured_providers(self) -> dict[str, bool]:
        return {
            "gemini": bool(self.gemini_api_key),
            "groq": bool(self.groq_api_key),
            "cerebras": bool(self.cerebras_api_key),
            "openrouter": bool(self.openrouter_api_key),
        }

    @property
    def configured_tools(self) -> dict[str, bool]:
        return {
            "tavily": bool(self.tavily_api_key),
            "serper": bool(self.serper_api_key),
            "e2b": bool(self.e2b_api_key),
            "browserless": bool(self.browserless_api_key or self.browserless_url),
        }

    @property
    def supabase_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
