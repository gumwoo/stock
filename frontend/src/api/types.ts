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
}

export interface Candle {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface Instrument {
  instrument_id: number;
  symbol: string;
  name: string;
  market: string;
  sector: string | null;
  currency: string;
  last_close: number | null;
  last_close_date: string | null;
  change_pct: number | null;
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
export interface LiveMember {
  instrument_id: number;
  code: string;
  name: string;
  rank: number;
  reasons: string[];
  overlay_points: number | null;
  attention_surge: number | null;
  regime: string | null;
  last: { price: number; change_pct: number; day_volume: number } | null;
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
    }
  | { type: "seeded"; codes: string[] };
