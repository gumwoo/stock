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
from app.scoring import nxt_study as nxt
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
NXT_FILE = "nxt_0800.json"
NXT_SAVE_EVERY = 25


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
    lost: dict[date, str] = field(default_factory=dict)
    """신호가 있었는데 관측하지 못한 평가일과 그 이유."""
    active: dict[date, list[str]] = field(default_factory=dict)
    """평가일 → 그날 큰 밤이었던 지표(품질 검사 통과분). 신호 없는 날은 빠진다."""
    liquid_names: list[str] = field(default_factory=list)
    prev_of: dict[date, date] = field(default_factory=dict)
    """평가일 → 그 전 한국 거래일(휴장일 보정 뒤)."""


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

    seen = {date.fromisoformat(r[0]) for f in (daily, hour) for rows in f.values() for r in rows}
    sessions = study.traded_sessions(KR.sessions_between(date(2023, 6, 1), study.EVAL_LAST), seen)
    prev_of = {d: sessions[i - 1] for i, d in enumerate(sessions) if i}
    days = [d for d in sessions if d in prev_of and d >= study.TRAIN_FIRST]
    kr_open = {d: KR.session_open(d) for d in days}
    kr_prev_close = {d: KR.session_close(prev_of[d]) for d in days}
    train = [d for d in days if d <= study.TRAIN_LAST]
    evald = [d for d in days if study.EVAL_FIRST <= d <= study.EVAL_LAST]
    res.prev_of = {d: prev_of[d] for d in evald}

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
    res.liquid_names = sorted(liquid)
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
    res.active = {d: a for d, a in active.items() if a}
    res.signal_days = {
        "study": sum(1 for d in evald if active[d] and d not in held),
        "holdout": sum(1 for d in evald if active[d] and d in held),
        **{i: sum(1 for d in evald if i in active[d]) for i in passed},
    }

    # 평가: 60분봉 09:00 봉 + 전날 일봉 종가. 전날 일봉 종가가 없으면 그 종목·날은 관측하지 않는다.
    # 60분봉 마지막 봉으로 메우지 않는다 - 계획에 없던 대용치를 결과를 본 뒤 넣으면 사후 선택이 된다.
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
            continue
        have = sum(1 for t in liquid if prev_of[d] in by_day[t])
        res.lost[d] = (
            f"전날({prev_of[d]}) 일봉 종가 {have}/{len(liquid)}종목뿐"
            if have < len(liquid) // 2
            else "목록 종목 모두 09:00 봉이나 전날 종가가 없거나 상한가 시가"
        )

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


# --- NXT 08:00 후속 연구 (`app/scoring/nxt_study.py`) ------------------------------------------


def _nxt_key(ticker: str, day: date) -> str:
    return f"{ticker}|{day.isoformat()}"


def nxt_pairs(res: Result, folder: Path) -> tuple[list[tuple[str, date]], dict[date, list[str]]]:
    """받을 (티커, 날짜): 신호 종목일과 비교군. 비교군은 그날 KRX 09:00 봉과 전날 종가가 있는 목록 밖 종목."""
    daily = _load(folder / KR_DAILY_FILE)
    hour = _load(folder / KR_HOUR_FILE)
    has_hour = {t: {r[0] for r in rows} for t, rows in hour.items()}
    has_daily = {t: {r[0] for r in rows} for t, rows in daily.items()}
    listed = {n for names in res.lists.values() for n, _ in names}
    pairs: set[tuple[str, date]] = set()
    controls: dict[date, list[str]] = {}
    for d, inds in sorted(res.active.items()):
        pairs |= {(n, d) for i in inds for n, _ in res.lists[i]}
        prev = res.prev_of[d].isoformat()
        cands = [
            t
            for t in res.liquid_names
            if d.isoformat() in has_hour.get(t, set()) and prev in has_daily.get(t, set())
        ]
        controls[d] = nxt.control_sample(d, cands, listed)
        pairs |= {(t, d) for t in controls[d]}
    return sorted(pairs, key=lambda p: (p[1], p[0])), controls


def _nxt_bars(body: dict[str, Any], ymd: str) -> list[list[Any]]:
    """응답에서 그날 08:00~09:00 봉만. 커서 09:00 앞 120개라 전날 애프터마켓 봉이 섞여 온다."""
    out = []
    for r in body.get("output2") or []:
        if not isinstance(r, dict) or str(r.get("stck_bsop_date")) != ymd:
            continue
        label = str(r.get("stck_cntg_hour") or "")[:4]
        if "0800" <= label <= "0900":
            out.append(
                [
                    label,
                    float(r["stck_oprc"]),
                    float(r["stck_hgpr"]),
                    float(r["stck_lwpr"]),
                    float(r["stck_prpr"]),
                    float(r.get("cntg_vol") or 0),
                ]
            )
    return sorted(out)


def fetch_nxt(folder: Path, pairs: Sequence[tuple[str, date]]) -> dict[str, Any]:
    """KIS NXT 1분봉. 종목일마다 1회, 날짜 오름차순(분봉 보관이 1년이라 오래된 날부터 사라진다). 그날 08:00~09:00
    봉을 원본대로 파일에 둔다. 이어받기."""
    from app.collectors.base import RateLimitedError, SkipCollection, UpstreamUnavailableError
    from app.collectors.kis import KisClient, KisError
    from app.collectors.kis_minute import STOCK_PATH, STOCK_TR, kis_run_lock

    path = folder / NXT_FILE
    data = _load(path)
    todo = [(t, d) for t, d in pairs if _nxt_key(t, d) not in data]
    calls = 0
    try:
        with KisClient() as client:
            for t, d in todo:
                ymd = d.strftime("%Y%m%d")
                rec: dict[str, Any] | None = None
                for _attempt in range(5):
                    try:
                        with kis_run_lock():
                            body, _ = client.get(
                                STOCK_PATH,
                                tr_id=STOCK_TR,
                                params={
                                    "FID_COND_MRKT_DIV_CODE": "NX",
                                    "FID_INPUT_ISCD": t.split(".")[0],
                                    "FID_INPUT_HOUR_1": "090000",
                                    "FID_INPUT_DATE_1": ymd,
                                    "FID_PW_DATA_INCU_YN": "Y",
                                    "FID_FAKE_TICK_INCU_YN": "N",
                                },
                            )
                        rec = {"bars": _nxt_bars(body, ymd)}
                        break
                    except SkipCollection:
                        time.sleep(60)  # 다른 KIS 실행이 돌고 있다. 두 배 속도로 부르지 않는다
                    except KisError as exc:
                        rec = {"error": f"{exc}"[:200]}  # 종목 하나의 거절(예: NXT 비상장)
                        break
                if rec is None:
                    raise SkipCollection("another KIS run kept the lock")
                calls += 1
                data[_nxt_key(t, d)] = rec
                if calls % NXT_SAVE_EVERY == 0:
                    _save(path, data)
                    logger.info("overnight nxt: %d/%d calls", calls, len(todo))
    except RateLimitedError:
        logger.error("overnight nxt: KIS refused the rate; stopping, progress kept")
        raise
    except UpstreamUnavailableError as exc:
        logger.error("overnight nxt: KIS unavailable (%s); progress kept", exc)
        raise
    finally:
        _save(path, data)
    return data


@dataclass(slots=True)
class NxtResult:
    base: Result
    pairs: int = 0
    fetched: int = 0
    reasons: Counter[str] = field(default_factory=Counter)
    """신호 종목일이 관측되지 않은 이유."""
    reasons_by_period: dict[str, Counter[str]] = field(default_factory=dict)
    signal_name_days: dict[str, int] = field(default_factory=dict)
    """기간 → 신호 종목일 수(관측 비율의 분모)."""
    per_indicator_names: dict[str, list[int]] = field(default_factory=dict)
    """지표 → 그 지표가 큰 밤인 신호일마다 관측 가능한 목록 종목 수."""
    control_names: list[int] = field(default_factory=list)
    early_values: dict[str, float] = field(default_factory=dict)
    """신호 쪽·비교군 관측 종목일의 08:00~08:04 거래대금 중앙값(원). 두 집단의 크기 차이를 본다."""
    observations: list[study.Observation] = field(default_factory=list)
    verdicts: list[study.Verdict] = field(default_factory=list)
    candidate: bool = False
    explore: dict[str, list[study.Verdict]] = field(default_factory=dict)
    """탐색(판정 아님): 같은 종목일의 g8·g9-g8·KRX 9→10, 그리고 진입가·최소 종목 수·비용을 바꾼 판정."""
    pooled: dict[str, tuple[int, float, float, float]] = field(default_factory=dict)
    """신호 종목일을 한데 모은 a·b의 (개수, 평균, 중앙값, 10% 절단평균). 비용 전. 탐색."""


def _trimmed(values: Sequence[float], share: float = 0.1) -> float:
    v = sorted(values)
    k = int(len(v) * share)
    return statistics.fmean(v[k : len(v) - k])


_Pick = Callable[[Sequence[nxt.Bar]], float | None]


def run_nxt(
    session: Session,
    folder: Path,
    kis_minutes: Path | None,
    fetch: bool,
    *,
    counts_only: bool = False,
) -> NxtResult:
    """`counts_only`면 관측 가능 수만 세고 판정하지 않는다(판정 전에 표본 크기를 먼저 본다)."""
    res = run(session, folder, kis_minutes)
    out = NxtResult(res)
    if res.gate is None or not res.gate.passed:
        return out
    pairs, controls = nxt_pairs(res, folder)
    if fetch:
        fetch_nxt(folder, pairs)
    raw = _load(folder / NXT_FILE)
    out.pairs = len(pairs)
    out.fetched = sum(1 for t, d in pairs if _nxt_key(t, d) in raw)

    daily = {
        t: {r[0]: float(r[2]) for r in rows} for t, rows in _load(folder / KR_DAILY_FILE).items()
    }
    hour = {
        t: {r[0]: (float(r[1]), float(r[2])) for r in rows}
        for t, rows in _load(folder / KR_HOUR_FILE).items()
    }
    lists = {i: [n for n, _ in names] for i, names in res.lists.items()}
    evald = sorted(res.prev_of)
    held = study.holdout_days(evald)
    study_days = sorted(d for d in res.active if d not in held)
    middle = study_days[len(study_days) // 2]

    def period(d: date) -> str:
        if d in held:
            return "holdout"
        return "study front" if d < middle else "study back"

    def bars_of(t: str, d: date) -> list[nxt.Bar]:
        rec = raw.get(_nxt_key(t, d)) or {}
        return [
            (str(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5]))
            for b in rec.get("bars", [])
        ]

    def values_for(
        d: date, names: Sequence[str], pick: _Pick, count: bool
    ) -> dict[str, nxt.NameDay]:
        got: dict[str, nxt.NameDay] = {}
        prev = res.prev_of[d].isoformat()
        for t in names:
            rec = raw.get(_nxt_key(t, d))
            why = None
            if rec is None:
                why = "not fetched"
            elif "error" in rec:
                why = "fetch refused"
            elif not rec["bars"]:
                why = "no NX bars that day"
            else:
                bars = bars_of(t, d)
                h, pc = hour.get(t, {}).get(d.isoformat()), daily.get(t, {}).get(prev)
                if not any(nxt.ENTRY_FIRST <= b[0] <= nxt.ENTRY_LAST and b[5] > 0 for b in bars):
                    why = "no trade 08:00-08:04"
                elif h is None or pc is None:
                    why = "no KRX price"
                elif (p8 := pick(bars)) is None:
                    why = "thin (< 10M KRW)"
                elif (v := nxt.name_day(p8, pc, h[0], h[1])) is None:
                    why = "price excluded"
                else:
                    got[t] = v
            if count:
                out.signal_name_days[period(d)] = out.signal_name_days.get(period(d), 0) + 1
                if why is not None:
                    out.reasons[why] += 1
                    out.reasons_by_period.setdefault(period(d), Counter())[why] += 1
        return got

    def observe_all(
        pick: _Pick, *, min_names: int, cost: float, count: bool
    ) -> list[study.Observation]:
        obs = []
        pooled: dict[str, list[float]] = {"a": [], "b": []}
        values5: dict[str, list[float]] = {"signal": [], "control": []}
        for d, inds in sorted(res.active.items()):
            names = sorted({n for i in inds for n in lists[i]})
            vals = values_for(d, names, pick, count)
            ctrl = values_for(d, controls[d], pick, False)
            if count:
                out.control_names.append(len(ctrl))
                values5["signal"] += [nxt.early_value(bars_of(t, d)) for t in vals]
                values5["control"] += [nxt.early_value(bars_of(t, d)) for t in ctrl]
                for i in inds:
                    out.per_indicator_names.setdefault(i, []).append(
                        sum(1 for n in lists[i] if n in vals)
                    )
                pooled["a"] += [v.a for v in vals.values()]
                pooled["b"] += [v.b for v in vals.values()]
            o = nxt.observe(
                d, inds, lists, {**vals, **ctrl}, controls[d], min_names=min_names, cost=cost
            )
            if o is not None:
                obs.append(o)
        if count:
            out.early_values = {k: statistics.median(v) for k, v in values5.items() if v}
            for key, vs in pooled.items():
                if vs:
                    out.pooled[key] = (
                        len(vs),
                        statistics.fmean(vs),
                        statistics.median(vs),
                        _trimmed(vs),
                    )
        return obs

    out.observations = observe_all(nxt.entry, min_names=nxt.MIN_NAMES, cost=nxt.COST, count=True)
    if counts_only:
        return out
    out.verdicts = study.judge(out.observations, evald, questions=nxt.QUESTIONS)
    out.candidate = nxt.candidate(out.verdicts)
    out.explore["same name-days"] = study.judge(out.observations, evald, questions=nxt.EXPLORE_KEYS)
    variants: dict[str, tuple[_Pick, int, float]] = {
        "entry: first bar open": (nxt.first_open, nxt.MIN_NAMES, nxt.COST),
        "entry: 08:49 close": (nxt.pre_close, nxt.MIN_NAMES, nxt.COST),
        "min names 1": (nxt.entry, 1, nxt.COST),
        "value floor 0": (lambda b: nxt.entry(b, min_value=0), nxt.MIN_NAMES, nxt.COST),
        "value floor 50M": (lambda b: nxt.entry(b, min_value=50_000_000), nxt.MIN_NAMES, nxt.COST),
        "cost 0.20%": (nxt.entry, nxt.MIN_NAMES, 0.002),
        "cost 0.40%": (nxt.entry, nxt.MIN_NAMES, 0.004),
    }
    for label, (pick, mn, cost) in variants.items():
        obs = observe_all(pick, min_names=mn, cost=cost, count=False)
        texts = [(k, t.replace("0.30%", f"{cost:.2%}")) for k, t in nxt.QUESTIONS]
        out.explore[label] = study.judge(obs, evald, questions=texts)
    return out
