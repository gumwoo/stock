"""The subscription boundary: no path may quietly bill an API key."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.llm.provider import ClaudeAgentSdkProvider, LlmUnavailableError
from app.services import llm_service


def test_an_api_key_in_the_environment_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude Code prefers the key over the login; the call would be metered."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def never(*_: object, **__: object) -> None:
        raise AssertionError("the SDK was reached")

    monkeypatch.setattr(ClaudeAgentSdkProvider, "_complete", never)
    with pytest.raises(LlmUnavailableError, match="ANTHROPIC_API_KEY"):
        ClaudeAgentSdkProvider().complete(system="s", prompt="p", schema={}, model="m")


def test_no_provider_unless_one_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """Installing the package must not start spending the owner's usage."""
    monkeypatch.setattr(get_settings(), "llm_provider", "")
    with pytest.raises(LlmUnavailableError, match="LLM_PROVIDER"):
        llm_service.default_provider()
