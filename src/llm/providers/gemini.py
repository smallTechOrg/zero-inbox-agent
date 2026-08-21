"""Gemini provider — the AUTOMATIC FALLBACK when NVIDIA NIM fails.

Gemini exposes an OpenAI-compatible chat-completions endpoint at
``https://generativelanguage.googleapis.com/v1beta/openai/``, so the shared
wrapper serves it unchanged. ``disable_thinking`` is a no-op here
(``gemini-2.5-flash-lite`` does not think by default), and a 400 on the
``json_schema`` response format is retried once without it — the client's own
schema validation is the real gate, never the endpoint's grammar.
"""

from __future__ import annotations

from llm.providers.openai_compat import OpenAICompatProvider

__all__ = ["GeminiProvider", "GEMINI_OPENAI_BASE_URL"]

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class GeminiProvider(OpenAICompatProvider):
    """OpenAI-compatible Gemini chat provider (fallback)."""

    provider_name = "gemini"
    supports_thinking_toggle = False

    def __init__(
        self,
        *,
        api_key: str,
        default_model: str,
        base_url: str = GEMINI_OPENAI_BASE_URL,
        timeout_s: float = 30.0,
        max_retries: int = 2,
        client=None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            default_model=default_model,
            timeout_s=timeout_s,
            max_retries=max_retries,
            client=client,
            api_key_env_var="AGENT_GEMINI_API_KEY",
        )
