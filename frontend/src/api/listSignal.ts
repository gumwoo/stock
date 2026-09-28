import type { ListSignalRow, Signal } from "./types";

/**
 * 목록 행의 채점 상세를 분석 서랍·일봉 화면이 읽는 Signal 모양으로. 상세가 없으면(채점 못 함) null.
 *
 * 판단 시각은 행의 `evaluated_at`(08:40)이다. 상세의 `decision_at`은 채점기 관례상 가격 데이터 시각(직전 종가)이다.
 * 점수 보류 사유는 행의 것(재무 사유까지 담긴다)을 쓴다.
 */
export function listRowToSignal(row: ListSignalRow): Signal | null {
  const d = row.detail;
  if (!d) return null;
  return {
    id: row.member_id,
    instrument_id: row.instrument_id,
    symbol: row.code ?? "",
    name: row.name,
    market: "KR",
    total_score: d.total_score,
    action: d.action,
    data_asof: d.data_asof,
    decision_at: row.evaluated_at ?? d.decision_at,
    earliest_execution_at: d.earliest_execution_at,
    strategy_version: d.strategy_version,
    policy: d.policy,
    abstained_reason: row.abstained_reason,
    factors: d.factors,
    reasons: d.reasons,
  };
}

/** 재무 요인을 쓰지 못했는가(행의 재무 점수 0.0은 "없음"을 뜻하지 않는다). */
export function fundamentalMissing(row: ListSignalRow): boolean {
  const f = row.detail?.factors.find((x) => x.engine === "FUNDAMENTAL");
  return !f || f.availability === "UNAVAILABLE" || f.effective_weight === 0;
}

/** 쓸 수 있던 요인 가중치로 낼 수 있는 최고 점수(재무가 빠지면 기술 가중치만큼). */
export function maxScore(row: ListSignalRow): number {
  const w = row.detail?.factors.reduce((sum, f) => sum + f.effective_weight, 0) ?? 1;
  return Math.round(w * 100);
}
