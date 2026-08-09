"""Settings for the NVIDIA NIM provider — no network, no key required."""

import pytest

import config.settings as settings_module


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "AGENT_NVIDIA_API_KEY",
        "AGENT_NVIDIA_BASE_URL",
        "AGENT_NVIDIA_DEFAULT_MODEL",
        "AGENT_LLM_TIMEOUT_SECONDS",
        "AGENT_LLM_BATCH_SIZE",
        "PORT",
        "AGENT_PORT",
    ):
        monkeypatch.delenv(var, raising=False)
    settings_module._settings = None
    yield
    settings_module._settings = None


def _settings(**env):
    return settings_module.Settings(_env_file=None, **env)


def test_defaults_match_the_documented_nim_endpoint():
    s = _settings()
    assert s.nvidia_base_url == "https://integrate.api.nvidia.com/v1"
    assert s.nvidia_default_model  # a default model id is always present
    assert s.llm_max_retries >= 1
    assert 1 <= s.llm_batch_size <= 50
    assert s.port == 8001


def test_every_llm_setting_is_env_overridable(monkeypatch):
    monkeypatch.setenv("AGENT_NVIDIA_API_KEY", "nvapi-unit-test")
    monkeypatch.setenv("AGENT_NVIDIA_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("AGENT_NVIDIA_DEFAULT_MODEL", "vendor/other-model")
    monkeypatch.setenv("AGENT_LLM_TIMEOUT_SECONDS", "12.5")
    settings_module._settings = None
    s = settings_module.get_settings()
    assert s.nvidia_api_key == "nvapi-unit-test"
    assert s.nvidia_base_url == "https://example.test/v1"
    assert s.nvidia_default_model == "vendor/other-model"
    assert s.llm_timeout_seconds == 12.5
    assert s.has_nvidia_key is True


def test_port_reads_the_unprefixed_env_var(monkeypatch):
    monkeypatch.setenv("PORT", "9123")
    settings_module._settings = None
    assert settings_module.get_settings().port == 9123


def test_has_nvidia_key_is_presence_only_and_false_when_blank():
    assert _settings(nvidia_api_key="   ").has_nvidia_key is False


def test_batch_size_is_bounded_to_the_spec_range():
    with pytest.raises(Exception):
        _settings(llm_batch_size=500)


def test_removed_providers_are_gone():
    """v1 ships NVIDIA NIM only — the skeleton's Anthropic/Gemini slots are removed."""
    s = _settings()
    assert not hasattr(s, "anthropic_api_key")
    assert not hasattr(s, "gemini_api_key")
    with pytest.raises(ModuleNotFoundError):
        __import__("llm.providers.anthropic")
