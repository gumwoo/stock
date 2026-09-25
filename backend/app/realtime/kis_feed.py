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


@dataclass
class LiveBook:
    """Each name's one-minute bars for today, built from what has arrived."""

    day: date
    bars: dict[str, dict[int, list[float]]] = field(default_factory=dict)
    last: dict[str, Trade] = field(default_factory=dict)

    def seed(self, code: str, bars: list[tuple[int, float, float, float, float, float]]) -> None:
        """Earlier minutes fetched by REST. A minute already built from trades is kept."""
        mine = self.bars.setdefault(code, {})
        for t, o, h, lo, c, v in bars:
            mine.setdefault(t, [o, h, lo, c, v])

    def add(self, trade: Trade) -> dict[str, Any]:
        """Fold a trade in; returns the minute it changed, as the chart wants it."""
        t = minute_epoch(self.day, trade.at)
        mine = self.bars.setdefault(trade.code, {})
        bar = mine.get(t)
        if bar is None:
            bar = mine[t] = [trade.price, trade.price, trade.price, trade.price, 0.0]
        bar[1] = max(bar[1], trade.price)
        bar[2] = min(bar[2], trade.price)
        bar[3] = trade.price
        bar[4] += trade.volume
        self.last[trade.code] = trade
        return {
            "time": t,
            "open": bar[0],
            "high": bar[1],
            "low": bar[2],
            "close": bar[3],
            "volume": bar[4],
        }

    def series(self, code: str) -> list[dict[str, float]]:
        return [
            {"time": t, "open": b[0], "high": b[1], "low": b[2], "close": b[3], "volume": b[4]}
            for t, b in sorted(self.bars.get(code, {}).items())
        ]
