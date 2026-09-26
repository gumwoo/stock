import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { ThemeNews } from "../components/ThemeNews";
import type { LiveBar, LiveMember, LiveMessage, LiveState } from "../api/types";
import { useLiveChart } from "../hooks/useLiveChart";
import "./Live.css";

/**
 * Today's watch: the morning's list, and a live chart of the chosen name.
 *
 * Display only. The minute bars here are built from KIS's trades as they
 * arrive; the record the system analyses is fetched separately after the
 * close. Why each name is on the list stays beside its chart, so the price is
 * read against the morning's reason for looking, not instead of it.
 */

const REASONS: Record<string, string> = {
  DISCOVERY_SURGE: "뉴스 급증",
  POSITIVE_NEWS_OVERLAY: "좋은 뉴스",
  NEGATIVE_NEWS_OVERLAY: "나쁜 뉴스",
  DISCLOSURE_EVENT: "공시",
  SEARCH_SURGE: "검색 급증",
  TRACKED_HIGH_SCORE: "점수 상위",
  TRACKED: "추적 종목",
};

const REGIMES: Record<string, string> = {
  RISK_ON: "상승장",
  NEUTRAL: "중립",
  RISK_OFF: "하락장",
  UNKNOWN: "국면 모름",
};

/** The feed's state, which the API reports as short English codes, in Korean. */
function statusLabel(status: string): string {
  if (status.startsWith("off")) return "꺼짐 (LIVE_FEED_ENABLED가 설정되지 않음)";
  if (status.startsWith("idle")) return "대기 중 (장 시간이 아님)";
  const refused = status.match(/^live \((\d+) subscriptions refused\)/);
  if (refused) return `실시간 수신 중 (구독 거절 ${refused[1]}개)`;
  if (status === "live") return "실시간 수신 중";
  if (status.startsWith("another process")) return "다른 프로세스가 실시간 연결을 쓰는 중";
  if (status.startsWith("no names")) return "오늘 볼 종목이 없음";
  if (status.startsWith("closed")) return "오늘 장 마감";
  if (status.startsWith("reconnecting")) return "재연결 중";
  return status;
}

function sourceLabel(source: string | null): string {
  if (!source) return "";
  if (source === "morning list") return "오늘 아침 목록";
  if (source.startsWith("morning list (no names")) return "오늘 조건에 맞는 종목 없음";
  if (source.startsWith("tracked names")) return "오늘 목록 없음(생성 실패) · 추적 종목을 참고용으로 표시";
  return source;
}

/** 사전 수집 상태 중 화면에 알릴 것만. 받았거나 이미 최신이면 표시하지 않는다. */
const PREFETCH: Record<string, string> = {
  SKIPPED_CAP: "데이터 미수집 (하루 상한 초과)",
  FAILED: "데이터 수집 실패",
  NO_DATA: "가격 데이터 없음",
};

type Interval = "1m" | "1s";

function signed(value: number | null | undefined, digits = 1, suffix = ""): string {
  if (value == null) return "–";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}${suffix}`;
}

export function Live() {
  const [state, setState] = useState<LiveState | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [interval, setInterval_] = useState<Interval>("1m");
  const [error, setError] = useState<string | null>(null);
  const { container, reset, push } = useLiveChart(440);
  const seconds = useRef<Map<number, LiveBar>>(new Map());
  const selectedRef = useRef<string | null>(null);
  const intervalRef = useRef<Interval>("1m");
  // False while a new name or interval is being loaded: a live update then
  // could be older than the series about to be set, which the chart refuses.
  const ready = useRef(false);
  selectedRef.current = selected;
  intervalRef.current = interval;

  // The list and each name's last trade, refreshed now and then.
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .live()
        .then((s) => {
          if (!alive) return;
          setState(s);
          setError(null);
          if (selectedRef.current === null && s.members.length > 0) setSelected(s.members[0].code);
        })
        .catch((e: Error) => alive && setError(e.message));
    load();
    const timer = window.setInterval(load, 15_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  // A new name or interval: start the chart from what is already known.
  useEffect(() => {
    seconds.current = new Map();
    ready.current = false;
    if (selected === null) return;
    if (interval === "1s") {
      reset([]);
      ready.current = true;
      return;
    }
    let current = true;
    api
      .liveBars(selected)
      .catch(() => [])
      .then((bars) => {
        if (!current) return;
        reset(bars);
        ready.current = true;
      });
    return () => {
      current = false;
    };
  }, [selected, interval, reset]);

  // Trades as they happen. A dropped socket is opened again a few seconds later.
  useEffect(() => {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    let socket: WebSocket | null = null;
    let retry: number | undefined;
    let closed = false;
    const open = () => {
      socket = new WebSocket(`${scheme}://${window.location.host}/ws/live`);
      socket.onmessage = onMessage;
      socket.onopen = () => setError(null);
      socket.onclose = () => {
        if (closed) return;
        setError("실시간 연결이 끊겨 다시 연결하는 중…");
        retry = window.setTimeout(open, 3_000);
      };
    };
    const onMessage = (event: MessageEvent) => {
      const message = JSON.parse(event.data) as LiveMessage;
      if (message.type === "state") {
        setState((prev) => (prev ? { ...prev, status: message.status ?? prev.status } : prev));
        return;
      }
      if (message.type === "seeded") {
        const code = selectedRef.current;
        if (code && message.codes.includes(code) && intervalRef.current === "1m") {
          api.liveBars(code).then(reset).catch(() => undefined);
        }
        return;
      }
      setState((prev) =>
        prev
          ? {
              ...prev,
              members: prev.members.map((m) =>
                m.code === message.code
                  ? {
                      ...m,
                      last: {
                        price: message.price,
                        change_pct: message.change_pct,
                        day_volume: (m.last?.day_volume ?? 0) + message.volume,
                      },
                    }
                  : m,
              ),
            }
          : prev,
      );
      if (message.code !== selectedRef.current || !ready.current) return;
      if (intervalRef.current === "1m") {
        push(message.bar);
        return;
      }
      const t = message.time;
      const bar = seconds.current.get(t);
      const next: LiveBar = bar
        ? {
            ...bar,
            high: Math.max(bar.high, message.price),
            low: Math.min(bar.low, message.price),
            close: message.price,
            volume: bar.volume + message.volume,
          }
        : { time: t, open: message.price, high: message.price, low: message.price, close: message.price, volume: message.volume };
      seconds.current.set(t, next);
      push(next);
    };
    open();
    return () => {
      closed = true;
      window.clearTimeout(retry);
      socket?.close();
    };
  }, [push, reset]);

  const member: LiveMember | undefined = useMemo(
    () => state?.members.find((m) => m.code === selected),
    [state, selected],
  );

  return (
    <section className="live">
      <header className="live__head">
        <div>
          <h1 className="live__title">오늘의 관찰</h1>
          <p className="live__disclaimer">
            관찰 목록 — 매수 추천이 아닙니다. 오늘 뉴스·공시·검색 급증이 있어 확인할 가치가 있는
            종목입니다.
          </p>
          <p className="live__status">
            {state
              ? `${statusLabel(state.status)}${state.source ? ` · ${sourceLabel(state.source)}` : ""}`
              : "불러오는 중…"}
            {error ? ` · ${error}` : ""}
          </p>
        </div>
        <div className="live__intervals" role="group" aria-label="봉 간격">
          {(["1m", "1s"] as Interval[]).map((i) => (
            <button
              key={i}
              className={interval === i ? "live__interval live__interval--on" : "live__interval"}
              onClick={() => setInterval_(i)}
            >
              {i === "1m" ? "1분봉" : "1초봉"}
            </button>
          ))}
        </div>
      </header>

      <ThemeNews />

      <div className="live__body">
        <ol className="live__list">
          {state?.members.map((m) => (
            <li key={m.code}>
              <button
                className={m.code === selected ? "live__item live__item--on" : "live__item"}
                onClick={() => setSelected(m.code)}
              >
                <span className="live__rank">{m.rank}</span>
                <span className="live__name">{m.name}</span>
                <span
                  className={
                    m.last && m.last.change_pct > 0
                      ? "live__change live__change--up"
                      : m.last && m.last.change_pct < 0
                        ? "live__change live__change--down"
                        : "live__change"
                  }
                >
                  {signed(m.last?.change_pct, 2, "%")}
                </span>
              </button>
            </li>
          ))}
          {state && state.members.length === 0 && (
            <li className="live__empty">
              {state.source?.startsWith("morning list (no names")
                ? "오늘 조건에 맞는 종목이 없습니다."
                : "볼 종목이 없습니다."}
            </li>
          )}
        </ol>

        <div className="live__main">
          {member && (
            <div className="live__why">
              <div className="live__price">
                <span className="live__big">
                  {member.last ? member.last.price.toLocaleString("ko-KR") : "–"}
                </span>
                <span className="live__code">
                  {member.name} · {member.code}
                </span>
              </div>
              <div className="live__reasons">
                {member.reasons.map((r) => (
                  <span key={r} className="live__reason">
                    {REASONS[r] ?? r}
                  </span>
                ))}
                <span className="live__fact">뉴스 점수 {signed(member.overlay_points)}</span>
                <span className="live__fact">
                  검색량 {member.attention_surge == null ? "–" : `${member.attention_surge.toFixed(2)}배`}
                </span>
                {member.regime && <span className="live__fact">{REGIMES[member.regime] ?? member.regime}</span>}
                <span className="live__fact">
                  관찰용 점수 {member.total_score == null ? "–" : member.total_score.toFixed(1)}
                </span>
                {member.prefetch_status && PREFETCH[member.prefetch_status] && (
                  <span className="live__fact live__fact--warn">{PREFETCH[member.prefetch_status]}</span>
                )}
              </div>
              {member.abstained_reason && <p className="live__abstain">점수 보류: {member.abstained_reason}</p>}
            </div>
          )}
          <div ref={container} className="live__chart" />
          <p className="live__note">
            들어오는 체결로 그린 화면용 차트이고 기록이 아닙니다. 분석에 쓰는 기록은 장 마감 뒤 받는
            1분봉입니다.{interval === "1s" ? " 1초봉은 이 화면을 연 때부터 그립니다." : ""}
          </p>
        </div>
      </div>
    </section>
  );
}
