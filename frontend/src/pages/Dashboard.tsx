import { useEffect, useState } from "react";
import { api } from "../api/client";
import { datetime, direction, money, percent } from "../api/format";
import type { Instrument, Signal } from "../api/types";
import { SignalFactorDrawer } from "../components/SignalFactorDrawer";
import "./Dashboard.css";

/**
 * Toss-flavoured information order: one large number, then a compact summary,
 * then today's signals, then a way into detail. Nothing competes with the
 * headline figure for attention.
 *
 * Until broker credentials exist there is no real account to value, so the
 * page says so plainly rather than inventing a portfolio. Showing a fabricated
 * balance would undermine the one thing this project is trying to be — honest
 * about what it knows.
 */

const ACTION_LABEL: Record<string, string> = {
  BUY_INTEREST: "매수 관심",
  WATCH: "관망",
  CAUTION: "주의",
  ABSTAINED: "판단 보류",
};

function ScoreBar({ score }: { score: number }) {
  return (
    <div className="scorebar" aria-hidden>
      <div className="scorebar__fill" style={{ width: `${Math.max(0, Math.min(100, score))}%` }} />
    </div>
  );
}

function SignalCard({
  signal,
  instrument,
  onOpen,
}: {
  signal: Signal;
  instrument: Instrument | undefined;
  onOpen: () => void;
}) {
  const change = instrument?.change_pct ?? null;
  const dir = change === null ? "flat" : direction(change);

  return (
    <article className="card">
      <header className="card__head">
        <div>
          <h3 className="card__name">{signal.name}</h3>
          <p className="card__symbol">
            {signal.symbol} · {signal.market}
          </p>
        </div>
        <span className={`tag tag--${signal.action.toLowerCase()}`}>
          {ACTION_LABEL[signal.action] ?? signal.action}
        </span>
      </header>

      {instrument?.last_close != null && (
        <div className="card__price">
          <span className="num">{money(instrument.last_close, instrument.currency)}</span>
          {change !== null && (
            <span className={`num delta delta--${dir}`}>{percent(change)}</span>
          )}
        </div>
      )}

      <div className="card__score">
        <div className="card__score-row">
          <span className="card__score-label">종합점수</span>
          <span className="card__score-num num">{signal.total_score.toFixed(1)}</span>
        </div>
        <ScoreBar score={signal.total_score} />
      </div>

      <ul className="card__reasons">
        {signal.reasons.slice(0, 3).map((r, i) => (
          <li key={i} className={`r--${r.status.toLowerCase()}`}>
            {r.text}
          </li>
        ))}
      </ul>

      <footer className="card__foot">
        <span className="card__timing">
          체결 가능 {datetime(signal.earliest_execution_at)}부터
        </span>
        <button className="card__more" onClick={onOpen}>
          분석 보기
        </button>
      </footer>
    </article>
  );
}

export function Dashboard() {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [open, setOpen] = useState<Signal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () =>
    Promise.all([api.signals(), api.instruments()])
      .then(([s, i]) => {
        setSignals(s);
        setInstruments(i);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));

  useEffect(() => {
    void load();
  }, []);

  const rescore = async () => {
    setBusy(true);
    try {
      await api.rescore();
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const byId = new Map(instruments.map((i) => [i.instrument_id, i]));

  return (
    <>
      <section className="lede">
        <p className="lede__label">내 포트폴리오</p>
        <p className="lede__value num">₩ —</p>
        <p className="lede__note">
          증권사 연동 전입니다. 실계좌 평가금액은 토스증권 키를 설정하면 표시됩니다.
        </p>
      </section>

      <section className="signals">
        <header className="signals__head">
          <h2 className="signals__title">오늘의 신호</h2>
          <button className="signals__action" onClick={rescore} disabled={busy}>
            {busy ? "계산 중…" : "다시 계산"}
          </button>
        </header>

        {error && <p className="signals__error">{error}</p>}

        {!error && signals.length === 0 && (
          <p className="signals__empty">
            아직 신호가 없습니다. 시세를 수집한 뒤 다시 계산해 주세요.
          </p>
        )}

        <div className="signals__grid">
          {signals.map((s) => (
            <SignalCard
              key={s.id}
              signal={s}
              instrument={byId.get(s.instrument_id)}
              onOpen={() => setOpen(s)}
            />
          ))}
        </div>
      </section>

      {open && <SignalFactorDrawer signal={open} onClose={() => setOpen(null)} />}
    </>
  );
}
