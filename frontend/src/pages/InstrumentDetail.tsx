import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { datetime, direction, money, percent, signed } from "../api/format";
import type { Candle, Instrument, Signal } from "../api/types";
import { movingAverage, useCandleChart } from "../hooks/useCandleChart";
import "./InstrumentDetail.css";

/**
 * The one screen where density is the point.
 *
 * Dashboard and portfolio are deliberately sparse — one number, then a summary.
 * Here the chart is the subject and the indicators belong beside it, so this
 * follows TradingView's arrangement rather than Toss's. It also defaults to
 * dark, since a chart read for any length of time is easier on a dark surface.
 *
 * The evidence list is rendered from the signal's stored reasons, never
 * composed here. If the UI wrote its own sentences they would drift from the
 * arithmetic the moment a threshold changed.
 */

const RANGES = [
  { label: "1M", bars: 22 },
  { label: "3M", bars: 66 },
  { label: "6M", bars: 130 },
  { label: "1Y", bars: 250 },
  { label: "2Y", bars: 500 },
] as const;

const MARKER: Record<string, { glyph: string; cls: string }> = {
  SUPPORTS: { glyph: "✓", cls: "supports" },
  NEUTRAL: { glyph: "△", cls: "neutral" },
  OPPOSES: { glyph: "✗", cls: "opposes" },
};

interface Props {
  instrumentId: number;
  onBack: () => void;
}

function IndicatorStrip({ signal }: { signal: Signal }) {
  const technical = signal.factors.find((f) => f.engine === "TECHNICAL");
  if (!technical) return null;

  return (
    <div className="strip">
      {technical.metrics.map((m) => (
        <div key={m.name} className="strip__item">
          <span className="strip__label">{m.name}</span>
          <span className="strip__raw num">{signed(m.raw, 2)}</span>
          <span className="strip__norm num">{m.normalized.toFixed(0)}</span>
        </div>
      ))}
    </div>
  );
}

export function InstrumentDetail({ instrumentId, onBack }: Props) {
  const [candles, setCandles] = useState<Candle[]>([]);
  const [signal, setSignal] = useState<Signal | null>(null);
  const [instrument, setInstrument] = useState<Instrument | null>(null);
  const [range, setRange] = useState<(typeof RANGES)[number]["label"]>("6M");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Dark is the default here; the rest of the app stays light.
    document.documentElement.setAttribute("data-theme", "dark");
    return () => document.documentElement.removeAttribute("data-theme");
  }, []);

  useEffect(() => {
    Promise.all([
      api.candles(instrumentId, 500),
      api.signal(instrumentId).catch(() => null),
      api.instruments(),
    ])
      .then(([c, s, list]) => {
        setCandles(c);
        setSignal(s);
        setInstrument(list.find((i) => i.instrument_id === instrumentId) ?? null);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, [instrumentId]);

  // Moving averages are computed over the *whole* series and only then sliced
  // to the visible window. Narrowing first would discard the history each
  // average needs, blanking the first 59 points of MA60 on a 6M view even
  // though those earlier bars are already loaded.
  const start = Math.max(0, candles.length - (RANGES.find((r) => r.label === range)?.bars ?? 130));

  const visible = useMemo(() => candles.slice(start), [candles, start]);

  const allMa = useMemo(() => {
    const closes = candles.map((c) => c.close);
    return { ma20: movingAverage(closes, 20), ma60: movingAverage(closes, 60) };
  }, [candles]);

  const ma20 = useMemo(() => allMa.ma20.slice(start), [allMa, start]);
  const ma60 = useMemo(() => allMa.ma60.slice(start), [allMa, start]);

  const chartRef = useCandleChart({ candles: visible, ma20, ma60, height: 400 });

  const last = visible.at(-1);
  const prev = visible.at(-2);
  const change =
    last && prev && prev.close !== 0 ? ((last.close - prev.close) / prev.close) * 100 : null;
  const currency = instrument?.currency ?? "KRW";

  return (
    <div className="detail">
      <button className="detail__back" onClick={onBack}>
        ← 목록
      </button>

      {error && <p className="detail__error">{error}</p>}

      <header className="detail__head">
        <div>
          <h1 className="detail__name">{instrument?.name ?? signal?.name ?? "—"}</h1>
          <p className="detail__symbol">
            {instrument?.symbol ?? signal?.symbol} · {instrument?.market ?? signal?.market}
          </p>
        </div>
        {last && (
          <div className="detail__price">
            <span className="num">{money(last.close, currency)}</span>
            {change !== null && (
              <span className={`num detail__delta detail__delta--${direction(change)}`}>
                {percent(change)}
              </span>
            )}
          </div>
        )}
      </header>

      <nav className="ranges" aria-label="Chart range">
        {RANGES.map((r) => (
          <button
            key={r.label}
            className={`ranges__btn ${range === r.label ? "ranges__btn--on" : ""}`}
            onClick={() => setRange(r.label)}
          >
            {r.label}
          </button>
        ))}
      </nav>

      <div className="chart" ref={chartRef} />
      <p className="chart__legend">
        <span className="chart__ma20">MA20</span>
        <span className="chart__ma60">MA60</span>
        <span className="chart__note">
          원본 시세 · 수정주가는 corporate action으로 조회 시점에 계산합니다
        </span>
      </p>

      {signal && (
        <>
          <IndicatorStrip signal={signal} />

          <section className="evidence-block">
            <h2 className="evidence-block__title">
              분석 근거 <span>규칙 기반</span>
            </h2>
            <ul>
              {signal.reasons.map((r, i) => {
                const m = MARKER[r.status] ?? MARKER.NEUTRAL;
                return (
                  <li key={i}>
                    <span className={`mark mark--${m.cls}`}>{m.glyph}</span>
                    {r.text}
                  </li>
                );
              })}
            </ul>
            {signal.reasons.length === 0 && (
              <p className="evidence-block__empty">
                {signal.abstained_reason ?? "표시할 근거가 없습니다."}
              </p>
            )}
          </section>

          <footer className="detail__clocks">
            <span>data_asof {datetime(signal.data_asof)}</span>
            <span>decision_at {datetime(signal.decision_at)}</span>
            <span>earliest exec {datetime(signal.earliest_execution_at)}</span>
          </footer>
        </>
      )}
    </div>
  );
}
