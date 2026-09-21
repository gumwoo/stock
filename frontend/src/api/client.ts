import type {
  BacktestRunDetail,
  BacktestRunSummary,
  Candle,
  Diagnostics,
  Instrument,
  Signal,
} from "./types";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText} on ${path}`);
  }
  return (await res.json()) as T;
}

async function post<T>(path: string): Promise<T> {
  const res = await fetch(path, { method: "POST" });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText} on ${path}`);
  }
  return (await res.json()) as T;
}

export const api = {
  instruments: () => get<Instrument[]>("/api/instruments"),
  signals: () => get<Signal[]>("/api/signals"),
  signal: (id: number) => get<Signal>(`/api/signals/${id}`),
  candles: (id: number, limit = 250) =>
    get<Candle[]>(`/api/candles/${id}?limit=${limit}`),
  rescore: () => post<Signal[]>("/api/signals/rescore"),
  config: () => get<Diagnostics>("/health/config"),
  backtests: () => get<BacktestRunSummary[]>("/api/backtests"),
  backtest: (id: number) => get<BacktestRunDetail>(`/api/backtests/${id}`),
};
