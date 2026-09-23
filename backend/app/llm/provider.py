"""The one door to a language model, and the Claude subscription behind it.

Everything that asks a model something — whether a PENDING news hit is about
its company, what an article says about it — goes through `LlmProvider`. One
implementation exists: `ClaudeAgentSdkProvider`, which runs the Claude Agent
SDK under the owner's Claude subscription rather than a metered API key. The
boundary is here because that arrangement is a policy, not a promise: Anthropic
announced moving Agent SDK usage onto separate credits for 2026-06-15 and
paused it the same day. When that changes, a second implementation replaces
this one and nothing else moves.

**No API key may be in play.** The SDK spawns the Claude Code binary with this
process's environment, and Claude Code prefers `ANTHROPIC_API_KEY` over the
subscription login when both exist — a key left in the environment would turn
every call into a metered one without a word. So the provider refuses to start
with a key set, and refuses any session whose init reports an API key as its
credential.

**It is a classifier, not an agent.** The SDK is Claude Code as a library, and
by default it loads the user's and the project's settings, `CLAUDE.md`, skills
and the account's connected MCP servers, with tools switched on, in whatever
directory it is started. Measured: with `tools=[]` alone the first session
still carried eight Claude Docs MCP tools from the account. Every one of those
is turned off here, and the process runs in an empty directory.

**The subscription is shared with the owner's own use of Claude.** Each
response carries the account's utilisation of its five-hour and seven-day
windows; the provider reports them so a caller can stop well before the owner
is locked out of their own tool.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Protocol

# Every way Claude Code can be pointed at something billed per call instead of
# at the subscription: an API key, a bearer token, or a cloud provider.
_METERED = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


class LlmUnavailableError(Exception):
    """The provider cannot be used as configured. Nothing was spent."""


class LlmRateLimitedError(Exception):
    """The subscription refused the call. Stop for now; try after the reset."""


@dataclass(frozen=True, slots=True)
class LlmUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    # What the provider would have charged on the API; informational only.
    notional_cost_usd: float | None = None
    five_hour_utilization: float | None = None
    seven_day_utilization: float | None = None


@dataclass(frozen=True, slots=True)
class LlmResult:
    structured: Any
    model: str
    usage: LlmUsage = field(default_factory=LlmUsage)


class LlmProvider(Protocol):
    name: str

    def complete(
        self, *, system: str, prompt: str, schema: dict[str, Any], model: str
    ) -> LlmResult: ...


class ClaudeAgentSdkProvider:
    """Claude through the Agent SDK, billed to the owner's subscription."""

    name = "claude_agent_sdk"

    def __init__(self, *, max_turns: int = 3) -> None:
        # Structured output takes a second turn: the model answers through a
        # tool, and the SDK closes the loop. Three leaves one retry.
        self._max_turns = max_turns

    def complete(
        self, *, system: str, prompt: str, schema: dict[str, Any], model: str
    ) -> LlmResult:
        for variable in _METERED:
            if os.environ.get(variable):
                raise LlmUnavailableError(
                    f"{variable} is set: the call would be billed outside the "
                    "subscription. Unset it to use the subscription."
                )
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError as exc:
            raise LlmUnavailableError("claude-agent-sdk is not installed") from exc
        return asyncio.run(self._complete(system=system, prompt=prompt, schema=schema, model=model))

    async def _complete(
        self, *, system: str, prompt: str, schema: dict[str, Any], model: str
    ) -> LlmResult:
        from claude_agent_sdk import ClaudeAgentOptions, query
        from claude_agent_sdk.types import ResultMessage, SystemMessage

        with tempfile.TemporaryDirectory(prefix="stock-llm-") as empty:
            options = ClaudeAgentOptions(
                model=model,
                system_prompt=system,
                setting_sources=[],
                tools=[],
                mcp_servers={},
                strict_mcp_config=True,
                max_turns=self._max_turns,
                cwd=empty,
                output_format={"type": "json_schema", "schema": schema},
            )
            five_hour = seven_day = None
            result: Any = None
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    source = (message.data or {}).get("apiKeySource")
                    if source not in (None, "none"):
                        raise LlmUnavailableError(
                            f"the session authenticated with {source}, not the subscription"
                        )
                elif type(message).__name__ == "RateLimitEvent":
                    info = getattr(message, "rate_limit_info", None)
                    raw = getattr(info, "raw", None) or {}
                    windows = raw.get("unifiedWindows") or {}
                    five_hour = (windows.get("five_hour") or {}).get("utilization", five_hour)
                    seven_day = (windows.get("seven_day") or {}).get("utilization", seven_day)
                    if getattr(info, "status", "allowed") not in ("allowed", "allowed_warning"):
                        raise LlmRateLimitedError(f"subscription limit: {raw}")
                    # Past the plan's limit a subscription can run on paid
                    # extra usage. That is exactly the bill this avoids.
                    if raw.get("isUsingOverage"):
                        raise LlmRateLimitedError("the subscription is into paid extra usage")
                elif isinstance(message, ResultMessage):
                    result = message

        if result is None:
            raise LlmUnavailableError("the session ended without a result")
        if result.is_error:
            if result.api_error_status == 429:
                raise LlmRateLimitedError("the API answered 429")
            raise LlmUnavailableError(f"the call failed: {result.subtype} {result.errors or ''}")
        usage = result.usage or {}
        return LlmResult(
            structured=result.structured_output,
            model=model,
            usage=LlmUsage(
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                notional_cost_usd=result.total_cost_usd,
                five_hour_utilization=_float(five_hour),
                seven_day_utilization=_float(seven_day),
            ),
        )


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None
