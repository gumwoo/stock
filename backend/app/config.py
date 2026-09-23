"""Configuration and capability diagnostics.

Every external credential is optional. The system starts with none of them and
degrades honestly: a collector whose credentials are absent is recorded as
SKIPPED rather than FAILED, because a missing key and a broken API are different
events and only one of them warrants alarm.

`Settings.diagnostics()` reports what is on, what is off, and what to fill in to
turn each thing on. It is served at `/health/config`, so the answer to a question
like "why is there no sentiment data?" is one request away.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import TypedDict

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CapabilityState(StrEnum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


class DisabledCapability(TypedDict):
    """A capability that is off, and what would turn it on."""

    name: str
    set_to_enable: list[str]
    effect: str


class Diagnostics(TypedDict):
    """The `/health/config` payload."""

    app_env: str
    enabled: list[str]
    disabled: list[DisabledCapability]
    summary: str


@dataclass(frozen=True, slots=True)
class Capability:
    """One optional feature and why it is or is not available."""

    name: str
    state: CapabilityState
    requires: tuple[str, ...]
    effect_when_disabled: str

    @property
    def enabled(self) -> bool:
        return self.state is CapabilityState.ENABLED


class Settings(BaseSettings):
    """Runtime configuration, read from `.env` and the process environment."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- core -------------------------------------------------------------
    app_env: str = "local"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://stock:stock@localhost:5433/stock"

    # --- Toss Securities --------------------------------------------------
    toss_client_id: str = ""
    toss_client_secret: str = ""
    toss_account_seq: str = ""
    toss_api_base: str = "https://openapi.tossinvest.com"
    toss_ws_url: str = "wss://openapi-ws.tossinvest.com/ws/v1"

    # --- fundamentals -----------------------------------------------------
    sec_user_agent: str = ""
    dart_api_key: str = ""

    # --- news / social ----------------------------------------------------
    naver_client_id: str = ""
    naver_client_secret: str = ""
    threads_access_token: str = ""
    threads_user_id: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "stock-research/0.1"

    # --- language model ---------------------------------------------------
    # Claude through the Agent SDK, billed to the owner's Claude subscription.
    # `anthropic_api_key` is deliberately unused by it: a key in the
    # environment makes Claude Code bill the API instead, and the provider
    # refuses to run when one is set. See app/llm/provider.py.
    anthropic_api_key: str = ""
    # Off unless named. The subscription login cannot be checked from here,
    # and a capability that turns itself on because a package is installed
    # would spend the owner's Claude usage without being asked to.
    llm_provider: str = ""
    # Haiku: 8 of 8 on the relevance cases the rule got wrong, at a fraction
    # of the subscription usage a larger model takes.
    relevance_llm_model: str = "claude-haiku-4-5"
    sentiment_llm_model: str = "claude-haiku-4-5"
    llm_batch_size: int = Field(default=25, gt=0, le=60)
    # Stop before the owner's own Claude usage runs out. The subscription's
    # windows are shared with every other use of Claude on the account.
    llm_max_five_hour_utilization: float = Field(default=0.5, gt=0, le=1)
    llm_max_seven_day_utilization: float = Field(default=0.8, gt=0, le=1)

    # --- notifications ----------------------------------------------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    alert_email_to: str = ""
    alert_webhook_url: str = ""

    # --- rate limits (req/s), per the published Toss and SEC limits --------
    toss_rate_account: float = Field(default=1.0, description="Toss Account group")
    toss_rate_market_data: float = Field(default=15.0, description="Toss Market Data group")
    toss_rate_charts: float = Field(default=20.0, description="Toss Charts group")
    sec_rate: float = Field(default=8.0, description="SEC allows 10/s; stay under it")
    naver_rate: float = Field(default=5.0, gt=0.0, description="Naver allows ~10/s; hold at half")

    # --- quota budgets ----------------------------------------------------
    # Published caps, and the share of each we allow ourselves. Exposed as
    # settings for one practical reason: checking that the refusal path works
    # should cost two calls rather than twelve thousand.
    #
    # Named after the quota *group*, not an endpoint. Naver meters its whole
    # search family against one cap, so a `naver_news_daily_limit` would invite
    # a second copy of the same allowance the day a blog collector arrives.
    quota_budget_fraction: float = Field(
        default=0.5, gt=0.0, le=1.0, description="Share of each published cap we will spend"
    )
    naver_search_daily_limit: int = Field(
        default=25_000, gt=0, description="Naver search calls per day, shared across the family"
    )
    naver_search_internal_31d_limit: int = Field(
        default=775_000,
        gt=0,
        description="Our own 31-day ceiling. Not published by Naver; taken from the console",
    )
    naver_datalab_monthly_limit: int = Field(
        default=50_000, gt=0, description="API Hub DataLab search-trend calls per month"
    )
    dart_daily_limit: int = Field(
        default=20_000, gt=0, description="DART's usual daily threshold; varies by account"
    )

    # How deep to page per instrument per run. The binding constraint is not
    # quota but what the next increment will pay to score: three hundred
    # articles a name per run is already more than anyone wants graded. Raise
    # with care once a second Naver search collector exists — news alone at
    # two pages is 80% of the group budget in the worst case.
    naver_news_max_pages: int = Field(
        default=1,
        gt=0,
        description=(
            "Pages of 100 per instrument per run. One, because the worst case "
            "has to fit the budget rather than merely be refused by it: the "
            "Korean master holds 3,991 listed candidates (measured), and at "
            "two pages twice a day that is 15,964 calls against a budget of "
            "12,500. The guard would stop the second sweep partway through "
            "every day, and a sweep that never finishes never advances the "
            "watermark. Raising this needs the arithmetic redone, and adding "
            "another Naver search consumer needs it redone again — they share "
            "one published cap. Note that one page does not make PARTIAL rare: "
            "any company with more than 100 articles inside the window fills "
            "its page and the run reports truncation, so on a busy day every "
            "run is PARTIAL and the window sits at its lookback floor. That is "
            "bounded rather than growing, and the fix for it is a per-instrument "
            "cursor, not a larger page budget."
        ),
    )

    # ---------------------------------------------------------------------
    # capability resolution
    # ---------------------------------------------------------------------
    @property
    def toss_enabled(self) -> bool:
        return bool(self.toss_client_id and self.toss_client_secret)

    @property
    def sec_enabled(self) -> bool:
        # SEC needs no key, only an identifying User-Agent carrying a contact.
        return bool(self.sec_user_agent.strip())

    @property
    def dart_enabled(self) -> bool:
        return bool(self.dart_api_key)

    @property
    def naver_enabled(self) -> bool:
        return bool(self.naver_client_id and self.naver_client_secret)

    @property
    def threads_enabled(self) -> bool:
        return bool(self.threads_access_token and self.threads_user_id)

    @property
    def reddit_enabled(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    @property
    def llm_sentiment_enabled(self) -> bool:
        if self.llm_provider != "claude_agent_sdk":
            return False
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def email_alerts_enabled(self) -> bool:
        return bool(self.smtp_host and self.alert_email_to)

    @property
    def webhook_alerts_enabled(self) -> bool:
        return bool(self.alert_webhook_url)

    def capabilities(self) -> tuple[Capability, ...]:
        """Every optional capability, its state, and what enabling it needs."""

        def cap(name: str, on: bool, requires: tuple[str, ...], effect: str) -> Capability:
            return Capability(
                name=name,
                state=CapabilityState.ENABLED if on else CapabilityState.DISABLED,
                requires=requires,
                effect_when_disabled=effect,
            )

        return (
            cap(
                "toss_broker",
                self.toss_enabled,
                ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET"),
                "No live account sync or realtime quotes. Market data falls back "
                "to yfinance so the dashboard still renders. Note that Toss also "
                "requires the calling IP to be registered, or it returns HTTP 403.",
            ),
            cap(
                "sec_fundamentals",
                self.sec_enabled,
                ("SEC_USER_AGENT",),
                "US fundamentals unavailable, so the fundamental factor sits out "
                "for US instruments. SEC needs no API key, only a User-Agent "
                "containing a contact address.",
            ),
            cap(
                "dart_fundamentals",
                self.dart_enabled,
                ("DART_API_KEY",),
                "KR fundamentals unavailable; the fundamental factor sits out for "
                "Korean instruments.",
            ),
            cap(
                "naver_news",
                self.naver_enabled,
                ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"),
                "No Korean news or DataLab search-trend input to the sentiment factor.",
            ),
            cap(
                "threads_social",
                self.threads_enabled,
                ("THREADS_ACCESS_TOKEN", "THREADS_USER_ID"),
                "No Threads posts collected. Meta app review is required before "
                "this token can be issued.",
            ),
            cap(
                "reddit_social",
                self.reddit_enabled,
                ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"),
                "No Reddit posts collected, which is the main US retail sentiment source.",
            ),
            cap(
                "llm_sentiment",
                self.llm_sentiment_enabled,
                ("LLM_PROVIDER=claude_agent_sdk", "pip install claude-agent-sdk", "claude login"),
                "PENDING news hits stay undecided and articles go unread for "
                "sentiment. A supported fallback, not an outage: nothing else "
                "depends on it yet.",
            ),
            cap(
                "email_alerts",
                self.email_alerts_enabled,
                ("SMTP_HOST", "ALERT_EMAIL_TO"),
                "Alerts are written to the log only.",
            ),
            cap(
                "webhook_alerts",
                self.webhook_alerts_enabled,
                ("ALERT_WEBHOOK_URL",),
                "Alerts are written to the log only.",
            ),
        )

    def diagnostics(self) -> Diagnostics:
        """Machine- and human-readable summary of what is switched on."""
        caps = self.capabilities()
        enabled = [c.name for c in caps if c.enabled]
        disabled = [c for c in caps if not c.enabled]
        return {
            "app_env": self.app_env,
            "enabled": enabled,
            "disabled": [
                {
                    "name": c.name,
                    "set_to_enable": list(c.requires),
                    "effect": c.effect_when_disabled,
                }
                for c in disabled
            ],
            "summary": (
                f"{len(enabled)}/{len(caps)} optional capabilities enabled. "
                "Missing credentials disable only their own collector; everything "
                "else keeps running."
            ),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
