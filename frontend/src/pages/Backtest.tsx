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
    return <p className="bt__empty">백테스트를 불러오지 못했습니다: {error}</p>;
  }
  if (runs === null) {
    return <p className="bt__empty">불러오는 중…</p>;
  }
  if (runs.length === 0) {
    return (
      <div className="bt__empty">
        <p>저장된 백테스트가 아직 없습니다.</p>
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
              {run.has_holdout ? " · 홀드아웃 측정됨" : ""}
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
            {run.period_start} — {run.period_end} · 학습 {run.train_sessions} /
            평가 {run.eval_sessions}세션 · {run.anchored ? "시작 고정" : "이동 창"}
          </p>
        </div>
        <button className="bt__info" onClick={onInfo}>
          실행 정보
        </button>
      </header>

      {run.fitter_version === null ? (
        <p className="bt__banner">
          학습한 파라미터가 없습니다. 모든 구간에서 같은 고정 규칙을 양쪽에 돌렸으므로, 아래의
          표본 내/외 차이는 과최적화와 무관합니다. 맞출 파라미터가 없었기 때문입니다. 같은 방식으로 잰
          두 기간일 뿐입니다.
        </p>
      ) : (
        <p className="bt__banner bt__banner--fitted">
          파라미터는 구간마다 <b>{run.fitter_version}</b>이(가) 학습 기간만 보고 골랐습니다. 여기서는
          표본 내/외 차이에 의미가 있습니다.
        </p>
      )}

      <table className="bt__table">
        <thead>
          <tr>
            <th className="bt__th">구간</th>
            <th className="bt__th">평가 기간</th>
            <th className="bt__th bt__th--num">표본 내</th>
            <th className="bt__th bt__th--num">표본 외</th>
            <th className="bt__th bt__th--num">최대 낙폭</th>
            <th className="bt__th bt__th--num">샤프</th>
            <th className="bt__th bt__th--num">거래 수</th>
            <th className="bt__th">주의</th>
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
              평균
            </td>
            <td className="bt__td bt__td--num">{pct(mean(inSample))}</td>
            <td className="bt__td bt__td--num">{pct(mean(outSample))}</td>
            <td className="bt__td" colSpan={4} />
          </tr>
        </tbody>
      </table>

      <div className="bt__holdout">
        <h3 className="bt__holdoutTitle">홀드아웃</h3>
        {holdout ? (
          <>
            <p className="bt__holdoutNote">
              어떤 구간도 만들기 전에 떼어 두고, 선택을 다 마친 뒤 한 번만 잰 기간입니다. 위의 어느 것도
              이 기간을 보지 못했습니다.
            </p>
            <div className="bt__holdoutFigures">
              <Figure label="기간" value={`${holdout.period_start} — ${holdout.period_end}`} />
              <Figure label="수익률" value={pct(holdout.total_return)} />
              <Figure label="최대 낙폭" value={pct(holdout.max_drawdown)} />
              <Figure label="샤프" value={num(holdout.sharpe)} />
              <Figure label="거래 수" value={String(holdout.trades)} />
            </div>
          </>
        ) : (
          <p className="bt__holdoutNote">
            {run.holdout_start
              ? `${run.holdout_start} — ${run.holdout_end}을(를) 떼어 두었고 아직 재지 않았습니다. 선택을 마쳤을 때 의도적으로 한 번만 잽니다.`
              : "이 실행에는 떼어 둔 기간이 없습니다."}
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
  if (window.abstained) parts.push(`판단 보류 ${window.abstained}`);
  if (window.without_data) parts.push(`시세 없음 ${window.without_data}`);
  if (window.unfilled) parts.push(`미체결 ${window.unfilled}`);
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
