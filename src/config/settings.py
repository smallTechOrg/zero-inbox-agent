"""Application settings — every value comes from the environment / `.env`.

Env prefix is ``AGENT_`` (except ``PORT`` and the LangSmith ``LANGCHAIN_*`` vars,
which keep their conventional names). Secrets live only in `.env`; nothing here
carries a real default for a credential.
"""

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Database -------------------------------------------------------
    database_url: str = Field(default="sqlite:///./data/agent.db")

    # --- LLM provider: NVIDIA NIM (OpenAI-compatible) -------------------
    nvidia_api_key: str = Field(default="")
    nvidia_base_url: str = Field(default="https://integrate.api.nvidia.com/v1")
    # Never hardcoded at a call site — every call may override this per user.
    nvidia_default_model: str = Field(default="nvidia/nemotron-3-nano-30b-a3b")

    # --- LLM call behaviour ---------------------------------------------
    llm_timeout_seconds: float = Field(default=90.0)
    llm_max_retries: int = Field(default=3)
    llm_batch_size: int = Field(default=25, ge=1, le=50)

    # --- Google OAuth ----------------------------------------------------
    google_client_id: str = Field(default="")
    google_client_secret: str = Field(default="")
    google_redirect_uri: str = Field(default="http://localhost:8001/auth/google/callback")

    # --- App -------------------------------------------------------------
    secret_key: str = Field(default="")
    log_level: str = Field(default="INFO")
    # `PORT` is conventionally unprefixed; `AGENT_PORT` also works.
    port: int = Field(default=8001, validation_alias=AliasChoices("PORT", "AGENT_PORT"))

    @property
    def has_nvidia_key(self) -> bool:
        """Presence-only check — never log or expose the key itself."""
        return bool(self.nvidia_api_key.strip())


class RuntimeSettings(BaseSettings):
    """Settings that do not use the ``AGENT_`` prefix."""

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    langchain_tracing_v2: str = Field(default="")
    langchain_api_key: str = Field(default="")
    langchain_project: str = Field(default="zero-inbox-agent")


_settings: Settings | None = None
_runtime_settings: RuntimeSettings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def get_runtime_settings() -> RuntimeSettings:
    global _runtime_settings
    if _runtime_settings is None:
        _runtime_settings = RuntimeSettings()
    return _runtime_settings
