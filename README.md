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

**A bar is not knowable until it closes.** A daily bar carries a close, a high,
a low and a volume, none of which exist while the session is still running. So
`ts` (bar open) and `available_at` (bar complete) are separate columns, and
simulations filter on the second. Filtering on the first would hand a decision
made at 10:00 that day's closing price.

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
| `DART_API_KEY` | Korean fundamentals and filings | Free; note it travels in the query string, so request-URL logging is suppressed by default |
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

The same five run in GitHub Actions on every push and pull request, against a
real Postgres service, alongside a frontend typecheck and build — so the claim
above is checked rather than asserted.

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

### Backtests

```bash
python -m app.cli collect --source yfinance --period 10y          # prices
python -m app.cli collect --source dart --period 10y              # and filings, same reach
python -m app.cli backtest run --symbol 005930 --strategy score   # the system's own rule
python -m app.cli backtest run --symbol 005930     # default: a moving-average harness
python -m app.cli backtest show --run 1            # what it recorded
python -m app.cli backtest holdout --run 1         # the final measurement, once
python -m app.cli backtest reproduce --run 1       # run it again and compare
```

`run` deliberately stops short of the holdout. A holdout reported on every run
gets fitted by eye, which is harder to notice than fitting it in code and no
less real, so taking it is a separate command. `--with-holdout` exists for the
case where the choices are already made, and for fitted runs, whose fitter is
code and cannot be rebuilt from a stored row afterwards.

`reproduce` exits non-zero when anything differs, so it works in a check.

The Backtest screen shows the same runs with in-sample and out-of-sample in
adjacent columns, and **Run info** opens every coordinate a reproduction would
need — strategy, fingerprints, commit, data snapshot, costs and split.

- With only the technical factor participating, the current policy cannot
  reach BUY_INTEREST. The threshold scales to the participating weight — 70 at
  full weight becomes 42 at technical's 0.6 — which needs a technical score of
  70, and the engine tops out near 62 on the strongest trend that can be
  constructed. An instrument with no filings can therefore hold or exit but
  never enter. The thresholds are deliberate and BUY_INTEREST is meant to be
  rare, but in practice the rule is gated on having financials at all. Pinned
  as a test so a change to a weight or threshold fails loudly rather than
  silently altering what the system can say.
- A run is refused when the period reaches back before the filings do, which
  is why `--period` applies to both collectors. The first ten-year Samsung run
  was made on ten years of prices and five years of DART filings, because the
  collector's default reaches back five. For six and a half of those years the
  fundamental factor stood down and the rule could hold or exit but never
  enter, and it returned +608% as though that were a verdict on the strategy.
  Nothing failed and nothing warned; the number simply looked plausible.

#### What it measures, on the two instruments collected

Ten years to 2026-09-18, full fundamental coverage, 5bp commission and 5bp
slippage, next-open fills.

| | | total return | MDD | Sharpe | trades |
|---|---|---:|---:|---:|---:|
| 삼성전자 | score 70/35 | +475.53% | -42.15% | 0.77 | 8 |
| | buy-and-hold | +714.38% | -45.16% | 0.79 | 0 |
| Apple | score 70/35 | +184.17% | -33.62% | 0.54 | 3 |
| | buy-and-hold | +1073.22% | -38.70% | 0.99 | 0 |

**The rule underperforms buying and holding on both, on every measure except a
slightly shallower drawdown.** Two instruments over one decade is not a verdict
on the strategy, and the decade is one long bull market in both — the sample a
buy-and-hold benchmark wins by construction. But it is the measurement the
harness produces, and reporting it is the point of having built the harness
this way. The corrected Samsung figure is also 133 percentage points below the
one the same code produced before the coverage check existed.

### Known limitations

- `yfinance` ticker mapping assumes KOSPI (`.KS`). KOSDAQ needs `.KQ`, which
  means `instrument` will need a listing venue rather than just `KR`/`US`. Due
  with the historical master in Phase 2.
- Korean fiscal periods are reconstructed from the filer's *current* fiscal
  year-end month (`acc_mt` from `company.json`), applied to every year we
  collect. A company that changed its closing month in the last few years will
  therefore have its older periods reconstructed against the new calendar. The
  fix needs a historical fiscal calendar, which DART does not expose directly;
  due with the Korean universe expansion, alongside `max_gap_days` becoming a
  fiscal-calendar policy rather than a fixed 430 days.
- Three sessions in Samsung's two-year history have no bar although
  `exchange_calendars` (4.13.2) says KRX traded: 2025-09-19, 2026-06-03 and
  2026-07-17. What is observed is the disagreement; the cause is not
  established. Two have plausible explanations — the first Wednesday of June
  in a local-election year, and 제헌절 — which would make them holes in the
  calendar's holiday data rather than in our collection, but the third has
  none, and an overlay built from inference would encode guesses as exchange
  facts. Backtests therefore refuse such sessions by default and name them;
  `require_complete_sessions=False` accepts them, marking each at the last
  price that printed. Resolving it properly needs a newer calendar release or
  KRX's own holiday record, which is due with the Korean universe expansion.
- DART fundamentals request consolidated statements (`fs_div=CFS`) only. A
  company that files no consolidated statements therefore yields no facts at
  all, which the absence logic correctly reports as
  `NO_OBSERVATION_IN_SOURCE` — the register shows the report, our value source
  holds nothing from it. Supporting them needs an `OFS` fallback, due when the
  universe grows beyond the two instruments in use now.
- The `portfolio` screen has no live account behind it until Toss credentials
  exist; it says so rather than showing an invented balance.
- The instrument detail screen forces dark mode and clears the attribute on
  exit. Once a user-selectable theme exists this must save and restore the
  previous value instead.
- Chart colours are read from CSS variables once at mount, so changing the
  theme or the up/down convention while a chart is open will not recolour it
  until remount. Due with the settings screen.

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

**Phase 1 complete** — a vertical slice runs end to end with no credentials
configured: watchlist, daily bars and FX from yfinance, the technical factor
engine, signal assembly with all three clocks enforced, and a dashboard whose
drawer shows raw → normalized → weight → contribution for every metric.

**Phase 2 complete.**

| | |
| --- | --- |
| SEC EDGAR point-in-time reconstruction | done |
| Filing register (proves what was published, not just what we tagged) | done |
| Fundamental engine, anchored to one fiscal period | done |
| Base layer scoring: technical 0.6 + fundamental 0.4 | done |
| DART collector | done |
| Korean fundamentals | done |

Then: Phase 3 backtest and walk-forward · Phase 4 event overlay, market regime
and forward-test · Phase 5 alerts, portfolio optimisation and the remaining
screens.

Note that sentiment is no longer a weighted factor. It became an event overlay
with its own half-life, sitting above the base score rather than inside it —
see the design note on the three layers.

---

## Disclaimer

Rule-based analysis only. No orders are placed by this system. All trading
decisions and their outcomes belong to the user. Backtest results are historical
and guarantee nothing about future returns.
