"""LLM Provider protocol and exceptions."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class ProviderError(Exception):
    """Base exception for provider errors."""


class ProviderTimeoutError(ProviderError):
    """Provider call timed out."""


class ProviderAuthError(ProviderError):
    """Authentication failure (invalid or missing API key)."""


@runtime_checkable
class LLMProvider(Protocol):
    def complete(self, messages: list[dict], tier: str = "cheap") -> str:
        """Send messages to the model at the given tier. Return response text.

        Args:
            messages: list of {"role": "user"|"system", "content": "..."} dicts.
            tier: "cheap", "standard", or "expensive".

        Returns:
            Model response text (caller parses as JSON where needed).
        """
        ...

    def estimate_cost(self, input_tokens: int, output_tokens: int, tier: str) -> float:
        """Estimate cost in USD. Return 0.0 if not supported."""
        ...
