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

// --- 백테스트·전략 표기. DB·API 값은 재현에 쓰는 키라 그대로 두고 화면에서만 한국어로 보인다. 모르는 값은 그대로.

const STRATEGY_KIND_LABEL: Record<string, string> = {
  technical_fundamental: "기술·재무 점수",
  moving_average_cross: "이동평균 교차",
  buy_and_hold: "단순 보유",
};

export function strategyKindLabel(kind: string): string {
  return STRATEGY_KIND_LABEL[kind] ?? kind;
}

const STRATEGY_VERSION_LABEL: Record<string, string> = {
  "v0.3-cross-sectional-fundamental": "v0.3 재무 비교군 순위",
  "v0.2-technical-fundamental": "v0.2 기술·재무",
  "v0.1-technical": "v0.1 기술",
  "buy-and-hold@v1": "단순 보유 v1",
};

/** 버전 키를 화면 이름으로. 백테스트는 끝에 "+매수관심/주의" 기준을 붙인다(backtest/strategies.py). */
export function strategyVersionLabel(version: string): string {
  // 첫 "+"에서만 자른다. 뒤가 정확히 "숫자/숫자"가 아니면(임의 --version 값) 원래 키를 그대로 보여 서로 다른 키가
  // 화면에서 같아지지 않게 한다.
  const plus = version.indexOf("+");
  const base = plus < 0 ? version : version.slice(0, plus);
  const thresholds = plus < 0 ? null : version.slice(plus + 1);
  const ma = /^ma-(\d+)-(\d+)@(v\d+)$/.exec(base);
  const name = ma ? `이동평균 교차 ${ma[1]}/${ma[2]}일 ${ma[3]}` : STRATEGY_VERSION_LABEL[base];
  if (name === undefined) return version;
  if (thresholds === null) return name;
  const cut = /^(\d+(?:\.\d+)?)\/(\d+(?:\.\d+)?)$/.exec(thresholds);
  return cut ? `${name} · 매수 관심 ${cut[1]} / 주의 ${cut[2]}` : version;
}

/** 종류와 버전을 한 줄로. 버전 이름이 이미 종류로 시작하면(단순 보유, 이동평균 교차) 종류를 되풀이하지 않는다. */
export function strategyLabel(kind: string, version: string): string {
  const k = strategyKindLabel(kind);
  const v = strategyVersionLabel(version);
  return v.startsWith(k) ? v : `${k} ${v}`;
}

const PARAM_LABEL: Record<string, string> = {
  buy_interest: "매수 관심",
  caution: "주의",
  currency: "통화",
  short: "짧은 이평",
  long: "긴 이평",
};
const CURRENCY_LABEL: Record<string, string> = { KRW: "원", USD: "달러" };

export function paramsLabel(params: Record<string, unknown>): string {
  const entries = Object.entries(params);
  if (entries.length === 0) return "없음";
  return entries
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, value]) => {
      const shown = key === "currency" ? (CURRENCY_LABEL[String(value)] ?? String(value)) : String(value);
      return PARAM_LABEL[key] ? `${PARAM_LABEL[key]} ${shown}` : `${key}=${shown}`;
    })
    .join(" · ");
}

const EXECUTION_MODEL_LABEL: Record<string, string> = {
  NEXT_OPEN: "다음 세션 시가",
  NEXT_BAR: "다음 봉 시작",
};

export function executionModelLabel(model: string): string {
  return EXECUTION_MODEL_LABEL[model] ?? model;
}

// API는 값("1d")을 준다. 이름(DAY_1)도 받는다.
const INTERVAL_LABEL: Record<string, string> = {
  "1d": "일봉",
  DAY_1: "일봉",
  "1m": "1분봉",
  MIN_1: "1분봉",
};

export function intervalLabel(interval: string): string {
  return INTERVAL_LABEL[interval] ?? interval;
}

/** 베이시스포인트를 퍼센트로: 5 → "0.05%". */
export function bpsLabel(bps: number): string {
  return `${(bps / 100).toLocaleString("ko-KR", { maximumFractionDigits: 4 })}%`;
}

/** 연도와 초까지 보이는 한국 시각(재현 좌표용). */
export function fullDatetime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("ko-KR", { timeZone: "Asia/Seoul" });
}

// 뉴스·공시 묶음의 사건 종류(backend/app/scoring/overlay.py DEFAULT_HALF_LIVES의 13종).
const EVENT_TYPE_LABEL: Record<string, string> = {
  PRICE_MOVE: "주가 움직임",
  OTHER: "기타",
  INDUSTRY: "업황",
  PRODUCT: "제품",
  MANAGEMENT: "경영",
  ANALYST_RATING: "증권사 의견",
  EARNINGS: "실적",
  GUIDANCE: "실적 전망",
  SHAREHOLDER_RETURN: "주주환원",
  ORDER_CONTRACT: "수주·계약",
  CAPITAL_RAISE: "자금 조달",
  LEGAL_REGULATORY: "법·규제",
  MERGER_ACQUISITION: "인수합병",
};

export function eventTypeLabel(type: string): string {
  return EVENT_TYPE_LABEL[type] ?? type;
}

/** "9/23 09:00" 꼴의 서울 시각. 읽을 수 없으면 빈 문자열. */
export function shortSeoulTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const parts = new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(d);
  const get = (t: string) => parts.find((x) => x.type === t)?.value ?? "";
  return `${get("month")}/${get("day")} ${get("hour")}:${get("minute")}`;
}

// --- 아침 목록 표기(오늘의 관찰과 신호 탭이 같이 쓴다) ---

// 좋은·나쁜 뉴스는 색이 아니라 기호로 구분한다(한국식 상승 빨강과 부딪히지 않게).
export const LIST_REASON_LABEL: Record<string, string> = {
  DISCOVERY_SURGE: "뉴스 급증",
  POSITIVE_NEWS_OVERLAY: "＋좋은 뉴스",
  NEGATIVE_NEWS_OVERLAY: "－나쁜 뉴스",
  DISCLOSURE_EVENT: "공시",
  SEARCH_SURGE: "검색 급증",
  TRACKED_HIGH_SCORE: "점수 상위",
  TRACKED: "추적 종목",
};

export const REGIME_LABEL: Record<string, string> = {
  RISK_ON: "상승장",
  NEUTRAL: "중립",
  RISK_OFF: "하락장",
  UNKNOWN: "국면 모름",
};

/** 사전 수집 상태 중 화면에 알릴 것만. 받았거나 이미 최신이면 표시하지 않는다. */
export const PREFETCH_WARNING: Record<string, string> = {
  SKIPPED_CAP: "데이터 미수집 (하루 상한 초과)",
  FAILED: "데이터 수집 실패",
  NO_DATA: "가격 데이터 없음",
};
