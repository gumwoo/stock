"""Asking a model about news, with a scripted model in its place.

What is pinned: a model's verdict is appended with its provenance and projects
to the mention table; a rule never overrules it; an UNSURE is not asked again;
a malformed answer writes nothing; every call is recorded; and the loop stops
on the call quota, on a refusal, and when the subscription is fuller than the
owner allowed. Readings are stored once per model and prompt version.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.naver_news import RULE_VERSION
from app.collectors.quota import QuotaExhausted
from app.config import get_settings
from app.core.calendar import Market
from app.core.quota import LimitSource, Quota
from app.llm.provider import LlmRateLimitedError, LlmResult, LlmUsage
from app.models import Base, Instrument, SymbolHistory
from app.models.llm import LlmCall
from app.models.news import (
    Decider,
    HitDecision,
    MatchMethod,
    NewsItem,
    NewsSentiment,
    NewsSource,
)
from app.repositories import news_repo
from app.repositories.news_repo import QueryHitRow
from app.services import llm_service

pytestmark = pytest.mark.integration

HOST = "llm-fixture.example.com"
NAME = "쀓엘엠"
MODEL = "fake-model"


@pytest.fixture(scope="module")
def engine() -> Iterator[object]:
    eng = create_engine(get_settings().database_url, future=True)
    try:
        with eng.connect():
            pass
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"database unavailable: {exc}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


class World:
    def __init__(self, session: Session, instrument_id: int, items: list[int]) -> None:
        self.session = session
        self.instrument_id = instrument_id
        self.items = items


@pytest.fixture
def world(engine: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """One company, four PENDING hits and two CONFIRMED ones, newest first by index."""
    settings = get_settings()
    monkeypatch.setattr(settings, "relevance_llm_model", MODEL)
    monkeypatch.setattr(settings, "sentiment_llm_model", MODEL)
    monkeypatch.setattr(settings, "llm_batch_size", 25)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)  # type: ignore[arg-type]
    with factory() as s:
        inst = Instrument(market=Market.KR, name=NAME, tracked=False)
        s.add(inst)
        s.flush()
        s.add(
            SymbolHistory(
                instrument_id=inst.instrument_id,
                symbol="990955",
                valid_from=datetime(2000, 1, 1, tzinfo=UTC).date(),
                source="SEED",
            )
        )
        now = datetime.now(UTC)  # noqa: TID251 - fixture timestamps only
        items: list[int] = []
        rows: list[QueryHitRow] = []
        for n in range(6):
            at = now - timedelta(hours=n + 1)
            item = NewsItem(
                source=NewsSource.NAVER_NEWS,
                url_hash=f"{HOST}-{n}".encode().hex()[:64].ljust(64, "0"),
                url=f"https://{HOST}/{n}",
                title=f"{NAME} 기사 {n}",
                summary=None,
                published_at=at,
                available_at=at,
            )
            s.add(item)
            s.flush()
            items.append(item.id)
            rows.append(
                QueryHitRow(
                    news_item_id=item.id,
                    instrument_id=inst.instrument_id,
                    matched_query=NAME,
                    decision=HitDecision.PENDING if n < 4 else HitDecision.CONFIRMED,
                    decision_reason="context:none" if n < 4 else "strong:title_lead",
                    match_method=MatchMethod.NAME,
                    snippet=f"{NAME} 스니펫 {n}",
                    rule_version=RULE_VERSION,
                    decided_by=Decider.RULE,
                )
            )
        news_repo.record_hits(s, rows)
        s.commit()
        try:
            yield World(s, inst.instrument_id, items)
        finally:
            s.rollback()
            s.execute(text("DELETE FROM llm_call WHERE model = :m"), {"m": MODEL})
            s.execute(text("DELETE FROM news_item WHERE url LIKE :h"), {"h": f"%{HOST}%"})
            s.execute(
                text("DELETE FROM symbol_history WHERE instrument_id = :i"),
                {"i": inst.instrument_id},
            )
            s.execute(
                text("DELETE FROM instrument WHERE instrument_id = :i"), {"i": inst.instrument_id}
            )
            s.commit()


class Script:
    """A provider that answers from a list, one answer per call."""

    name = "scripted"

    def __init__(self, *answers: Any, usage: LlmUsage | None = None) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.usage = usage or LlmUsage(input_tokens=10, output_tokens=5)

    def complete(
        self, *, system: str, prompt: str, schema: dict[str, Any], model: str
    ) -> LlmResult:
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return LlmResult(structured=answer, model=model, usage=self.usage)


class Guard:
    def __init__(self, allow: int = 100) -> None:
        self.allow = allow
        self.reserved = 0

    def reserve(self, group: str, endpoint: str, **_: Any) -> None:
        if self.reserved >= self.allow:
            quota = Quota(
                key="t",
                group=group,
                official_limit=2,
                window=timedelta(hours=1),
                limit_source=LimitSource.INTERNAL,
                note="test",
            )
            raise QuotaExhausted(quota=quota, spent=1, allowed=1, retry_after=None)
        self.reserved += 1


def verdicts(*pairs: tuple[int, str]) -> dict[str, Any]:
    return {"verdicts": [{"id": i, "verdict": v, "reason": f"because {v}"} for i, v in pairs]}


def latest(world: World, item: int) -> Any:
    world.session.expire_all()
    for d in news_repo.decisions_asof(
        world.session,
        datetime.now(UTC) + timedelta(seconds=5),  # noqa: TID251
        instrument_ids=[world.instrument_id],
    ):
        if d.news_item_id == item:
            return d
    return None


def judge(world: World, provider: Any, guard: Any = None) -> llm_service.LlmRunReport:
    return llm_service.judge_pending(
        world.session,
        limit=100,
        instrument_ids=[world.instrument_id],
        provider=provider,
        guard=guard or Guard(),
    )


def calls(world: World) -> list[LlmCall]:
    world.session.expire_all()
    return list(world.session.execute(select(LlmCall).where(LlmCall.model == MODEL)).scalars())


class TestRelevance:
    def test_verdicts_are_appended_with_their_provenance(self, world: World) -> None:
        provider = Script(
            verdicts((1, "CONFIRMED"), (2, "REJECTED"), (3, "UNSURE"), (4, "CONFIRMED"))
        )

        report = judge(world, provider)

        assert report.counts == {"CONFIRMED": 2, "REJECTED": 1, "PENDING": 1}
        first = latest(world, world.items[0])
        assert (first.decision, first.decided_by) == (HitDecision.CONFIRMED, Decider.LLM)
        row = world.session.execute(
            text(
                "SELECT d.model, d.prompt_version, d.rationale FROM news_relevance_decision d "
                "JOIN news_query_hit h ON h.id = d.query_hit_id "
                "WHERE h.news_item_id = :i AND d.decided_by = 'LLM'"
            ),
            {"i": world.items[0]},
        ).one()
        assert tuple(row) == (MODEL, llm_service.RELEVANCE_PROMPT_VERSION, "because CONFIRMED")
        assert latest(world, world.items[1]).decision is HitDecision.REJECTED
        assert latest(world, world.items[2]).decision is HitDecision.PENDING
        assert news_repo.projection_drift(world.session) == (0, 0)
        mentioned = set(
            world.session.execute(
                text("SELECT news_item_id FROM news_mention WHERE instrument_id = :i"),
                {"i": world.instrument_id},
            ).scalars()
        )
        assert {world.items[0], world.items[3]} <= mentioned
        assert world.items[1] not in mentioned
        (call,) = calls(world)
        assert (call.purpose, call.status, call.items) == ("relevance", "OK", 4)

    def test_the_names_and_text_reach_the_prompt(self, world: World) -> None:
        provider = Script(verdicts((1, "UNSURE")))
        judge(world, provider)
        assert f"{NAME} (990955)" in provider.prompts[0]
        assert f"{NAME} 스니펫 0" in provider.prompts[0]

    def test_a_rule_does_not_overrule_the_model(self, world: World) -> None:
        judge(world, Script(verdicts((1, "REJECTED"))))
        news_repo.record_hits(
            world.session,
            [
                QueryHitRow(
                    news_item_id=world.items[0],
                    instrument_id=world.instrument_id,
                    matched_query=NAME,
                    decision=HitDecision.CONFIRMED,
                    decision_reason="strong:title_lead",
                    match_method=MatchMethod.NAME,
                    snippet="another sweep",
                    rule_version=RULE_VERSION,
                    decided_by=Decider.RULE,
                )
            ],
        )
        world.session.commit()
        assert latest(world, world.items[0]).decided_by is Decider.LLM

    def test_an_unsure_answer_is_not_asked_again(self, world: World) -> None:
        judge(world, Script(verdicts((1, "UNSURE"), (2, "UNSURE"), (3, "UNSURE"), (4, "UNSURE"))))
        second = Script()
        report = judge(world, second)
        assert (report.asked, second.prompts) == (0, [])

    def test_a_verdict_from_an_older_prompt_is_asked_again(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A model verdict is final for its own prompt only.

        The first real prompt confirmed baseball teams; without this its
        confirmations would stand for ever.
        """
        judge(
            world, Script(verdicts((1, "CONFIRMED"), (2, "REJECTED"), (3, "UNSURE"), (4, "UNSURE")))
        )
        current = llm_service.RELEVANCE_PROMPT_VERSION
        monkeypatch.setattr(llm_service, "RELEVANCE_PROMPT_VERSION", current + 1)
        again = Script(verdicts((1, "REJECTED"), (2, "REJECTED"), (3, "REJECTED"), (4, "REJECTED")))

        report = judge(world, again)

        assert report.asked == 4
        assert latest(world, world.items[0]).decision is HitDecision.REJECTED
        assert news_repo.projection_drift(world.session) == (0, 0)

    def test_a_malformed_answer_writes_nothing(self, world: World) -> None:
        for answer in (
            {"verdicts": [{"id": 9, "verdict": "CONFIRMED", "reason": ""}]},
            {"verdicts": [{"id": 1, "verdict": "MAYBE", "reason": ""}]},
            {"nothing": []},
        ):
            report = judge(world, Script(answer))
            assert report.malformed_batches == 1
            assert latest(world, world.items[0]).decided_by is Decider.RULE
        assert [c.status for c in calls(world)] == ["MALFORMED"] * 3


class TestStopping:
    def test_the_call_quota_stops_before_the_call(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "llm_batch_size", 1)
        provider = Script(verdicts((1, "UNSURE")), verdicts((1, "UNSURE")))
        report = judge(world, provider, Guard(allow=1))
        assert report.calls == 1
        assert len(provider.prompts) == 1
        assert report.stopped is not None and "quota" in report.stopped

    def test_a_refusal_stops_and_is_recorded(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "llm_batch_size", 1)
        report = judge(world, Script(LlmRateLimitedError("429")))
        assert report.stopped is not None and "refused" in report.stopped
        assert [c.status for c in calls(world)] == ["RATE_LIMITED"]

    def test_a_full_subscription_stops_after_the_call_that_said_so(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The owner's own Claude usage shares the window."""
        monkeypatch.setattr(get_settings(), "llm_batch_size", 1)
        monkeypatch.setattr(get_settings(), "llm_max_seven_day_utilization", 0.8)
        busy = LlmUsage(seven_day_utilization=0.85)
        provider = Script(verdicts((1, "UNSURE")), verdicts((1, "UNSURE")), usage=busy)
        report = judge(world, provider)
        assert report.calls == 1
        assert report.stopped is not None and "seven-day" in report.stopped
        (call,) = calls(world)
        assert call.seven_day_utilization == pytest.approx(0.85)


class TestAcrossRuns:
    def test_a_run_after_a_full_window_does_not_spend_a_call_to_find_out(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "llm_batch_size", 1)
        monkeypatch.setattr(get_settings(), "llm_max_five_hour_utilization", 0.5)
        judge(world, Script(verdicts((1, "UNSURE")), usage=LlmUsage(five_hour_utilization=0.6)))

        second = Script()
        report = judge(world, second)

        assert second.prompts == []
        assert report.stopped is not None and "not starting" in report.stopped

    def test_another_providers_readings_do_not_stop_this_one(self, world: World) -> None:
        """Utilisation belongs to the account a provider bills; another's says nothing."""
        world.session.add(
            LlmCall(
                purpose="relevance",
                provider="some_other_provider",
                model=MODEL,
                prompt_version=1,
                items=1,
                status="OK",
                five_hour_utilization=0.99,
                seven_day_utilization=0.99,
            )
        )
        world.session.commit()

        provider = Script(verdicts((1, "UNSURE"), (2, "UNSURE"), (3, "UNSURE"), (4, "UNSURE")))
        report = judge(world, provider)

        assert len(provider.prompts) == 1
        assert report.stopped is None

    def test_an_unexpected_failure_is_recorded_before_it_unwinds(self, world: World) -> None:
        with pytest.raises(RuntimeError):
            judge(world, Script(RuntimeError("the CLI died")))
        assert [c.status for c in calls(world)] == ["ERROR"]

    def test_items_the_answer_left_out_are_counted_and_stay_open(self, world: World) -> None:
        report = judge(world, Script(verdicts((1, "REJECTED"))))
        assert (report.written, report.unanswered) == (1, 3)
        assert latest(world, world.items[1]).decided_by is Decider.RULE


def readings(*items: tuple[int, float]) -> dict[str, Any]:
    return {
        "readings": [
            {
                "id": i,
                "sentiment": s,
                "event_type": "SHAREHOLDER_RETURN",
                "intensity": 0.6,
                "confidence": 0.8,
                "evidence": "자사주 매입",
            }
            for i, s in items
        ]
    }


def read(world: World, provider: Any) -> llm_service.LlmRunReport:
    return llm_service.read_confirmed(
        world.session,
        limit=100,
        instrument_ids=[world.instrument_id],
        provider=provider,
        guard=Guard(),
    )


def stored_readings(world: World) -> list[NewsSentiment]:
    world.session.expire_all()
    return list(
        world.session.execute(
            select(NewsSentiment).where(NewsSentiment.instrument_id == world.instrument_id)
        ).scalars()
    )


class TestReading:
    def test_confirmed_articles_are_read_once(self, world: World) -> None:
        report = read(world, Script(readings((1, 0.7), (2, -0.2))))
        assert report.written == 2
        rows = stored_readings(world)
        assert {r.news_item_id for r in rows} == {world.items[4], world.items[5]}
        assert all(r.model == MODEL and r.prompt_version == 1 for r in rows)

        again = Script()
        assert read(world, again).asked == 0

    def test_a_new_prompt_reads_again_beside_the_old(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        read(world, Script(readings((1, 0.7), (2, -0.2))))
        monkeypatch.setattr(llm_service, "SENTIMENT_PROMPT_VERSION", 2)
        read(world, Script(readings((1, 0.5), (2, -0.1))))
        assert sorted(r.prompt_version for r in stored_readings(world)) == [1, 1, 2, 2]

    def test_a_value_outside_its_range_writes_nothing(self, world: World) -> None:
        report = read(world, Script(readings((1, 1.7), (2, 0.1))))
        assert (report.malformed_batches, stored_readings(world)) == (1, [])

    def test_only_confirmed_hits_are_read(self, world: World) -> None:
        provider = Script(readings((1, 0.1), (2, 0.1)))
        read(world, provider)
        assert f"{NAME} 스니펫 4" in provider.prompts[0]
        assert f"{NAME} 스니펫 0" not in provider.prompts[0]
