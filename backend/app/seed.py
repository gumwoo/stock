"""Initial watchlist.

Two instruments, one per market, chosen so that every cross-market concern is
exercised from day one rather than discovered later:

* **005930 Samsung Electronics** — KRX, KRW, DART corp code, 09:00 KST session.
* **AAPL Apple Inc.** — NYSE/Nasdaq, USD, SEC CIK, 09:30 ET session, and a
  position whose KRW return has to be split into stock and currency parts.

The external anchors are the stable ones: CIK 0000320193 for Apple and DART
corp code 00126380 for Samsung. Neither changes when a ticker does, which is
the whole reason the schema keys on them instead of on the symbol.

Apple is also a useful anchor for the SEC work in Phase 2: its XBRL history
starts 2009-10-27, and the same (concept, period) appears under several filing
dates as later 10-Ks restate it — which is exactly what makes point-in-time
reconstruction possible and worth testing against.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from app.core.calendar import Market
from app.repositories import instrument_repo

logger = logging.getLogger(__name__)

WATCHLIST = [
    {
        "market": Market.KR,
        "name": "삼성전자",
        "symbol": "005930",
        "sector": "Semiconductors",
        "kr_corp_code": "00126380",
        "listed_at": date(1975, 6, 11),
    },
    {
        "market": Market.US,
        "name": "Apple Inc.",
        "symbol": "AAPL",
        "sector": "Consumer Electronics",
        "us_cik": "0000320193",
        "listed_at": date(1980, 12, 12),
    },
]


def seed_watchlist(session: Session) -> list[int]:
    """Create or refresh the starting instruments. Idempotent."""
    ids: list[int] = []
    for entry in WATCHLIST:
        instrument = instrument_repo.upsert_instrument(
            session,
            market=entry["market"],  # type: ignore[arg-type]
            name=entry["name"],  # type: ignore[arg-type]
            symbol=entry["symbol"],  # type: ignore[arg-type]
            sector=entry.get("sector"),  # type: ignore[arg-type]
            us_cik=entry.get("us_cik"),  # type: ignore[arg-type]
            kr_corp_code=entry.get("kr_corp_code"),  # type: ignore[arg-type]
            listed_at=entry.get("listed_at"),  # type: ignore[arg-type]
            symbol_source="SEED",
        )
        ids.append(instrument.instrument_id)
        logger.info(
            "seeded %s %s (%s) -> instrument_id=%s",
            entry["market"],
            entry["symbol"],
            entry["name"],
            instrument.instrument_id,
        )
    session.commit()
    return ids
