"""Korean news, one query per listed company.

The search API is cheap enough to sweep the whole listing master: 25,000 calls
a day against the client ID, and a full pass over two and a half thousand names
at two pages each is well inside half of that. Threads and Reddit are not —
their caps are an order of magnitude smaller — which is why reach differs by
source and is decided by budget rather than by market.

**Every request is reserved before it is sent.** `QuotaGuard` is the floor a
bug cannot get under; the page cap and the watermark below are the design that
keeps the floor from ever being reached. If the guard starts refusing during
normal operation, something is wrong and the run says so.

**A search result is not evidence the article is about the company.** Naver
matches body text and related terms, so a query for a large holding returns
pieces that merely mention its sector. Storing a mention on that basis would
hand the sentiment scorer, when it arrives, articles about the wrong company —
and the resulting score would look entirely ordinary. So a mention is written
only when the name or a registered alias actually appears in the title or
summary, and `match_method` records which.

**A search result is not evidence about the company the query named, either.**
252 of the 3,961 real listed names sit inside another listed name, and in the
middle rather than at the front: `한화` is part of `대한화섬`. Korean attaches
particles straight to the noun, so no rule about the neighbouring character can
separate them without also rejecting `한화는`. What works is position: at any
place in the text, the longest registered name wins.

**The watermark comes from the last *complete* run**, and a run that swept
fewer instruments than the market holds, or stopped at the page cap on one of
them, is not complete. The watermark is global and cannot say "covered here,
not there", so anything short of a full sweep leaves it where it was. That
costs a re-read of up to seven days, which the call estimate already pays for,
and the alternative is articles that are never read at all.
See `CollectorStatusLookup.last_full_success`.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from sqlalchemy.orm import Session

from app.collectors.base import (
    BaseCollector,
    CollectionResult,
    CollectorStatusLookup,
    RateLimitedError,
    SkipCollection,
    TokenBucket,
    UpstreamUnavailableError,
    as_object,
    as_rows,
    as_text,
)
from app.collectors.quota import QuotaExhausted, QuotaGuard
from app.config import get_settings
from app.core.calendar import Market
from app.core.clock import ensure_utc, utc_now
from app.models import Instrument
from app.models.news import MatchMethod, NewsSource
from app.repositories import instrument_repo, news_repo
from app.repositories.news_repo import NewsItemRow, NewsMentionRow

# NAVER API Hub, not the developer centre. Naver stopped issuing search
# credentials at developers.naver.com on 2026-07-31, so a new application gets
# an API Hub key and only this gateway accepts it. Three things move together:
# the host, the path shape (`/search/v1/news`, not `/v1/search/news.json`) and
# the header names in `_get`. Calling the old host with a Hub key returns 401
# `errorCode 024`, which reads as a bad secret rather than as a wrong address.
BASE = "https://naverapihub.apigw.ntruss.com/search/v1/news"

QUOTA_GROUP = "naver_search"
ENDPOINT = "news"

# The API's own ceilings. `start + display - 1` may not exceed 1,000, so with a
# full page there are ten pages at most however deep we are willing to go.
PAGE_SIZE = 100
MAX_START = 1_000

# How far back a first run, or a run after a long gap, is willing to read.
# Without a floor a watermark that stops advancing lets the window grow without
# limit, and every run then pays for history it already has.
FIRST_RUN_LOOKBACK = timedelta(days=3)
MAX_LOOKBACK = timedelta(days=7)

# Overlap so an article published while the previous run was mid-sweep is not
# missed between two windows that merely touch.
WATERMARK_OVERLAP = timedelta(hours=2)

# Query text for companies whose registered name is a poor search term. A
# handful, maintained by hand, and the first real sweep is what says which
# names belong here — see the mention reject rate in the run detail.
QUERY_OVERRIDES: Mapping[str, str] = {
    "NAVER": "네이버 주가",
}

# Spellings that count as the company appearing in the text. Same source of
# truth as the overrides: observed, not guessed.
ALIASES: Mapping[str, tuple[str, ...]] = {
    "NAVER": ("네이버",),
    "POSCO홀딩스": ("포스코홀딩스", "포스코"),
    "SK하이닉스": ("하이닉스",),
    "LG화학": ("엘지화학",),
    "KT&G": ("케이티앤지",),
}

# Query parameters that identify a referral rather than a document. Two URLs
# differing only in these are the same article.
#
# **Only names that cannot mean anything else.** Stripping a parameter that
# turns out to identify the document merges two different articles into one
# row, and the survivor keeps the other one's mentions — a mention on an
# article that never named the company. Not stripping one that really was a
# referral costs a duplicate row, which is harmless. The two errors are not
# symmetric, so `ref`, `from` and `spm` are deliberately absent: they are
# ordinary words and a publisher may well number articles with them.
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "fbclid",
        "gclid",
        "igshid",
    }
)


def _is_hangul(char: str) -> bool:
    """Precomposed syllables and the Jamo blocks around them."""
    return "가" <= char <= "힣" or "ᄀ" <= char <= "ᇿ" or "㄰" <= char <= "㆏"


_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
# Whether a neighbouring character continues a number, for symbol matching.
_DIGIT = re.compile(r"\d")


@dataclass(frozen=True, slots=True)
class Yield:
    """What one company's search produced, judged article by article.

    `matched` counts articles that name the company, whether or not the mention
    was new this run — a re-run inserts nothing and must not look like a
    company whose every result was rejected.
    """

    name: str
    symbol: str | None
    matched: int
    rejected: int

    @property
    def evaluated(self) -> int:
        return self.matched + self.rejected

    @property
    def reject_rate(self) -> float:
        return self.rejected / self.evaluated if self.evaluated else 0.0


# How many companies each list in the report names. The plan asked for the
# twenty worst; the rollout reads this after every stage.
REPORT_TOP = 20


@dataclass(frozen=True, slots=True)
class Sweep:
    """What paging one query produced, and why it stopped.

    `exhausted` carries a budget refusal back rather than letting it unwind the
    stack. A refusal on page two would otherwise discard page one — which was
    fetched, paid for out of the same budget, and is perfectly good. Throwing
    away the last thing the budget bought, at the exact moment the budget runs
    out, is the wrong response to running out.
    """

    rows: list[tuple[NewsItemRow, str]]
    read: int
    skipped: int
    hit_page_cap: bool
    exhausted: QuotaExhausted | None = None


class NaverNewsCollector(BaseCollector):
    """Sweeps every listed Korean company's name through the news search."""

    name = "NAVER_NEWS"

    def __init__(
        self,
        *,
        max_pages: int | None = None,
        max_instruments: int | None = None,
        guard: QuotaGuard | None = None,
    ) -> None:
        settings = get_settings()
        self._client_id = settings.naver_client_id
        self._client_secret = settings.naver_client_secret
        # Half the rate Naver is documented to allow. The per-second shape is
        # this bucket's job; the daily total is the guard's.
        self._bucket = TokenBucket(settings.naver_rate)
        self._guard = guard if guard is not None else QuotaGuard()
        self.max_pages = max_pages if max_pages is not None else settings.naver_news_max_pages
        if self.max_pages < 1:
            # Zero pages reads nothing, and `_sweep` would report a clean stop
            # rather than a truncation, so the run would finish SUCCESS having
            # asked no questions and move the watermark past the answers.
            raise ValueError(f"max_pages must be at least 1, got {self.max_pages}")
        # Present so the end-to-end check can cost one call rather than a
        # full sweep. Never set in scheduled operation.
        self.max_instruments = max_instruments

    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def skip_reason(self) -> str:
        return (
            "set NAVER_CLIENT_ID and NAVER_CLIENT_SECRET to enable "
            "(a NAVER API Hub application key, from console.ncloud.com)"
        )

    # --- pure conversion --------------------------------------------------

    @staticmethod
    def strip_html(raw: str) -> str:
        """Drop the search highlighting and decode entities.

        Titles and snippets come back wrapped in `<b>` around the matched term
        and with `&quot;` for quotes. Stored as-is, the markup ends up in the
        database and then in a scoring prompt.

        Tags go first and entities second, so an escaped `&lt;b&gt;` in the
        article's own text survives as text instead of becoming a tag to strip.
        """
        return _SPACE.sub(" ", html.unescape(_TAG.sub("", raw))).strip()

    @staticmethod
    def parse_pub_date(raw: str) -> datetime | None:
        """RFC 1123 with an offset, e.g. `Mon, 21 Sep 2026 14:03:00 +0900`.

        Returns None rather than raising: one unparseable row should cost that
        row, not the sweep. The caller counts what it skipped.
        """
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if parsed is None or parsed.tzinfo is None:
            # A naive timestamp would have to be given a zone by guessing, and
            # a guessed zone on an availability column is a silent nine-hour
            # error. Better to drop the row and count it.
            return None
        return ensure_utc(parsed, field="pubDate")

    @staticmethod
    def canonical_url(*, originallink: str, link: str) -> str | None:
        """One spelling per article, so the same piece hashes to one row.

        Prefers the publisher's own URL; Naver's mirror is a location, not an
        identity, and the same article reached through both is one article.
        """
        chosen = (originallink or "").strip() or (link or "").strip()
        if not chosen:
            return None

        parts = urlsplit(chosen)
        if not parts.netloc:
            return None

        query = sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS
        )

        # A default port is the same address written twice, and so is a
        # trailing slash. Which spelling arrives depends on the publisher, so
        # folding them here is what keeps one article to one row.
        scheme = parts.scheme.lower()
        host = parts.netloc.lower()
        for named, port in (("http", ":80"), ("https", ":443")):
            if scheme == named and host.endswith(port):
                host = host[: -len(port)]
        path = parts.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/") or "/"

        return urlunsplit((scheme, host, path, urlencode(query), ""))

    @staticmethod
    def url_hash(canonical: str) -> str:
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def publisher_host(canonical: str) -> str | None:
        host = urlsplit(canonical).netloc.lower()
        return host or None

    @staticmethod
    def aliases_for(instrument: Instrument) -> tuple[str, ...]:
        return ALIASES.get(instrument.name, ())

    @staticmethod
    def query_for(instrument: Instrument) -> str:
        return QUERY_OVERRIDES.get(instrument.name, instrument.name)

    @staticmethod
    def term_pattern(term: str) -> re.Pattern[str] | None:
        """A company name as copy actually writes it.

        Whitespace inside the name is optional, because Korean coverage writes
        `SK하이닉스` and `SK 하이닉스` interchangeably and a check that treated
        those as different companies would reject half the real hits. The Latin
        part is case-insensitive, so `naver` counts as `NAVER`.

        Searching the original text rather than a whitespace-stripped copy is
        what keeps positions meaningful, and positions are what `swallowed_by`
        needs to work at all.
        """
        squeezed = [ch for ch in term if not ch.isspace()]
        if not squeezed:
            return None

        parts: list[str] = []
        for index, char in enumerate(squeezed):
            if index and not (_is_hangul(squeezed[index - 1]) and _is_hangul(char)):
                # A space is allowed where the script changes, which is where
                # copy actually puts one: `SK 하이닉스`, `KT & G`. Allowing it
                # between two Hangul syllables instead turns `한 화면에` into
                # 한화 and `최 대 유 통` into 대유 — ordinary Korean sentences,
                # not contrived ones, and the names it costs are the short
                # well-known ones that appear most often.
                parts.append(r"\s*")
            parts.append(re.escape(char))
        return re.compile("".join(parts), re.IGNORECASE)

    @classmethod
    def spans(cls, text: str, term: str) -> list[tuple[int, int]]:
        pattern = cls.term_pattern(term)
        if pattern is None:
            return []
        return [(m.start(), m.end()) for m in pattern.finditer(text)]

    @staticmethod
    def registry(names: Iterable[str]) -> tuple[tuple[str, str], ...]:
        """Every registered spelling beside its whitespace-free form.

        Prepared once per run. Squeezing inside the per-instrument scan instead
        would run the regex six million times over a full master.
        """
        return tuple((name, _SPACE.sub("", name).casefold()) for name in names)

    @classmethod
    def conflicts_for(
        cls, terms: Sequence[str], registry: Sequence[tuple[str, str]]
    ) -> tuple[str, ...]:
        """Registered names long enough to swallow one of `terms`.

        Measured on the real `corpCode.xml`: 252 of 3,961 listed names sit
        inside another listed name, and not as a prefix — `한화` is in the
        middle of `대한화섬`. A hand-kept whitelist cannot cover that, and the
        names that collide are the short well-known ones, so the damage would
        land on exactly the companies most often in the news.

        Narrowing the set to names containing one of ours keeps the cost sane:
        the quadratic scan happens once per run over the master, and the
        per-article work stays proportional to the few that actually collide.
        """
        squeezed = {_SPACE.sub("", t).casefold() for t in terms}
        squeezed.discard("")
        if not squeezed:
            return ()
        return tuple(
            name
            for name, flat in registry
            if any(len(flat) > len(t) and t in flat for t in squeezed)
        )

    @staticmethod
    def stands_alone(text: str, span: tuple[int, int], term: str) -> bool:
        """For a Latin-only name, is this an occurrence of the word itself?

        `KT` is inside `KTX`, `SK` inside `TASK`, `LG` inside `ALGO`. Latin
        script has word boundaries, so these are cheap to reject, and the
        companies with two-letter Latin names are among the most written about
        in the market.

        The test is deliberately ASCII-only: `SK는` must still match, and the
        particle is not ASCII. Names carrying any Hangul or punctuation —
        `SK하이닉스`, `KT&G` — are long or distinctive enough that this rule
        would only cost matches, so it does not apply to them.
        """
        flat = _SPACE.sub("", term)
        if not flat or not flat.isascii() or not flat.isalnum():
            return True
        start, end = span
        before = text[start - 1 : start] if start else ""
        after = text[end : end + 1]
        return not (before.isascii() and before.isalnum()) and not (
            after.isascii() and after.isalnum()
        )

    @staticmethod
    def swallowed_by(span: tuple[int, int], covers: Sequence[tuple[int, int]]) -> bool:
        """Is this match only part of a longer company's name?

        Containment, not adjacency. A rule about the neighbouring character
        cannot work in Korean, where particles attach straight to the noun:
        rejecting `한화` because `는` follows would reject `한화는`, which is
        the ordinary way to write the subject of a sentence.
        """
        start, end = span
        return any(lo <= start and hi >= end and (hi - lo) > (end - start) for lo, hi in covers)

    @classmethod
    def match_method(
        cls,
        text: str,
        *,
        name: str,
        aliases: Sequence[str] = (),
        symbol: str | None = None,
        conflicts: Sequence[str] = (),
    ) -> MatchMethod | None:
        """What, if anything, actually names the company in this text.

        A longer registered name at the same position wins. Without that rule
        `대한화섬 3분기 실적` is a mention of 한화, `LG디스플레이` is a mention
        of 레이, and the sentiment scorer that arrives next would attach both
        to the wrong company while looking entirely ordinary.

        None means the search returned the article but the article never names
        the company. That is a reject, not a mention.
        """
        covers = [span for other in conflicts for span in cls.spans(text, other)]

        for term, method in (
            (name, MatchMethod.NAME),
            *((alias, MatchMethod.ALIAS) for alias in aliases),
        ):
            for span in cls.spans(text, term):
                if cls.stands_alone(text, span, term) and not cls.swallowed_by(span, covers):
                    return method

        if symbol:
            # Digit neighbours make it a different number. Squeezing whitespace
            # out first, as this once did, turns `주가 100 5930 원` into a
            # match for 005930.
            for hit in re.finditer(re.escape(symbol), text):
                before = text[hit.start() - 1 : hit.start()] if hit.start() else ""
                after = text[hit.end() : hit.end() + 1]
                if not _DIGIT.match(before) and not _DIGIT.match(after):
                    return MatchMethod.SYMBOL
        return None

    @staticmethod
    def reject_report(yields: Sequence[Yield], *, top: int = REPORT_TOP) -> str:
        """The rollout metric: how often a search result failed to name its company.

        The plan puts the full sweep behind two smaller ones — one company, then
        ten — and gates each step on this number. A total alone cannot do that
        job. The companies worth looking at are the outliers in both
        directions: a high reject rate means the query is poor or an alias is
        missing, and many mentions with no rejects at all can mean a false
        match is passing straight through. Both lists are here for that reason.

        Rejected articles leave no mention behind, and which query fetched an
        article is recorded only on a mention, so this cannot be reconstructed
        from the database afterwards. It has to be written down now.
        """
        evaluated = sum(y.evaluated for y in yields)
        if not evaluated:
            return "no articles evaluated"
        rejected = sum(y.rejected for y in yields)
        parts = [f"reject rate {rejected}/{evaluated} ({rejected / evaluated:.0%})"]

        def label(y: Yield) -> str:
            return f"{y.name}({y.symbol})" if y.symbol else y.name

        worst = sorted(
            (y for y in yields if y.rejected),
            key=lambda y: (-y.reject_rate, -y.rejected, y.name),
        )[:top]
        if worst:
            parts.append(
                "highest reject rate: "
                + ", ".join(
                    f"{label(y)} {y.rejected}/{y.evaluated} {y.reject_rate:.0%}" for y in worst
                )
            )

        loud = sorted(
            (y for y in yields if y.matched and not y.rejected),
            key=lambda y: (-y.matched, y.name),
        )[:top]
        if loud:
            parts.append(
                "most mentions with no reject: "
                + ", ".join(f"{label(y)} {y.matched}" for y in loud)
            )
        return "; ".join(parts)

    @classmethod
    def to_rows(
        cls, items: Iterable[Mapping[str, Any]], *, since: datetime | None
    ) -> tuple[list[tuple[NewsItemRow, str]], int, bool]:
        """Convert a page into rows, plus how many were unusable.

        Returns `(rows paired with their canonical url, skipped, exhausted)`.
        `exhausted` is True once an item older than `since` appears: results
        come back newest first, so that is the end of the interesting range
        and the signal to stop paging.
        """
        rows: list[tuple[NewsItemRow, str]] = []
        skipped = 0
        exhausted = False

        for item in items:
            if not isinstance(item, Mapping):
                # One malformed entry costs that entry. Counting it as unusable
                # is what the caller already does with a missing date or link.
                skipped += 1
                continue
            published = cls.parse_pub_date(str(item.get("pubDate", "")))
            canonical = cls.canonical_url(
                originallink=str(item.get("originallink", "")),
                link=str(item.get("link", "")),
            )
            title = cls.strip_html(str(item.get("title", "")))
            if published is None or canonical is None or not title:
                skipped += 1
                continue

            if since is not None and published < since:
                exhausted = True
                continue

            # `as_text`, like every other field above it. This line was the
            # fifth instance of the same defect and the second inside this
            # function: the guard on the item was added, and the value under
            # it was not. A `link` that is not a string raises here, the sweep
            # dies before its commit, and everything gathered for every earlier
            # company in the run goes with it.
            naver_url = as_text(item, "link") or None
            rows.append(
                (
                    NewsItemRow(
                        source=NewsSource.NAVER_NEWS,
                        url_hash=cls.url_hash(canonical),
                        url=canonical,
                        naver_url=naver_url if naver_url != canonical else None,
                        publisher_host=cls.publisher_host(canonical),
                        title=title[:500],
                        summary=cls.strip_html(str(item.get("description", ""))) or None,
                        published_at=published,
                        # News names a moment, so availability is publication.
                        # The next-session rule filings use exists because a
                        # filing date names only a day.
                        available_at=published,
                    ),
                    canonical,
                )
            )

        return rows, skipped, exhausted

    # --- transport --------------------------------------------------------

    def _get(self, client: httpx.Client, *, query: str, start: int) -> dict[str, Any]:
        """One search request, reserved before it is sent.

        The reservation comes first so that a process dying mid-request cannot
        lose the count. A refused reservation raises `QuotaExhausted`, which is
        a `SkipCollection` and therefore not a failure — we decided to stop.
        """
        self._guard.reserve(QUOTA_GROUP, ENDPOINT)
        self._bucket.acquire()

        try:
            response = client.get(
                BASE,
                params={
                    "query": query,
                    "display": PAGE_SIZE,
                    "start": start,
                    # Accuracy ordering returns an arbitrary slice, which
                    # cannot be cut by time and hands back the same old
                    # articles every run.
                    "sort": "date",
                },
                headers={
                    "X-NCP-APIGW-API-KEY-ID": self._client_id,
                    "X-NCP-APIGW-API-KEY": self._client_secret,
                },
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(f"Naver news request failed: {exc}") from exc

        if response.status_code == 429:
            # The provider turning us away means our ledger and theirs
            # disagree, which is an accounting bug and has to be loud.
            raise RateLimitedError(
                "Naver refused the request as over quota, but our ledger had room. "
                "The ledger is wrong; check the budget before collecting again"
            )
        if response.status_code != 200:
            raise UpstreamUnavailableError(
                f"Naver returned {response.status_code} for a news search: {response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamUnavailableError("Naver returned non-JSON for a news search") from exc
        return as_object(payload, source="Naver news search")

    # --- collection -------------------------------------------------------

    def watermark(self, session: Session, *, now: datetime) -> datetime:
        """How far back this run reads.

        Anchored on the last run that finished everything, not the last that
        finished anything: advancing past a partial sweep would skip the range
        its unvisited instruments were never asked about.
        """
        last = CollectorStatusLookup(session).last_full_success(self.name)
        if last is None:
            return now - FIRST_RUN_LOOKBACK
        return max(
            ensure_utc(last, field="last_full_success") - WATERMARK_OVERLAP, now - MAX_LOOKBACK
        )

    def collect(self, session: Session) -> CollectionResult:
        now = utc_now()
        universe = instrument_repo.list_active(session, asof=now.date(), market=Market.KR)
        if not universe:
            # SKIPPED, not SUCCESS. Reporting success here advances the
            # watermark to now, and the state this happens in — migrated but
            # not yet seeded or mastered — is exactly the one a few minutes
            # before thousands of names arrive. Their previous three days of
            # coverage would be stepped over and never read.
            raise SkipCollection("no active Korean instruments to search")

        # Built from the whole master, and before any truncation: whether a
        # name is swallowed by a longer one is a fact about the market, not
        # about how many instruments this particular run chose to sweep. A
        # `--limit` run that matched by different rules would be useless as a
        # rehearsal for a full one.
        registry = self.registry(
            [i.name for i in universe] + [a for v in ALIASES.values() for a in v]
        )

        instruments = universe
        unasked = 0
        if self.max_instruments is not None and self.max_instruments < len(universe):
            instruments = universe[: self.max_instruments]
            unasked = len(universe) - len(instruments)

        since = self.watermark(session, now=now)
        read = saved = mentions = rejected = unusable = 0
        yields: list[Yield] = []
        capped: list[str] = []
        warnings: list[str] = []
        stopped_early: str | None = None

        with httpx.Client() as client:
            for instrument in instruments:
                symbol = instrument_repo.current_symbol(session, instrument.instrument_id)
                query = self.query_for(instrument)
                aliases = self.aliases_for(instrument)
                conflicts = self.conflicts_for((instrument.name, *aliases), registry)

                pages = self._sweep(client, query=query, since=since)
                if pages.exhausted is not None:
                    # Our own budget, mid-run. Whatever this sweep already
                    # fetched is stored below before the loop ends; the run
                    # reports that it stopped short rather than finishing.
                    stopped_early = str(pages.exhausted)

                if pages.hit_page_cap:
                    capped.append(instrument.name)
                read += pages.read
                unusable += pages.skipped

                if not pages.rows:
                    if stopped_early:
                        break
                    continue

                written, ids = news_repo.save_news_items(session, [row for row, _ in pages.rows])
                saved += written

                links: list[NewsMentionRow] = []
                matched_here = rejected_here = 0
                for row, _ in pages.rows:
                    method = self.match_method(
                        f"{row.title} {row.summary or ''}",
                        name=instrument.name,
                        aliases=aliases,
                        symbol=symbol,
                        conflicts=conflicts,
                    )
                    if method is None:
                        rejected += 1
                        rejected_here += 1
                        continue
                    matched_here += 1
                    item_id = ids.get(row.url_hash)
                    if item_id is None:
                        continue
                    links.append(
                        NewsMentionRow(
                            news_item_id=item_id,
                            instrument_id=instrument.instrument_id,
                            matched_query=query,
                            match_method=method,
                        )
                    )
                mentions += news_repo.save_mentions(session, links)
                yields.append(
                    Yield(
                        name=instrument.name,
                        symbol=symbol,
                        matched=matched_here,
                        rejected=rejected_here,
                    )
                )

                if stopped_early:
                    break

        session.commit()

        if stopped_early:
            warnings.append(stopped_early)
        if unusable:
            warnings.append(f"{unusable} items had no usable date, link or title")
        if unasked:
            # A run told to sweep one instrument did not sweep the market, and
            # SUCCESS here would move the watermark for all 2,500 names — the
            # exact silent loss `last_full_success` exists to prevent. Observed
            # once for real: a `--limit 1` rehearsal cut the next full run's
            # window from three days to fourteen hours.
            warnings.append(f"{unasked} instruments were never asked about (--limit)")
        if capped:
            # The plan called this deliberate truncation and kept it out of
            # PARTIAL, on the grounds that a daily PARTIAL for Samsung would
            # drain the word of meaning. That holds for the truncation itself
            # and fails for what follows it: the watermark is global, so a run
            # that stopped two pages short of the window still advances past
            # the articles it never read, and they do not come back. Bounded
            # cost, unbounded loss. Until a per-instrument cursor exists, the
            # run says it did not finish. The budget already pays for it — the
            # window then sits at its seven-day floor, which is the worst case
            # the call estimate was built on.
            warnings.append(f"{len(capped)} instruments hit the {self.max_pages}-page cap")

        detail = (
            f"{len(instruments)} of {len(universe)} instruments since "
            f"{since:%Y-%m-%d %H:%M}Z, {mentions} mentions, "
            f"{rejected} rejected (name absent from title and summary)"
        )
        if capped:
            detail += f"; {len(capped)} hit the {self.max_pages}-page cap"
        detail += "; " + self.reject_report(yields)

        return CollectionResult(
            items_read=read,
            items_saved=saved,
            partial=bool(warnings),
            warnings=warnings,
            detail=detail,
        )

    def _sweep(self, client: httpx.Client, *, query: str, since: datetime) -> Sweep:
        """Page one query until the results run past the watermark."""
        rows: list[tuple[NewsItemRow, str]] = []
        read = skipped = 0
        start = 1

        for _page in range(self.max_pages):
            try:
                payload = self._get(client, query=query, start=start)
            except QuotaExhausted as refused:
                return Sweep(
                    rows=rows, read=read, skipped=skipped, hit_page_cap=False, exhausted=refused
                )
            items = as_rows(payload.get("items"), source="Naver news search")
            read += len(items)

            page_rows, page_skipped, exhausted = self.to_rows(items, since=since)
            rows.extend(page_rows)
            skipped += page_skipped

            if exhausted or len(items) < PAGE_SIZE:
                return Sweep(rows=rows, read=read, skipped=skipped, hit_page_cap=False)

            start += PAGE_SIZE
            if start > MAX_START:
                return Sweep(rows=rows, read=read, skipped=skipped, hit_page_cap=True)

        return Sweep(rows=rows, read=read, skipped=skipped, hit_page_cap=self.max_pages > 0)
