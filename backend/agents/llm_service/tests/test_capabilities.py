"""Tests for llm_service.capabilities (provider-keyed structured-output capability check)."""

import pytest

from llm_service.capabilities import provider_supports_structured_output


def test_provider_supports_structured_output_ollama_true() -> None:
    assert provider_supports_structured_output("ollama") is True


def test_provider_supports_structured_output_bedrock_false() -> None:
    """Recorded as unsupported on Strategy Lab's Bedrock-via-strands integration path
    (which bypasses llm_service entirely), not a claim about Bedrock itself."""
    assert provider_supports_structured_output("bedrock") is False


@pytest.mark.parametrize("provider", ["claude", "dummy", "", "nonsense-provider"])
def test_provider_supports_structured_output_unknown_provider_false(provider: str) -> None:
    assert provider_supports_structured_output(provider) is False
