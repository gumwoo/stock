// Mirrors backend/app/api/schemas.py. Kept hand-written and small rather than
// generated: the surface is narrow and an explicit type is easier to read than
// a generated one.

export interface Metric {
  name: string;
  raw: number;
  normalized: number;
  detail: string | null;
}

export interface Factor {
  engine: string;
  score: number;
  metrics: Metric[];
  /** What the strategy config asked for. */
  requested_weight: number;
  /** What was actually applied after availability was resolved. */
  effective_weight: number;
  contribution: number;
  availability: "AVAILABLE" | "UNAVAILABLE";
  availability_reason: string | null;
  source_asof: string | null;
  source_checked_at: string | null;
  freshness_status: "FRESH" | "STALE" | "MISSING";
}

export interface Reason {
  status: "SUPPORTS" | "NEUTRAL" | "OPPOSES";
  text: string;
  engine: string;
  metric_name: string | null;
}

export interface Signal {
  id: number;
  instrument_id: number;
  symbol: string;
  name: string;
  market: string;
  total_score: number;
  action: "BUY_INTEREST" | "WATCH" | "CAUTION" | "ABSTAINED";
  /** The data this was computed from. */
  data_asof: string;
  /** When the judgement was finalised. */
  decision_at: string;
  /** Soonest an order could honestly fill. Nothing here was ever filled. */
  earliest_execution_at: string;
  strategy_version: string;
  policy: string;
  abstained_reason: string | null;
  factors: Factor[];
  reasons: Reason[];
  /** 요인이 빠져 가중치 합이 1보다 작을 때, 판단에 실제로 쓴 점수와 기준(목록 신호만). */
  scale?: { weight: number; score: number; buy: number; caution: number };
}

export interface Candle {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface DisabledCapability {
  name: string;
  set_to_enable: string[];
  effect: string;
}

export interface Diagnostics {
  app_env: string;
  enabled: string[];
  disabled: DisabledCapability[];
  summary: string;
}

/** One name on today's morning list, as the live feed holds it. */
export interface LiveMember extends MarketWeightFields {
  instrument_id: number;
  code: string;
  name: string;
  rank: number;
  reasons: string[];
  overlay_points: number | null;
  attention_surge: number | null;
  regime: string | null;
  /** 08:40에 계산한 그날 관찰용 점수. 참고용이고 선정 기준이 아니다. */
  total_score: number | null;
  prefetch_status: string | null;
  abstained_reason: string | null;
  /** 목록 이유 뒤의 뉴스·공시 묶음(뉴스 점수가 큰 순, 최대 5개). 옛 서버는 보내지 않는다. */
  events?: LiveEvent[];
  last: { price: number; change_pct: number; day_volume: number } | null;
  /** 08:35 기술 점수(표시 전용). 추적 종목 폴백·옛 응답에는 없다. */
  technical_score?: number | null;
}

export interface LiveEvent {
  event_type: string;
  /** 묶음의 첫 기사·공시가 나온 시각(ISO). */
  first_at: string;
  title: string;
  sentiment: number;
  /** 묶음의 읽기 전체(기사 + 공시). */
  articles: number;
  /** 그중 공시 수. 옛 서버는 보내지 않는다. */
  disclosures?: number;
  /** 제목을 준 기사(네이버 주소 우선)나 DART 공시 주소. 못 찾으면 null. */
  url: string | null;
  /** 쉬운 설명(판단 보조). 옛 서버나 설명을 붙이지 못한 경우 없다. */
  kind?: string | null;
  what?: string | null;
  /** 좋음 / 다소 좋음 / 애매 / 다소 나쁨 / 나쁨 */
  verdict?: string | null;
  why?: string | null;
  /** 좋음·나쁨의 출처: "공시 제목 규칙" 또는 "기사 판독(언어 모델)" */
  verdict_source?: string | null;
  /** 지난 3개월 같은 공시의 9시 시가 기준 기록(공시만). */
  usual?: string | null;
  usual_short?: string | null;
  /** 우리 목록 기록(건수가 충분할 때만). */
  usual_ours?: string | null;
  usual_ours_short?: string | null;
}

export interface LiveState {
  status: string;
  source: string | null;
  day: string | null;
  members: LiveMember[];
}

/** A bar as lightweight-charts wants it: seconds since the epoch. */
export interface LiveBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export type LiveMessage =
  | ({ type: "state" } & Partial<LiveState>)
  | {
      type: "trade";
      code: string;
      time: number;
      price: number;
      volume: number;
      change_pct: number;
      bar: LiveBar;
      /** 서버가 쌓은 그 초의 봉(1초봉 차트용). */
      sbar: LiveBar;
    }
  | { type: "seeded"; codes: string[] };

/** 테마어 뉴스(표시 전용): 전날 장 마감 뒤 그 테마어로 찾은 기사 수와, 기사에 이름이 나온 종목. */
export interface ThemeNews {
  theme: string;
  query: string;
  since: string;
  asked_at: string;
  articles: number;
  /** 검색 API 상한(1,000건)에 걸려 실제 기사는 더 많을 수 있다. */
  capped: boolean;
  headlines: { title: string; url: string; published_at: string; host: string | null }[];
  mentions: { instrument_id: number; name: string; articles: number }[];
}

export interface ThemeNewsDay {
  day: string | null;
  themes: ThemeNews[];
}

/** 아침 흐름 한 단계. status가 null이면 아직 기록이 없다. SLOW는 60분 넘게 진행 중, MISSING은 개장했는데 목록이 없음. */
export interface PreopenStage {
  name: string;
  label: string;
  at: string;
  status: string | null;
  note: string | null;
}

export interface PreopenToday {
  now: string;
  /** 다음(또는 오늘) 아침이 속한 거래일. */
  day: string;
  list_at: string;
  list_passed: boolean;
  opened: boolean;
  pool: { status: string; pool_count: number; asof: string | null } | null;
  stages: PreopenStage[];
}

/** 08:40 채점의 상세(`score_detail`). `/api/signals`의 Signal에서 종목 정보와 id를 뺀 모양. */
export interface ScoreDetail {
  data_asof: string;
  decision_at: string;
  earliest_execution_at: string;
  total_score: number;
  action: Signal["action"];
  strategy_version: string;
  policy: string;
  abstained_reason: string | null;
  factors: Factor[];
  reasons: Reason[];
  /** 나중에 같은 입력으로 다시 계산해 채운 시각(2026-09-28 목록). */
  backfilled_at?: string;
}

/** 지수 대형주 표시(참고용). 목록 날 이전 시가총액 순위표 기준. 옛 서버는 보내지 않는다. */
export interface MarketWeightFields {
  /** 그 시장(KOSPI/KOSDAQ) 시가총액에서 차지하는 비중(%). 순위표 30위 밖이거나 표가 없으면 null. */
  market_weight_pct?: number | null;
  market_listing?: string | null;
  /** 비중 5% 이상. */
  heavyweight?: boolean;
  sector?: string | null;
  /** 전 거래일 상한가(참고용): LOCKED 점상, CLOSED 상한가 마감(장중 거래), TOUCHED 장중 터치. 옛 서버는 보내지 않는다. */
  prev_limit?: "LOCKED" | "CLOSED" | "TOUCHED" | null;
  /** 전 거래일 등락(%). */
  prev_change_pct?: number | null;
}

/** 한국 거래일 개장 전에 끝난 미국 반도체 등락(`/api/overnight/us-semis`). */
export interface OvernightSemis {
  day: string;
  refs: {
    code: string;
    label: string;
    /** % 단위. 새 미국 세션이 없거나 분할 의심이면 null. */
    change_pct: number | null;
    split_suspect: boolean;
    /** 합친 미국 세션 날짜(뉴욕). 한국 연휴 뒤에는 여럿. */
    us_sessions: string[];
  }[];
}

/** 증권사 투자의견 요약(KIS, 목록 날 전 90일, 참고용). */
export interface AnalystSummary {
  window_days: number;
  count: number;
  brokers: number;
  avg_target: number | null;
  target_brokers: number;
  /** 평균 목표가 / 전일 종가 - 1(%). 전일 종가가 없으면 null. */
  upside_pct: number | null;
  opinions: Record<string, number>;
  raised: number;
  lowered: number;
  latest: { date: string; broker: string; opinion: string; label: string; target: number | null } | null;
  /** 100행 상한에 걸려 창 안이 다 오지 않았다(건수는 "이상"). */
  truncated: boolean;
  fetched_after_open: boolean;
}

/** 그날 아침 목록 한 종목의 신호(`/api/lists/{day}/signals`). */
export interface ListSignalRow extends MarketWeightFields {
  member_id: number;
  instrument_id: number;
  code: string | null;
  name: string;
  rank: number;
  /** 목록에 오른 이유 코드(뉴스·공시·검색 급증 등). */
  list_reasons: string[];
  total_score: number | null;
  action: Signal["action"] | null;
  technical_score: number | null;
  fundamental_score: number | null;
  prefetch_status: string | null;
  abstained_reason: string | null;
  regime: string | null;
  overlay_points: number | null;
  attention_surge: number | null;
  /** 실제 채점 시각(08:40). */
  evaluated_at: string | null;
  detail: ScoreDetail | null;
  /** 참여한 요인 가중치 합(상세가 없으면 null). 판단은 합계를 이것으로 나눈 값을 기준에 댄 것과 같다. */
  weight_total: number | null;
  /** 판단 기준(지금 규칙). */
  thresholds: { buy_interest: number; caution: number };
  /** 증권사 의견(참고). 조회하지 못했으면 null, 조회했는데 리포트가 없으면 count 0. 옛 서버는 보내지 않는다. */
  analyst?: AnalystSummary | null;
}

/** 그날 관찰 목록(`/api/lists/{day}/members`). 시세(`last`)는 없다. */
export interface ListMembers {
  day: string;
  source: string;
  members: LiveMember[];
}

/** 전략 실험실(연구 탭). 수익률은 %, 비용(0.30%) 뒤. take·stop은 비율(0.025 = 2.5%), stop null은 손절 없음(10시 매도). */
export interface LabRuleStat {
  take: number;
  stop: number | null;
  n: number;
  days: number;
  mean: number | null;
  t: number | null;
  first: number | null;
  second: number | null;
  flag: string;
}

export interface LabGridCell extends LabRuleStat {
  market: string;
}

export interface LabCondition {
  feature: string;
  label: string;
  n: number;
  hit25: number | null;
  rules: LabRuleStat[];
}

export interface LabDay {
  day: string;
  asof: string;
  members: number;
  measured: number;
  /** 선정 3에서 목록에서 뺀 종목 중 잴 수 있었던 수와 그 종목들의 대표 규칙 평균(목록에 없으니 참고). */
  excluded_names: number;
  excluded_rules: { take: number; stop: number | null; mean: number | null }[];
  kospi: number;
  kosdaq: number;
  excluded: Record<string, number>;
  pending: boolean;
  hit25_10: number | null;
  hit25_60: number | null;
  hit5_60: number | null;
  rules: { take: number; stop: number | null; mean: number | null }[];
  at_ten: number | null;
}

export interface LabJudged {
  days: number;
  n: number;
  mean: number | null;
  t: number | null;
  first: number | null;
  second: number | null;
  state: string;
}

export interface LabHypothesis {
  key: string;
  text: string;
  take: number;
  stop: number | null;
  sign: number;
  basis: string;
  reference: LabRuleStat | null;
  before: LabRuleStat;
  after: LabJudged;
}

export interface LabListHypothesis {
  key: string;
  text: string;
  days: number;
  mean: number | null;
  t: number | null;
  first: number | null;
  second: number | null;
  state: string;
}

/** S3(3개 겹침 + 기술 상위 2)가 고른 종목 하나의 그날 경로. 최대는 09:00 봉 고가 포함 원시 고가(묘사), 도달은 체결 판정. */
export interface LabPickRow {
  day: string;
  /** before: 가설 고정 전(참고) · after: 판정에 셈 · late: 장중에 늦게 만든 목록(셈하지 않음) */
  phase: "before" | "after" | "late";
  instrument_id: number;
  name: string;
  rank: number;
  technical: number | null;
  reasons: string[];
  unmeasured: string | null;
  peak: {
    max_ten: number;
    max_ten_at: string;
    max_day: number;
    max_day_at: string;
    low_ten: number;
    dip_before_peak: number;
    last: number;
    last_at: string;
    after_ten: boolean;
  } | null;
  peak_ten_bucket: string | null;
  peak_day_bucket: string | null;
  /** 단계("0.02" 등)별 처음 체결된 봉 표기. 10시 전(ten)·장중(day). */
  levels: Record<string, { ten: string | null; day: string | null }>;
  ret_take: number | null;
}

export interface LabPickSummary {
  measured: number;
  days: number;
  levels: { level: number; ten: number | null; day: number | null }[];
  peak_ten_at: Record<string, number>;
  peak_day_at: Record<string, number>;
  median_max_ten: number | null;
  median_max_day: number | null;
  median_dip: number | null;
}

export interface LabPickTrack {
  key: string;
  text: string;
  basis: string;
  frozen_at: string;
  base: number;
  take: number;
  rows: LabPickRow[];
  after: LabPickSummary & {
    judged: LabJudged;
    sign_p: number | null;
    wins: number;
    losses: number;
    ret_take_mean: number | null;
    unmeasured: number;
  };
  before: LabPickSummary;
}

export interface LabView {
  cost_pct: number;
  frozen_at: string;
  days: LabDay[];
  grid: { ours: LabGridCell[]; reference: LabGridCell[] };
  conditions: { ours: LabCondition[]; reference: LabCondition[] };
  kept_rules: LabRuleStat[];
  hypotheses: LabHypothesis[];
  list_hypotheses: LabListHypothesis[];
  /** 보조 섹션: 계산이 실패하면 null(나머지 화면은 그대로). */
  pick_track: LabPickTrack | null;
  reference_meta: Record<string, string | number>;
}
