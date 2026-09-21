import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { BacktestRunDetail, BacktestRunSummary, BacktestWindow } from "../api/types";
import { RunInfoDrawer } from "../components/RunInfoDrawer";
import "./Backtest.css";

/**
 * Stored backtest runs, in and out of sample side by side.
 *
 * The comparison is the screen. A rule that looks good on the months it was
 * chosen from and poor on the months it was not has said something, and the
 * only way to see that is to put the two columns next to each other.
 *
 * Two things this screen refuses to do. It will not average the gap into a
 * single "overfitting score", because no fitting happened in a fixed run and
 * the gap then means nothing at all — the banner says so rather than letting
 * the reader assume. And it will not hide the caveats: a window that abstained
 * for half its sessions produced a number that means something different from
 * one that traded throughout.
 */
export function Backtest() {
  const [runs, setRuns] = useState<BacktestRunSummary[] | null>(null);
  const [selected, setSelected] = useState<BacktestRunDetail | null>(null);
  const [showInfo, setShowInfo] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .backtests()
      .then((rows) => {
        setRuns(rows);
        if (rows.length > 0) void open(rows[0].id);
      })
      .catch((err: Error) => setError(err.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function open(id: number) {
    try {
      setSelected(await api.backtest(id));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  if (error) {
    return <p className="bt__empty">Could not load backtests: {error}</p>;
  }
  if (runs === null) {
    return <p className="bt__empty">Loading…</p>;
  }
  if (runs.length === 0) {
    return (
      <div className="bt__empty">
        <p>No backtest runs stored yet.</p>
        <code className="bt__cmd">python -m app.cli backtest run --symbol 005930</code>
      </div>
    );
  }

  return (
    <div className="bt">
      <div className="bt__list">
        {runs.map((run) => (
          <button
            key={run.id}
            className={
              selected?.id === run.id ? "bt__run bt__run--on" : "bt__run"
            }
            onClick={() => void open(run.id)}
          >
            <span className="bt__runName">
              {run.name} · {run.strategy_version}
            </span>
            <span className="bt__runMeta">
              #{run.id} · {run.period_start} — {run.period_end}
              {run.has_holdout ? " · holdout taken" : ""}
            </span>
          </button>
        ))}
      </div>

      {selected ? <RunDetail run={selected} onInfo={() => setShowInfo(true)} /> : null}

      {showInfo && selected ? (
        <RunInfoDrawer run={selected} onClose={() => setShowInfo(false)} />
      ) : null}
    </div>
  );
}

function RunDetail({
  run,
  onInfo,
}: {
  run: BacktestRunDetail;
  onInfo: () => void;
}) {
  const inSample = run.window_rows.filter((w) => w.sample_type === "IN_SAMPLE");
  const outSample = run.window_rows.filter((w) => w.sample_type === "OUT_OF_SAMPLE");
  const holdout = run.window_rows.find((w) => w.sample_type === "HOLDOUT");
  const pairs = outSample.map((out) => ({
    out,
    inn: inSample.find((i) => i.window_index === out.window_index) ?? null,
  }));

  return (
    <section className="bt__detail">
      <header className="bt__header">
        <div>
          <h2 className="bt__title">
            {run.name} <span className="bt__symbol">{run.symbol}</span>
          </h2>
          <p className="bt__sub">
            {run.strategy_kind}@{run.strategy_version} ·{" "}
            {run.period_start} — {run.period_end} · train {run.train_sessions} /
            eval {run.eval_sessions} · {run.anchored ? "anchored" : "rolling"}
          </p>
        </div>
        <button className="bt__info" onClick={onInfo}>
          Run info
        </button>
      </header>

      {run.fitter_version === null ? (
        <p className="bt__banner">
          Nothing was fitted. The same fixed rule ran on both sides of every
          split, so the in/out gap below says nothing about overfitting — there
          were no parameters to overfit. It is two periods measured the same way.
        </p>
      ) : (
        <p className="bt__banner bt__banner--fitted">
          Parameters were chosen per window by <b>{run.fitter_version}</b>, from
          the training period only. The in/out gap is meaningful here.
        </p>
      )}

      <table className="bt__table">
        <thead>
          <tr>
            <th className="bt__th">Window</th>
            <th className="bt__th">Evaluated</th>
            <th className="bt__th bt__th--num">In-sample</th>
            <th className="bt__th bt__th--num">Out-of-sample</th>
            <th className="bt__th bt__th--num">MDD</th>
            <th className="bt__th bt__th--num">Sharpe</th>
            <th className="bt__th bt__th--num">Trades</th>
            <th className="bt__th">Caveats</th>
          </tr>
        </thead>
        <tbody>
          {pairs.map(({ out, inn }) => (
            <tr key={out.window_index}>
              <td className="bt__td">#{out.window_index}</td>
              <td className="bt__td bt__td--dates">
                {out.period_start} — {out.period_end}
              </td>
              <td className="bt__td bt__td--num">{pct(inn?.total_return ?? null)}</td>
              <td className="bt__td bt__td--num">{pct(out.total_return)}</td>
              <td className="bt__td bt__td--num">{pct(out.max_drawdown)}</td>
              <td className="bt__td bt__td--num">{num(out.sharpe)}</td>
              <td className="bt__td bt__td--num">{out.trades}</td>
              <td className="bt__td">{caveats(out)}</td>
            </tr>
          ))}
          <tr className="bt__mean">
            <td className="bt__td" colSpan={2}>
              Mean
            </td>
            <td className="bt__td bt__td--num">{pct(mean(inSample))}</td>
            <td className="bt__td bt__td--num">{pct(mean(outSample))}</td>
            <td className="bt__td" colSpan={4} />
          </tr>
        </tbody>
      </table>

      <div className="bt__holdout">
        <h3 className="bt__holdoutTitle">Holdout</h3>
        {holdout ? (
          <>
            <p className="bt__holdoutNote">
              Reserved before any window was built and measured once, after the
              choices were made. Nothing above was allowed to see it.
            </p>
            <div className="bt__holdoutFigures">
              <Figure label="Period" value={`${holdout.period_start} — ${holdout.period_end}`} />
              <Figure label="Return" value={pct(holdout.total_return)} />
              <Figure label="MDD" value={pct(holdout.max_drawdown)} />
              <Figure label="Sharpe" value={num(holdout.sharpe)} />
              <Figure label="Trades" value={String(holdout.trades)} />
            </div>
          </>
        ) : (
          <p className="bt__holdoutNote">
            {run.holdout_start
              ? `Reserved ${run.holdout_start} — ${run.holdout_end} and not yet measured. It is taken once, deliberately, when the choices are made.`
              : "None reserved for this run."}
          </p>
        )}
      </div>
    </section>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div className="bt__figure">
      <span className="bt__figureLabel">{label}</span>
      <span className="bt__figureValue">{value}</span>
    </div>
  );
}

function caveats(window: BacktestWindow): string {
  const parts: string[] = [];
  if (window.abstained) parts.push(`${window.abstained} abstained`);
  if (window.without_data) parts.push(`${window.without_data} no bar`);
  if (window.unfilled) parts.push(`${window.unfilled} unfilled`);
  return parts.join(", ") || "—";
}

function mean(windows: BacktestWindow[]): number | null {
  const values = windows
    .map((w) => w.total_return)
    .filter((v): v is number => v !== null);
  if (values.length === 0) return null;
  return values.reduce((a, b) => a + b, 0) / values.length;
}

function pct(value: number | null): string {
  if (value === null) return "—";
  return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(2)}%`;
}

function num(value: number | null): string {
  return value === null ? "—" : value.toFixed(2);
}
