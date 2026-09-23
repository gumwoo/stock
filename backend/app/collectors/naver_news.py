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
import math
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, NamedTuple
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
    storable,
)
from app.collectors.quota import QuotaExhausted, QuotaGuard
from app.config import get_settings
from app.core.calendar import Market
from app.core.clock import ensure_utc, utc_now
from app.models import Instrument
from app.models.news import Decider, HitDecision, MatchMethod, NewsSource
from app.repositories import instrument_repo, news_repo
from app.repositories.news_repo import NewsItemRow, QueryHitRow

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
    # Naver splits `카페24` into `카페` and `24`, so the newest hundred results
    # are cafés and anything with a 24 in it: 96 of 100 did not name the
    # company. Measured on 2026-09-23 over one three-day window, one request
    # each: `카페24` confirmed 4, `카페24 주가` 4, `cafe24` 0, and
    # `카페24 쇼핑몰` 15 with the confirmed titles all about the company. The
    # cost is recall on stories that never say 쇼핑몰, such as a bare price
    # move. Quoting the name is not used: exact-match syntax is not part of
    # the documented API, and correctness should not rest on it.
    "카페24": "카페24 쇼핑몰",
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


def _word_ends_at(text: str, pos: int, particles: Collection[str]) -> bool:
    """Whether the word ends at `pos`, allowing one attached particle.

    Korean attaches particles on the right, so a Hangul syllable after a name
    does not by itself make it a different word — unless it is not a particle,
    or the particle itself runs on into more Hangul.
    """
    following = text[pos : pos + 1]
    if not following or not _is_hangul(following):
        return True
    after_particle = text[pos + 1 : pos + 2]
    return following in particles and not (after_particle and _is_hangul(after_particle))


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
    pending: int = 0

    @property
    def evaluated(self) -> int:
        return self.matched + self.rejected + self.pending

    @property
    def reject_rate(self) -> float:
        return self.rejected / self.evaluated if self.evaluated else 0.0

    @property
    def reject_floor(self) -> float:
        """The reject rate this sample supports with confidence, not merely shows.

        The lower bound of the Wilson interval at 95%. Ranking by the raw rate
        lets every company with one article and one reject tie at 100% and
        fill the list, and across four thousand companies there are dozens of
        those — enough to push a company rejecting ninety of a hundred off the
        bottom. One of one supports a floor near 21%; ninety of a hundred
        supports about 83%. The report still prints the raw counts; only the
        order uses this.
        """
        n = self.evaluated
        if not n:
            return 0.0
        z = 1.96
        p = self.rejected / n
        centre = p + z * z / (2 * n)
        margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
        return (centre - margin) / (1 + z * z / n)


# The relevance rule's version. Bump it whenever `judge` would reach a
# different verdict on the same text, and `rejudge` will find and re-decide
# exactly the hits an older rule decided.
RULE_VERSION = 3

# Words that put a company, rather than the ordinary word, in the sentence.
# Weak on their own — "투자" or "계약" turn up in anything — so two are needed.
CONTEXT_WORDS: tuple[str, ...] = (
    "실적",
    "매출",
    "영업이익",
    "순이익",
    "수주",
    "공시",
    "주가",
    "주식",
    "대표이사",
    "상장",
    "코스피",
    "코스닥",
    "투자",
    "계약",
    "배당",
)
WEAK_SIGNALS_NEEDED = 2

# A headline about a company leads with its name, then a comma or a subject
# particle: `원림, ESG 혁신 TF 가동`, `원림은 ...`. An article about a garden
# does not open that way. Tags like `[단독]` or `[카드]` come first and are
# skipped before the check. The particle must attach to the name and end the
# word; `원림 이야기` and `원림이야기` are not `원림이`.
TITLE_LEAD_PARTICLES = frozenset("은는이가")
# One-syllable particles that may attach to a name without making it another
# word: `㈜남성의` is 남성, `㈜남성산업` is not. Two-syllable ones (`에서`,
# `으로`) are left out on purpose; missing them only sends a hit to PENDING.
_ATTACHED_PARTICLES = frozenset("은는이가의을를와과도에로")
_TITLE_TAG = re.compile(r"^\s*(?:\[[^\]]*\]|<[^>]*>|【[^】]*】|\([^)]*\))\s*")
_CORPORATE_MARKS = ("(주)", "㈜")
# Space and the quotation marks a headline may open with, straight and curly.
_OPENING_QUOTES = " \"'" + chr(0x201C) + chr(0x2018)


class Judgement(NamedTuple):
    """The verdict on one hit, and why."""

    decision: HitDecision
    method: MatchMethod | None
    reason: str


# How many companies each list in the report names. The plan asked for the
# twenty worst; the rollout reads this after every stage.
REPORT_TOP = 20

# Column widths in `news_item`. A value wider than its column is refused by
# the database at the flush, which happens once, at the end of the sweep — so
# one over-long URL used to discard every article gathered for every company
# before it, and the run was filed as a defect in this file. The title was
# already cut to fit; the fields beside it were not.
MAX_URL = 1_000
MAX_HOST = 200
MAX_TITLE = 500


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
    # Requests actually sent. A company refused before its first one was
    # never asked, and the run header must not count it as if it were.
    requests: int = 0


class NaverNewsCollector(BaseCollector):
    """Sweeps every listed Korean company's name through the news search."""

    name = "NAVER_NEWS"

    def __init__(
        self,
        *,
        max_pages: int | None = None,
        max_instruments: int | None = None,
        only: Sequence[str] | None = None,
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
        # Company names to sweep and no others, for checking a rule change on
        # the companies that prompted it before paying for the whole market.
        # Never set in scheduled operation either, and a run using it is
        # PARTIAL for the same reason a limited one is.
        self.only = frozenset(only) if only else None

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
        text = _SPACE.sub(" ", html.unescape(_TAG.sub("", raw))).strip()
        # Neither a lone surrogate nor NUL can be written to the database, and
        # the refusal comes at the flush that saves the whole sweep. In prose
        # each is a lost character: the surrogate becomes a replacement
        # character and NUL is dropped. A URL gets no such repair — see
        # `canonical_url`.
        text = text.replace(chr(0), "")
        return text.encode("utf-8", "replace").decode("utf-8")

    @staticmethod
    def parse_pub_date(raw: str) -> datetime | None:
        """RFC 1123 with an offset, e.g. `Mon, 21 Sep 2026 14:03:00 +0900`.

        Returns None rather than raising: one unparseable row should cost that
        row, not the sweep. The caller counts what it skipped.
        """
        # Parsing and the conversion to UTC are guarded together. The first
        # fix wrapped only the parse, and the overflow a year like 9999 causes
        # comes from the conversion on the next line — the guard one line
        # above the value that breaks, which is the shape this file kept
        # repeating. A test that fed it the value is what showed it.
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed is None or parsed.tzinfo is None:
                # A naive timestamp would have to be given a zone by guessing,
                # and a guessed zone on an availability column is a silent
                # nine-hour error. Better to drop the row and count it.
                return None
            return ensure_utc(parsed, field="pubDate")
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def canonical_url(*, originallink: str, link: str) -> str | None:
        """One spelling per article, so the same piece hashes to one row.

        Prefers the publisher's own URL; Naver's mirror is a location, not an
        identity, and the same article reached through both is one article.
        """
        chosen = (originallink or "").strip() or (link or "").strip()
        if not chosen:
            return None
        if not storable(chosen):
            # A URL is an identity, not prose: one that cannot be encoded
            # cannot be hashed or stored, and there is no honest repair.
            return None

        try:
            parts = urlsplit(chosen)
            if not parts.netloc:
                return None
            query = sorted(
                (k, v)
                for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k.lower() not in TRACKING_PARAMS
            )
        except ValueError:
            # `urlsplit` raises for a malformed bracketed host and for a
            # netloc that changes under NFKC normalisation. Either way the
            # article has no usable address, which is what None means here.
            return None

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

        if symbol and cls.symbol_present(text, symbol):
            return MatchMethod.SYMBOL
        return None

    @staticmethod
    def symbol_present(text: str, symbol: str) -> bool:
        """The six-digit code standing on its own, not inside a longer number.

        Digit neighbours make it a different number. Squeezing whitespace out
        first, as this once did, turns `주가 100 5930 원` into a match for
        005930.
        """
        for hit in re.finditer(re.escape(symbol), text):
            before = text[hit.start() - 1 : hit.start()] if hit.start() else ""
            after = text[hit.end() : hit.end() + 1]
            if not _DIGIT.match(before) and not _DIGIT.match(after):
                return True
        return False

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
        pending = sum(y.pending for y in yields)
        parts = [
            f"reject rate {rejected}/{evaluated} ({rejected / evaluated:.0%}), "
            f"pending {pending}/{evaluated} ({pending / evaluated:.0%})"
        ]

        def label(y: Yield) -> str:
            return f"{y.name}({y.symbol})" if y.symbol else y.name

        worst = sorted(
            (y for y in yields if y.rejected),
            key=lambda y: (-y.reject_floor, -y.rejected, y.name),
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

        # The companies whose name appeared and the rule could not tell whether
        # the article meant them. This list is where the next rule, or the
        # model pass, earns its keep.
        undecided = sorted(
            (y for y in yields if y.pending),
            key=lambda y: (-y.pending, y.name),
        )[:top]
        if undecided:
            parts.append(
                "most pending: "
                + ", ".join(f"{label(y)} {y.pending}/{y.evaluated}" for y in undecided)
            )
        return "; ".join(parts)

    @staticmethod
    def requires_context(name: str) -> bool:
        """Whether seeing this name is not enough to know the company is meant.

        Two Hangul syllables and nothing else: 193 of the 2,648 listed Korean
        names, among them 남성, 노을, 나노 and 원림 — words that turn up in
        ordinary prose. The first rollout found seven of 원림's eighteen
        accepted articles were about gardens. Three syllables is the next
        candidate and is deliberately not included yet; the full sweep's
        pending and no-reject lists are what should decide it.

        A rule, not a list: a list covers only the names somebody noticed.
        """
        flat = _SPACE.sub("", name)
        return len(flat) == 2 and all(_is_hangul(ch) for ch in flat)

    @classmethod
    def strong_signal(cls, title: str, text: str, *, name: str, symbol: str | None) -> str | None:
        """One piece of evidence that the company, not the word, is meant."""
        if symbol and cls.symbol_present(text, symbol):
            return "symbol"

        headline = title
        while True:
            stripped = _TITLE_TAG.sub("", headline, count=1)
            if stripped == headline:
                break
            headline = stripped
        headline = headline.lstrip(_OPENING_QUOTES)
        pattern = cls.term_pattern(name)
        if pattern is None:
            return None

        lead = pattern.match(headline)
        if lead is not None:
            rest = headline[lead.end() :]
            # A comma may sit after a space; a particle may not. The first
            # version stripped spaces before looking, so `원림 이야기` read as
            # `원림이` and a garden article was confirmed — the very case this
            # rule exists to hold back. `남성 가수` and `나노 이하` went the same way.
            if rest.lstrip().startswith(","):
                return "title_lead"
            if rest[:1] in TITLE_LEAD_PARTICLES and not _is_hangul(rest[1:2] or " "):
                return "title_lead"

        # `(주)원림`, `원림㈜` — with the name ending, or starting, where the mark
        # says it does. Comparing whitespace-free text confirmed 남성 on
        # `㈜남성산업`, a different firm whose name merely begins the same way.
        # No space either side: across one the mark belongs to a neighbour, as
        # in `지원 대상 (주)한빛` or `삼성전자㈜ 남성 임원`.
        for mark in _CORPORATE_MARKS:
            escaped = re.escape(mark)
            for hit in re.finditer(escaped + "(?:" + pattern.pattern + ")", text, re.IGNORECASE):
                if _word_ends_at(text, hit.end(), _ATTACHED_PARTICLES):
                    return "corporate_mark"
            for hit in re.finditer("(?:" + pattern.pattern + ")" + escaped, text, re.IGNORECASE):
                if not _is_hangul(text[hit.start() - 1 : hit.start()] or " "):
                    return "corporate_mark"
        return None

    @classmethod
    def stands_as_a_word(
        cls, text: str, name: str, conflicts: Sequence[str], *, whole: bool = False
    ) -> bool:
        """Whether the name occurs at the start of a word, not inside another.

        The left edge always. Nothing attaches in front of a company name, so a
        Hangul syllable there means the match is the middle of some other word:
        `상보` inside `예상보다`, `레이` inside `리레이팅`. Two context words
        nearby were enough to confirm those.

        The right edge only when `whole`. Particles attach there — `원림은`,
        `원림이` — so a following syllable proves nothing by itself; it does
        when it is not a particle. `태양광`, `동서발전`, `삼일회계법인` and
        `배럴당` start with a listed name and are other words, and in a
        financial article they come with context words of their own.
        """
        covers = [span for other in conflicts for span in cls.spans(text, other)]
        for start, end in cls.spans(text, name):
            if cls.swallowed_by((start, end), covers):
                continue
            if _is_hangul(text[start - 1 : start] or " "):
                continue
            if not whole or _word_ends_at(text, end, _ATTACHED_PARTICLES):
                return True
        return False

    @staticmethod
    def weak_signals(text: str) -> list[str]:
        """Company-context words present, in the order they are listed."""
        return [word for word in CONTEXT_WORDS if word in text]

    @classmethod
    def judge(
        cls,
        title: str,
        summary: str,
        *,
        name: str,
        aliases: Sequence[str] = (),
        symbol: str | None = None,
        conflicts: Sequence[str] = (),
    ) -> Judgement:
        """CONFIRMED, PENDING or REJECTED, with the reason recorded.

        REJECTED when the company is not named at all. CONFIRMED when it is
        named and the name cannot be mistaken for an ordinary word, or when it
        can but the text says a company is meant: one strong signal, or two
        weak ones. PENDING otherwise — named, and undecided. Kept rather than
        dropped, so a model can decide it later.
        """
        text = f"{title} {summary}"
        method = cls.match_method(
            text, name=name, aliases=aliases, symbol=symbol, conflicts=conflicts
        )
        absent = "absent"
        if (
            method is MatchMethod.NAME
            and cls.requires_context(name)
            and not cls.stands_as_a_word(text, name, conflicts)
        ):
            # The two syllables appear only inside other words, so the name
            # was never there. An alias or the stock code may still be.
            method = cls.match_method(
                text, name="", aliases=aliases, symbol=symbol, conflicts=conflicts
            )
            absent = "absent:inside_word"
        if method is None:
            return Judgement(HitDecision.REJECTED, None, absent)
        if method is MatchMethod.SYMBOL:
            return Judgement(HitDecision.CONFIRMED, method, "strong:symbol")
        if method is MatchMethod.ALIAS or not cls.requires_context(name):
            return Judgement(HitDecision.CONFIRMED, method, method.value.lower())

        strong = cls.strong_signal(title, text, name=name, symbol=symbol)
        if strong is not None:
            return Judgement(HitDecision.CONFIRMED, method, f"strong:{strong}")
        weak = cls.weak_signals(text)
        if len(weak) >= WEAK_SIGNALS_NEEDED:
            # Context words say the article is financial, not which company it
            # is about, so the name itself must be a word here and not the front
            # of a compound.
            if cls.stands_as_a_word(text, name, conflicts, whole=True):
                return Judgement(HitDecision.CONFIRMED, method, "weak:" + "+".join(weak[:3]))
            return Judgement(HitDecision.PENDING, method, "context:compound")
        return Judgement(HitDecision.PENDING, method, "context:" + (weak[0] if weak else "none"))

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
            if published is None or canonical is None or not title or len(canonical) > MAX_URL:
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
            if naver_url is not None and len(naver_url) > MAX_URL:
                # The mirror is a location, not the article's identity, so an
                # unusable one costs the link and keeps the article. Text the
                # database cannot hold never gets this far: `as_text` already
                # returned nothing for it.
                naver_url = None
            host = cls.publisher_host(canonical)
            if host is not None and len(host) > MAX_HOST:
                host = None
            rows.append(
                (
                    NewsItemRow(
                        source=NewsSource.NAVER_NEWS,
                        url_hash=cls.url_hash(canonical),
                        url=canonical,
                        naver_url=naver_url if naver_url != canonical else None,
                        publisher_host=host,
                        title=title[:MAX_TITLE],
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
        if self.only is not None:
            instruments = [i for i in universe if i.name in self.only]
        elif self.max_instruments is not None and self.max_instruments < len(universe):
            instruments = universe[: self.max_instruments]

        since = self.watermark(session, now=now)
        read = saved = mentions = rejected = pending = unusable = 0
        yields: list[Yield] = []
        asked = 0
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
                if pages.requests:
                    asked += 1
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

                hits: list[QueryHitRow] = []
                matched_here = rejected_here = pending_here = 0
                for row, _ in pages.rows:
                    verdict = self.judge(
                        row.title,
                        row.summary or "",
                        name=instrument.name,
                        aliases=aliases,
                        symbol=symbol,
                        conflicts=conflicts,
                    )
                    if verdict.decision is HitDecision.REJECTED:
                        rejected += 1
                        rejected_here += 1
                    elif verdict.decision is HitDecision.PENDING:
                        pending += 1
                        pending_here += 1
                    else:
                        matched_here += 1
                    item_id = ids.get(row.url_hash)
                    if item_id is None:
                        continue
                    hits.append(
                        QueryHitRow(
                            news_item_id=item_id,
                            instrument_id=instrument.instrument_id,
                            matched_query=query,
                            decision=verdict.decision,
                            decision_reason=verdict.reason,
                            match_method=verdict.method,
                            snippet=row.summary,
                            rule_version=RULE_VERSION,
                            decided_by=Decider.RULE,
                            decided_at=now,
                        )
                    )
                mentions += news_repo.record_hits(session, hits).mentions_added
                yields.append(
                    Yield(
                        name=instrument.name,
                        symbol=symbol,
                        matched=matched_here,
                        rejected=rejected_here,
                        pending=pending_here,
                    )
                )

                if stopped_early:
                    break

        session.commit()

        # Everything not asked, for whatever reason: a `--limit` or a budget
        # that ran out partway. The header used to count the companies the run
        # meant to ask, so a sweep stopped after twenty-five said sixty.
        unasked = len(universe) - asked

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
            warnings.append(f"{unasked} instruments were never asked about")
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
            f"{asked} of {len(universe)} instruments since "
            f"{since:%Y-%m-%d %H:%M}Z, {mentions} mentions, "
            f"{rejected} rejected (name absent from title and summary), "
            f"{pending} pending (name present, company not evident)"
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
        read = skipped = sent = 0
        start = 1

        for _page in range(self.max_pages):
            try:
                payload = self._get(client, query=query, start=start)
            except QuotaExhausted as refused:
                return Sweep(
                    rows=rows,
                    read=read,
                    skipped=skipped,
                    hit_page_cap=False,
                    exhausted=refused,
                    requests=sent,
                )
            sent += 1
            items = as_rows(payload.get("items"), source="Naver news search")
            read += len(items)

            page_rows, page_skipped, exhausted = self.to_rows(items, since=since)
            rows.extend(page_rows)
            skipped += page_skipped

            if exhausted or len(items) < PAGE_SIZE:
                return Sweep(
                    rows=rows, read=read, skipped=skipped, hit_page_cap=False, requests=sent
                )

            start += PAGE_SIZE
            if start > MAX_START:
                return Sweep(
                    rows=rows, read=read, skipped=skipped, hit_page_cap=True, requests=sent
                )

        return Sweep(
            rows=rows,
            read=read,
            skipped=skipped,
            hit_page_cap=self.max_pages > 0,
            requests=sent,
        )


class RejudgeResult(NamedTuple):
    """What re-deciding stored hits under the current rule changed."""

    judged: int
    confirmed: int
    pending: int
    rejected: int
    mentions_added: int
    mentions_removed: int
    skipped: int
    # Stored before hits kept their own snippet. What those verdicts read is
    # unknown, so they are left as they are rather than judged on other text.
    unread: int = 0


def rejudge_hits(
    session: Session, *, instrument_ids: Collection[int] | None = None
) -> RejudgeResult:
    """Re-decide every RULE verdict an older rule reached, from what it read.

    No request is made: the title is on `news_item`, and the snippet the
    verdict read is on the hit. Not `news_item.summary` — that is the snippet
    of whichever search stored the article first, and judging another
    company's hit by it turned a confirmed 삼성전자 hit into "absent" the
    moment the rule version moved. Hits with no snippet are counted as
    `unread` and left alone. The whole instrument
    master is loaded for the same reason `collect` loads it — whether a name is
    swallowed by a longer one depends on every registered name, not on the
    handful being re-judged.

    Hits whose company is no longer in the Korean universe are counted as
    skipped and left as they are.
    """
    stale = news_repo.rule_hits_before(
        session, rule_version=RULE_VERSION, instrument_ids=instrument_ids
    )
    if not stale:
        return RejudgeResult(0, 0, 0, 0, 0, 0, 0)

    now = utc_now()
    universe = instrument_repo.list_active(session, asof=now.date(), market=Market.KR)
    by_id = {i.instrument_id: i for i in universe}
    registry = NaverNewsCollector.registry(
        [i.name for i in universe] + [a for v in ALIASES.values() for a in v]
    )

    context: dict[int, tuple[tuple[str, ...], str | None, tuple[str, ...]]] = {}
    rows: list[QueryHitRow] = []
    tally = {HitDecision.CONFIRMED: 0, HitDecision.PENDING: 0, HitDecision.REJECTED: 0}
    skipped = 0

    unread = 0
    for hit in stale:
        if hit.snippet is None:
            unread += 1
            continue
        instrument = by_id.get(hit.instrument_id)
        if instrument is None:
            skipped += 1
            continue
        if hit.instrument_id not in context:
            aliases = NaverNewsCollector.aliases_for(instrument)
            context[hit.instrument_id] = (
                aliases,
                instrument_repo.current_symbol(session, instrument.instrument_id),
                NaverNewsCollector.conflicts_for((instrument.name, *aliases), registry),
            )
        aliases, symbol, conflicts = context[hit.instrument_id]

        verdict = NaverNewsCollector.judge(
            hit.title,
            hit.snippet,
            name=instrument.name,
            aliases=aliases,
            symbol=symbol,
            conflicts=conflicts,
        )
        tally[verdict.decision] += 1
        rows.append(
            QueryHitRow(
                news_item_id=hit.news_item_id,
                instrument_id=hit.instrument_id,
                matched_query=hit.matched_query,
                decision=verdict.decision,
                decision_reason=verdict.reason,
                match_method=verdict.method,
                snippet=hit.snippet,
                rule_version=RULE_VERSION,
                decided_by=Decider.RULE,
                decided_at=now,
            )
        )

    written = news_repo.record_hits(session, rows)
    session.commit()
    return RejudgeResult(
        judged=len(rows),
        confirmed=tally[HitDecision.CONFIRMED],
        pending=tally[HitDecision.PENDING],
        rejected=tally[HitDecision.REJECTED],
        mentions_added=written.mentions_added,
        mentions_removed=written.mentions_removed,
        skipped=skipped,
        unread=unread,
    )
