import { fundamentalMissing, judgedScore, maxScore } from "../api/listSignal";
import type { ListSignalRow } from "../api/types";
import "./ScoreParts.css";

/**
 * 08:40 점수의 네 칸: 판단 점수 · 기술 · 재무 · 합계. 서버가 저장한 값을 그대로 보이고 다시 계산하지 않는다.
 *
 * 합계 = 요인 점수 × 실제 반영 비중의 합(100점 만점). 판단 점수 = 합계 / 반영된 비중 합 — 재무가 없으면 기술 점수와 같다.
 * 판단 보류·점수 없음이면 아무것도 그리지 않는다(사유 문장은 부르는 쪽이 보인다).
 */

function weightOf(row: ListSignalRow, engine: string): number | null {
  const f = row.detail?.factors.find((x) => x.engine === engine);
  return f ? Math.round(f.requested_weight * 100) : null;
}

function Cell({ label, value, sub }: { label: string; value: string; sub?: string | null }) {
  return (
    <div className="sp__cell">
      <span className="sp__label">{label}</span>
      <span className="sp__value num">{value}</span>
      {sub && <span className="sp__sub">{sub}</span>}
    </div>
  );
}

/** 네 칸을 그릴 수 있는가: 판단 점수가 있고 채점 상세가 있을 때(판단 보류 행은 값이 0.0이라 거른다). */
export function hasScoreParts(row: ListSignalRow): boolean {
  return row.detail !== null && row.total_score !== null && judgedScore(row) !== null;
}

export function ScoreParts({ row, showJudged = true }: { row: ListSignalRow; showJudged?: boolean }) {
  const judged = judgedScore(row);
  if (!hasScoreParts(row) || judged === null || row.total_score === null) return null;
  const missing = fundamentalMissing(row);
  const tw = weightOf(row, "TECHNICAL");
  const fw = weightOf(row, "FUNDAMENTAL");
  const tech = row.technical_score;
  const fund = row.fundamental_score;

  return (
    <div className="sp">
      <div className={showJudged ? "sp__grid" : "sp__grid sp__grid--three"}>
        {showJudged && <Cell label="판단 점수" value={judged.toFixed(1)} />}
        <Cell
          label="기술"
          value={tech === null ? "–" : tech.toFixed(1)}
          sub={tw === null ? null : `비중 ${tw}%`}
        />
        <Cell
          label="재무"
          value={missing || fund === null ? "없음" : fund.toFixed(1)}
          sub={fw === null ? null : missing ? `비중 ${fw}% (반영 0)` : `비중 ${fw}%`}
        />
        <Cell label="합계" value={row.total_score.toFixed(1)} sub="/ 100" />
      </div>
      {missing && (
        <p className="sp__note">재무 자료 없음 → 판단은 기술 점수로 (합계는 최대 {maxScore(row)})</p>
      )}
    </div>
  );
}
