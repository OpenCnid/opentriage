"""Tests for LLM provider protocol and implementations."""

from opentriage.provider.protocol import LLMProvider, ProviderError, ProviderAuthError
from tests.conftest import MockProvider


def test_mock_provider_is_llm_provider():
    p = MockProvider()
    assert isinstance(p, LLMProvider)


def test_mock_provider_complete():
    p = MockProvider({"cheap": '{"result": "ok"}'})
    result = p.complete([{"role": "user", "content": "test"}], tier="cheap")
    assert result == '{"result": "ok"}'
    assert len(p.calls) == 1
    assert p.calls[0][0] == "cheap"


def test_mock_provider_estimate_cost():
    p = MockProvider()
    cost = p.estimate_cost(1000, 500, "cheap")
    assert cost == 0.01


def test_ollama_provider_estimate_cost():
    from opentriage.provider.ollama import OllamaProvider
    p = OllamaProvider()
    assert p.estimate_cost(1000, 500, "cheap") == 0.0


def test_provider_errors():
    assert issubclass(ProviderAuthError, ProviderError)


def test_anthropic_provider_missing_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    try:
        from opentriage.provider.anthropic import AnthropicProvider
        AnthropicProvider(api_key_env="ANTHROPIC_API_KEY")
        assert False, "Should have raised"
    except (ProviderAuthError, ProviderError):
        pass
    except ImportError:
        pass  # anthropic package not installed


def test_openai_provider_missing_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        from opentriage.provider.openai import OpenAIProvider
        OpenAIProvider(api_key_env="OPENAI_API_KEY")
        assert False, "Should have raised"
    except (ProviderAuthError, ProviderError):
        pass
    except ImportError:
        pass  # openai package not installed
