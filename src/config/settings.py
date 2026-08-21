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

    # --- LLM fallback: Gemini -------------------------------------------
    gemini_api_key: str = Field(default="")
    gemini_fallback_model: str = Field(default="gemini-2.5-flash-lite")

    # --- LLM call behaviour ---------------------------------------------
    # Hard per-call timeout — a stalled provider is impossible by construction
    # (spec/architecture.md; AGENT_LLM_TIMEOUT_SECONDS, default 30s).
    llm_timeout_seconds: float = Field(default=30.0)
    llm_max_retries: int = Field(default=3)
    llm_batch_size: int = Field(default=25, ge=1, le=50)

    # --- Triage run ------------------------------------------------------
    chunk_limit: int = Field(default=50, ge=1, le=200)

    # --- Google OAuth ----------------------------------------------------
    google_client_id: str = Field(default="")
    google_client_secret: str = Field(default="")
    google_redirect_uri: str = Field(default="http://localhost:8001/auth/google/callback")

    # --- App -------------------------------------------------------------
    # REQUIRED. Declared with an empty default so importing settings never raises a
    # raw pydantic ValidationError dump; the requirement is enforced by
    # `require_secret_key()` below, which fails with a message a human can act on.
    secret_key: str = Field(default="")
    log_level: str = Field(default="INFO")
    # `PORT` is conventionally unprefixed; `AGENT_PORT` also works.
    port: int = Field(default=8001, validation_alias=AliasChoices("PORT", "AGENT_PORT"))

    @property
    def has_nvidia_key(self) -> bool:
        """Presence-only check — never log or expose the key itself."""
        return bool(self.nvidia_api_key.strip())

    @property
    def has_gemini_key(self) -> bool:
        """Presence-only check — never log or expose the key itself."""
        return bool(self.gemini_api_key.strip())

    @property
    def has_google_oauth(self) -> bool:
        """Presence-only check for the OAuth client pair."""
        return bool(self.google_client_id.strip()) and bool(self.google_client_secret.strip())


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


# --- Required secrets ----------------------------------------------------

SECRET_KEY_ENV_VAR = "AGENT_SECRET_KEY"

#: The one line a human sees when the app refuses to start. It names the variable,
#: says where it goes, and gives the exact command that produces a value. It is a
#: single line on purpose: the server runs under a restart supervisor, so this text
#: is what a reader of a crash-looping log has to be able to act on at a glance.
MISSING_SECRET_KEY_MESSAGE = (
    "FATAL CONFIG ERROR: AGENT_SECRET_KEY is not set — Zero Inbox refuses to start. "
    "Add AGENT_SECRET_KEY=<value> to .env (see .env.example), generating the value with: "
    'python -c "import secrets; print(secrets.token_hex(32))". '
    "There is no fallback key: it encrypts stored OAuth refresh tokens and signs session "
    "cookies, and a published constant would make every session forgeable. "
    "Restarting will not fix this — set the variable, then start the server again."
)


class FatalConfigError(RuntimeError):
    """An unrecoverable configuration error. Restarting the process cannot fix it.

    ``exit_code`` is ``os.EX_CONFIG`` (78), the conventional "configuration error"
    status, so a supervisor can distinguish "misconfigured, do not respin" from an
    ordinary crash.
    """

    exit_code = 78  # os.EX_CONFIG


def fatal_config_banner(message: str = MISSING_SECRET_KEY_MESSAGE) -> str:
    """Render ``message`` so it is unmissable at the top of a supervised log."""
    rule = "=" * 78
    return f"\n{rule}\n{message}\n{rule}\n"


def require_secret_key() -> str:
    """Return ``AGENT_SECRET_KEY`` or raise :class:`FatalConfigError`.

    The insecure development fallback is deleted — there is no value this returns
    that was not supplied by the operator.
    """
    import os

    value = (get_settings().secret_key or "").strip()
    if not value:
        value = os.environ.get(SECRET_KEY_ENV_VAR, "").strip()
    if not value:
        raise FatalConfigError(MISSING_SECRET_KEY_MESSAGE)
    return value
