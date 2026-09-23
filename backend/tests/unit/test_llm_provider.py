"""The subscription boundary: no path may quietly bill an API key."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.llm.provider import ClaudeAgentSdkProvider, LlmUnavailableError
from app.services import llm_service


@pytest.mark.parametrize(
    "variable",
    [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ],
)
def test_a_metered_route_in_the_environment_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """Claude Code prefers any of these over the login; the call would be metered."""
    for other in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(variable, "1")

    def never(*_: object, **__: object) -> None:
        raise AssertionError("the SDK was reached")

    monkeypatch.setattr(ClaudeAgentSdkProvider, "_complete", never)
    with pytest.raises(LlmUnavailableError, match=variable):
        ClaudeAgentSdkProvider().complete(system="s", prompt="p", schema={}, model="m")


def test_no_provider_unless_one_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """Installing the package must not start spending the owner's usage."""
    monkeypatch.setattr(get_settings(), "llm_provider", "")
    with pytest.raises(LlmUnavailableError, match="LLM_PROVIDER"):
        llm_service.default_provider()


def test_paid_extra_usage_stops_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the plan's limit a subscription can run on paid overage. Refused."""
    sdk = pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk.types import SystemMessage

    from app.llm.provider import LlmRateLimitedError

    class RateLimitEvent:
        def __init__(self) -> None:
            self.rate_limit_info = type(
                "Info", (), {"status": "allowed", "raw": {"isUsingOverage": True}}
            )()

    async def fake_query(**_: object):  # type: ignore[no-untyped-def]
        yield SystemMessage(subtype="init", data={"apiKeySource": "none"})
        yield RateLimitEvent()

    monkeypatch.setattr(sdk, "query", fake_query)
    for variable in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(LlmRateLimitedError, match="extra usage"):
        ClaudeAgentSdkProvider().complete(system="s", prompt="p", schema={}, model="m")


def test_a_session_on_an_api_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk.types import SystemMessage

    async def fake_query(**_: object):  # type: ignore[no-untyped-def]
        yield SystemMessage(subtype="init", data={"apiKeySource": "ANTHROPIC_API_KEY"})

    monkeypatch.setattr(sdk, "query", fake_query)
    for variable in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(LlmUnavailableError, match="not the subscription"):
        ClaudeAgentSdkProvider().complete(system="s", prompt="p", schema={}, model="m")
