"""Initial watchlist.

Eighteen instruments, nine per market. The first two were chosen so that every
cross-market concern is exercised from day one; the other sixteen exist because
two instruments cannot answer the question the backtest raised.

**Why the list grew.** Measured over ten years, the rule trailed buy-and-hold
badly on Apple and the reason was legible: the fundamental engine reads P/E and
P/B, so a rising price worsens the valuation, and the rule stopped buying Apple
during exactly the stretch Apple kept rising. Whether that is a property of the
rule or a property of Apple cannot be settled by looking harder at Apple. The
names below deliberately span the value-growth axis in both markets — banks,
steel, telecoms and staples on one side, semiconductors and internet on the
other — so the same measurement has something to vary against.

**How they were chosen, stated before the results are known:** large, liquid,
continuously listed under one symbol since before 2016, spread across sectors.
Picking today's large caps is survivorship by construction, which this project
does not correct and does say — see the note on universe reconstruction in the
status section.

**Every external anchor here was read from the registry, not recalled.** DART's
`corpCode.xml` for the Korean corp codes and SEC's `company_tickers.json` for
the CIKs, then each one verified to carry XBRL or DART history reaching before
2016. A plausible-looking identifier that belongs to another company pulls that
company's financials into this row and nothing downstream can tell.

That check earned its keep immediately. SEC's ticker map points XOM at CIK
0002115436, "ExxonMobil Holdings Corp", which holds four facts all filed on
2026-08-03; the seventeen years of history sit on the old CIK 0000034088.
Exxon reorganised into a holding company, the ticker followed the new entity,
and the filings did not. So a CIK is a stable anchor for a company but not
across a reorganisation, which `instrument.us_cik` — a single column — cannot
express. XOM is left out rather than seeded against an anchor already known to
be going stale, and CVX stands in for energy.

`listed_at` is set only where it was verified. Left NULL, the point-in-time
filter does not exclude the instrument, which is correct for every name here:
all were listed long before the window any backtest uses. Inventing dates to
fill the column would put a guess where the schema promises a fact.

The original two:

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
from typing import NotRequired, TypedDict

from sqlalchemy.orm import Session

from app.core.calendar import Market
from app.repositories import instrument_repo

logger = logging.getLogger(__name__)


class SeedEntry(TypedDict):
    """One watchlist row.

    Typed rather than a bare dict because the optional keys are the ones that
    matter: an entry missing `us_cik` is a Korean filer, an entry missing
    `listed_at` is one whose listing date was never verified. Inference over a
    plain dict collapses those to `object` the moment the entries stop being
    uniform, which is exactly when the distinctions start carrying meaning.
    """

    market: Market
    name: str
    symbol: str
    sector: NotRequired[str]
    us_cik: NotRequired[str]
    kr_corp_code: NotRequired[str]
    listed_at: NotRequired[date]


WATCHLIST: list[SeedEntry] = [
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
    # --- KOSPI. KOSDAQ needs the .KQ suffix, which is a known limitation, so
    # every Korean name here trades on KOSPI.
    {
        "market": Market.KR,
        "name": "SK하이닉스",
        "symbol": "000660",
        "sector": "Semiconductors",
        "kr_corp_code": "00164779",
    },
    {
        "market": Market.KR,
        "name": "현대자동차",
        "symbol": "005380",
        "sector": "Automobiles",
        "kr_corp_code": "00164742",
    },
    {
        "market": Market.KR,
        "name": "POSCO홀딩스",
        "symbol": "005490",
        "sector": "Steel",
        "kr_corp_code": "00155319",
    },
    {
        "market": Market.KR,
        "name": "NAVER",
        "symbol": "035420",
        "sector": "Internet",
        "kr_corp_code": "00266961",
    },
    {
        "market": Market.KR,
        "name": "LG화학",
        "symbol": "051910",
        "sector": "Chemicals",
        "kr_corp_code": "00356361",
    },
    {
        "market": Market.KR,
        "name": "신한지주",
        "symbol": "055550",
        "sector": "Banks",
        "kr_corp_code": "00382199",
    },
    {
        "market": Market.KR,
        "name": "SK텔레콤",
        "symbol": "017670",
        "sector": "Telecoms",
        "kr_corp_code": "00159023",
    },
    {
        "market": Market.KR,
        "name": "현대모비스",
        "symbol": "012330",
        "sector": "Auto Components",
        "kr_corp_code": "00164788",
    },
    # --- US
    {
        "market": Market.US,
        "name": "Microsoft Corporation",
        "symbol": "MSFT",
        "sector": "Software",
        "us_cik": "0000789019",
    },
    {
        "market": Market.US,
        "name": "NVIDIA Corporation",
        "symbol": "NVDA",
        "sector": "Semiconductors",
        "us_cik": "0001045810",
    },
    {
        "market": Market.US,
        "name": "Intel Corporation",
        "symbol": "INTC",
        "sector": "Semiconductors",
        "us_cik": "0000050863",
    },
    {
        "market": Market.US,
        "name": "Johnson & Johnson",
        "symbol": "JNJ",
        "sector": "Pharmaceuticals",
        "us_cik": "0000200406",
    },
    {
        "market": Market.US,
        "name": "JPMorgan Chase & Co.",
        "symbol": "JPM",
        "sector": "Banks",
        "us_cik": "0000019617",
    },
    {
        "market": Market.US,
        "name": "The Coca-Cola Company",
        "symbol": "KO",
        "sector": "Beverages",
        "us_cik": "0000021344",
    },
    {
        "market": Market.US,
        "name": "The Procter & Gamble Company",
        "symbol": "PG",
        "sector": "Household Products",
        "us_cik": "0000080424",
    },
    {
        "market": Market.US,
        "name": "Chevron Corporation",
        "symbol": "CVX",
        "sector": "Energy",
        "us_cik": "0000093410",
    },
]


def seed_watchlist(session: Session) -> list[int]:
    """Create or refresh the starting instruments. Idempotent."""
    ids: list[int] = []
    for entry in WATCHLIST:
        instrument = instrument_repo.upsert_instrument(
            session,
            market=entry["market"],
            name=entry["name"],
            symbol=entry["symbol"],
            sector=entry.get("sector"),
            us_cik=entry.get("us_cik"),
            kr_corp_code=entry.get("kr_corp_code"),
            listed_at=entry.get("listed_at"),
            symbol_source="SEED",
            # The watchlist is what gets prices, filings and a score. Without
            # this the column's server default leaves every seeded row
            # untracked, and `score_all` on a fresh database scores nothing.
            tracked=True,
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
