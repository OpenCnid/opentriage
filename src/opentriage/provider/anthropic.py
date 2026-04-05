"""Anthropic LLM provider implementation."""

from __future__ import annotations

import os
import time
from typing import Any

from opentriage.provider.protocol import (
    ProviderAuthError,
    ProviderError,
    ProviderTimeoutError,
)

# Cost per 1M tokens (input, output) by model
_COST_TABLE: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-6": (15.0, 75.0),
}

_DEFAULT_MODELS: dict[str, str] = {
    "cheap": "claude-haiku-4-5-20251001",
    "standard": "claude-sonnet-4-6",
    "expensive": "claude-opus-4-6",
}


class AnthropicProvider:
    """Anthropic provider using the anthropic SDK."""

    def __init__(
        self,
        api_key_env: str = "ANTHROPIC_API_KEY",
        cheap_model: str = "",
        standard_model: str = "",
        expensive_model: str = "",
        base_url: str = "",
        timeout_seconds: int = 60,
        **kwargs: Any,
    ) -> None:
        try:
            import anthropic
        except ImportError:
            raise ProviderError(
                "Provider 'anthropic' requires the 'anthropic' package. "
                "Install: pip install opentriage[anthropic]"
            )

        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            raise ProviderAuthError(
                f"No API key found in environment variable '{api_key_env}'"
            )

        client_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": float(timeout_seconds),
        }
        if base_url:
            client_kwargs["base_url"] = base_url

        self._client = anthropic.Anthropic(**client_kwargs)
        self._models = {
            "cheap": cheap_model or _DEFAULT_MODELS["cheap"],
            "standard": standard_model or _DEFAULT_MODELS["standard"],
            "expensive": expensive_model or _DEFAULT_MODELS["expensive"],
        }
        self._timeout = timeout_seconds

    def complete(self, messages: list[dict], tier: str = "cheap") -> str:
        import anthropic

        model = self._models.get(tier, self._models["cheap"])
        system_msg = ""
        user_msgs = []
        for m in messages:
            if m.get("role") == "system":
                system_msg = m["content"]
            else:
                user_msgs.append({"role": m.get("role", "user"), "content": m["content"]})

        if not user_msgs:
            user_msgs = [{"role": "user", "content": "Respond."}]

        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": 2048,
                "messages": user_msgs,
            }
            if system_msg:
                kwargs["system"] = system_msg
            response = self._client.messages.create(**kwargs)
            return response.content[0].text
        except anthropic.AuthenticationError as e:
            raise ProviderAuthError(str(e)) from e
        except anthropic.APITimeoutError as e:
            raise ProviderTimeoutError(str(e)) from e
        except anthropic.APIError as e:
            raise ProviderError(str(e)) from e

    def estimate_cost(self, input_tokens: int, output_tokens: int, tier: str) -> float:
        model = self._models.get(tier, self._models["cheap"])
        costs = _COST_TABLE.get(model, (3.0, 15.0))
        return (input_tokens * costs[0] + output_tokens * costs[1]) / 1_000_000
