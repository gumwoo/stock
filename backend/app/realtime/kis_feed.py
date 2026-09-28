"""Reading KIS's real-time trade feed and keeping each name's minutes in memory.

Pure. A real-time frame is pipe-separated: whether it is encrypted, the
TR id, how many records it carries, and the records — each a run of
caret-separated fields in the order KIS's own sample lists them for
`H0STCNT0`. Anything else on the socket is JSON: an answer to a subscription,
or a PINGPONG to be echoed.

`LiveBook` folds trades into one-minute bars per name. It is what the chart
shows while the session runs; it is never stored. The record is the REST
minute bars fetched after the close (`app/collectors/kis_minute.py`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")
TRADE_TR = "H0STCNT0"

# The field order of H0STCNT0, as KIS's sample (domestic_stock_functions_ws.py) lists it.
TRADE_FIELDS = (
    "MKSC_SHRN_ISCD", "STCK_CNTG_HOUR", "STCK_PRPR", "PRDY_VRSS_SIGN", "PRDY_VRSS", "PRDY_CTRT",
    "WGHN_AVRG_STCK_PRC", "STCK_OPRC", "STCK_HGPR", "STCK_LWPR", "ASKP1", "BIDP1", "CNTG_VOL",
    "ACML_VOL", "ACML_TR_PBMN", "SELN_CNTG_CSNU", "SHNU_CNTG_CSNU", "NTBY_CNTG_CSNU", "CTTR",
    "SELN_CNTG_SMTN", "SHNU_CNTG_SMTN", "CCLD_DVSN", "SHNU_RATE", "PRDY_VOL_VRSS_ACML_VOL_RATE",
    "OPRC_HOUR", "OPRC_VRSS_PRPR_SIGN", "OPRC_VRSS_PRPR", "HGPR_HOUR", "HGPR_VRSS_PRPR_SIGN",
    "HGPR_VRSS_PRPR", "LWPR_HOUR", "LWPR_VRSS_PRPR_SIGN", "LWPR_VRSS_PRPR", "BSOP_DATE",
    "NEW_MKOP_CLS_CODE", "TRHT_YN", "ASKP_RSQN1", "BIDP_RSQN1", "TOTAL_ASKP_RSQN",
    "TOTAL_BIDP_RSQN", "VOL_TNRT", "PRDY_SMNS_HOUR_ACML_VOL", "PRDY_SMNS_HOUR_ACML_VOL_RATE",
    "HOUR_CLS_CODE", "MRKT_TRTM_CLS_CODE", "VI_STND_PRC",
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Trade:
    code: str
    at: time
    """Seoul time of the trade, to the second."""
    price: float
    volume: int
    day_volume: int
    change_pct: float


def subscribe_message(approval_key: str, code: str, *, subscribe: bool = True) -> str:
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1" if subscribe else "2",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": TRADE_TR, "tr_key": code}},
        }
    )


def parse_trades(raw: str) -> list[Trade]:
    """The trades in one real-time frame; nothing if it is not an unencrypted H0STCNT0 frame."""
    parts = raw.split("|", 3)
    if len(parts) != 4 or parts[0] != "0" or parts[1] != TRADE_TR:
        return []
    try:
        count = int(parts[2])
    except ValueError:
        return []
    values = parts[3].split("^")
    width = len(TRADE_FIELDS)
    trades = []
    for n in range(count):
        record = values[n * width : (n + 1) * width]
        if len(record) < width:
            break
        row = dict(zip(TRADE_FIELDS, record, strict=True))
        try:
            hhmmss = row["STCK_CNTG_HOUR"]
            trades.append(
                Trade(
                    code=row["MKSC_SHRN_ISCD"],
                    at=time(int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6])),
                    price=float(row["STCK_PRPR"]),
                    volume=int(row["CNTG_VOL"]),
                    day_volume=int(row["ACML_VOL"]),
                    change_pct=float(row["PRDY_CTRT"]),
                )
            )
        except (ValueError, KeyError):
            continue
    return trades


@dataclass(frozen=True, slots=True)
class Control:
    """A JSON frame: a PINGPONG to echo, or KIS's answer to a subscription."""

    tr_id: str
    code: str | None
    ok: bool | None
    message: str | None


def parse_control(raw: str) -> Control | None:
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    raw_header, raw_body = payload.get("header"), payload.get("body")
    header: dict[str, Any] = raw_header if isinstance(raw_header, dict) else {}
    body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}
    tr_id = str(header.get("tr_id") or "")
    if not tr_id:
        return None
    rt = body.get("rt_cd")
    return Control(
        tr_id=tr_id,
        code=header.get("tr_key"),
        ok=None if rt is None else rt == "0",
        message=body.get("msg1"),
    )


def minute_epoch(day: date, at: time) -> int:
    """Seconds since the epoch of the start of the minute `at` falls in, Seoul time."""
    start = datetime.combine(day, time(at.hour, at.minute), tzinfo=SEOUL)
    return int(start.astimezone(UTC).timestamp())


def downsample(values: list[float], n: int) -> list[float]:
    """고르게 `n`개로 줄인다. 첫 점과 마지막 점은 항상 남긴다. 짧으면 그대로."""
    if n < 2 or len(values) <= n:
        return list(values)
    step = (len(values) - 1) / (n - 1)
    return [values[round(i * step)] for i in range(n)]


@dataclass
class LiveBook:
    """Each name's one-minute and one-second bars for today, built from what has arrived.

    초봉은 분봉과 같은 체결로 함께 쌓는다. 클릭한 종목만이 아니라 목록 전부를, 서버가 실시간 연결을 시작한 때부터.
    과거 초봉은 받을 곳이 없어 REST로 채우지 못한다(분봉만 `seed`로 채운다). 저장하지 않는 화면용이다.
    메모리(예측, 실측 아님): 초봉 하나가 dict 항목·int 키·float 5개 리스트로 약 300바이트라, 40종목이 장중 매초
    체결되는 최악이면 약 94만 봉, 곧 280MB 안팎이다. 실제로는 체결 없는 초가 많아 그보다 작다. 상한은 두지 않는다.
    장이 끝나도 다음 거래일 연결 때까지 그대로 둔다(장 뒤에도 그날 차트를 볼 수 있게). 그동안 이 메모리를 쥔다.
    """

    day: date
    bars: dict[str, dict[int, list[float]]] = field(default_factory=dict)
    seconds: dict[str, dict[int, list[float]]] = field(default_factory=dict)
    last: dict[str, Trade] = field(default_factory=dict)
    last_second: dict[str, Any] = field(default_factory=dict)

    def seed(self, code: str, bars: list[tuple[int, float, float, float, float, float]]) -> None:
        """Earlier minutes fetched by REST. A minute already built from trades is kept."""
        mine = self.bars.setdefault(code, {})
        for t, o, h, lo, c, v in bars:
            mine.setdefault(t, [o, h, lo, c, v])

    def add(self, trade: Trade) -> dict[str, Any]:
        """Fold a trade in; returns the minute it changed, as the chart wants it. The second it changed is
        `last_second` (방송에 함께 실어 브라우저가 서버의 완성된 초봉을 그대로 그리게 한다)."""
        t = minute_epoch(self.day, trade.at)
        mine = self.bars.setdefault(trade.code, {})
        bar = mine.get(t)
        if bar is None:
            bar = mine[t] = [trade.price, trade.price, trade.price, trade.price, 0.0]
        bar[1] = max(bar[1], trade.price)
        bar[2] = min(bar[2], trade.price)
        bar[3] = trade.price
        bar[4] += trade.volume
        # 초 키는 게이트웨이가 방송하는 `time`과 같다(분 시작 + 초).
        sec = self.seconds.setdefault(trade.code, {})
        s = t + trade.at.second
        tick = sec.get(s)
        if tick is None:
            tick = sec[s] = [trade.price, trade.price, trade.price, trade.price, 0.0]
        tick[1] = max(tick[1], trade.price)
        tick[2] = min(tick[2], trade.price)
        tick[3] = trade.price
        tick[4] += trade.volume
        self.last_second = {
            "time": s,
            "open": tick[0],
            "high": tick[1],
            "low": tick[2],
            "close": tick[3],
            "volume": tick[4],
        }
        self.last[trade.code] = trade
        return {
            "time": t,
            "open": bar[0],
            "high": bar[1],
            "low": bar[2],
            "close": bar[3],
            "volume": bar[4],
        }

    def series(self, code: str, interval: str = "1m") -> list[dict[str, float]]:
        """오늘의 봉, 시간순. API 스레드가 읽는 동안 체결이 들어와도 되게 먼저 복사한다(C 수준 한 번의 복사).

        복사는 목록 수준이다. 봉 리스트는 공유하므로, 읽는 도중 체결이 끼면 가장 최근 봉 하나가 잠깐 어긋난 값일 수 있다
        (그 체결의 방송이 곧 같은 시각 봉을 덮어 바로잡는다).
        """
        source = self.seconds if interval == "1s" else self.bars
        mine = source.get(code, {}).copy()
        return [
            {"time": t, "open": b[0], "high": b[1], "low": b[2], "close": b[3], "volume": b[4]}
            for t, b in sorted(mine.items())
        ]

    def closes(self, code: str, points: int = 60) -> list[float]:
        """목록 추세선용: 오늘 1분봉 종가를 시간순으로, 최대 `points`개가 되게 고르게 솎는다."""
        mine = self.bars.get(code, {}).copy()
        return downsample([b[3] for _, b in sorted(mine.items())], points)
