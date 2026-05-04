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
    llm_mode: Literal["mock", "providers"] = Field(default="mock", alias="LLM_MODE")

    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    groq_api_key: str | None = Field(default=None, alias="GROQ_API_KEY")
    cerebras_api_key: str | None = Field(default=None, alias="CEREBRAS_API_KEY")
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")

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
            "openrouter": bool(self.openrouter_api_key) and not self.free_only,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()

