"""밤사이 미국 업종 연구(`app/scoring/overnight_study.py`)의 데이터와 실행. DB에는 쓰지 않는다.

지난 공시 연구에서 연구용 일봉을 `candle`에 넣었더니 장전 사전 수집이 그 종목을 "최신"으로 보고 다시 받지 않는
식으로 운영이 바뀌었다. 이번에는 yfinance에서 받은 것을 모두 `data/overnight_study/` 파일에만 둔다(git 제외).
DB는 종목 마스터를 읽기만 한다.

- 미국: 지표 7개와 QQQ의 일봉 종가(auto_adjust=False).
- 한국 일봉(학습 갭, 유동성, 전날 종가): 2023-06-01부터, auto_adjust=False.
- 한국 60분봉 09:00 봉(평가일 진입가·첫 1시간): `period="max"`(2024-09-26부터). 09:00 봉만 남긴다.
- yfinance는 비공식이라 100종목마다 쉬어 가며 받고, 실패한 종목을 기록한다. 이미 받은 종목은 건너뛰므로 끊겨도
  이어서 받는다.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.collectors.yfinance_history import yf_ticker
from app.core.calendar import Market, MarketCalendar
from app.core.clock import utc_now
from app.repositories import instrument_repo
from app.scoring import overnight_study as study

logger = logging.getLogger(__name__)
KR = MarketCalendar(Market.KR)
SEOUL = ZoneInfo("Asia/Seoul")
NEW_YORK = ZoneInfo("America/New_York")
START = "2023-06-01"
END = "2026-09-26"
BATCH = 100
PAUSE = 3.0

US_FILE = "us_daily.json"
KR_DAILY_FILE = "kr_daily.json"
KR_HOUR_FILE = "kr_0900.json"
FAILURES_FILE = "failures.json"


# --- 수집 -------------------------------------------------------------------------------


def universe(session: Session) -> dict[str, int]:
    """yfinance 티커 → instrument_id. 지금 마스터의 KOSPI·KOSDAQ 종목(생존 편향)."""
    out: dict[str, int] = {}
    for inst in instrument_repo.list_active(
        session, asof=KR.local_today(utc_now()), market=Market.KR, tracked=None
    ):
        symbol = instrument_repo.current_symbol(session, inst.instrument_id)
        if symbol:
            out[yf_ticker(symbol, inst.market, inst.listing)] = inst.instrument_id
    return out


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def fetch_us(folder: Path) -> dict[str, Any]:
    import yfinance as yf

    data: dict[str, Any] = {}
    for t in (*study.INDICATORS, study.MARKET):
        frame = yf.Ticker(t).history(start=START, end=END, interval="1d", auto_adjust=False)
        data[t] = [[i.date().isoformat(), float(r["Close"])] for i, r in frame.iterrows()]
    _save(folder / US_FILE, data)
    return data


def _fetch_many(
    folder: Path, name: str, tickers: Sequence[str], fetch: Callable[[str], Any]
) -> dict[str, Any]:
    path = folder / name
    data = _load(path)
    failures = _load(folder / FAILURES_FILE)
    todo = [t for t in tickers if t not in data and t not in failures.get(name, {})]
    for n, t in enumerate(todo, 1):
        try:
            got = fetch(t)
        except Exception as exc:  # noqa: BLE001 - yfinance 오류는 종류가 일정하지 않다. 한 종목만 뺀다
            failures.setdefault(name, {})[t] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        if got:
            data[t] = got
        else:
            failures.setdefault(name, {})[t] = "no rows"
        if n % BATCH == 0:
            _save(path, data)
            _save(folder / FAILURES_FILE, failures)
            logger.info("overnight study %s: %d/%d", name, n, len(todo))
            time.sleep(PAUSE)
    _save(path, data)
    _save(folder / FAILURES_FILE, failures)
    return data


def fetch_kr(folder: Path, tickers: Sequence[str]) -> None:
    import yfinance as yf

    def daily(t: str) -> Any:
        frame = yf.Ticker(t).history(start=START, end=END, interval="1d", auto_adjust=False)
        return [
            [i.date().isoformat(), float(r["Open"]), float(r["Close"]), float(r["Volume"])]
            for i, r in frame.iterrows()
        ]

    def hour(t: str) -> Any:
        frame = yf.Ticker(t).history(period="max", interval="60m", auto_adjust=False)
        rows = []
        for i, r in frame.iterrows():
            local = i.tz_convert(SEOUL) if i.tzinfo else i
            if local.hour == 9 and local.minute == 0:
                rows.append([local.date().isoformat(), float(r["Open"]), float(r["Close"])])
        return rows

    _fetch_many(folder, KR_DAILY_FILE, tickers, daily)
    _fetch_many(folder, KR_HOUR_FILE, tickers, hour)


# --- 품질 게이트 -----------------------------------------------------------------------


@dataclass(slots=True)
class Gate:
    compared: int = 0
    open_ok: float = 0.0
    close_ok: float = 0.0
    internal_compared: int = 0
    internal_ok: float = 0.0

    @property
    def passed(self) -> bool:
        return (
            self.compared > 0
            and self.open_ok >= 0.95
            and self.close_ok >= 0.90
            and self.internal_compared > 0
            and self.internal_ok >= 0.90
        )


def quality_gate(folder: Path, kis_minutes: Path | None, ticker_of: dict[int, str]) -> Gate:
    """KIS 1분봉과 yfinance 09:00 봉, 그리고 yfinance 60분봉 시가와 일봉 시가의 일치."""
    hour = _load(folder / KR_HOUR_FILE)
    daily = _load(folder / KR_DAILY_FILE)
    gate = Gate()
    if kis_minutes is not None and kis_minutes.exists():
        opens = closes = 0
        for key, got in json.loads(kis_minutes.read_text(encoding="utf-8"))["days"].items():
            if "bars" not in got or not got["bars"]:
                continue
            iid, day = key.split(":")
            t = ticker_of.get(int(iid))
            yf_row = next((r for r in hour.get(t, []) if r[0] == day), None) if t else None
            if yf_row is None:
                continue
            bars = sorted(got["bars"])
            first = bars[0]
            last = [b for b in bars if b[0] < "1000"]
            if not last:
                continue
            gate.compared += 1
            opens += abs(yf_row[1] / first[1] - 1) <= 0.001
            closes += abs(yf_row[2] / last[-1][4] - 1) <= 0.003
        if gate.compared:
            gate.open_ok = opens / gate.compared
            gate.close_ok = closes / gate.compared
    ok = 0
    for t, rows in hour.items():
        opens_d = {r[0]: r[1] for r in daily.get(t, [])}
        for day, o, _ in rows:
            d_open = opens_d.get(day)
            if d_open:
                gate.internal_compared += 1
                ok += abs(o / d_open - 1) <= 0.001
    if gate.internal_compared:
        gate.internal_ok = ok / gate.internal_compared
    return gate


# --- 표본과 실행 -----------------------------------------------------------------------


def _us_series(rows: Sequence[Sequence[Any]]) -> list[tuple[datetime, float]]:
    out = []
    for d, close in rows:
        day = date.fromisoformat(d)
        instant = datetime(day.year, day.month, day.day, 16, 0, tzinfo=NEW_YORK).astimezone(UTC)
        out.append((instant, float(close)))
    return out


@dataclass(slots=True)
class Result:
    universe: int = 0
    liquid: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    gate: Gate | None = None
    betas: dict[str, float] = field(default_factory=dict)
    quality: list[study.Quality] = field(default_factory=list)
    lists: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    overlaps: dict[str, int] = field(default_factory=dict)
    signal_days: dict[str, int] = field(default_factory=dict)
    """판정 단위 예상 관측 수(미국 데이터만): 'study', 'holdout', 지표별."""
    observations: list[study.Observation] = field(default_factory=list)
    verdicts: list[study.Verdict] = field(default_factory=list)
    leave_one_out: dict[str, list[study.Verdict]] = field(default_factory=dict)
    halves: tuple[Counter[str], Counter[str]] = (Counter(), Counter())
    per_indicator: dict[str, dict[str, tuple[int, float | None]]] = field(default_factory=dict)
    o3_costs: dict[float, tuple[float | None, float | None]] = field(default_factory=dict)
    build_v3: bool = False


def run(session: Session, folder: Path, kis_minutes: Path | None) -> Result:
    res = Result()
    tickers = universe(session)
    ticker_of = {v: k for k, v in tickers.items()}
    res.universe = len(tickers)
    res.failures = {k: len(v) for k, v in _load(folder / FAILURES_FILE).items()}
    res.gate = quality_gate(folder, kis_minutes, ticker_of)
    if not res.gate.passed:
        logger.warning("overnight study: quality gate failed %s", res.gate)
        return res

    us = _load(folder / US_FILE)
    daily = _load(folder / KR_DAILY_FILE)
    hour = _load(folder / KR_HOUR_FILE)

    sessions = KR.sessions_between(date(2023, 6, 1), study.EVAL_LAST)
    prev_of = {d: sessions[i - 1] for i, d in enumerate(sessions) if i}
    days = [d for d in sessions if d in prev_of and d >= study.TRAIN_FIRST]
    kr_open = {d: KR.session_open(d) for d in days}
    kr_prev_close = {d: KR.session_close(prev_of[d]) for d in days}
    train = [d for d in days if d <= study.TRAIN_LAST]
    evald = [d for d in days if study.EVAL_FIRST <= d <= study.EVAL_LAST]

    r_mkt = study.align(days, kr_open, kr_prev_close, _us_series(us[study.MARKET]))
    e: dict[str, dict[date, float | None]] = {}
    sigma: dict[str, dict[date, float | None]] = {}
    for ind in study.INDICATORS:
        r = study.align(days, kr_open, kr_prev_close, _us_series(us[ind]))
        beta = study.fit_beta(r, r_mkt, train)
        res.betas[ind] = beta
        e[ind] = study.residuals(r, r_mkt, beta)
        sigma[ind] = study.trailing_sigma(e[ind])

    # 학습 구간: 일봉 갭(전날 종가 대비), 유동성.
    by_day: dict[str, dict[date, tuple[float, float, float]]] = {
        t: {date.fromisoformat(d): (o, c, v) for d, o, c, v in rows} for t, rows in daily.items()
    }
    liquid = []
    for t, series in by_day.items():
        tr = [
            (c * v) for d, (_, c, v) in series.items() if study.TRAIN_FIRST <= d <= study.TRAIN_LAST
        ]
        if len(tr) >= study.MIN_TRAIN_DAYS and statistics.fmean(tr) >= study.MIN_TRADED_VALUE:
            liquid.append(t)
    res.liquid = len(liquid)
    gap: dict[str, dict[date, float]] = {}
    for t in liquid:
        s = by_day[t]
        g = {}
        for d in train:
            today, before = s.get(d), s.get(prev_of[d])
            if today and before and before[1] > 0 and today[0] > 0:
                x = today[0] / before[1] - 1
                if abs(x) <= study.PRICE_LIMIT + study.LIMIT_SLACK:
                    g[d] = x
        gap[t] = g
    ew = {
        d: statistics.fmean(vals) for d in train if (vals := [g[d] for g in gap.values() if d in g])
    }
    gap_rel = {t: {d: x - ew[d] for d, x in g.items() if d in ew} for t, g in gap.items()}

    passed = []
    for ind in study.INDICATORS:
        q = study.quality(ind, e[ind], gap_rel, train)
        res.quality.append(q)
        if q.passed:
            passed.append(ind)
            res.lists[ind] = study.select(e[ind], gap_rel, train)
    for k, a in enumerate(passed):
        for b in passed[k + 1 :]:
            common = {n for n, _ in res.lists[a]} & {n for n, _ in res.lists[b]}
            if common:
                res.overlaps[f"{a}&{b}"] = len(common)

    # 예상 관측 수: 미국 데이터와 지표 구성만으로(한국 결과를 보기 전).
    held = study.holdout_days(evald)
    active = {d: [i for i in passed if study.is_big(e[i][d], sigma[i][d])] for d in evald}
    res.signal_days = {
        "study": sum(1 for d in evald if active[d] and d not in held),
        "holdout": sum(1 for d in evald if active[d] and d in held),
        **{i: sum(1 for d in evald if i in active[d]) for i in passed},
    }

    # 평가: 60분봉 09:00 봉 + 전날 일봉 종가.
    lists = {i: [n for n, _ in res.lists[i]] for i in passed}
    need = {n for names in lists.values() for n in names}
    window = [d for d in sessions if d >= study.EVAL_FIRST - timedelta(days=120)]
    window_days = [d for d in window if d in prev_of]
    bars: dict[str, dict[date, study.DayBar]] = {}
    for t in liquid:
        hs = {date.fromisoformat(d): (o, c) for d, o, c in hour.get(t, [])}
        s = by_day[t]
        out = {}
        for d in window_days:
            h, before = hs.get(d), s.get(prev_of[d])
            if h and before and before[1] > 0 and h[0] > 0:
                out[d] = study.DayBar(h[0] / before[1] - 1, h[1] / h[0] - 1)
        bars[t] = out
    calm = {
        n: {
            d: all(study.is_calm(e[i].get(d), sigma[i].get(d)) for i in passed if n in lists[i])
            for d in window_days
        }
        for n in need
    }
    for d in evald:
        if not active[d]:
            continue
        o = study.observe(d, active[d], lists, bars, window_days, calm)
        if o is not None:
            res.observations.append(o)

    res.verdicts = study.judge(res.observations, evald)
    res.leave_one_out = {i: study.judge(res.observations, evald, drop=i) for i in passed}
    study_obs = [o for o in sorted(res.observations, key=lambda o: o.day) if o.day not in held]
    half = len(study_obs) // 2
    res.halves = (
        Counter(i for o in study_obs[:half] for i in o.by_indicator),
        Counter(i for o in study_obs[half:] for i in o.by_indicator),
    )
    for i in passed:
        res.per_indicator[i] = {}
        for key, _ in study.QUESTIONS:
            vals = [
                o.by_indicator[i][key]
                for o in study_obs
                if i in o.by_indicator and key in o.by_indicator[i]
            ]
            res.per_indicator[i][key] = (len(vals), statistics.fmean(vals) if vals else None)
    for cost in (0.002, 0.003, 0.004):
        vals = [
            statistics.fmean(p["O3"] + study.COST - cost for p in o.by_indicator.values())
            for o in study_obs
        ]
        res.o3_costs[cost] = study.mean_t(vals)
    res.build_v3 = study.build_v3(res.verdicts)
    return res
