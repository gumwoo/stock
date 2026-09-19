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

    # --- sentiment scoring ------------------------------------------------
    anthropic_api_key: str = ""
    sentiment_llm_model: str = "claude-sonnet-5"
    sentiment_prompt_version: str = "v1"

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
        return bool(self.anthropic_api_key)

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
                ("ANTHROPIC_API_KEY",),
                "Sentiment text is scored by the deterministic rule-based scorer "
                "instead. This is a supported fallback, not an outage.",
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
