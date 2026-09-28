import { useEffect, useState } from "react";
import { api } from "../api/client";
import {
  ACTION_LABEL,
  LIST_REASON_LABEL,
  PREFETCH_WARNING,
  REGIME_LABEL,
  datetime,
} from "../api/format";
import { fundamentalMissing, listRowToSignal, maxScore } from "../api/listSignal";
import type { ListSignalRow, Signal } from "../api/types";
import { dayLabel } from "../components/MorningStatus";
import { SignalFactorDrawer } from "../components/SignalFactorDrawer";
import "./Dashboard.css";

/**
 * 신호: 그날 아침 목록 종목을 08:40에 전 거래일 종가·재무로 채점한 것.
 *
 * 추적 종목 매일 채점은 2026-09-28에 멈췄다. 이 화면은 날짜를 골라 그날 목록의 점수와 근거를 본다. 점수 상세는
 * 저장된 것을 그대로 보여 주고 화면에서 다시 계산하지 않는다.
 *
 * 증권사 연동 전에는 실계좌가 없으므로 포트폴리오 숫자를 지어내지 않고 그렇다고 말한다.
 */

function ScoreBar({ score }: { score: number }) {
  return (
    <div className="scorebar" aria-hidden>
      <div className="scorebar__fill" style={{ width: `${Math.max(0, Math.min(100, score))}%` }} />
    </div>
  );
}

function SignalCard({
  row,
  onOpen,
  onChart,
  onDaily,
}: {
  row: ListSignalRow;
  onOpen: (signal: Signal) => void;
  onChart: () => void;
  onDaily: () => void;
}) {
  const signal = listRowToSignal(row);
  const noFundamental = row.detail !== null && fundamentalMissing(row);
  const warning = row.prefetch_status ? PREFETCH_WARNING[row.prefetch_status] : undefined;

  return (
    <article className="card">
      <header className="card__head">
        <div>
          <h3 className="card__name">{row.name}</h3>
          <p className="card__symbol">
            {row.code ?? "코드 없음"} · 목록 {row.rank}위
            {row.regime ? ` · ${REGIME_LABEL[row.regime] ?? row.regime}` : ""}
          </p>
        </div>
        {row.action ? (
          <span className={`tag tag--${row.action.toLowerCase()}`}>{ACTION_LABEL[row.action] ?? row.action}</span>
        ) : (
          <span className="tag tag--abstained">점수 없음</span>
        )}
      </header>

      {row.total_score !== null && (
        <div className="card__score">
          <div className="card__score-row">
            <span className="card__score-label">종합점수</span>
            <span className="card__score-num num">{row.total_score.toFixed(1)}</span>
          </div>
          <ScoreBar score={row.total_score} />
          <p className="card__parts">
            기술 {row.technical_score === null ? "–" : row.technical_score.toFixed(1)} ·{" "}
            {noFundamental
              ? `재무 없음 · 기술 점수만(최대 ${maxScore(row)})`
              : `재무 ${row.fundamental_score === null ? "–" : row.fundamental_score.toFixed(1)}`}
          </p>
        </div>
      )}

      {row.detail && row.detail.reasons.length > 0 && (
        <ul className="card__reasons">
          {row.detail.reasons.slice(0, 3).map((r, i) => (
            <li key={i} className={`r--${r.status.toLowerCase()}`}>
              {r.text}
            </li>
          ))}
        </ul>
      )}

      <p className="card__listReasons">
        {row.list_reasons.map((r) => (
          <span key={r} className="card__listReason">
            {LIST_REASON_LABEL[r] ?? r}
          </span>
        ))}
      </p>

      {warning && <p className="card__warn">{warning}</p>}
      {row.total_score === null && row.abstained_reason && (
        <p className="card__note">{row.abstained_reason}</p>
      )}

      <footer className="card__foot">
        <span className="card__timing">
          {row.evaluated_at ? `채점 ${datetime(row.evaluated_at)}` : "채점 기록 없음"}
        </span>
        <div className="card__actions">
          <button className="card__more" onClick={onDaily} aria-label={`${row.name} 일봉`}>
            일봉
          </button>
          <button
            className="card__more"
            onClick={onChart}
            disabled={!row.code}
            aria-label={`${row.name} 차트·뉴스`}
          >
            차트·뉴스
          </button>
          <button
            className="card__more"
            onClick={() => signal && onOpen(signal)}
            disabled={!signal}
            aria-label={`${row.name} 분석 보기`}
          >
            분석 보기
          </button>
        </div>
      </footer>
    </article>
  );
}

export function Dashboard({
  day,
  onDay,
  onOpenChart,
  onOpenDaily,
}: {
  day: string | null;
  onDay: (day: string) => void;
  onOpenChart: (day: string, code: string) => void;
  onOpenDaily: (row: ListSignalRow) => void;
}) {
  const [days, setDays] = useState<string[] | null>(null);
  const [rows, setRows] = useState<ListSignalRow[] | null>(null);
  const [open, setOpen] = useState<Signal | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listDays()
      .then((d) => {
        setDays(d);
        if (day === null && d.length > 0) onDay(d[0]);
      })
      .catch((e: Error) => setError(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (day === null) return;
    let alive = true;
    setRows(null);
    api
      .listSignals(day)
      .then((r) => {
        if (!alive) return;
        setRows(r);
        setError(null);
      })
      .catch((e: Error) => alive && setError(e.message));
    return () => {
      alive = false;
    };
  }, [day]);

  // 점수 높은 순. 점수가 없는 종목은 뒤로(목록 순위 순).
  const sorted = rows
    ? [...rows].sort((a, b) => {
        if (a.total_score === null && b.total_score === null) return a.rank - b.rank;
        if (a.total_score === null) return 1;
        if (b.total_score === null) return -1;
        return b.total_score - a.total_score;
      })
    : [];
  const backfilled = rows?.find((r) => r.detail?.backfilled_at)?.detail?.backfilled_at;

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
          <h2 className="signals__title">신호</h2>
          {days && days.length > 0 && (
            <select
              className="signals__day"
              aria-label="목록 날짜"
              value={day ?? ""}
              onChange={(e) => onDay(e.target.value)}
            >
              {days.map((d) => (
                <option key={d} value={d}>
                  {dayLabel(d)} 목록
                </option>
              ))}
            </select>
          )}
        </header>
        <p className="signals__lead">
          그날 아침 목록 종목을 08:40에 전 거래일 종가·재무로 채점한 것입니다. 매매 권유가 아닙니다.
          {backfilled ? ` 이 날의 점수 상세는 ${datetime(backfilled)}에 같은 입력으로 다시 계산해 채웠습니다.` : ""}
        </p>

        {error && <p className="signals__error">{error}</p>}

        {!error && days !== null && days.length === 0 && (
          <p className="signals__empty">아직 아침 목록이 없습니다. 평일 08:50에 만들어집니다.</p>
        )}
        {!error && day !== null && rows === null && <p className="signals__empty">불러오는 중…</p>}

        <div className="signals__grid">
          {day !== null &&
            sorted.map((r) => (
              <SignalCard
                key={r.member_id}
                row={r}
                onOpen={setOpen}
                onChart={() => r.code && onOpenChart(day, r.code)}
                onDaily={() => onOpenDaily(r)}
              />
            ))}
        </div>
      </section>

      {open && <SignalFactorDrawer signal={open} onClose={() => setOpen(null)} />}
    </>
  );
}
