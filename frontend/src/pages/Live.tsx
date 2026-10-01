import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { LIST_REASON_LABEL, eventTypeLabel, shortSeoulTime } from "../api/format";
import { MorningStatus, dayLabel } from "../components/MorningStatus";
import { Sparkline } from "../components/Sparkline";
import {
  HeavyweightBadge,
  OvernightSemis,
  PrevLimitBadge,
  heavyweightNote,
  prevLimitNote,
} from "../components/MarketWeight";
import { AnalystLine } from "../components/AnalystLine";
import { ScoreParts, hasScoreParts } from "../components/ScoreParts";
import { ThemeNews } from "../components/ThemeNews";
import type { ListSignalRow, LiveBar, LiveMember, LiveMessage, LiveState, PreopenToday } from "../api/types";
import { useLiveChart } from "../hooks/useLiveChart";
import "./Live.css";

/**
 * 오늘의 관찰: 그날 아침 목록과 고른 종목의 차트·근거 뉴스.
 *
 * 두 가지로 본다. 고른 날이 실시간 연결이 들고 있는 날이면 **실시간**(웹소켓 체결로 그리는 1분봉·1초봉). 그 밖의
 * 날(지난 목록, 장 밖에 서버를 다시 켠 뒤의 오늘)은 **기록**: 저장된 목록과 저장된 봉(장 마감 뒤 1분봉, 연결돼 있던
 * 동안의 1초봉)을 보여 준다. 기록 모드에서는 웹소켓 체결을 차트에 넣지 않는다.
 *
 * 점수는 신호 탭에 있다. 여기는 차트와 목록에 오른 이유(뉴스·공시·검색량)다.
 */

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

/** 상태 앞의 기호. 색만으로 구분하지 않는다. */
function statusMark(status: string): string {
  if (status.startsWith("live")) return "●";
  if (status.startsWith("off") || status.startsWith("another process")) return "✕";
  return "○";
}

/** 묶음의 기사·공시 수. 하나뿐이면 적지 않는다. articles는 공시까지 센 수다. */
function countLabel(articles: number, disclosures: number): string {
  const news = Math.max(articles - disclosures, 0);
  if (news + disclosures <= 1) return "";
  const parts = [news > 0 ? `기사 ${news}건` : "", disclosures > 0 ? `공시 ${disclosures}건` : ""].filter(Boolean);
  return ` · ${parts.join(" · ")}`;
}

function changeClass(change: number | undefined): string {
  if (change == null || change === 0) return "live__change";
  return change > 0 ? "live__change live__change--up" : "live__change live__change--down";
}

function sourceLabel(source: string | null): string {
  if (!source) return "";
  if (source === "morning list") return "오늘 아침 목록";
  if (source.startsWith("morning list (no names")) return "오늘 조건에 맞는 종목 없음";
  if (source.startsWith("tracked names")) return "오늘 목록 없음(생성 실패) · 추적 종목을 참고용으로 표시";
  return source;
}

/**
 * 1초봉을 1분봉으로 합친다(장 마감 뒤 REST 1분봉을 받기 전의 당일용). 순수하다.
 * 봉 시각은 UTC epoch 초이고 한국 시각의 분 경계도 60의 배수라(오프셋 32,400초) 60으로 내리면 그 분의 시작이다.
 */
function secondsToMinutes(bars: LiveBar[]): LiveBar[] {
  const out: LiveBar[] = [];
  for (const b of bars) {
    const t = Math.floor(b.time / 60) * 60;
    const last = out[out.length - 1];
    if (last && last.time === t) {
      last.high = Math.max(last.high, b.high);
      last.low = Math.min(last.low, b.low);
      last.close = b.close;
      last.volume += b.volume;
    } else {
      out.push({ time: t, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume });
    }
  }
  return out;
}

type Interval = "1m" | "1s";
type Mode = "pending" | "live" | "archive";
type TradeMessage = Extract<LiveMessage, { type: "trade" }>;

function signed(value: number | null | undefined, digits = 1, suffix = ""): string {
  if (value == null) return "–";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}${suffix}`;
}

export function Live({
  focus,
  onFocusUsed,
}: {
  /** 신호 탭의 "차트·뉴스"로 넘어온 날짜·종목. 한 번 쓰고 `onFocusUsed`로 비운다. */
  focus: { day: string; code: string } | null;
  onFocusUsed: () => void;
}) {
  const [state, setState] = useState<LiveState | null>(null);
  // /api/live를 받지 못했으면(서버 오류) 실시간 없이 기록으로 본다.
  const [liveFailed, setLiveFailed] = useState(false);
  const [days, setDays] = useState<string[] | null>(null);
  // 사용자가 날짜를 직접 골랐는가. 고르지 않았으면 새 목록(다음 날 08:50)이 생길 때 최신으로 따라간다.
  const userPicked = useRef(false);
  const [day, setDay] = useState<string | null>(null);
  const [archive, setArchive] = useState<{ day: string; members: LiveMember[]; failed?: boolean } | null>(
    null,
  );
  const archiveDay = useRef<string | null>(null);
  // 그날 목록을 못 받았으면 60초마다 다시 시도한다(날짜 폴링이 올린다).
  const archiveFailed = useRef(false);
  const [archiveRetry, setArchiveRetry] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  // 브라우저 소켓이 다시 붙을 때마다 올린다. 끊긴 동안 놓친 체결을 서버 봉으로 다시 받게 한다.
  const [reconnects, setReconnects] = useState(0);
  const [interval, setInterval_] = useState<Interval>("1m");
  const [chartNote, setChartNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [preopen, setPreopen] = useState<PreopenToday | null>(null);
  const [sparks, setSparks] = useState<Record<string, number[]>>({});
  const [preopenError, setPreopenError] = useState(false);
  const [stepsOpen, setStepsOpen] = useState(false);
  // 그날 08:40 점수(신호 탭과 같은 행). 날짜와 묶어 둔다: 두 날 목록에 같은 종목이 흔해서, 묶지 않으면 날짜를 바꾼 직후나
  // 요청이 실패했을 때 전날 점수가 보인다.
  const [scores, setScores] = useState<{ day: string; rows: ListSignalRow[] | null } | null>(null);

  useEffect(() => {
    if (day === null) return;
    let alive = true;
    setScores(null);
    api
      .listSignals(day)
      .then((rows) => alive && setScores({ day, rows }))
      .catch(() => alive && setScores({ day, rows: null })); // 보조 정보: 실패하면 점수 칸만 뺀다(날짜를 바꾸면 다시 받는다)
    return () => {
      alive = false;
    };
  }, [day]);
  const { container, reset, push } = useLiveChart(440);

  const liveHasList = !!state && !!state.day && state.members.length > 0;
  const mode: Mode =
    day === null || (state === null && !liveFailed)
      ? "pending"
      : state !== null && day === state.day && liveHasList
        ? "live"
        : "archive";

  const selectedRef = useRef<string | null>(null);
  const intervalRef = useRef<Interval>("1m");
  const modeRef = useRef<Mode>("pending");
  const dayRef = useRef<string | null>(null);
  // False while a new name, interval or day is being loaded: a live update then
  // could be older than the series about to be set, which the chart refuses.
  const ready = useRef(false);
  // 봉을 받는 동안 들어온 선택 종목의 체결(실시간 모드에서만).
  const pending = useRef<TradeMessage[]>([]);
  selectedRef.current = selected;
  intervalRef.current = interval;
  modeRef.current = mode;
  dayRef.current = day;

  // 체결 한 건을 차트에 반영한다. 1분봉도 1초봉도 서버가 쌓은 그 봉을 그대로 그린다(브라우저에서 더하지 않는다).
  // `after`보다 이른 봉은 이미 받은 봉에 있다.
  const apply = useCallback(
    (message: TradeMessage, after: number) => {
      const bar = intervalRef.current === "1m" ? message.bar : message.sbar;
      if (bar && bar.time >= after) push(bar);
    },
    [push],
  );

  // 실시간 목록과 각 종목의 마지막 체결, 추세선. 15초마다.
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .live()
        .then((s) => {
          if (!alive) return;
          setState(s);
          setLiveFailed(false);
          setError(null);
        })
        .catch((e: Error) => {
          if (!alive) return;
          setLiveFailed(true);
          setError(e.message);
        });
    const loadSparks = () =>
      api
        .liveSparks()
        .then((sp) => {
          if (alive) setSparks(sp);
        })
        .catch(() => undefined);
    load();
    loadSparks();
    const timer = window.setInterval(() => {
      load();
      loadSparks();
    }, 15_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  // 아침 흐름 상태와 목록 날짜들. 실시간 연결과 따로 DB에서 읽는다.
  useEffect(() => {
    let alive = true;
    const load = () => {
      api
        .preopenToday()
        .then((p) => {
          if (!alive) return;
          setPreopen(p);
          setPreopenError(false);
        })
        .catch(() => {
          if (alive) setPreopenError(true);
        });
      api
        .listDays()
        .then((d) => {
          if (!alive) return;
          setDays(d);
          // 목록을 못 받았던 날이면 다시 받게 한다.
          if (archiveFailed.current) setArchiveRetry((n) => n + 1);
        })
        .catch(() => alive && setDays((prev) => prev ?? []));
    };
    load();
    const timer = window.setInterval(load, 60_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  // 고를 수 있는 날짜: 목록 날짜들, 그리고 목록 없이 추적 종목을 참고로 보이는 실시간 날(목록 날짜에 없음).
  const dayOptions = useMemo(() => {
    const out = [...(days ?? [])];
    if (liveHasList && state?.day && !out.includes(state.day)) out.unshift(state.day);
    return out;
  }, [days, liveHasList, state?.day]);

  // 기본 날짜는 목록 날짜의 최신(없으면 실시간 날). 사용자가 직접 고르지 않았으면 새 목록이 생길 때(다음 날
  // 08:50) 최신으로 따라간다. 신호 탭에서 넘어온 날짜가 있으면 그날(사용자가 고른 것으로 친다).
  const latest = days?.[0] ?? (liveHasList ? (state?.day ?? null) : null);
  useEffect(() => {
    if (focus) {
      // 최신 날짜의 종목으로 들어왔으면 고정하지 않는다(다음 날 목록이 생기면 따라간다). 지난 날짜면 고정한다.
      userPicked.current = focus.day !== latest;
      setDay(focus.day);
      setSelected(focus.code);
      onFocusUsed();
      return;
    }
    if (!userPicked.current && latest && latest !== day) {
      setDay(latest);
      setSelected(null);
    }
  }, [focus, onFocusUsed, day, latest]);

  // 기록 모드의 목록. 날짜가 바뀌면 다시 받는다.
  useEffect(() => {
    if (mode !== "archive" || day === null) return;
    if (archiveDay.current === day) return;
    archiveDay.current = day;
    let alive = true;
    // 실패한 날을 다시 시도하는 동안은 실패 문구를 그대로 두어 깜박이지 않게 한다.
    setArchive((a) => (a?.day === day && a.failed ? a : null));
    archiveFailed.current = false;
    api
      .listMembers(day)
      .then((r) => alive && setArchive({ day, members: r.members }))
      .catch(() => {
        if (!alive) return;
        archiveDay.current = null; // 다음 시도 때 다시 받게
        archiveFailed.current = true;
        setArchive({ day, members: [], failed: true });
      });
    return () => {
      alive = false;
      if (archiveDay.current === day) archiveDay.current = null;
    };
  }, [mode, day, archiveRetry]);

  const shown: LiveMember[] =
    mode === "live" ? (state?.members ?? []) : mode === "archive" && archive?.day === day ? archive.members : [];
  // 목록 날짜도 실시간 목록도 없으면(첫 목록 전) 고를 날이 없다. 그때는 아침 카드를 보인다.
  const noLists = state !== null && days !== null && days.length === 0 && !liveHasList;
  const loading = !noLists && (mode === "pending" || (mode === "archive" && archive?.day !== day));
  const empty = noLists || (!loading && shown.length === 0);

  // 고른 종목이 이 목록에 없으면 첫 종목. 체결마다 도는 것을 막으려고 코드 목록 문자열로 본다.
  const codesKey = shown.map((m) => m.code).join(",");
  useEffect(() => {
    if (loading) return;
    const codes = codesKey ? codesKey.split(",") : [];
    if (codes.length === 0) return;
    if (selected === null || !codes.includes(selected)) setSelected(codes[0]);
  }, [loading, codesKey, selected]);

  // 종목·간격·날짜·모드가 바뀌면 차트를 새로 받는다. 실시간은 서버의 book, 기록은 저장된 봉.
  const archiveIdKey = mode === "archive" ? codesKey : "";
  useEffect(() => {
    ready.current = false;
    pending.current = [];
    setChartNote(null);
    if (selected === null || day === null || mode === "pending") return;
    let current = true;
    if (mode === "live") {
      api
        .liveBars(selected, interval)
        .catch(() => [])
        .then((bars) => {
          if (!current) return;
          reset(bars, interval === "1s" ? 300 : undefined);
          const last = bars.length ? bars[bars.length - 1].time : -Infinity;
          ready.current = true;
          for (const m of pending.current) apply(m, last);
          pending.current = [];
        });
    } else {
      const iid = archive?.members.find((m) => m.code === selected)?.instrument_id;
      if (iid === undefined) return;
      const load = async (): Promise<{ bars: LiveBar[]; note: string | null }> => {
        const bars = await api.listBars(day, iid, interval).catch(() => [] as LiveBar[]);
        if (bars.length > 0 || interval === "1s") {
          return { bars, note: bars.length === 0 ? "그날 저장된 1초봉이 없습니다." : null };
        }
        // 장 마감 뒤 1분봉을 받기 전(16:20 전)의 당일: 저장된 1초봉을 합쳐 보인다.
        const secs = await api.listBars(day, iid, "1s").catch(() => [] as LiveBar[]);
        if (secs.length === 0) return { bars: [], note: "그날 저장된 봉이 없습니다." };
        return {
          bars: secondsToMinutes(secs),
          note: "장 마감 뒤 1분봉을 받기 전이라 저장된 1초봉(실시간 연결이 있던 구간만)을 합쳐 보입니다.",
        };
      };
      void load().then(({ bars, note }) => {
        // 받는 사이 모드·날짜·종목·간격이 바뀌었으면 버린다.
        if (!current) return;
        reset(bars, interval === "1s" ? 300 : undefined);
        setChartNote(note);
      });
    }
    return () => {
      current = false;
    };
    // archive는 코드 목록(archiveIdKey)으로 대신한다.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, interval, reconnects, reset, apply, mode, day, archiveIdKey]);

  // 체결 실시간. 끊기면 몇 초 뒤 다시 연다. 기록 모드에서는 차트에 넣지 않는다.
  useEffect(() => {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    let socket: WebSocket | null = null;
    let retry: number | undefined;
    let closed = false;
    let dropped = false;
    const open = () => {
      socket = new WebSocket(`${scheme}://${window.location.host}/ws/live`);
      socket.onmessage = onMessage;
      socket.onopen = () => {
        setError(null);
        if (dropped) setReconnects((n) => n + 1);
      };
      socket.onclose = () => {
        if (closed) return;
        dropped = true;
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
        if (modeRef.current !== "live") return;
        const code = selectedRef.current;
        const seenDay = dayRef.current;
        if (code && message.codes.includes(code) && intervalRef.current === "1m") {
          api
            .liveBars(code)
            .then((bars) => {
              // 받는 사이 모드·날짜·종목·간격이 바뀌었으면 이 1분봉은 버린다.
              if (
                modeRef.current === "live" &&
                dayRef.current === seenDay &&
                selectedRef.current === code &&
                intervalRef.current === "1m"
              ) {
                reset(bars);
              }
            })
            .catch(() => undefined);
        }
        return;
      }
      // 현재가는 실시간 목록(state)에만 적는다. 기록 모드 화면은 이 state를 쓰지 않는다.
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
      if (modeRef.current !== "live" || message.code !== selectedRef.current) return;
      if (!ready.current) {
        pending.current.push(message);
        return;
      }
      apply(message, -Infinity);
    };
    open();
    return () => {
      closed = true;
      window.clearTimeout(retry);
      socket?.close();
    };
  }, [reset, apply]);

  const member: LiveMember | undefined = shown.find((m) => m.code === selected);
  const scoreRow =
    member && scores && scores.day === day && scores.rows
      ? scores.rows.find((r) => r.instrument_id === member.instrument_id)
      : undefined;

  // 테마 기사에 목록 종목 배지를 붙일 집합(아침 목록일 때만).
  const isMorningList = mode === "archive" || state?.source === "morning list";
  const idKey = isMorningList ? shown.map((m) => m.instrument_id).join(",") : "";
  const listIds = useMemo(() => (idKey ? new Set(idKey.split(",").map(Number)) : null), [idKey]);

  const snapshot = preopen?.stages.find((s) => s.name === "snapshot")?.status ?? null;
  const latestListDay = days && days.length > 0 ? days[0] : null;
  // 다음 아침 목록이 아직 없으면(아침 상태의 날이 최신 목록보다 뒤) 모드와 상관없이 알린다.
  const nextPending = !!preopen && (latestListDay === null || preopen.day > latestListDay);

  // 목록 시각은 서버가 준다(2026-10-02부터 08:38).
  const listTime = preopen ? shortSeoulTime(preopen.list_at).split(" ")[1] ?? "" : "";

  const morningTitle = (): string => {
    if (!preopen && preopenError) return "아침 상태를 불러오지 못했습니다. API 서버가 켜져 있는지 확인해 주세요.";
    if (!preopen) return "아침 상태를 불러오는 중…";
    if (snapshot === "MISSING") return `${dayLabel(preopen.day)} 목록을 만들지 못했습니다.`;
    if (!preopen.list_passed) return `다음 관찰 목록: ${dayLabel(preopen.day)} ${listTime}`;
    if (snapshot === "SUCCESS") return `${dayLabel(preopen.day)} 목록이 만들어졌습니다.`;
    return `${dayLabel(preopen.day)} 목록을 만드는 중입니다(${listTime}).`;
  };

  const statusLine = (): string => {
    if (mode === "pending") return "불러오는 중…";
    if (mode === "archive" && day) return `저장된 기록 · ${dayLabel(day)} 목록`;
    if (!state) return "";
    return `${statusLabel(state.status)}${state.source ? ` · ${sourceLabel(state.source)}` : ""}`;
  };

  return (
    <section className="live">
      <header className="live__head">
        <div>
          <h1 className="live__title">오늘의 관찰</h1>
          <p className="live__disclaimer">
            관찰 목록 — 매수 추천이 아닙니다. 그날 뉴스·공시·검색 급증이 있어 확인할 가치가 있던 종목입니다.
          </p>
          <p className="live__status">
            {mode === "live" && state && <span aria-hidden="true">{statusMark(state.status)} </span>}
            {statusLine()}
            {error ? ` · ${error}` : ""}
          </p>
        </div>
        <div className="live__controls">
          {dayOptions.length > 0 && (
            <select
              className="live__day"
              aria-label="목록 날짜"
              value={day ?? ""}
              onChange={(e) => {
                userPicked.current = true;
                setDay(e.target.value);
                setSelected(null);
              }}
            >
              {dayOptions.map((d) => (
                <option key={d} value={d}>
                  {dayLabel(d)}
                  {days && !days.includes(d) ? " (목록 없음·추적 종목 참고)" : ""}
                  {d === state?.day && liveHasList ? " · 실시간" : ""}
                </option>
              ))}
            </select>
          )}
          <div className="live__intervals" role="group" aria-label="봉 간격" hidden={empty || loading}>
            {(["1m", "1s"] as Interval[]).map((i) => (
              <button
                key={i}
                className={interval === i ? "live__interval live__interval--on" : "live__interval"}
                aria-pressed={interval === i}
                onClick={() => setInterval_(i)}
              >
                {i === "1m" ? "1분봉" : "1초봉"}
              </button>
            ))}
          </div>
        </div>
      </header>

      {nextPending && preopen && latestListDay !== null && (
        <div className="live__stale">
          <p>{morningTitle()}</p>
          <button className="live__link" aria-expanded={stepsOpen} onClick={() => setStepsOpen(!stepsOpen)}>
            {stepsOpen ? "아침 단계 접기" : "아침 단계 보기"}
          </button>
          {stepsOpen && <MorningStatus data={preopen} title="아침 단계" />}
        </div>
      )}

      <OvernightSemis day={day} />
      <ThemeNews listIds={listIds} listDay={day} onlyDay={day} />

      <div className="live__body">
        <div className="live__side">
          <p className="live__listhead">
            {loading
              ? "불러오는 중…"
              : empty
                ? "목록 없음"
                : `${day ? dayLabel(day) : ""} 목록 ${shown.length}종목${mode === "live" ? " · 등락은 전일 대비" : ""}`}
          </p>
          <ol className="live__list">
            {shown.map((m) => {
              const reasons = m.reasons.map((r) => LIST_REASON_LABEL[r] ?? r);
              return (
                <li key={m.code}>
                  <button
                    className={m.code === selected ? "live__item live__item--on" : "live__item"}
                    aria-current={m.code === selected ? "true" : undefined}
                    onClick={() => setSelected(m.code)}
                  >
                    <span className="live__row">
                      <span className="live__rank">{m.rank}</span>
                      <span className="live__name">{m.name}</span>
                      <Sparkline
                        values={mode === "live" ? sparks[m.code] : undefined}
                        change={m.last?.change_pct}
                      />
                      <span className={changeClass(m.last?.change_pct)}>
                        {signed(m.last?.change_pct, 2, "%")}
                      </span>
                    </span>
                    <span className="live__tags">
                      <HeavyweightBadge row={m} />
                      <PrevLimitBadge row={m} />
                      {reasons.slice(0, 3).map((r) => (
                        <span key={r} className="live__tag">
                          {r}
                        </span>
                      ))}
                      {reasons.length > 3 && <span className="live__tag">+{reasons.length - 3}</span>}
                    </span>
                  </button>
                </li>
              );
            })}
          </ol>
        </div>

        <div className="live__main">
          {empty &&
            (!noLists && mode === "archive" && archive?.failed ? (
              <p className="live__empty">그날 목록을 불러오지 못했습니다. 잠시 뒤 다시 시도합니다.</p>
            ) : !noLists && mode === "archive" && latestListDay !== null ? (
              <p className="live__empty">
                {day ? dayLabel(day) : "그날"}은 조건에 맞는 종목이 없었습니다.
              </p>
            ) : preopen ? (
              <MorningStatus data={preopen} title={morningTitle()} />
            ) : (
              <p className="live__empty">{morningTitle()}</p>
            ))}
          {member && (
            <div className="live__why">
              <div className="live__price">
                {/* 현재가는 실시간일 때만 있다. 기록 모드는 차트가 그날 가격을 보여 준다. */}
                {mode === "live" && (
                  <>
                    <span className="live__big">
                      {member.last ? member.last.price.toLocaleString("ko-KR") : "–"}
                    </span>
                    <span className={changeClass(member.last?.change_pct)}>
                      {signed(member.last?.change_pct, 2, "%")}
                    </span>
                  </>
                )}
                <span className="live__code">
                  {member.name} · {member.code}
                </span>
              </div>
              <div className="live__reasons">
                <HeavyweightBadge row={member} />
                <PrevLimitBadge row={member} />
                {member.reasons.map((r) => (
                  <span key={r} className="live__reason">
                    {LIST_REASON_LABEL[r] ?? r}
                  </span>
                ))}
              </div>
              {scoreRow && <AnalystLine analyst={scoreRow.analyst} showNone />}
              {scoreRow && hasScoreParts(scoreRow) && (
                <section className="live__scores" aria-label="아침 점수">
                  <p className="live__scoresTitle">아침 점수 (전 거래일 종가·재무 기준, 뉴스 점수는 합계에 들어가지 않음)</p>
                  <ScoreParts row={scoreRow} />
                </section>
              )}
              {heavyweightNote(member) && <p className="mw__note">{heavyweightNote(member)}</p>}
              {prevLimitNote(member) && <p className="mw__note">{prevLimitNote(member)}</p>}
              <dl className="live__facts">
                <div>
                  <dt>뉴스 점수</dt>
                  <dd>{signed(member.overlay_points)}</dd>
                </div>
                <div>
                  <dt>검색량</dt>
                  <dd>{member.attention_surge == null ? "–" : `${member.attention_surge.toFixed(2)}배`}</dd>
                </div>
              </dl>
              {member.events && member.events.length > 0 && (
                <section className="live__events" aria-labelledby="live-events-title">
                  <h2 id="live-events-title" className="live__eventsTitle">
                    근거 뉴스·공시
                  </h2>
                  {member.events.some((e) => e.kind) && (
                    <p className="live__explainGuide">
                      좋은 일/나쁜 일은 공시는 제목 규칙, 뉴스는 AI 판독입니다. '보통'은 9시 시가 기준 지난 기록이고 그대로 된다는
                      뜻이 아닙니다.
                    </p>
                  )}
                  <ul className="live__eventList">
                    {member.events.map((e, i) => {
                      const dir = e.sentiment > 0 ? "up" : e.sentiment < 0 ? "down" : "flat";
                      return (
                        <li key={i} className="live__event">
                          <span
                            className={`live__eventDir live__eventDir--${dir}`}
                            role="img"
                            aria-label={dir === "up" ? "좋은 소식" : dir === "down" ? "나쁜 소식" : "방향 없음"}
                          >
                            {dir === "up" ? "+" : dir === "down" ? "−" : "·"}
                          </span>
                          <span className="live__eventMeta">
                            {eventTypeLabel(e.event_type)}
                            {e.first_at ? ` · ${shortSeoulTime(e.first_at)}` : ""}
                            {countLabel(e.articles, e.disclosures ?? 0)}
                          </span>
                          {e.url ? (
                            <a className="live__eventTitle" href={e.url} target="_blank" rel="noopener noreferrer">
                              {e.title}
                              <span className="live__srOnly"> (새 탭)</span>
                            </a>
                          ) : (
                            <span className="live__eventTitle">{e.title}</span>
                          )}
                          {e.kind && (
                            <div className="live__explain">
                              <p>
                                <strong>{e.kind}</strong> — <span className="live__verdict">{e.verdict}</span>
                                {e.verdict_source && <span className="live__explainSrc"> ({e.verdict_source})</span>}
                              </p>
                              {e.what && <p>무슨 일: {e.what}</p>}
                              {e.why && <p>왜: {e.why}</p>}
                              {(e.usual || !e.usual_ours) && <p>보통: {e.usual ?? "기록 부족"}</p>}
                              {e.usual_ours && <p>우리 목록: {e.usual_ours}</p>}
                            </div>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </section>
              )}
            </div>
          )}
          {chartNote && <p className="live__chartNote">{chartNote}</p>}
          <div ref={container} className="live__chart" hidden={empty || loading} />
        </div>
      </div>
    </section>
  );
}
