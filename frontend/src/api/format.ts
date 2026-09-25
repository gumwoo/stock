/** Display formatting. Kept in one place so a price never renders two ways. */

export function money(value: number, currency: string): string {
  if (currency === "KRW") {
    return `₩${Math.round(value).toLocaleString("ko-KR")}`;
  }
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function percent(value: number, digits = 2): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}%`;
}

export function signed(value: number, digits = 2): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}`;
}

/** Direction, for choosing a colour token. Zero is flat, not "up". */
export function direction(value: number): "up" | "down" | "flat" {
  if (value > 0) return "up";
  if (value < 0) return "down";
  return "flat";
}

/** Timestamps are UTC on the wire; show them in the reader's own zone. */
export function datetime(iso: string): string {
  return new Date(iso).toLocaleString("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function day(iso: string): string {
  return new Date(iso).toLocaleDateString("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
}

/*
 * Korean labels for the codes the API sends. The codes stay as they are on
 * the wire and in the database; only what is shown is translated, and an
 * unknown code is shown as itself rather than hidden.
 */

const METRIC_LABEL: Record<string, string> = {
  RSI: "RSI",
  "MA20 distance": "20일선 이격",
  "MA20/MA60 spread": "20일선·60일선 간격",
  "Volume z-score": "거래량 편차",
  "Bollinger position": "볼린저 밴드 위치",
  "MACD histogram": "MACD 히스토그램",
  "P/E": "PER",
  ROE: "ROE",
  "Debt ratio": "부채비율",
  "Operating margin": "영업이익률",
  "Revenue growth": "매출 성장률",
};

export function metricLabel(name: string): string {
  return METRIC_LABEL[name] ?? name;
}

const ENGINE_LABEL: Record<string, string> = {
  TECHNICAL: "기술적 분석",
  FUNDAMENTAL: "재무 분석",
  SENTIMENT: "뉴스 심리",
  PORTFOLIO: "포트폴리오",
};

export function engineLabel(engine: string): string {
  return ENGINE_LABEL[engine] ?? engine;
}

const FRESHNESS_LABEL: Record<string, string> = {
  FRESH: "최신",
  STALE: "오래됨",
  MISSING: "없음",
  UNKNOWN: "알 수 없음",
};

export function freshnessLabel(status: string): string {
  return FRESHNESS_LABEL[status] ?? status;
}

export const ACTION_LABEL: Record<string, string> = {
  BUY_INTEREST: "매수 관심",
  WATCH: "관망",
  CAUTION: "주의",
  ABSTAINED: "판단 보류",
};

const POLICY_LABEL: Record<string, string> = {
  ZERO: "없는 요인은 0점",
  RENORMALIZE: "없는 요인은 빼고 재가중",
  ABSTAIN: "필수 요인이 없으면 판단 보류",
};

export function policyLabel(policy: string): string {
  return POLICY_LABEL[policy] ?? policy;
}

const MARKET_LABEL: Record<string, string> = { KR: "한국", US: "미국" };

export function marketLabel(market: string | undefined): string {
  return market ? (MARKET_LABEL[market] ?? market) : "";
}
