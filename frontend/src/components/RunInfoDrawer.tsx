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
    <div className="runinfo" role="dialog" aria-label={`실행 ${run.id} 정보`}>
      <div className="runinfo__head">
        <div>
          <div className="runinfo__eyebrow">실행 정보</div>
          <h2 className="runinfo__title">실행 #{run.id}</h2>
        </div>
        <button className="runinfo__close" onClick={onClose} aria-label="닫기">
          ✕
        </button>
      </div>

      <Section title="전략">
        <Row label="종류" value={run.strategy_kind} />
        <Row label="버전" value={run.strategy_version} />
        <Row label="파라미터" value={describeParams(run.strategy_params)} mono />
        <Row label="지문" value={run.strategy_fingerprint} mono />
        {run.fitter_version ? (
          <Row label="학습기" value={run.fitter_version} />
        ) : null}
        <Row label="학습 기록 지문" value={run.fit_trace_fingerprint} mono />
      </Section>

      <Section title="코드">
        <Row label="커밋" value={run.git_commit_sha} mono />
        {run.git_dirty ? (
          <p className="runinfo__warn">
            실행할 때 커밋하지 않은 변경이 있었습니다. 그래서 커밋만으로는 이 숫자를 만든 코드를 다
            설명하지 못합니다.
          </p>
        ) : null}
      </Section>

      <Section title="데이터">
        <Row label="스냅샷" value={run.data_snapshot_at} mono />
        <Row label="기간" value={`${run.period_start} — ${run.period_end}`} />
        <Row label="봉 간격" value={run.interval} />
        <Row
          label="빠진 거래일"
          value={
            run.require_complete_sessions
              ? "허용하지 않음"
              : "허용, 마지막 체결가로 평가"
          }
        />
        <Row
          label="비교 종목군"
          value={
            run.universe
              ? `${run.universe.length}개 종목 (#${run.universe.join(", #")})`
              : "없음 — 재무 비율은 고정 척도로 채점"
          }
        />
      </Section>

      <Section title="체결 가정">
        <Row label="모델" value={run.execution_model} />
        <Row label="시작 자금" value={run.starting_cash.toLocaleString("ko-KR")} />
        <Row label="수수료" value={`${run.commission_bps} bp`} />
        <Row label="슬리피지" value={`${run.slippage_bps} bp`} />
        <Row label="최소 수수료" value={String(run.min_commission)} />
      </Section>

      <Section title="구간 나누기">
        <Row label="학습" value={`${run.train_sessions}세션`} />
        <Row label="평가" value={`${run.eval_sessions}세션`} />
        <Row label="학습 창" value={run.anchored ? "시작 고정" : "이동 창"} />
        <Row
          label="홀드아웃"
          value={
            run.holdout_start
              ? `${run.holdout_start} — ${run.holdout_end}`
              : "떼어 두지 않음"
          }
        />
        <Row
          label="홀드아웃 측정"
          value={
            run.has_holdout
              ? `했음, 전략 ${run.holdout_strategy_fingerprint ?? "(기록 없음)"}`
              : "아직 안 함"
          }
        />
      </Section>

      <Section title="재현">
        <p className="runinfo__note">
          구간마다 저장된 전략을 이 스냅샷 그대로 다시 돌리고, 다르게 나온 숫자를 모두 보고합니다.
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
  if (entries.length === 0) return "없음";
  return entries
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, value]) => `${key}=${String(value)}`)
    .join("  ");
}
