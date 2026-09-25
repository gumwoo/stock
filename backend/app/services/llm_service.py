"""Asking a model about news: relevance of the undecided, and what the confirmed say.

Two jobs, one loop. Both take stored hits in batches, send each batch in one
call, validate the structured answer, write what it says, and record the call.
The loop stops, keeping what it already wrote, on the first of:

- the internal call quota (`claude_subscription`), reserved before each call;
- the subscription reporting its five-hour or seven-day window fuller than the
  configured share — those windows are the owner's own Claude usage too;
- a refusal from the subscription.

A malformed answer writes nothing for its batch and the loop moves on: the
hits stay as they were and the next run asks again.

**Relevance (rollout step 8).** A PENDING hit is one whose name appeared but
whose context did not settle whether the company is meant. The model answers
CONFIRMED, REJECTED or UNSURE, and the answer is appended to the verdict
history as an LLM verdict, with model, prompt version and its one-line reason.
UNSURE stays PENDING but is the model's, so the same prompt does not ask
again. A rule never overrules it.

**Reading (Phase 4-3).** A CONFIRMED hit is read for direction, event type,
intensity and confidence, with the words the reading rests on. One reading per
(article, company, model, prompt version), never replaced.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.naver_news import RULE_VERSION as NEWS_RULE_VERSION
from app.collectors.quota import QuotaExhausted, QuotaGuard
from app.config import get_settings
from app.core.calendar import Market, MarketCalendar
from app.core.clock import ensure_utc, utc_now
from app.db import advisory_lock, session_scope
from app.llm.provider import (
    ClaudeAgentSdkProvider,
    LlmProvider,
    LlmRateLimitedError,
    LlmResult,
    LlmUnavailableError,
)
from app.models import Instrument
from app.models.forward import CandidateSnapshot
from app.models.news import Decider, HitDecision, NewsQueryHit, RuleAudit, SentimentEvent
from app.repositories import instrument_repo, llm_repo, news_repo, promotion_repo
from app.repositories.llm_repo import LlmCallRow, SentimentRow
from app.repositories.news_repo import OpenHit, QueryHitRow

QUOTA_GROUP = "claude_subscription"

# 2: version 1 confirmed 19 of 22 hits wrongly on the first real batch — the
# company's baseball and esports teams, the broadcaster named as a report's
# source, a hospital sharing the name, a listed affiliate. It rejected all 26
# of its rejections correctly.
RELEVANCE_PROMPT_VERSION = 2
# 2: asks whether the item matters to an investor at all (`material`). Articles
# about the company that are not news about its value — a charity drive, a
# sponsored team's game — were being read for sentiment like any other.
SENTIMENT_PROMPT_VERSION = 2

RELEVANCE_SYSTEM = """You judge Korean news search results for a stock research tool.
Each item names one company listed on the Korean exchange (KOSPI or KOSDAQ) and gives a
headline and snippet that contain the company's name.
The name alone proves nothing: many listed names are also ordinary words, places, people,
brands, or parts of longer names (원림 is also a garden, 남성 means male, 오로라 is also the
aurora, 전방 means front or downstream, 제우스 is also a player's nickname).
CONFIRMED only if the text reports something about that listed company itself: its business,
results, stock price, products or services, contracts, investments, disputes, management,
shareholders, or a filing. Mentioning the company in passing within a list of market movers
counts as CONFIRMED.
REJECTED in all of these cases, even though the company's name appears:
- the name is an ordinary word, a place, a person, or part of a longer name;
- a sports or esports team that carries the name (두산 베어스, 한화 이글스, LG 트윈스,
  NC 다이노스, 농심 레드포스, 디플러스 기아) and the text is about games or players;
- a broadcaster or newspaper that is only the source of the report or of a quote
  ("SBS 라디오에서", "YTN 기자입니다", "SBS Biz"), unless the text is news about that media company;
- a different organisation that shares the name (a hospital, school, fire station, terminal);
- a group affiliate that is a different company with its own name (LG유플러스 is not LG,
  한진부산컨테이너터미널 is not 한진).
UNSURE: the text is too short or ambiguous to tell.
Judge only from the given text. Give a short reason in English."""

RELEVANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["CONFIRMED", "REJECTED", "UNSURE"]},
                    "reason": {"type": "string", "maxLength": 120},
                },
                "required": ["id", "verdict", "reason"],
            },
        }
    },
    "required": ["verdicts"],
}

SENTIMENT_SYSTEM = """You read Korean news about a listed Korean company for a stock research tool.
For each item, judge only what the headline and snippet say about the named company:
- sentiment: from -1 (clearly bad for the company's value) to +1 (clearly good); 0 if neutral
  or if the item merely mentions the company.
- event_type: the kind of news, one of the allowed values.
- intensity: from 0 (trivial) to 1 (material enough to move the stock by itself).
- confidence: from 0 to 1, how sure you can be from this short text alone.
- evidence: the exact words from the text your reading rests on, at most 120 characters.
- material: true if an investor in the company would care — its business, results, stock,
  contracts, capital, disputes, management or shareholders; false if the item is about the
  company but not about its value (charity, sponsorships, sports teams, events, advertising,
  a passing mention in a list unrelated to the company's business).
Do not use outside knowledge about the company or the market."""

SENTIMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "readings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "sentiment": {"type": "number", "minimum": -1, "maximum": 1},
                    "event_type": {"type": "string", "enum": [e.value for e in SentimentEvent]},
                    "intensity": {"type": "number", "minimum": 0, "maximum": 1},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence": {"type": "string", "maxLength": 200},
                    "material": {"type": "boolean"},
                },
                "required": [
                    "id",
                    "sentiment",
                    "event_type",
                    "intensity",
                    "confidence",
                    "evidence",
                    "material",
                ],
            },
        }
    },
    "required": ["readings"],
}


@dataclass
class LlmRunReport:
    purpose: str
    asked: int = 0
    calls: int = 0
    written: int = 0
    malformed_batches: int = 0
    # Items the model's answer left out. Still open, so asked again next run.
    unanswered: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    stopped: str | None = None
    five_hour_utilization: float | None = None
    seven_day_utilization: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class MalformedAnswerError(Exception):
    """The model's structured answer did not fit the batch it was asked about."""


def default_provider() -> LlmProvider:
    settings = get_settings()
    if settings.llm_provider != "claude_agent_sdk":
        raise LlmUnavailableError(
            "no language model configured: set LLM_PROVIDER=claude_agent_sdk "
            "(it bills the Claude subscription, never an API key)"
        )
    return ClaudeAgentSdkProvider()


def _describe(session: Session, hits: Sequence[OpenHit]) -> dict[int, tuple[str, str]]:
    names: dict[int, tuple[str, str]] = {}
    for hit in hits:
        if hit.instrument_id not in names:
            instrument = instrument_repo.get_by_id(session, hit.instrument_id)
            symbol = instrument_repo.current_symbol(session, hit.instrument_id) or "-"
            names[hit.instrument_id] = (instrument.name if instrument else "?", symbol)
    return names


def _prompt(session: Session, hits: Sequence[OpenHit], header: str) -> str:
    names = _describe(session, hits)
    lines = [header, ""]
    for n, hit in enumerate(hits, 1):
        name, symbol = names[hit.instrument_id]
        lines.append(
            f"{n}. company: {name} ({symbol})\n   headline: {hit.title}\n   snippet: {hit.snippet}"
        )
    return "\n".join(lines)


# 모든 LLM 실행이 함께 잡는 잠금. 아침 07:00 해석과 08:30 보충 해석, 사람이
# CLI로 돌린 해석이 같은 구독을 동시에 쓰지 않게 한다. 한도는 호출마다 따로
# 예약되므로 겹쳐도 넘지는 않는다. 막는 것은 두 실행이 같은 대기 기사를 함께
# 집어 판정을 두 번 쓰는 일과, 시작할 때 한 번 보는 사용률 확인이 서로의
# 사용분을 못 보는 일이다.
LLM_RUN_LOCK = "llm_run"


def _run(session: Session, **kwargs: Any) -> LlmRunReport:
    """모델 호출 한 묶음을, 다른 LLM 실행이 없을 때만.

    판정·해석·감사가 모두 이 함수를 지나므로, 잠금을 여기서 잡으면 워커의
    아침 작업이든 사람이 CLI로 돌린 실행이든 같은 규칙을 따른다. 잠금은
    커밋하지 않는 별도 연결에 걸어, 결과를 쓰며 커밋해도 풀리지 않게 한다.
    다른 실행이 잡고 있으면 기다리지 않고 멈췄다고 보고한다.
    """
    report = LlmRunReport(purpose=kwargs["purpose"])
    if not kwargs["hits"]:
        return report
    with session_scope() as lock_session, advisory_lock(lock_session, LLM_RUN_LOCK) as held:
        if not held:
            report.stopped = "another LLM run is in progress"
            return report
        return _run_unlocked(session, **kwargs)


def _run_unlocked(
    session: Session,
    *,
    purpose: str,
    hits: Sequence[OpenHit],
    model: str,
    prompt_version: int,
    system: str,
    schema: dict[str, Any],
    header: str,
    apply: Callable[[Sequence[OpenHit], LlmResult, LlmRunReport], int],
    provider: LlmProvider,
    guard: Any,
) -> LlmRunReport:
    settings = get_settings()
    report = LlmRunReport(purpose=purpose)
    size = settings.llm_batch_size
    # The limits hold across runs, not only within one: a run started after an
    # earlier one stopped for a full window must not spend a call to find out.
    five, seven = llm_repo.recent_utilization(session, provider=provider.name)
    if hits and five is not None and five >= settings.llm_max_five_hour_utilization:
        report.stopped = f"five-hour usage at {five:.0%} by the last call; not starting"
        return report
    if hits and seven is not None and seven >= settings.llm_max_seven_day_utilization:
        report.stopped = f"seven-day usage at {seven:.0%} by the last call; not starting"
        return report
    for start in range(0, len(hits), size):
        batch = hits[start : start + size]
        try:
            guard.reserve(QUOTA_GROUP, purpose)
        except QuotaExhausted as refused:
            report.stopped = f"call quota: {refused}"
            break

        report.calls += 1
        report.asked += len(batch)
        try:
            result = provider.complete(
                system=system,
                prompt=_prompt(session, batch, header),
                schema=schema,
                model=model,
            )
        except LlmRateLimitedError as exc:
            _ledger(
                session,
                provider,
                purpose,
                model,
                prompt_version,
                len(batch),
                "RATE_LIMITED",
                error=str(exc),
            )
            report.stopped = f"subscription refused: {exc}"
            break
        except LlmUnavailableError as exc:
            _ledger(
                session,
                provider,
                purpose,
                model,
                prompt_version,
                len(batch),
                "UNAVAILABLE",
                error=str(exc),
            )
            report.stopped = f"unavailable: {exc}"
            break
        except Exception as exc:
            # Unanticipated, so not handled — but the call happened and was
            # reserved, and the ledger must say so before it unwinds.
            _ledger(
                session,
                provider,
                purpose,
                model,
                prompt_version,
                len(batch),
                "ERROR",
                error=repr(exc),
            )
            raise

        report.input_tokens += result.usage.input_tokens
        report.output_tokens += result.usage.output_tokens
        report.five_hour_utilization = result.usage.five_hour_utilization
        report.seven_day_utilization = result.usage.seven_day_utilization
        try:
            report.written += apply(batch, result, report)
            _ledger(session, provider, purpose, model, prompt_version, len(batch), "OK", result)
        except MalformedAnswerError as exc:
            session.rollback()
            report.malformed_batches += 1
            _ledger(
                session,
                provider,
                purpose,
                model,
                prompt_version,
                len(batch),
                "MALFORMED",
                result,
                str(exc),
            )

        five, seven = result.usage.five_hour_utilization, result.usage.seven_day_utilization
        if five is not None and five >= settings.llm_max_five_hour_utilization:
            report.stopped = f"five-hour usage at {five:.0%}"
            break
        if seven is not None and seven >= settings.llm_max_seven_day_utilization:
            report.stopped = f"seven-day usage at {seven:.0%}"
            break
    return report


def _ledger(
    session: Session,
    provider: LlmProvider,
    purpose: str,
    model: str,
    prompt_version: int,
    items: int,
    status: str,
    result: LlmResult | None = None,
    error: str | None = None,
) -> None:
    """Record one call and commit it with whatever its batch wrote."""
    usage = result.usage if result is not None else None
    llm_repo.record_call(
        session,
        LlmCallRow(
            purpose=purpose,
            provider=provider.name,
            model=model,
            prompt_version=prompt_version,
            items=items,
            status=status,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            notional_cost_usd=usage.notional_cost_usd if usage else None,
            five_hour_utilization=usage.five_hour_utilization if usage else None,
            seven_day_utilization=usage.seven_day_utilization if usage else None,
            error=error[:500] if error else None,
        ),
    )
    session.commit()


def _answers(result: LlmResult, key: str, batch_size: int) -> dict[int, dict[str, Any]]:
    """The structured answer keyed by item number, or MalformedAnswerError."""
    payload = result.structured
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise MalformedAnswerError(f"no {key!r} list in the answer")
    out: dict[int, dict[str, Any]] = {}
    for entry in payload[key]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), int):
            raise MalformedAnswerError(f"an entry without an integer id: {entry!r}"[:200])
        n = entry["id"]
        if not 1 <= n <= batch_size or n in out:
            raise MalformedAnswerError(f"id {n} is outside the batch or repeated")
        out[n] = entry
    return out


_VERDICTS = {
    "CONFIRMED": HitDecision.CONFIRMED,
    "REJECTED": HitDecision.REJECTED,
    "UNSURE": HitDecision.PENDING,
}


def judge_pending(
    session: Session,
    *,
    limit: int,
    instrument_ids: Sequence[int] | None = None,
    provider: LlmProvider | None = None,
    guard: Any = None,
    after_hit_id: int | None = None,
) -> LlmRunReport:
    """Ask the model about PENDING hits, newest first, and append its verdicts."""
    settings = get_settings()
    model = settings.relevance_llm_model
    hits = news_repo.pending_for_model(
        session,
        limit=limit,
        prompt_version=RELEVANCE_PROMPT_VERSION,
        instrument_ids=instrument_ids,
        after_hit_id=after_hit_id,
    )

    def apply(batch: Sequence[OpenHit], result: LlmResult, report: LlmRunReport) -> int:
        answers = _answers(result, "verdicts", len(batch))
        rows: list[QueryHitRow] = []
        tally: dict[str, int] = {}
        for n, hit in enumerate(batch, 1):
            entry = answers.get(n)
            if entry is None:
                continue
            decision = _VERDICTS.get(str(entry.get("verdict")))
            if decision is None:
                raise MalformedAnswerError(f"verdict {entry.get('verdict')!r}")
            tally[decision.value] = tally.get(decision.value, 0) + 1
            rows.append(
                QueryHitRow(
                    news_item_id=hit.news_item_id,
                    instrument_id=hit.instrument_id,
                    matched_query=hit.matched_query,
                    decision=decision,
                    decision_reason="llm:" + str(entry["verdict"]).lower(),
                    match_method=hit.match_method,
                    snippet=hit.snippet,
                    rule_version=hit.rule_version,
                    decided_by=Decider.LLM,
                    model=result.model,
                    prompt_version=RELEVANCE_PROMPT_VERSION,
                    rationale=str(entry.get("reason") or "")[:500] or None,
                )
            )
        news_repo.record_hits(session, rows)
        report.unanswered += len(batch) - len(rows)
        # Only once the whole batch was valid: a malformed answer writes nothing.
        for key, n in tally.items():
            report.counts[key] = report.counts.get(key, 0) + n
        return len(rows)

    return _run(
        session,
        purpose="relevance",
        hits=hits,
        model=model,
        prompt_version=RELEVANCE_PROMPT_VERSION,
        system=RELEVANCE_SYSTEM,
        schema=RELEVANCE_SCHEMA,
        header="Judge each item.",
        apply=apply,
        provider=provider if provider is not None else default_provider(),
        guard=guard if guard is not None else QuotaGuard(),
    )


def read_confirmed(
    session: Session,
    *,
    limit: int,
    instrument_ids: Sequence[int] | None = None,
    provider: LlmProvider | None = None,
    guard: Any = None,
    after_hit_id: int | None = None,
) -> LlmRunReport:
    """Read CONFIRMED hits not yet read under this model and prompt, newest first."""
    settings = get_settings()
    model = settings.sentiment_llm_model
    hits = news_repo.confirmed_unread(
        session,
        model=model,
        prompt_version=SENTIMENT_PROMPT_VERSION,
        limit=limit,
        instrument_ids=instrument_ids,
        after_hit_id=after_hit_id,
    )

    def apply(batch: Sequence[OpenHit], result: LlmResult, report: LlmRunReport) -> int:
        answers = _answers(result, "readings", len(batch))
        rows: list[SentimentRow] = []
        tally: dict[str, int] = {}
        for n, hit in enumerate(batch, 1):
            entry = answers.get(n)
            if entry is None:
                continue
            try:
                event = SentimentEvent(str(entry["event_type"]))
                sentiment = float(entry["sentiment"])
                intensity = float(entry["intensity"])
                confidence = float(entry["confidence"])
            except (KeyError, TypeError, ValueError) as exc:
                raise MalformedAnswerError(f"item {n}: {exc}") from exc
            if not (-1 <= sentiment <= 1 and 0 <= intensity <= 1 and 0 <= confidence <= 1):
                raise MalformedAnswerError(f"item {n}: a value outside its range")
            evidence = str(entry.get("evidence") or "").strip()
            if not evidence:
                raise MalformedAnswerError(f"item {n}: no evidence")
            material = entry.get("material")
            if not isinstance(material, bool):
                raise MalformedAnswerError(f"item {n}: material is not true or false")
            tally[event.value] = tally.get(event.value, 0) + 1
            rows.append(
                SentimentRow(
                    news_item_id=hit.news_item_id,
                    instrument_id=hit.instrument_id,
                    model=result.model,
                    prompt_version=SENTIMENT_PROMPT_VERSION,
                    sentiment=sentiment,
                    event_type=event,
                    intensity=intensity,
                    confidence=confidence,
                    evidence=evidence[:500],
                    material=material,
                )
            )
        written = llm_repo.save_readings(session, rows)
        report.unanswered += len(batch) - len(rows)
        for key, n in tally.items():
            report.counts[key] = report.counts.get(key, 0) + n
        return written

    return _run(
        session,
        purpose="sentiment",
        hits=hits,
        model=model,
        prompt_version=SENTIMENT_PROMPT_VERSION,
        system=SENTIMENT_SYSTEM,
        schema=SENTIMENT_SCHEMA,
        header="Read each item.",
        apply=apply,
        provider=provider if provider is not None else default_provider(),
        guard=guard if guard is not None else QuotaGuard(),
    )


@dataclass(slots=True)
class BudgetedRun:
    """판정과 해석을 한 예산 안에서 돌린 결과."""

    budget: int
    judged: LlmRunReport | None = None
    read: LlmRunReport | None = None
    stopped: str | None = None

    @property
    def items(self) -> int:
        """모델에 보낸 기사 수 합계. 예산과 비교하는 값이 이것이다."""
        return sum(r.asked for r in (self.judged, self.read) if r is not None)


def run_within_budget(
    session: Session,
    *,
    budget: int,
    instrument_ids: Sequence[int] | None,
    after_hit_id: int | None = None,
    provider: LlmProvider | None = None,
    guard: Any = None,
) -> BudgetedRun:
    """판정 뒤 해석을, 둘을 합쳐 `budget`건 안에서. 다른 LLM 실행이 돌면 하지 않는다.

    예전 아침 작업은 판정 `limit`건과 해석 `limit`건을 따로 세서, 상한이
    100이라도 200건을 보낼 수 있었다. 여기서는 판정이 쓴 만큼 해석의 몫이
    줄어든다. 판정이 새로 확정한 기사를 같은 실행 안에서 바로 해석할 수 있게
    순서는 판정이 먼저다. 잠금은 각 호출 묶음(`_run`)이 잡는다.
    """
    out = BudgetedRun(budget=budget)
    if budget <= 0:
        out.stopped = "no budget"
        return out
    out.judged = judge_pending(
        session,
        limit=budget,
        instrument_ids=instrument_ids,
        provider=provider,
        guard=guard,
        after_hit_id=after_hit_id,
    )
    if out.judged.stopped:
        out.stopped = out.judged.stopped
        return out
    left = budget - out.judged.asked
    if left <= 0:
        return out
    out.read = read_confirmed(
        session,
        limit=left,
        instrument_ids=instrument_ids,
        provider=provider,
        guard=guard,
        after_hit_id=after_hit_id,
    )
    out.stopped = out.read.stopped
    return out


def tracked_ids(session: Session) -> list[int]:
    return [
        i.instrument_id
        for i in instrument_repo.list_active(session, asof=utc_now().date(), tracked=True)
    ]


# Candidates listed within this many days count as in focus.
FOCUS_CANDIDATE_DAYS = 3


def focus_ids(session: Session) -> list[int]:
    """Where the model's reading is used: tracked names and recent candidates.

    Tracked names carry an overlay into every signal; candidates are the names
    the forward record follows. The rest of the master is left to the rule —
    reading all of it would take the subscription's whole allowance every day.
    """
    recent = session.execute(
        select(CandidateSnapshot.instrument_id).where(
            CandidateSnapshot.asof > utc_now() - timedelta(days=FOCUS_CANDIDATE_DAYS)
        )
    ).scalars()
    return sorted(set(tracked_ids(session)) | set(recent))


def focus_ids_asof(session: Session, asof: datetime) -> list[int]:
    """`asof` 시점의 관심 종목: 그때 추적 중이던 종목과 그 전 3일의 후보.

    `focus_ids`는 지금 시각으로 읽는다. 과거 아침을 다시 계산하는 데 그것을
    쓰면 그 뒤에 추적된 종목과 그 뒤에 나온 후보가 섞인다. 여기서는 그때
    기록돼 있던 것만 본다: 후보는 `asof` 전 3일 안에 찍혔고 그때까지 저장된
    것, 추적은 `asof` 뒤의 승격을 뺀 것.
    """
    asof = ensure_utc(asof, field="asof")
    later = promotion_repo.promoted_after(session, asof)
    # 상장 여부는 서울 날짜로 본다. 07:00 KST는 UTC로 전날이다.
    day = MarketCalendar(Market.KR).local_today(asof)
    tracked = {
        i.instrument_id
        for i in instrument_repo.list_active(session, asof=day, market=Market.KR, tracked=True)
        if i.instrument_id not in later
    }
    recent = session.execute(
        select(CandidateSnapshot.instrument_id).where(
            CandidateSnapshot.asof > asof - timedelta(days=FOCUS_CANDIDATE_DAYS),
            CandidateSnapshot.asof <= asof,
            CandidateSnapshot.taken_at <= asof,
        )
    ).scalars()
    return sorted(tracked | set(recent))


def audit_rules(
    session: Session,
    *,
    sample: int,
    instrument_ids: Sequence[int] | None = None,
    provider: LlmProvider | None = None,
    guard: Any = None,
) -> LlmRunReport:
    """Put a random sample of the rule's confirmations to the model. Records, never re-judges.

    The answers go to `rule_audit` beside the rule's verdict. The verdict is
    left alone: the model's confirmations were wrong about one time in seven
    in the sample read by hand, so letting it overrule the rule on a sample
    would trade one error for another. What the audit gives is where the two
    disagree, which is what the next rule version is written from.
    """
    model = get_settings().relevance_llm_model
    targets = news_repo.rule_confirmed_sample(
        session,
        limit=sample,
        model=model,
        prompt_version=RELEVANCE_PROMPT_VERSION,
        # The rule as it stands: an older version's confirmations are not what is measured.
        rule_version=NEWS_RULE_VERSION,
        instrument_ids=instrument_ids,
    )
    by_pair = {(t.hit.news_item_id, t.hit.instrument_id): t for t in targets}

    def apply(batch: Sequence[OpenHit], result: LlmResult, report: LlmRunReport) -> int:
        answers = _answers(result, "verdicts", len(batch))
        rows: list[RuleAudit] = []
        for n, hit in enumerate(batch, 1):
            entry = answers.get(n)
            if entry is None:
                continue
            verdict = str(entry.get("verdict"))
            if verdict not in _VERDICTS:
                raise MalformedAnswerError(f"verdict {verdict!r}")
            target = by_pair[(hit.news_item_id, hit.instrument_id)]
            rows.append(
                RuleAudit(
                    query_hit_id=target.query_hit_id,
                    rule_version=hit.rule_version,
                    rule_decision=HitDecision.CONFIRMED,
                    rule_reason=target.decision_reason[:64],
                    model=result.model,
                    prompt_version=RELEVANCE_PROMPT_VERSION,
                    model_verdict=verdict,
                    rationale=str(entry.get("reason") or "")[:500] or None,
                )
            )
        session.add_all(rows)
        session.flush()
        report.unanswered += len(batch) - len(rows)
        for row in rows:
            report.counts[row.model_verdict] = report.counts.get(row.model_verdict, 0) + 1
        return len(rows)

    return _run(
        session,
        purpose="rule_audit",
        hits=[t.hit for t in targets],
        model=model,
        prompt_version=RELEVANCE_PROMPT_VERSION,
        system=RELEVANCE_SYSTEM,
        schema=RELEVANCE_SCHEMA,
        header="Judge each item.",
        apply=apply,
        provider=provider if provider is not None else default_provider(),
        guard=guard if guard is not None else QuotaGuard(),
    )


def name_shape(name: str) -> str:
    """The shape of a company name, which is where the rule's hard cases are."""
    flat = "".join(name.split())
    length = f"{len(flat)}" if len(flat) < 4 else "4+"
    if flat.isascii() and flat.isalnum():
        return f"latin {length}"
    if flat and all("가" <= ch <= "힣" for ch in flat):
        return f"hangul {length}"
    return "mixed"


@dataclass(frozen=True, slots=True)
class Agreement:
    audited: int
    confirmed: int
    rejected: int
    unsure: int

    @property
    def precision(self) -> float | None:
        """The share of the model's decided answers that agree with the rule."""
        decided = self.confirmed + self.rejected
        return self.confirmed / decided if decided else None


def audit_report(session: Session, *, rule_version: int) -> dict[str, dict[str, Agreement]]:
    """Agreement with the rule's confirmations, by name shape and by the rule's reason.

    Only the current model and prompt: a different judge's answers are a
    different measurement and are not added into this one.
    """
    stmt = (
        select(RuleAudit.model_verdict, RuleAudit.rule_reason, Instrument.name)
        .join(NewsQueryHit, NewsQueryHit.id == RuleAudit.query_hit_id)
        .join(Instrument, Instrument.instrument_id == NewsQueryHit.instrument_id)
        .where(
            RuleAudit.rule_version == rule_version,
            RuleAudit.model == get_settings().relevance_llm_model,
            RuleAudit.prompt_version == RELEVANCE_PROMPT_VERSION,
        )
    )
    tallies: dict[str, dict[str, list[int]]] = {"shape": {}, "reason": {}}
    for verdict, reason, name in session.execute(stmt).all():
        for kind, key in (("shape", name_shape(name)), ("reason", reason.split(":")[0])):
            t = tallies[kind].setdefault(key, [0, 0, 0, 0])
            t[0] += 1
            t[1] += verdict == "CONFIRMED"
            t[2] += verdict == "REJECTED"
            t[3] += verdict == "UNSURE"
    return {
        kind: {key: Agreement(*t) for key, t in sorted(rows.items())}
        for kind, rows in tallies.items()
    }
