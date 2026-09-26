"""진입·청산 규칙 연구(`app/scoring/entry_rules.py`)의 실행. 공시 v2가 받은 1분봉 파일만 읽고 DB에는 쓰지 않는다."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import partial
from pathlib import Path

from app.scoring import entry_rules as er

Rule = Callable[[Sequence[er.Bar]], float | None]


def load(path: Path) -> dict[date, list[list[er.Bar]]]:
    """`instrument_id:날짜` → 봉. 진입일별로 묶는다. 오류로 기록된 종목일은 뺀다."""
    raw = json.loads(path.read_text(encoding="utf-8"))["days"]
    out: dict[date, list[list[er.Bar]]] = defaultdict(list)
    for key, rec in raw.items():
        bars = rec.get("bars")
        if not bars:
            continue
        day = date.fromisoformat(key.split(":", 1)[1])
        out[day].append([(str(b[0]), *(float(x) for x in b[1:5])) for b in bars])  # type: ignore[misc]
    return dict(sorted(out.items()))


@dataclass(slots=True)
class EntryResult:
    name_days: int = 0
    with_open: int = 0
    verdicts: list[er.Verdict] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    bought: dict[str, float] = field(default_factory=dict)
    """규칙별 산 종목일 비율(09:00 봉이 있는 종목일 대비)."""
    pooled: dict[str, tuple[int, float, float, float]] = field(default_factory=dict)
    explore: dict[str, list[er.Verdict]] = field(default_factory=dict)


def _trimmed(values: Sequence[float]) -> float:
    v = sorted(values)
    k = int(len(v) * 0.1)
    return statistics.fmean(v[k : len(v) - k])


def run(path: Path) -> EntryResult:
    days = load(path)
    res = EntryResult()
    all_names = [bars for names in days.values() for bars in names]
    res.name_days = len(all_names)
    opened = [bars for bars in all_names if er.r0(bars) is not None]
    res.with_open = len(opened)
    res.verdicts = er.judge(days)
    res.candidates = er.candidates(res.verdicts)

    rules: dict[str, Rule] = {"R0": er.r0, "R1": er.r1, "R2": er.r2, "R3": er.r3}
    for name, rule in rules.items():
        vals = [v for bars in opened if (v := rule(bars)) is not None]
        res.bought[name] = len(vals) / len(opened) if opened else 0.0
        if vals:
            res.pooled[name] = (
                len(vals),
                statistics.fmean(vals),
                statistics.median(vals),
                _trimmed(vals),
            )

    # --- 탐색(판정 아님): 같은 판정 규칙을 바꾼 조건에 ---
    def judged(rule: Rule, label: str, sample: dict[date, list[list[er.Bar]]] = days) -> None:
        entry_days = sorted(days)
        res.explore[label] = [
            er.judge_one(f"E:{label}", "비용 뒤", er.day_values(sample, rule), entry_days),
        ]

    judged(er.r0, "R0 as is (baseline for the rows below)")
    judged(er.buy_at_0905, "unconditional buy at first bar >=09:05")
    for stop, take in ((0.01, 0.01), (0.02, 0.02), (0.03, 0.03)):
        judged(partial(er.r3, stop=stop, take=take), f"R3 -{stop:.0%}/+{take:.0%}")
    judged(partial(er.r1, confirm="0902", after="0903"), "R1 confirm 09:02 buy >=09:03")
    judged(partial(er.r1, confirm="0909", after="0910"), "R1 confirm 09:09 buy >=09:10")
    judged(partial(er.r1, entry_ticks=1), "R1 entry +1 tick")
    judged(partial(er.r2, entry_ticks=1), "R2 entry +1 tick")

    late = {d: [er.first_trade_as_open(b) for b in names] for d, names in days.items()}
    for name, rule in rules.items():
        judged(rule, f"{name} incl. no-09:00 name-days (first trade as open)", late)

    counts = sorted(len(er.hour(b)) for b in opened)
    mid = counts[len(counts) // 2] if counts else 0
    liquid = {d: [b for b in names if len(er.hour(b)) >= mid] for d, names in days.items()}
    thin = {d: [b for b in names if len(er.hour(b)) < mid] for d, names in days.items()}
    for name, rule in rules.items():
        judged(rule, f"{name} busier half (>= {mid} bars)", liquid)
        judged(rule, f"{name} quieter half (< {mid} bars)", thin)
    return res
