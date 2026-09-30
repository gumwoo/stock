import type { AnalystSummary } from "../api/types";

/**
 * 증권사 의견 한 줄(참고). KIS 종목투자의견을 목록 날 전 90일로 요약한 서버 값을 그대로 보인다.
 *
 * 앱의 판단("매수 관심")과 섞여 읽히지 않게 "증권사 의견(참고)"으로 시작하고, 괴리에는 상승·하락 색을 쓰지 않는다.
 * 조회하지 못한 종목(null)은 그리지 않는다. 조회는 됐는데 리포트가 없으면 `showNone`일 때만 "없음"을 적는다.
 */

const LABEL: Record<string, string> = { BUY: "매수", HOLD: "중립", SELL: "매도" };

function shortDay(iso: string): string {
  const [, m, d] = iso.split("-");
  return `${Number(m)}/${Number(d)}`;
}

function won(n: number): string {
  return `${n.toLocaleString("ko-KR")}원`;
}

export function analystText(a: AnalystSummary): string {
  const count = `${a.count}${a.truncated ? "건 이상" : "건"}`;
  const parts = [`최근 3개월 리포트 ${count}(${a.brokers}곳)`];
  if (a.avg_target !== null) {
    const target =
      a.target_brokers > 1 ? `평균 목표가 ${won(a.avg_target)}` : `목표가 ${won(a.avg_target)}(1곳)`;
    const upside =
      a.upside_pct === null ? "" : ` · 전일 종가 대비 ${a.upside_pct > 0 ? "+" : ""}${a.upside_pct.toFixed(1)}%`;
    parts.push(target + upside);
  }
  if (a.latest) {
    const raw = a.latest.opinion.toLowerCase().replace(/\s/g, "");
    const label = LABEL[a.latest.label] ?? (raw === "notrated" || raw === "nr" ? "의견 없음" : a.latest.opinion);
    const target = a.latest.target === null ? "" : ` ${won(a.latest.target)}`;
    parts.push(`최근 ${shortDay(a.latest.date)} ${a.latest.broker} ${label}${target}`);
  }
  if (a.raised || a.lowered) parts.push(`목표가 상향 ${a.raised} · 하향 ${a.lowered}`);
  return parts.join(" · ");
}

export function AnalystLine({
  analyst,
  showNone = false,
}: {
  analyst: AnalystSummary | null | undefined;
  showNone?: boolean;
}) {
  if (!analyst) return null;
  if (analyst.count === 0) {
    // 100행 상한에 걸려 창 안이 오지 않았으면 "없음"이라고 말할 수 없다.
    if (analyst.truncated) return null;
    return showNone ? <p className="mw__note">증권사 의견(참고): 최근 3개월 리포트 없음(KIS 기준)</p> : null;
  }
  return <p className="mw__note">증권사 의견(참고): {analystText(analyst)}</p>;
}
