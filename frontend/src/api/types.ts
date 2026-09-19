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
