from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
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
    adaptive_router_enabled: bool = Field(default=True, alias="KEMY_ADAPTIVE_ROUTER_ENABLED")
    self_review_enabled: bool = Field(default=True, alias="KEMY_SELF_REVIEW_ENABLED")
    self_review_max_chars: int = Field(default=24000, alias="KEMY_SELF_REVIEW_MAX_CHARS")
    response_cache_enabled: bool = Field(default=True, alias="KEMY_RESPONSE_CACHE_ENABLED")
    response_cache_ttl_seconds: int = Field(default=3600, alias="KEMY_RESPONSE_CACHE_TTL_SECONDS")
    llm_request_timeout_seconds: int = Field(default=45, alias="KEMY_LLM_TIMEOUT_SECONDS")
    llm_retry_attempts: int = Field(default=2, alias="KEMY_LLM_RETRY_ATTEMPTS")
    daily_quota: int = Field(default=40, alias="KEMY_DAILY_QUOTA")
    auth_user: str = Field(default="admin", alias="KEMY_AUTH_USER")
    auth_password: str = Field(default="kemy-ai", alias="KEMY_AUTH_PASSWORD")
    auth_secret: str = Field(default="change-this-secret", alias="KEMY_AUTH_SECRET")

    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    groq_api_key: str | None = Field(default=None, alias="GROQ_API_KEY")
    cerebras_api_key: str | None = Field(default=None, alias="CEREBRAS_API_KEY")
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    openai_api_key: str | None = Field(default=None, validation_alias=AliasChoices("OPENAI_API_KEY", "CHATGPT_API_KEY"))
    openai_model: str = Field(default="gpt-4.1-mini", alias="OPENAI_MODEL")
    tavily_api_key: str | None = Field(default=None, alias="TAVILY_API_KEY")
    serper_api_key: str | None = Field(default=None, alias="SERPER_API_KEY")
    e2b_api_key: str | None = Field(default=None, alias="E2B_API_KEY")
    browserless_api_key: str | None = Field(default=None, alias="BROWSERLESS_API_KEY")
    browserless_url: str | None = Field(default=None, alias="BROWSERLESS_URL")
    pollinations_api_key: str | None = Field(default=None, alias="POLLINATIONS_API_KEY")
    pollinations_image_model: str = Field(default="flux", alias="POLLINATIONS_IMAGE_MODEL")
    pollinations_image_size: str = Field(default="1024x1024", alias="POLLINATIONS_IMAGE_SIZE")
    pollinations_image_quality: str = Field(default="medium", alias="POLLINATIONS_IMAGE_QUALITY")
    gotenberg_url: str | None = Field(default=None, alias="GOTENBERG_URL")
    gotenberg_timeout_seconds: int = Field(default=90, alias="GOTENBERG_TIMEOUT_SECONDS")
    supabase_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("KIMI_SUPABASE_URL", "SUPABASE_URL"),
    )
    supabase_anon_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("KIMI_SUPABASE_ANON_KEY", "SUPABASE_ANON_KEY"),
    )
    supabase_service_role_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("KIMI_SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_ROLE_KEY"),
    )

    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")
    github_client_id: str | None = Field(default=None, alias="GITHUB_CLIENT_ID")
    github_client_secret: str | None = Field(default=None, alias="GITHUB_CLIENT_SECRET")
    app_public_url: str = Field(default="http://localhost:8000", alias="APP_PUBLIC_URL")
    github_repo_url: str | None = Field(default=None, alias="GITHUB_REPO_URL")
    github_default_branch: str = Field(default="main", alias="GITHUB_DEFAULT_BRANCH")
    github_user_name: str = Field(default="Kemy AI", alias="GITHUB_USER_NAME")
    github_user_email: str = Field(default="kemy@ai.local", alias="GITHUB_USER_EMAIL")
    vercel_token: str | None = Field(default=None, alias="VERCEL_TOKEN")
    vercel_team_id: str | None = Field(default=None, alias="VERCEL_TEAM_ID")
    vercel_project_id: str | None = Field(default=None, alias="VERCEL_PROJECT_ID")
    site_base_domain: str | None = Field(default=None, alias="KEMY_SITE_BASE_DOMAIN")
    site_public_prefix: str = Field(default="/p", alias="KEMY_SITE_PUBLIC_PREFIX")

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
            "openai": bool(self.openai_api_key),
        }

    @property
    def configured_tools(self) -> dict[str, bool]:
        return {
            "tavily": bool(self.tavily_api_key),
            "serper": bool(self.serper_api_key),
            "e2b": bool(self.e2b_api_key),
            "browserless": bool(self.browserless_api_key or self.browserless_url),
            "pollinations": bool(self.pollinations_api_key),
            "gotenberg": bool(self.gotenberg_url),
            "vercel": bool(self.vercel_token),
        }

    @property
    def supabase_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
