"""장 초반 동반주 흐름(가짜 KIS·세션·AI): 체결 없으면 쓰지 않음, 현재가 조회 상한, AI 묻기의 멈춤·기록·이름 확인."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.collectors import kis_open_movers as kom
from app.collectors.base import SkipCollection
from app.collectors.quota import QuotaExhausted
from app.scoring import companions as cp
from app.services import companion_service as cs
from app.services import llm_service

DAY = date(2026, 10, 13)
START = datetime(2026, 10, 13, 0, 2, tzinfo=UTC)


class FakeInsert:
    def __init__(self, model: Any) -> None:
        self.model = model
        self.rows: list[dict[str, Any]] = []

    def values(self, rows: list[dict[str, Any]]) -> FakeInsert:
        self.rows = rows
        return self

    def on_conflict_do_nothing(self) -> FakeInsert:
        return self


class FakeSession:
    def __init__(self) -> None:
        self.executed: list[FakeInsert] = []
        self.commits = 0

    def execute(self, stmt: FakeInsert) -> None:
        self.executed.append(stmt)

    def commit(self) -> None:
        self.commits += 1

    def rows(self, name: str) -> list[dict[str, Any]]:
        return [r for s in self.executed if s.model.__name__ == name for r in s.rows]


def rank_row(code: str, change: float, vol: int = 1000) -> dict[str, str]:
    return {"stck_shrn_iscd": code, "hts_kor_isnm": code, "prdy_ctrt": str(change), "stck_prpr": "1000", "acml_vol": str(vol)}  # fmt: skip


class FakeClient:
    def __init__(self, ranks: dict[str, list[dict[str, str]]]) -> None:
        self.ranks = ranks
        self.price_calls: list[str] = []

    def get(self, path: str, *, tr_id: str, params: dict[str, str], tr_cont: str = "") -> tuple[dict[str, Any], str]:  # fmt: skip
        if tr_id == kom.RANK_TR:
            return {"rt_cd": "0", "output": self.ranks[params["fid_input_iscd"]]}, ""
        self.price_calls.append(params["FID_INPUT_ISCD"])
        return {"rt_cd": "0", "output": {"prdy_ctrt": "5.0", "stck_prpr": "500"}}, ""

    def close(self) -> None:
        pass


@pytest.fixture
def master(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(session: Any, code: str, market: Any, *, asof: Any) -> Any:
        return SimpleNamespace(instrument_id=int(code))

    monkeypatch.setattr(kom.instrument_repo, "resolve_symbol", resolve)
    monkeypatch.setattr(kom.instrument_repo, "current_symbol", lambda s, i: f"{i:06d}")
    monkeypatch.setattr(kom, "pg_insert", FakeInsert)


def collector(
    client: FakeClient, related: dict[int, list[cp.Related]]
) -> kom.KisOpenMoverCollector:
    return kom.KisOpenMoverCollector(
        companions=lambda s, ids: {i: related.get(i, []) for i in ids},
        client=client,  # type: ignore[arg-type]
        now=lambda: START,
    )


def test_no_trades_writes_nothing(master: None) -> None:
    client = FakeClient(
        {"0001": [rank_row("001210", 9.0, 0)], "1001": [rank_row("000020", 8.0, 0)]}
    )
    session = FakeSession()
    with pytest.raises(SkipCollection):
        collector(client, {})._collect(session, DAY, START)  # type: ignore[arg-type]
    assert session.executed == [] and session.commits == 0


def test_price_calls_stop_at_eight_and_unknown_changes_stay_empty(master: None) -> None:
    # 이끄는 종목 둘, 각자 순위 밖 동반 후보 6개 → 현재가는 8번만, 알림 행은 6+6.
    ranks = {
        "0001": [rank_row("001210", 9.0)],
        "1001": [rank_row("000020", 12.0), rank_row("002990", 3.0)],
    }
    related = {
        1210: [cp.Related(100 + k, ("NAME",)) for k in range(6)],
        20: [cp.Related(200 + k, ("AI",)) for k in range(6)] + [cp.Related(2990, ("NAME",))],
    }
    client = FakeClient(ranks)
    session = FakeSession()
    result = collector(client, related)._collect(session, DAY, START)  # type: ignore[arg-type]
    assert len(client.price_calls) == kom.MAX_PRICE_CALLS
    alerts = session.rows("CompanionAlert")
    assert len(alerts) == 12 and session.commits == 1
    assert len(session.rows("OpenMover")) == 3 and "2 leaders" in result.detail
    by = {(a["leader_id"], a["companion_id"]): a for a in alerts}
    # 순위에 있던 동반 후보(+3%)는 순위 값을 쓰고, 덜 오른 순이라 맨 앞이다.
    assert by[(20, 2990)]["companion_change"] == 3.0
    # 큰 이끄는 종목(+12%)이 현재가 6번을 먼저 쓰고, 다음 종목은 2번만 받아 4개가 비어 있다.
    assert sum(1 for a in alerts if a["companion_change"] is None) == 4


# --- AI 묻기 ---------------------------------------------------------------------------------------------------


class FakeProvider:
    name = "fake"

    def __init__(self, answers: list[Any], five: float = 0.1) -> None:
        self.answers = answers
        self.five = five
        self.calls = 0

    def complete(self, *, system: str, prompt: str, schema: Any, model: str) -> Any:
        self.calls += 1
        usage = SimpleNamespace(five_hour_utilization=self.five, seven_day_utilization=0.1)
        return SimpleNamespace(structured=self.answers.pop(0), usage=usage)


class Guard:
    def __init__(self, allow: int) -> None:
        self.allow = allow

    def reserve(self, group: str, endpoint: str) -> None:
        if self.allow <= 0:
            raise QuotaExhausted.__new__(QuotaExhausted)  # 생성자 인자 없이: 거절만 흉내
        self.allow -= 1


@pytest.fixture
def asking(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    log: dict[str, list[Any]] = {"asks": [], "ledger": []}
    names = {1: "금호전기", 2: "금호건설", 3: "동양고속", 4: "천일고속"}
    monkeypatch.setattr(cs, "_master", lambda s, d: names)
    monkeypatch.setattr(cs, "targets", lambda s, now, extra: [1, 3])
    monkeypatch.setattr(cs.llm_repo, "recent_utilization", lambda s, provider: (0.1, 0.1))

    def save(session: Any, now: Any, model: str, members: Any, answer: Any, status: str) -> int:
        log["asks"].append(status)
        return len(log["asks"])

    monkeypatch.setattr(cs, "_save_ask", save)
    monkeypatch.setattr(
        llm_service, "_ledger", lambda *a, **k: log["ledger"].append(a[6] if len(a) > 6 else k)
    )
    monkeypatch.setattr(cs, "pg_insert", FakeInsert)
    return log


def run(provider: FakeProvider, guard: Guard) -> tuple[dict[str, Any], FakeSession]:
    session = FakeSession()
    report = cs._ask(
        session,  # type: ignore[arg-type]
        extra=(),
        max_batches=6,
        provider=provider,  # type: ignore[arg-type]
        guard=guard,
        now=START,
    )
    return report, session


def test_names_only_count_when_they_match_the_master(asking: dict[str, list[Any]]) -> None:
    answer = {
        "items": [
            {"id": 1, "related": [{"name": "금호 건설", "reason": "같은 금호"}, {"name": "없는회사", "reason": "?"}]},
            {"id": 2, "related": [{"name": "천일고속", "reason": "고속버스"}, {"name": "동양고속", "reason": "자기"}]},
        ]
    }  # fmt: skip
    report, session = run(FakeProvider([answer]), Guard(5))
    rows = session.rows("CompanionRelation")
    assert {(r["instrument_id"], r["related_id"], r["in_batch"]) for r in rows} == {
        (1, 2, False),
        (3, 4, False),
    }
    assert report["unresolved"] == 1 and asking["asks"] == ["OK"] and asking["ledger"] == ["OK"]


def test_a_malformed_answer_is_recorded_and_writes_nothing(asking: dict[str, list[Any]]) -> None:
    report, session = run(FakeProvider([{"oops": 1}]), Guard(5))
    assert session.rows("CompanionRelation") == [] and asking["asks"] == ["MALFORMED"]
    assert asking["ledger"] == ["MALFORMED"] and report["calls"] == 1


def test_no_quota_means_no_call(asking: dict[str, list[Any]]) -> None:
    provider = FakeProvider([{"items": []}])
    report, _ = run(provider, Guard(0))
    assert provider.calls == 0 and "call quota" in report["stopped"] and asking["asks"] == []


def test_a_full_window_stops_before_asking(asking: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    monkeypatch.setattr(cs.llm_repo, "recent_utilization", lambda s, provider: (0.99, 0.1))
    provider = FakeProvider([{"items": []}])
    report, _ = run(provider, Guard(5))
    assert provider.calls == 0 and "five-hour" in report["stopped"]
