"""트럼프 관세 글 연구(`app/scoring/trump_study.py`)의 실행. 파일만 읽고 DB에는 쓰지 않는다.

- 트럼프: `data/trump/truth_archive.csv`(CNN 공개본). `created_at`은 게시 시각(UTC).
- 한국·미국 가격: 밤사이 연구 파일(`data/overnight_study/`) 그대로. 유니버스도 같은 유동성 규칙(`liquid_tickers`).
"""

from __future__ import annotations

import csv
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from app.scoring import overnight_study as ov
from app.scoring import trump_study as ts
from app.services import overnight_study_service as svc

NEW_YORK = ZoneInfo("America/New_York")
ARCHIVE = "truth_archive.csv"


@dataclass(frozen=True, slots=True)
class Post:
    at: datetime
    content: str


def load_posts(path: Path) -> list[Post]:
    csv.field_size_limit(10_000_000)
    out = []
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            raw = row.get("created_at") or ""
            if not raw:
                continue
            at = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
            out.append(Post(at, row.get("content") or ""))
    return out


def us_closed(at: datetime) -> bool:
    """뉴욕 정규장(09:30~16:00, 평일) 밖에 올린 글인가. 공휴일은 따지지 않는다(탐색용 근사)."""
    ny = at.astimezone(NEW_YORK)
    if ny.weekday() >= 5:
        return True
    return not (time(9, 30) <= ny.time() < time(16, 0))


@dataclass(slots=True)
class TrumpResult:
    posts: int = 0
    tariff_posts: int = 0
    dropped_days: list[date] = field(default_factory=list)
    """1,000종목 미만이라 관측하지 않은 평가일."""
    days: list[ts.Day] = field(default_factory=list)
    verdicts: list[ts.Verdict] = field(default_factory=list)
    candidate: bool = False
    explore: dict[str, list[ts.Verdict]] = field(default_factory=dict)
    explore_fits: dict[str, ts.Fit] = field(default_factory=dict)
    describe: dict[str, tuple[int, float, float, float]] = field(default_factory=dict)
    """집단별 하루 값의 (일수, 평균, 중앙값, 10% 절단평균). 탐색."""


def _trimmed(values: Sequence[float]) -> float:
    v = sorted(values)
    k = int(len(v) * 0.1)
    return statistics.fmean(v[k : len(v) - k])


def run(folder: Path, archive: Path) -> TrumpResult:
    res = TrumpResult()
    posts = load_posts(archive)
    res.posts = len(posts)
    tariff = [p for p in posts if ts.is_tariff(p.content)]
    res.tariff_posts = len(tariff)

    daily = svc._load(folder / svc.KR_DAILY_FILE)
    hour = svc._load(folder / svc.KR_HOUR_FILE)
    seen = {date.fromisoformat(r[0]) for f in (daily, hour) for rows in f.values() for r in rows}
    sessions = ov.traded_sessions(svc.KR.sessions_between(date(2023, 6, 1), ts.EVAL_LAST), seen)
    prev_of = {d: sessions[i - 1] for i, d in enumerate(sessions) if i}
    evald = [d for d in sessions if ts.EVAL_FIRST <= d <= ts.EVAL_LAST and d in prev_of]
    windows = {d: ts.window(svc.KR.session_close(prev_of[d]), d) for d in evald}

    by_day = {
        t: {date.fromisoformat(d): (o, c, v) for d, o, c, v in rows} for t, rows in daily.items()
    }
    liquid = svc.liquid_tickers(by_day)
    hours = {t: {r[0]: (float(r[1]), float(r[2])) for r in rows} for t, rows in hour.items()}

    def build(counts: dict[date, int]) -> list[ts.Day]:
        out = []
        for d in evald:
            prev = prev_of[d]
            names = []
            for t in liquid:
                h = hours.get(t, {}).get(d.isoformat())
                pc = by_day[t].get(prev)
                if h is not None and pc is not None:
                    names.append((pc[1], h[0], h[1]))
            got = ts.day_value(names)
            if got is None:
                continue
            start, end = windows[d]
            out.append(
                ts.Day(
                    day=d,
                    signal=counts[d] > 0,
                    posts=counts[d],
                    long=end - start > ts.LONG_WINDOW,
                    monday=d.weekday() == 0,
                    g=got[0],
                    f=got[1],
                    names=got[2],
                )
            )
        return out

    counts = ts.count_in_windows([p.at for p in tariff], windows)
    res.days = build(counts)
    observed = {d.day for d in res.days}
    res.dropped_days = [d for d in evald if d not in observed]
    res.verdicts = ts.judge(res.days)
    res.candidate = ts.candidate(res.verdicts)

    # --- 탐색(판정 아님) ---
    def relabel(keep: Callable[[Post], bool]) -> list[ts.Day]:
        c = ts.count_in_windows([p.at for p in tariff if keep(p)], windows)
        return [_with_signal(d, c[d.day] > 0, c[d.day]) for d in res.days]

    res.explore["overnight windows only"] = ts.judge([d for d in res.days if not d.long])
    res.explore["without motivation days"] = ts.judge(
        [d for d in res.days if d.day not in ts.MOTIVATION_DAYS]
    )
    res.explore["posts while US closed"] = ts.judge(relabel(lambda p: us_closed(p.at)))
    res.explore["posts while US open"] = ts.judge(relabel(lambda p: not us_closed(p.at)))
    res.explore["korea in text"] = ts.judge(relabel(lambda p: "korea" in p.content.lower()))
    uniq: dict[date, set[str]] = {}
    for d, (start, end) in windows.items():
        uniq[d] = {p.content.strip() for p in tariff if start <= p.at < end}
    res.explore["2+ distinct tariff posts"] = ts.judge(
        [_with_signal(d, len(uniq[d.day]) >= 2, len(uniq[d.day])) for d in res.days]
    )
    res.explore["2025-07-08 image letters as signal"] = ts.judge(
        [
            _with_signal(d, True, max(d.posts, 1)) if d.day == ts.IMAGE_LETTER_DAY else d
            for d in res.days
        ]
    )

    held = ts.holdout_days([d.day for d in res.days])
    study = [d for d in res.days if d.day not in held]
    qqq = ov.align(
        [d.day for d in study],
        {d.day: windows[d.day][1] for d in study},
        {d.day: windows[d.day][0] for d in study},
        svc._us_series(svc._load(folder / svc.US_FILE)[ov.MARKET]),
    )
    with_q = [d for d in study if qqq.get(d.day) is not None]
    for key, fld in (("T1", "g"), ("T3", "f")):
        res.explore_fits[f"{key} + QQQ overnight (study)"] = ts.fit_difference(
            with_q, fld, extra={"qqq": [float(qqq[d.day] or 0.0) for d in with_q]}
        )
    cut = sorted(d.g for d in study)[len(study) // 3]
    low = [d for d in study if d.g <= cut]
    res.explore_fits["T3 on gap-down third, gap as covariate (study)"] = ts.fit_difference(
        low, "f", extra={"gap": [d.g for d in low]}
    )

    for label, group in (
        ("signal", [d for d in study if d.signal]),
        ("comparison", [d for d in study if not d.signal]),
    ):
        for fld in ("g", "f"):
            vals = [getattr(d, fld) for d in group]
            if vals:
                res.describe[f"{label} {fld}"] = (
                    len(vals),
                    statistics.fmean(vals),
                    statistics.median(vals),
                    _trimmed(vals),
                )
    return res


def _with_signal(d: ts.Day, signal: bool, posts: int) -> ts.Day:
    return ts.Day(d.day, signal, posts, d.long, d.monday, d.g, d.f, d.names)
