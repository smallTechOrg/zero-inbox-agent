"""Slice db-and-domain — settings surface (src/config/settings.py)."""

import pytest

from config.settings import (
    FatalConfigError,
    MISSING_SECRET_KEY_MESSAGE,
    Settings,
    require_secret_key,
)


def _clean(monkeypatch) -> None:
    """Strip every AGENT_* var and .env so defaults are what is under test."""
    import os

    for key in list(os.environ):
        if key.startswith("AGENT_") or key in ("PORT",):
            monkeypatch.delenv(key, raising=False)


def _settings(monkeypatch, **env) -> Settings:
    _clean(monkeypatch)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


class TestDefaults:
    def test_spec_defaults(self, monkeypatch):
        s = _settings(monkeypatch)
        assert s.database_url == "sqlite:///./data/agent.db"
        assert s.nvidia_default_model == "nvidia/nemotron-3-nano-30b-a3b"
        assert s.gemini_fallback_model == "gemini-2.5-flash-lite"
        assert s.llm_timeout_seconds == 30.0
        assert s.chunk_limit == 50
        assert s.port == 8001
        assert s.google_redirect_uri.endswith("/auth/google/callback")

    def test_presence_checks_are_boolean_only(self, monkeypatch):
        s = _settings(monkeypatch)
        assert s.has_nvidia_key is False
        assert s.has_gemini_key is False
        assert s.has_google_oauth is False
        s2 = _settings(
            monkeypatch,
            AGENT_NVIDIA_API_KEY="k",
            AGENT_GEMINI_API_KEY="k",
            AGENT_GOOGLE_CLIENT_ID="id",
            AGENT_GOOGLE_CLIENT_SECRET="sec",
        )
        assert s2.has_nvidia_key and s2.has_gemini_key and s2.has_google_oauth


class TestEnvOverrides:
    def test_agent_prefix_is_honoured(self, monkeypatch):
        s = _settings(
            monkeypatch,
            AGENT_DATABASE_URL="sqlite:///./data/other.db",
            AGENT_LLM_TIMEOUT_SECONDS="12.5",
        )
        assert s.database_url == "sqlite:///./data/other.db"
        assert s.llm_timeout_seconds == 12.5

    def test_port_accepts_unprefixed_env(self, monkeypatch):
        assert _settings(monkeypatch, PORT="9002").port == 9002
        assert _settings(monkeypatch, AGENT_PORT="9003").port == 9003


class TestSecretKey:
    def test_missing_key_is_a_fatal_actionable_error(self, monkeypatch):
        import config.settings as m

        _clean(monkeypatch)
        monkeypatch.setattr(m, "_settings", Settings(_env_file=None))
        with pytest.raises(FatalConfigError) as excinfo:
            require_secret_key()
        message = str(excinfo.value)
        assert message == MISSING_SECRET_KEY_MESSAGE
        assert "AGENT_SECRET_KEY" in message and ".env" in message
        assert FatalConfigError.exit_code == 78

    def test_present_key_is_returned_never_defaulted(self, monkeypatch):
        import config.settings as m

        _clean(monkeypatch)
        monkeypatch.setenv("AGENT_SECRET_KEY", "unit-test-secret")
        monkeypatch.setattr(m, "_settings", Settings(_env_file=None, secret_key="unit-test-secret"))
        assert require_secret_key() == "unit-test-secret"
