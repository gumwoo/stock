import type { Factor, Signal } from "../api/types";
import { datetime, percent, signed } from "../api/format";
import "./SignalFactorDrawer.css";

/**
 * How a score was arrived at, shown as arithmetic rather than asserted.
 *
 * This is the reason signal_factor stores what it stores. Each metric shows
 * its raw measurement and the normalized position it maps to, and each factor
 * shows requested weight beside effective weight. That pair is the important
 * one: it answers "why was Technical's contribution unusually large today?"
 * without anyone having to re-run anything. The usual answer is that some
 * other factor sat out.
 *
 * Nothing here is computed in the browser. Every number was stored by the
 * scorer, so what is read on screen and what is in the database cannot drift.
 */

interface Props {
  signal: Signal;
  onClose: () => void;
}

const MARKER: Record<string, { glyph: string; cls: string }> = {
  SUPPORTS: { glyph: "✓", cls: "supports" },
  NEUTRAL: { glyph: "△", cls: "neutral" },
  OPPOSES: { glyph: "✗", cls: "opposes" },
};

function FactorBlock({ factor }: { factor: Factor }) {
  const unavailable = factor.availability === "UNAVAILABLE";

  return (
    <section className={`factor ${unavailable ? "factor--out" : ""}`}>
      <header className="factor__head">
        <h3 className="factor__name">{factor.engine}</h3>
        {unavailable ? (
          <span className="factor__badge">UNAVAILABLE</span>
        ) : (
          <span className="factor__score num">
            {factor.score.toFixed(1)}
            <span className="factor__outof"> / 100</span>
          </span>
        )}
      </header>

      {unavailable && factor.availability_reason && (
        <p className="factor__reason">{factor.availability_reason}</p>
      )}

      {factor.metrics.length > 0 && (
        <table className="metrics">
          <thead>
            <tr>
              <th>metric</th>
              <th className="r">raw</th>
              <th className="r">normalized</th>
            </tr>
          </thead>
          <tbody>
            {factor.metrics.map((m) => (
              <tr key={m.name}>
                <td>
                  {m.name}
                  {m.detail && <span className="metrics__detail">{m.detail}</span>}
                </td>
                <td className="r num">{signed(m.raw, 3)}</td>
                <td className="r num">
                  <span className="metrics__norm">{m.normalized.toFixed(1)}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <dl className="weights">
        <div>
          <dt>requested</dt>
          <dd className="num">{(factor.requested_weight * 100).toFixed(0)}%</dd>
        </div>
        <div>
          <dt>effective</dt>
          <dd
            className={`num ${
              factor.effective_weight !== factor.requested_weight ? "weights--changed" : ""
            }`}
          >
            {(factor.effective_weight * 100).toFixed(0)}%
          </dd>
        </div>
        <div>
          <dt>contribution</dt>
          <dd className="num weights--contrib">{signed(factor.contribution, 2)}</dd>
        </div>
      </dl>

      <p className="provenance">
        <span className={`freshness freshness--${factor.freshness_status.toLowerCase()}`}>
          {factor.freshness_status}
        </span>
        {factor.source_asof && <> · data {datetime(factor.source_asof)}</>}
        {factor.source_checked_at && <> · source checked {datetime(factor.source_checked_at)}</>}
      </p>
    </section>
  );
}

export function SignalFactorDrawer({ signal, onClose }: Props) {
  const weightTotal = signal.factors.reduce((sum, f) => sum + f.effective_weight, 0);
  const shrunk = weightTotal < 0.999;

  return (
    <div className="drawer__scrim" onClick={onClose} role="presentation">
      <aside
        className="drawer"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label={`How ${signal.symbol} scored ${signal.total_score.toFixed(1)}`}
      >
        <header className="drawer__head">
          <div>
            <p className="drawer__eyebrow">How this score was reached</p>
            <h2 className="drawer__title">
              {signal.name} <span className="drawer__symbol">{signal.symbol}</span>
            </h2>
          </div>
          <button className="drawer__close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        <div className="drawer__total">
          <span className="drawer__total-num num">{signal.total_score.toFixed(1)}</span>
          <span className="drawer__total-label">{signal.action.replace("_", " ")}</span>
        </div>

        {signal.abstained_reason && (
          <p className="drawer__abstain">{signal.abstained_reason}</p>
        )}

        <div className="drawer__factors">
          {signal.factors.map((f) => (
            <FactorBlock key={f.engine} factor={f} />
          ))}
        </div>

        <div className="drawer__sum">
          <span>
            {signal.factors.map((f) => signed(f.contribution, 1)).join("  +  ")}
          </span>
          <strong className="num">= {signal.total_score.toFixed(1)}</strong>
        </div>

        {shrunk && (
          <p className="drawer__shrunk">
            Effective weight totals {percent(weightTotal * 100, 0)}, so the score is
            measured against a reduced maximum. A factor sat out.
          </p>
        )}

        {signal.reasons.length > 0 && (
          <section className="evidence">
            <h3 className="evidence__title">Evidence</h3>
            <ul>
              {signal.reasons.map((r, i) => {
                const m = MARKER[r.status] ?? MARKER.NEUTRAL;
                return (
                  <li key={i}>
                    <span className={`evidence__mark evidence__mark--${m.cls}`}>
                      {m.glyph}
                    </span>
                    {r.text}
                  </li>
                );
              })}
            </ul>
          </section>
        )}

        {/* The three clocks. Surfacing them is the point of separating them. */}
        <section className="clocks">
          <h3 className="clocks__title">Timing</h3>
          <dl>
            <div>
              <dt>data as of</dt>
              <dd className="num">{datetime(signal.data_asof)}</dd>
            </div>
            <div>
              <dt>decided at</dt>
              <dd className="num">{datetime(signal.decision_at)}</dd>
            </div>
            <div>
              <dt>earliest execution</dt>
              <dd className="num">{datetime(signal.earliest_execution_at)}</dd>
            </div>
          </dl>
          <p className="clocks__note">
            A decision made on a session's close cannot fill at that close, so the
            earliest honest fill is the next session's open. This system places no
            orders — nothing here was executed.
          </p>
        </section>

        <footer className="drawer__foot">
          strategy {signal.strategy_version} · policy {signal.policy}
        </footer>
      </aside>
    </div>
  );
}
