"""NVIDIA NIM provider — the PRIMARY LLM provider.

NIM exposes an OpenAI-compatible chat-completions API at
``https://integrate.api.nvidia.com/v1``. First-class support for reasoning
models (Nemotron): ``disable_thinking=True`` turns chain-of-thought off via
``chat_template_kwargs`` so structured-output calls spend their token budget on
the answer, not on thought text.

Failover to Gemini on error/429/timeout is owned by ``llm.client`` — this class
only guarantees that a single call can never stall past the hard timeout.
"""

from __future__ import annotations

from llm.providers.openai_compat import OpenAICompatProvider

__all__ = ["NvidiaProvider"]


class NvidiaProvider(OpenAICompatProvider):
    """OpenAI-compatible NVIDIA NIM chat provider (primary)."""

    provider_name = "nvidia"
    supports_thinking_toggle = True

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        default_model: str,
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
            api_key_env_var="AGENT_NVIDIA_API_KEY",
        )
