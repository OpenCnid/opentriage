"""Ollama (local model) LLM provider implementation."""

from __future__ import annotations

import json
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import URLError

from opentriage.provider.protocol import (
    ProviderError,
    ProviderTimeoutError,
)

_DEFAULT_MODELS: dict[str, str] = {
    "cheap": "llama3.1:8b",
    "standard": "llama3.1:70b",
    "expensive": "llama3.1:405b",
}


class OllamaProvider:
    """Ollama provider using HTTP API to local server."""

    def __init__(
        self,
        cheap_model: str = "",
        standard_model: str = "",
        expensive_model: str = "",
        base_url: str = "",
        timeout_seconds: int = 60,
        **kwargs: Any,
    ) -> None:
        self._base_url = (base_url or "http://localhost:11434").rstrip("/")
        self._models = {
            "cheap": cheap_model or _DEFAULT_MODELS["cheap"],
            "standard": standard_model or _DEFAULT_MODELS["standard"],
            "expensive": expensive_model or _DEFAULT_MODELS["expensive"],
        }
        self._timeout = timeout_seconds

    def complete(self, messages: list[dict], tier: str = "cheap") -> str:
        model = self._models.get(tier, self._models["cheap"])
        payload = json.dumps({
            "model": model,
            "messages": messages,
            "stream": False,
        }).encode()

        req = Request(
            f"{self._base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(req, timeout=self._timeout) as resp:
                body = json.loads(resp.read())
                return body.get("message", {}).get("content", "")
        except TimeoutError as e:
            raise ProviderTimeoutError(str(e)) from e
        except URLError as e:
            raise ProviderError(f"Ollama connection failed: {e}") from e

    def estimate_cost(self, input_tokens: int, output_tokens: int, tier: str) -> float:
        return 0.0  # Local models have no API cost
