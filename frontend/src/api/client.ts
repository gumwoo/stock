import type {
  BacktestRunDetail,
  BacktestRunSummary,
  Candle,
  Diagnostics,
  ListMembers,
  ListSignalRow,
  LiveBar,
  LiveState,
  PreopenToday,
  ThemeNewsDay,
} from "./types";

// 오류 문구는 화면에 그대로 나오므로 한국어로 만든다. 경로는 개발자 도구의 네트워크 탭에서 볼 수 있다.
async function request<T>(path: string, init: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch {
    throw new Error("서버에 연결하지 못했습니다");
  }
  if (!res.ok) {
    throw new Error(`서버 응답 오류 (${res.status})`);
  }
  try {
    return (await res.json()) as T;
  } catch {
    throw new Error("서버 응답을 읽지 못했습니다");
  }
}

function get<T>(path: string): Promise<T> {
  return request<T>(path, { headers: { Accept: "application/json" } });
}

export const api = {
  candles: (id: number, limit = 250) =>
    get<Candle[]>(`/api/candles/${id}?limit=${limit}`),
  listDays: () => get<string[]>("/api/lists/days"),
  listSignals: (day: string) => get<ListSignalRow[]>(`/api/lists/${day}/signals`),
  listMembers: (day: string) => get<ListMembers>(`/api/lists/${day}/members`),
  listBars: (day: string, instrumentId: number, interval: "1m" | "1s") =>
    get<LiveBar[]>(`/api/lists/${day}/bars/${instrumentId}?interval=${interval}`),
  config: () => get<Diagnostics>("/health/config"),
  backtests: () => get<BacktestRunSummary[]>("/api/backtests"),
  backtest: (id: number) => get<BacktestRunDetail>(`/api/backtests/${id}`),
  live: () => get<LiveState>("/api/live"),
  liveBars: (code: string, interval: "1m" | "1s" = "1m") =>
    get<LiveBar[]>(`/api/live/${code}/bars?interval=${interval}`),
  liveSparks: () => get<Record<string, number[]>>("/api/live/sparks"),
  themes: () => get<ThemeNewsDay>("/api/themes/today"),
  preopenToday: () => get<PreopenToday>("/api/preopen/today"),
};
