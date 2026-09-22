import type { BacktestRunDetail } from "../api/types";
import "./RunInfoDrawer.css";

/**
 * Every coordinate needed to reproduce a run.
 *
 * This is the difference between "I ran a backtest once" and "this result
 * came from this code, this strategy and this data snapshot". A screen that
 * shows only a return is asking to be believed; one that shows what would
 * reproduce it is asking to be checked.
 *
 * The dirty-tree warning is not decoration. A run made from uncommitted edits
 * cannot be reproduced from its commit, so the sha alone would overstate what
 * is recoverable.
 */
export function RunInfoDrawer({
  run,
  onClose,
}: {
  run: BacktestRunDetail;
  onClose: () => void;
}) {
  return (
    <div className="runinfo" role="dialog" aria-label={`Run ${run.id} information`}>
      <div className="runinfo__head">
        <div>
          <div className="runinfo__eyebrow">Run information</div>
          <h2 className="runinfo__title">Run #{run.id}</h2>
        </div>
        <button className="runinfo__close" onClick={onClose} aria-label="Close">
          ✕
        </button>
      </div>

      <Section title="Strategy">
        <Row label="Kind" value={run.strategy_kind} />
        <Row label="Version" value={run.strategy_version} />
        <Row label="Parameters" value={describeParams(run.strategy_params)} mono />
        <Row label="Fingerprint" value={run.strategy_fingerprint} mono />
        {run.fitter_version ? (
          <Row label="Fitter" value={run.fitter_version} />
        ) : null}
        <Row label="Fit trace" value={run.fit_trace_fingerprint} mono />
      </Section>

      <Section title="Code">
        <Row label="Commit" value={run.git_commit_sha} mono />
        {run.git_dirty ? (
          <p className="runinfo__warn">
            The working tree had uncommitted changes when this ran, so the commit
            alone does not describe the code that produced these numbers.
          </p>
        ) : null}
      </Section>

      <Section title="Data">
        <Row label="Snapshot" value={run.data_snapshot_at} mono />
        <Row label="Period" value={`${run.period_start} — ${run.period_end}`} />
        <Row label="Interval" value={run.interval} />
        <Row
          label="Missing sessions"
          value={
            run.require_complete_sessions
              ? "refused"
              : "accepted, marked at the last printed price"
          }
        />
        <Row
          label="Peer group"
          value={
            run.universe
              ? `${run.universe.length} instruments (#${run.universe.join(", #")})`
              : "none — fundamental ratios scored on their fixed scale"
          }
        />
      </Section>

      <Section title="Execution">
        <Row label="Model" value={run.execution_model} />
        <Row label="Starting cash" value={run.starting_cash.toLocaleString()} />
        <Row label="Commission" value={`${run.commission_bps} bp`} />
        <Row label="Slippage" value={`${run.slippage_bps} bp`} />
        <Row label="Minimum commission" value={String(run.min_commission)} />
      </Section>

      <Section title="Split">
        <Row label="Train" value={`${run.train_sessions} sessions`} />
        <Row label="Evaluate" value={`${run.eval_sessions} sessions`} />
        <Row label="Training window" value={run.anchored ? "anchored" : "rolling"} />
        <Row
          label="Holdout"
          value={
            run.holdout_start
              ? `${run.holdout_start} — ${run.holdout_end}`
              : "none reserved"
          }
        />
        <Row
          label="Holdout taken"
          value={
            run.has_holdout
              ? `yes, with ${run.holdout_strategy_fingerprint ?? "an unrecorded strategy"}`
              : "not yet"
          }
        />
      </Section>

      <Section title="Reproduce">
        <p className="runinfo__note">
          Re-runs every window from its own stored strategy, under this snapshot,
          and reports any figure that comes back different.
        </p>
        <code className="runinfo__cmd">
          python -m app.cli backtest reproduce --run {run.id}
        </code>
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="runinfo__section">
      <h3 className="runinfo__sectionTitle">{title}</h3>
      {children}
    </section>
  );
}

function Row({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="runinfo__row">
      <span className="runinfo__label">{label}</span>
      <span className={mono ? "runinfo__value runinfo__value--mono" : "runinfo__value"}>
        {value}
      </span>
    </div>
  );
}

function describeParams(params: Record<string, unknown>): string {
  const entries = Object.entries(params);
  if (entries.length === 0) return "none";
  return entries
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, value]) => `${key}=${String(value)}`)
    .join("  ");
}
