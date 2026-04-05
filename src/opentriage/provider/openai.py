"""OpenAI LLM provider implementation."""

from __future__ import annotations

import os
from typing import Any

from opentriage.provider.protocol import (
    ProviderAuthError,
    ProviderError,
    ProviderTimeoutError,
)

_COST_TABLE: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.0),
    "o3": (10.0, 40.0),
}

_DEFAULT_MODELS: dict[str, str] = {
    "cheap": "gpt-4o-mini",
    "standard": "gpt-4o",
    "expensive": "o3",
}


class OpenAIProvider:
    """OpenAI provider using the openai SDK."""

    def __init__(
        self,
        api_key_env: str = "OPENAI_API_KEY",
        cheap_model: str = "",
        standard_model: str = "",
        expensive_model: str = "",
        base_url: str = "",
        timeout_seconds: int = 60,
        **kwargs: Any,
    ) -> None:
        try:
            import openai
        except ImportError:
            raise ProviderError(
                "Provider 'openai' requires the 'openai' package. "
                "Install: pip install opentriage[openai]"
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

        self._client = openai.OpenAI(**client_kwargs)
        self._models = {
            "cheap": cheap_model or _DEFAULT_MODELS["cheap"],
            "standard": standard_model or _DEFAULT_MODELS["standard"],
            "expensive": expensive_model or _DEFAULT_MODELS["expensive"],
        }

    def complete(self, messages: list[dict], tier: str = "cheap") -> str:
        import openai

        model = self._models.get(tier, self._models["cheap"])
        try:
            response = self._client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                max_tokens=2048,
            )
            return response.choices[0].message.content or ""
        except openai.AuthenticationError as e:
            raise ProviderAuthError(str(e)) from e
        except openai.APITimeoutError as e:
            raise ProviderTimeoutError(str(e)) from e
        except openai.APIError as e:
            raise ProviderError(str(e)) from e

    def estimate_cost(self, input_tokens: int, output_tokens: int, tier: str) -> float:
        model = self._models.get(tier, self._models["cheap"])
        costs = _COST_TABLE.get(model, (2.50, 10.0))
        return (input_tokens * costs[0] + output_tokens * costs[1]) / 1_000_000
