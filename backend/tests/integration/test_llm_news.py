"""Asking a model about news, with a scripted model in its place.

What is pinned: a model's verdict is appended with its provenance and projects
to the mention table; a rule never overrules it; an UNSURE is not asked again;
a malformed answer writes nothing; every call is recorded; and the loop stops
on the call quota, on a refusal, and when the subscription is fuller than the
owner allowed. Readings are stored once per model and prompt version, and
say whether the news is financially material. A rule audit records the
model's opinion beside the rule's verdict and changes no verdict. The model's
default scope is tracked names and recent candidates.
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
from app.models.forward import CandidateSnapshot
from app.models.llm import LlmCall
from app.models.news import (
    Decider,
    HitDecision,
    MatchMethod,
    NewsItem,
    NewsQueryHit,
    NewsSentiment,
    NewsSource,
    RuleAudit,
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


def readings(*items: tuple[int, float], material: Any = True) -> dict[str, Any]:
    return {
        "readings": [
            {
                "id": i,
                "sentiment": s,
                "event_type": "SHAREHOLDER_RETURN",
                "intensity": 0.6,
                "confidence": 0.8,
                "evidence": "자사주 매입",
                "material": material,
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
        version = llm_service.SENTIMENT_PROMPT_VERSION
        assert all(r.model == MODEL and r.prompt_version == version for r in rows)
        assert all(r.material is True for r in rows)

        again = Script()
        assert read(world, again).asked == 0

    def test_a_new_prompt_reads_again_beside_the_old(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        read(world, Script(readings((1, 0.7), (2, -0.2))))
        v = llm_service.SENTIMENT_PROMPT_VERSION
        monkeypatch.setattr(llm_service, "SENTIMENT_PROMPT_VERSION", v + 1)
        read(world, Script(readings((1, 0.5), (2, -0.1))))
        assert sorted(r.prompt_version for r in stored_readings(world)) == [v, v, v + 1, v + 1]

    def test_a_value_outside_its_range_writes_nothing(self, world: World) -> None:
        report = read(world, Script(readings((1, 1.7), (2, 0.1))))
        assert (report.malformed_batches, stored_readings(world)) == (1, [])

    def test_only_confirmed_hits_are_read(self, world: World) -> None:
        provider = Script(readings((1, 0.1), (2, 0.1)))
        read(world, provider)
        assert f"{NAME} 스니펫 4" in provider.prompts[0]
        assert f"{NAME} 스니펫 0" not in provider.prompts[0]

    def test_a_reading_says_whether_the_news_is_material(self, world: World) -> None:
        read(world, Script(readings((1, 0.1), (2, 0.1), material=False)))
        assert [r.material for r in stored_readings(world)] == [False, False]

    @pytest.mark.parametrize("material", [None, "yes", 1])
    def test_a_reading_without_a_true_or_false_material_writes_nothing(
        self, world: World, material: Any
    ) -> None:
        report = read(world, Script(readings((1, 0.1), (2, 0.1), material=material)))
        assert (report.malformed_batches, stored_readings(world)) == (1, [])


def audit(world: World, provider: Any) -> llm_service.LlmRunReport:
    return llm_service.audit_rules(
        world.session,
        sample=100,
        instrument_ids=[world.instrument_id],
        provider=provider,
        guard=Guard(),
    )


def audits(world: World) -> list[RuleAudit]:
    world.session.expire_all()
    return list(
        world.session.execute(
            select(RuleAudit)
            .join(NewsQueryHit, NewsQueryHit.id == RuleAudit.query_hit_id)
            .where(NewsQueryHit.instrument_id == world.instrument_id)
        ).scalars()
    )


class TestRuleAudit:
    def test_only_the_rules_confirmations_are_asked(self, world: World) -> None:
        provider = Script(verdicts((1, "REJECTED"), (2, "CONFIRMED")))
        report = audit(world, provider)
        assert report.asked == 2
        assert f"{NAME} 스니펫 4" in provider.prompts[0]
        assert f"{NAME} 스니펫 0" not in provider.prompts[0]

    def test_answers_are_recorded_and_no_verdict_changes(self, world: World) -> None:
        audit(world, Script(verdicts((1, "REJECTED"), (2, "REJECTED"))))
        rows = audits(world)
        assert sorted(r.model_verdict for r in rows) == ["REJECTED", "REJECTED"]
        assert all(
            r.rule_decision is HitDecision.CONFIRMED
            and r.rule_reason == "strong:title_lead"
            and r.rule_version == RULE_VERSION
            and r.model == MODEL
            for r in rows
        )
        for item in world.items[4:]:
            verdict = latest(world, item)
            assert (verdict.decision, verdict.decided_by) == (HitDecision.CONFIRMED, Decider.RULE)

    def test_confirmations_under_an_older_rule_are_not_sampled(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The stored verdicts are this rule's; a newer rule has confirmed nothing yet.
        monkeypatch.setattr(llm_service, "NEWS_RULE_VERSION", RULE_VERSION + 1)
        assert audit(world, Script()).asked == 0

    def test_an_audited_hit_is_not_asked_again(self, world: World) -> None:
        audit(world, Script(verdicts((1, "CONFIRMED"), (2, "UNSURE"))))
        assert audit(world, Script()).asked == 0

    def test_a_malformed_answer_records_nothing(self, world: World) -> None:
        report = audit(world, Script(verdicts((1, "MAYBE"), (2, "CONFIRMED"))))
        assert (report.malformed_batches, audits(world)) == (1, [])

    def test_the_report_counts_by_name_shape_and_reason(self, world: World) -> None:
        audit(world, Script(verdicts((1, "REJECTED"), (2, "CONFIRMED"))))
        world.session.commit()
        tables = llm_service.audit_report(world.session, rule_version=RULE_VERSION)
        shape = tables["shape"]["hangul 3"]
        reason = tables["reason"]["strong"]
        # Other audits in the database may share these buckets; ours are in them.
        assert shape.audited >= 2 and reason.audited >= 2
        assert shape.confirmed >= 1 and shape.rejected >= 1


class TestScope:
    def test_focus_is_tracked_names_and_recent_candidates(self, world: World) -> None:
        s = world.session
        assert world.instrument_id not in llm_service.focus_ids(s)

        now = datetime.now(UTC)  # noqa: TID251 - fixture timestamps only
        snapshot = CandidateSnapshot(
            asof=now - timedelta(days=llm_service.FOCUS_CANDIDATE_DAYS + 1),
            rank=1,
            instrument_id=world.instrument_id,
            recent_mentions=3,
            baseline_mentions=0,
            recent_days=1.0,
            baseline_days=7.0,
            score=4.0,
            news_freshness="FRESH",
        )
        s.add(snapshot)
        s.flush()
        assert world.instrument_id not in llm_service.focus_ids(s)

        snapshot.asof = now - timedelta(hours=1)
        s.flush()
        assert world.instrument_id in llm_service.focus_ids(s)
        assert set(llm_service.tracked_ids(s)) <= set(llm_service.focus_ids(s))

    def test_focus_as_of_a_moment_reads_only_what_was_there(self, world: World) -> None:
        # 과거 아침을 다시 계산할 때 그 뒤에 나온 후보가 섞이면 안 된다.
        s = world.session
        now = datetime.now(UTC)  # noqa: TID251 - fixture timestamps only
        s.add(
            CandidateSnapshot(
                asof=now - timedelta(hours=1),
                taken_at=now - timedelta(hours=1),
                rank=1,
                instrument_id=world.instrument_id,
                recent_mentions=3,
                baseline_mentions=0,
                recent_days=1.0,
                baseline_days=7.0,
                score=4.0,
                news_freshness="FRESH",
            )
        )
        s.flush()
        assert world.instrument_id in llm_service.focus_ids_asof(s, now)
        assert world.instrument_id not in llm_service.focus_ids_asof(s, now - timedelta(hours=2))
        assert world.instrument_id not in llm_service.focus_ids_asof(s, now + timedelta(days=4))


def _hit_ids(world: World) -> list[int]:
    world.session.expire_all()
    return list(
        world.session.execute(
            select(NewsQueryHit.id)
            .where(NewsQueryHit.instrument_id == world.instrument_id)
            .order_by(NewsQueryHit.id)
        ).scalars()
    )


class TestMorningBudget:
    """장전 LLM: 보충이 새로 가져온 기사만, 판정·해석 합계 예산 안에서, 한 번에 한 실행만."""

    def test_only_pairs_seen_after_the_mark_are_asked(self, world: World) -> None:
        ids = _hit_ids(world)
        mark = ids[1]  # 앞의 두 쌍(기사 0, 1)은 보충 전에 이미 있던 것
        pending = news_repo.pending_for_model(
            world.session,
            limit=100,
            prompt_version=llm_service.RELEVANCE_PROMPT_VERSION,
            instrument_ids=[world.instrument_id],
            after_hit_id=mark,
        )
        assert {h.news_item_id for h in pending} == {world.items[2], world.items[3]}
        assert news_repo.last_hit_id(world.session) >= ids[-1]

    def test_judging_and_reading_share_one_budget(self, world: World) -> None:
        # 대기 4건, 확정 2건. 예산 3이면 판정이 3건을 쓰고 해석에는 남는 몫이 없다.
        provider = Script(verdicts((1, "UNSURE"), (2, "UNSURE"), (3, "UNSURE")))
        run = llm_service.run_within_budget(
            world.session,
            budget=3,
            instrument_ids=[world.instrument_id],
            provider=provider,
            guard=Guard(),
        )
        assert run.judged is not None and run.judged.asked == 3
        assert run.read is None
        assert run.items == 3 and run.stopped is None

    def test_what_judging_leaves_goes_to_reading(self, world: World) -> None:
        provider = Script(
            verdicts((1, "UNSURE"), (2, "UNSURE"), (3, "UNSURE"), (4, "UNSURE")),
            readings((1, 0.5)),
        )
        run = llm_service.run_within_budget(
            world.session,
            budget=5,
            instrument_ids=[world.instrument_id],
            provider=provider,
            guard=Guard(),
        )
        assert run.judged is not None and run.judged.asked == 4
        assert run.read is not None and run.read.asked == 1
        assert run.items == 5

    def test_another_run_holding_the_lock_stops_this_one_before_any_call(
        self, world: World, engine: object
    ) -> None:
        from app.db import advisory_lock

        factory = sessionmaker(bind=engine, future=True)  # type: ignore[arg-type]
        with factory() as other, advisory_lock(other, llm_service.LLM_RUN_LOCK) as held:
            assert held
            provider = Script()
            report = judge(world, provider)
        assert report.stopped == "another LLM run is in progress"
        assert provider.prompts == [] and calls(world) == []
