import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
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
  DISCOVERY_SURGE: "news surge",
  POSITIVE_NEWS_OVERLAY: "good news",
  NEGATIVE_NEWS_OVERLAY: "bad news",
  DISCLOSURE_EVENT: "disclosure",
  SEARCH_SURGE: "search surge",
  TRACKED_HIGH_SCORE: "high score",
  TRACKED: "tracked",
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
        setError("the live socket closed; reconnecting…");
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
          <h1 className="live__title">Today</h1>
          <p className="live__status">
            {state ? `${state.status}${state.source ? ` · ${state.source}` : ""}` : "loading…"}
            {error ? ` · ${error}` : ""}
          </p>
        </div>
        <div className="live__intervals" role="group" aria-label="interval">
          {(["1m", "1s"] as Interval[]).map((i) => (
            <button
              key={i}
              className={interval === i ? "live__interval live__interval--on" : "live__interval"}
              onClick={() => setInterval_(i)}
            >
              {i === "1m" ? "1 min" : "1 sec"}
            </button>
          ))}
        </div>
      </header>

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
          {state && state.members.length === 0 && <li className="live__empty">No names to watch.</li>}
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
                <span className="live__fact">news overlay {signed(member.overlay_points)}</span>
                <span className="live__fact">
                  searches {member.attention_surge == null ? "–" : `${member.attention_surge.toFixed(2)}×`}
                </span>
                {member.regime && <span className="live__fact">{member.regime}</span>}
              </div>
            </div>
          )}
          <div ref={container} className="live__chart" />
          <p className="live__note">
            Built from trades as they arrive; for looking, not for the record. The record is the minute
            bars fetched after the close.{interval === "1s" ? " One-second bars start when this view opens." : ""}
          </p>
        </div>
      </div>
    </section>
  );
}
