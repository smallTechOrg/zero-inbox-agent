"""Public surface of the LLM layer.

Import from here: ``from llm import LLMClient, ClassifierView, get_llm_client``.
NVIDIA NIM is the primary provider; Gemini is the automatic fallback; the input
type (:class:`ClassifierView`) makes email bodies unrepresentable in a prompt.
"""

from llm.client import (
    BatchClassification,
    ClassifierView,
    FallbackEvent,
    LLMClient,
    LLMError,
    LLMResult,
    LLMSchemaError,
    get_llm_client,
    make_fallback_provider,
    make_primary_provider,
    reset_llm_client,
)

__all__ = [
    "BatchClassification",
    "ClassifierView",
    "FallbackEvent",
    "LLMClient",
    "LLMError",
    "LLMResult",
    "LLMSchemaError",
    "get_llm_client",
    "make_fallback_provider",
    "make_primary_provider",
    "reset_llm_client",
]
