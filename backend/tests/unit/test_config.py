"""Capability diagnostics.

The requirement these tests protect: the system must start and stay useful with
no credentials at all, and it must be able to say which value to fill in next.
A config layer that raises on a missing key would make the first run a wall.
"""

from __future__ import annotations

from app.config import CapabilityState, Settings


def bare() -> Settings:
    """Settings with nothing configured, ignoring any local .env."""
    return Settings(_env_file=None)  # type: ignore[call-arg]


class TestStartsWithoutCredentials:
    def test_construction_does_not_raise(self) -> None:
        assert bare().app_env == "local"

    def test_every_capability_is_off_but_reported(self) -> None:
        caps = bare().capabilities()
        assert caps, "capabilities must be enumerable even when all are off"
        assert all(c.state is CapabilityState.DISABLED for c in caps)

    def test_each_disabled_capability_names_its_variables(self) -> None:
        """The dashboard's 'what do I fill in?' answer comes from here."""
        for cap in bare().capabilities():
            assert cap.requires, f"{cap.name} gives no way to enable it"
            assert cap.effect_when_disabled, f"{cap.name} does not say what is degraded"


class TestCapabilityResolution:
    def test_toss_needs_both_halves_of_the_credential(self) -> None:
        assert not Settings(_env_file=None, toss_client_id="abc").toss_enabled  # type: ignore[call-arg]
        assert Settings(  # type: ignore[call-arg]
            _env_file=None, toss_client_id="abc", toss_client_secret="xyz"
        ).toss_enabled

    def test_sec_needs_only_a_user_agent(self) -> None:
        """SEC issues no API key; the User-Agent with a contact is the whole bar."""
        assert Settings(  # type: ignore[call-arg]
            _env_file=None, sec_user_agent="stock-research me@example.com"
        ).sec_enabled

    def test_whitespace_user_agent_does_not_count(self) -> None:
        assert not Settings(_env_file=None, sec_user_agent="   ").sec_enabled  # type: ignore[call-arg]

    def test_email_alerts_need_a_destination_not_just_a_server(self) -> None:
        assert not Settings(_env_file=None, smtp_host="smtp.example.com").email_alerts_enabled  # type: ignore[call-arg]


class TestDiagnostics:
    def test_reports_nothing_enabled_without_claiming_failure(self) -> None:
        diag = bare().diagnostics()
        assert diag["enabled"] == []
        assert "keeps running" in str(diag["summary"])

    def test_enabling_one_capability_moves_it(self) -> None:
        settings = Settings(_env_file=None, dart_api_key="key")  # type: ignore[call-arg]
        diag = settings.diagnostics()

        assert "dart_fundamentals" in diag["enabled"]  # type: ignore[operator]
        disabled_names = [d["name"] for d in diag["disabled"]]  # type: ignore[index,union-attr]
        assert "dart_fundamentals" not in disabled_names

    def test_llm_absence_is_described_as_a_fallback_not_an_outage(self) -> None:
        """Rule-based scoring is a supported mode, and the wording should say so."""
        diag = bare().diagnostics()
        llm = next(
            d
            for d in diag["disabled"]  # type: ignore[union-attr]
            if d["name"] == "llm_sentiment"  # type: ignore[index]
        )
        assert "fallback" in str(llm["effect"])  # type: ignore[index]
