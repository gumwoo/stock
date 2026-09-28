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

/** One measured window of a stored backtest run. */
export interface BacktestWindow {
  window_index: number;
  sample_type: "IN_SAMPLE" | "OUT_OF_SAMPLE" | "HOLDOUT";
  period_start: string;
  period_end: string;
  strategy: string;
  strategy_params: Record<string, unknown>;
  sessions: number;
  observations: number;
  total_return: number | null;
  cagr: number | null;
  max_drawdown: number | null;
  sharpe: number | null;
  win_rate: number | null;
  profit_factor: number | null;
  trades: number;
  abstained: number;
  without_data: number;
  unfilled: number;
}

export interface BacktestRunSummary {
  id: number;
  instrument_id: number;
  symbol: string;
  name: string;
  strategy_kind: string;
  strategy_version: string;
  strategy_params: Record<string, unknown>;
  fitter_version: string | null;
  period_start: string;
  period_end: string;
  started_at: string;
  windows: number;
  has_holdout: boolean;
}

export interface BacktestRunDetail extends BacktestRunSummary {
  market: string;
  interval: string;
  strategy_fingerprint: string;
  fit_trace_fingerprint: string;
  holdout_strategy_fingerprint: string | null;
  git_commit_sha: string;
  git_dirty: boolean;
  data_snapshot_at: string;
  starting_cash: number;
  commission_bps: number;
  slippage_bps: number;
  min_commission: number;
  execution_model: string;
  bar_minutes: number | null;
  universe: number[] | null;
  train_sessions: number;
  eval_sessions: number;
  anchored: boolean;
  require_complete_sessions: boolean;
  holdout_start: string | null;
  holdout_end: string | null;
  window_rows: BacktestWindow[];
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
}

/** 그날 관찰 목록(`/api/lists/{day}/members`). 시세(`last`)는 없다. */
export interface ListMembers {
  day: string;
  source: string;
  members: LiveMember[];
}
