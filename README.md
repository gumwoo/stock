# stock

Point-in-time correct stock analysis, portfolio tracking and signal generation
for Korean (KRX) and US markets.

This is an **analysis tool**. It produces signals and the evidence behind them.
It does not place orders, and it is not investment advice.

---

## What this is trying to get right

Most hobby backtesters produce impressive numbers by accident. The numbers are
impressive because the backtest quietly saw the future. This project treats that
as the main engineering problem rather than an afterthought.

**Two time axes, not one.** Every source row records both `available_at` (when
a market participant could have known it) and `ingested_at` (when our database
actually got it). Filtering on the first alone is not enough: a filing
backfilled next month has a *past* filing date, sails through the point-in-time
filter, and silently changes the result of a backtest that ran before it
arrived. Reproduce mode filters on both.

**Data is append-only, so corrections do not rewrite the past.** When a provider
restates a bar, a new revision is stored beside the original rather than
replacing it. Updating in place while holding `ingested_at` at its first-seen
value would be worse than either alternative: the row's values would come from
one date while its transaction time claimed another, and a snapshot taken
before the correction would serve the correction anyway.

**Filings are usable the next session, not the same day.** DART publishes
`rcept_dt` as `YYYYMMDD` and SEC's `filed` is a date too. Neither can
distinguish a disclosure that appeared at 06:00 from one that appeared at 14:00
— and under Regulation S-T Rule 13, anything transmitted after 17:30 ET is
deemed filed the *next* business day anyway. Since the data granularity cannot
separate these cases, the boundary is the next session's open.

**US fundamentals are reconstructed from SEC XBRL, not scraped from a snapshot.**
Each XBRL fact carries its own `filed` date and accession number, and the same
(concept, period) appears repeatedly as later filings restate it. That is what
makes "the value as known on date X" recoverable. yfinance only exposes the
latest revision, so it is a fallback for gaps, never the primary source.

**A decision cannot fill at the price that produced it.** A signal computed from
a session's close is finalised *after* that close, so the earliest honest fill
is the next session's open. Three separate timestamps — `data_asof`,
`decision_at`, `earliest_execution_at` — keep that explicit.

**Collector failure is not the same as data being unusable.** A DART collector
that failed this morning says nothing about a quarterly filing collected last
week. Availability is decided through a chain: collector health → data freshness
→ factor availability → missing-factor policy. Freshness itself is judged
differently per factor — technical against trading sessions, news against
wall-clock age, fundamentals against how recently the source was successfully
checked.

**Scores are decomposed, not asserted.** An RSI-based 80 and an ROE-based 80 are
not the same quantity, so raw metrics are normalized cross-sectionally before
weighting. Every factor stores its raw value, normalized position, requested
weight, effective weight and resulting contribution — so "why was this 59.7?" is
answerable from stored rows alone.

---

## Quick start

Nothing needs an API key to start. The dashboard runs with zero credentials
configured; each collector that lacks its key sits out and says so.

```bash
cp .env.example .env
docker compose up -d db
cd backend
python -m venv .venv && ./.venv/Scripts/python.exe -m pip install -e ".[dev]"
./.venv/Scripts/alembic.exe upgrade head
./.venv/Scripts/uvicorn.exe app.main:app --reload
```

Then open <http://localhost:8000/health/config> to see what is switched on and
which environment variable enables each thing that is not.

> The containerised database listens on **5433**, not 5432, because this machine
> already runs a local PostgreSQL 17 on the default port.

### Credentials, when you want them

Fill any subset into `.env` and restart — no code changes.

| Variable | Enables | Cost |
| --- | --- | --- |
| `TOSS_CLIENT_ID` / `_SECRET` | Live account sync, realtime quotes, KR+US orders data | Free; **your calling IP must be registered** or Toss returns 403 |
| `SEC_USER_AGENT` | US fundamentals with true point-in-time reconstruction | Free, **no API key** — just `app-name your@email`, max 10 req/s |
| `DART_API_KEY` | Korean fundamentals and filings | Free |
| `NAVER_CLIENT_ID` / `_SECRET` | Korean news + DataLab search trends | Free |
| `THREADS_ACCESS_TOKEN` / `THREADS_USER_ID` | Threads posts | Free, 2,200 queries/24h, needs Meta app review |
| `REDDIT_CLIENT_ID` / `_SECRET` | Reddit posts (main US retail sentiment source) | Free non-commercial, 100 QPM, manual approval |
| `ANTHROPIC_API_KEY` | LLM sentiment scoring | Paid; falls back to a rule-based scorer otherwise |
| `SMTP_*` or `ALERT_WEBHOOK_URL` | Alert delivery | Free; logs only without it |

X (Twitter) is deliberately absent: since its February 2026 move to pay-per-use,
post reads bill per request and full-archive search is enterprise-only.

---

## Development

```bash
cd backend && ./check.sh
```

Runs, in order: `ruff format --check`, `ruff check`, `mypy`, `lint-imports`,
`pytest`. All five must pass.

### The architecture contract is executable

Factor engines and the backtest engine are forbidden from importing SQLAlchemy,
`app.models` or `app.collectors`. This is not a code-review convention — it is
checked by `import-linter` and breaks the build:

```
Factor engines must not touch the ORM, the DB session or collectors BROKEN
app.engines is not allowed to import app.models:
-   app.engines.technical -> app.models (l.12)
```

The reason it is enforced rather than trusted: the point-in-time filter lives in
the repository layer. A `session.query(Candle)` inside an engine bypasses it and
reintroduces look-ahead bias without failing a single test.

### Known limitations

- `yfinance` ticker mapping assumes KOSPI (`.KS`). KOSDAQ needs `.KQ`, which
  means `instrument` will need a listing venue rather than just `KR`/`US`. Due
  with the historical master in Phase 2.
- The `portfolio` screen has no live account behind it until Toss credentials
  exist; it says so rather than showing an invented balance.

### Layout

```
backend/app/
  core/          pure domain — clock, trading calendar, value types. No IO.
  models/        SQLAlchemy ORM
  repositories/  the only layer allowed to query
  brokers/       Toss REST + WebSocket (read-only; no order placement)
  collectors/    external data ingestion, each isolated from the others
  engines/       technical / fundamental / sentiment / portfolio factors
  scoring/       normalize -> weight -> combine, plus availability policy
  backtest/      pit_repository, execution invariants, metrics, walk-forward
  forwardtest/   sentiment validation against realised forward returns
  api/           FastAPI routes
  worker.py      scheduler process, separate from the API
```

The API process and the worker process are separate on purpose: APScheduler
embedded in a web app fires once per uvicorn worker, so two workers means every
collector runs twice. Jobs additionally take a Postgres advisory lock.

---

## Status

**Phase 1 (in progress)** — project skeleton, toolchain, schema, instrument
identity, trading calendar, capability diagnostics.

Then: Phase 2 fundamentals (SEC EDGAR + DART) · Phase 3 backtest and
walk-forward · Phase 4 sentiment and forward-test · Phase 5 alerts, portfolio
optimisation and the remaining dashboard screens.

---

## Disclaimer

Rule-based analysis only. No orders are placed by this system. All trading
decisions and their outcomes belong to the user. Backtest results are historical
and guarantee nothing about future returns.
