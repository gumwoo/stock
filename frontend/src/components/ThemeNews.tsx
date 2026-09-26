import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { ThemeNewsDay } from "../api/types";
import "./ThemeNews.css";

/** 외부 기사 주소는 http(s)일 때만 링크로 연다(저장할 때도 거르지만 화면에서 한 번 더). */
const safe = (url: string) => /^https?:\/\//i.test(url);

/** 접힌 상태에서 보이는 칩 수. API가 기사 수 순으로 준다. */
const FOLDED = 6;
const EXPANDED_KEY = "stock.themes.expanded";

function readExpanded(): boolean {
  try {
    return window.localStorage.getItem(EXPANDED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeExpanded(value: boolean): void {
  try {
    window.localStorage.setItem(EXPANDED_KEY, value ? "1" : "0");
  } catch {
    // 저장이 막힌 환경(사생활 보호 창 등)에서는 기억하지 않을 뿐이다.
  }
}

/**
 * 테마어 뉴스: 전날 장 마감 뒤 테마어("AI 관련주", "원전" 등)로 찾은 기사 수와, 그 기사에 이름이 나온 종목.
 *
 * 표시 전용이다. 목록의 선정·순위·점수에는 들어가지 않는다. 밤사이 업종 연구와 NXT 후속 연구에서 이런
 * 신호로 9시나 8시에 사서 비용 뒤·평소(비교군) 대비 남는다는 근거를 찾지 못했으므로, 테마 기사가 많다는 것은
 * "살 이유"가 아니라 "볼 곳"이다.
 *
 * `listIds`는 오늘 아침 목록 종목의 id다. 같은 날의 목록일 때만 넘긴다(`listDay`로 확인). 테마 기사에 목록 종목이
 * 나오면 칩에 개수를 붙이고 상세에서 이름을 굵게 한다.
 */
export function ThemeNews({
  listIds,
  listDay,
}: {
  listIds: ReadonlySet<number> | null;
  listDay: string | null;
}) {
  const [data, setData] = useState<ThemeNewsDay | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<boolean>(readExpanded);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .themes()
        .then((d) => {
          if (!alive) return;
          setData(d);
          setError(null);
        })
        .catch((e: Error) => alive && setError(e.message));
    load();
    const timer = window.setInterval(load, 5 * 60_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  if (error) return <p className="themes__note">테마 뉴스를 불러오지 못했습니다 · {error}</p>;
  if (!data || data.themes.length === 0) return null;

  const first = data.themes[0];
  const selected = data.themes.find((t) => t.theme === open) ?? null;
  // 목록 겹침은 같은 날의 아침 목록일 때만 센다.
  const ids = listIds && listDay && listDay === data.day ? listIds : null;
  const onList = (theme: (typeof data.themes)[number]) =>
    ids ? theme.mentions.filter((m) => ids.has(m.instrument_id)).length : 0;
  // 접혀 있어도 지금 열어 둔 테마의 칩은 보이게 한다(상세만 떠 있고 칩이 사라지지 않게).
  const visible = expanded
    ? data.themes
    : data.themes.filter((t, i) => i < FOLDED || t.theme === open);
  const time = (iso: string) =>
    new Date(iso).toLocaleString("ko-KR", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  const toggle = () => {
    writeExpanded(!expanded);
    setExpanded(!expanded);
  };

  return (
    <section className="themes" aria-label="테마 뉴스">
      <div className="themes__head">
        <h2 className="themes__title">테마 뉴스</h2>
        <p className="themes__note">
          {time(first.since)} 장 마감 뒤 ~ {time(first.asked_at)} · 표시만 하고 목록 선정·점수에는 쓰지
          않습니다
        </p>
      </div>
      <div className="themes__chips">
        {visible.map((t) => {
          const hits = onList(t);
          return (
            <button
              key={t.theme}
              className={t.theme === open ? "themes__chip themes__chip--on" : "themes__chip"}
              aria-pressed={t.theme === open}
              onClick={() => setOpen(t.theme === open ? null : t.theme)}
              title={`검색어: ${t.query}`}
            >
              <span className="themes__name">{t.theme}</span>
              <span className="themes__count">
                {t.articles.toLocaleString("ko-KR")}
                {t.capped ? "+" : ""}
              </span>
              {hits > 0 && (
                <span className="themes__badge" aria-label={`오늘 목록 종목 ${hits}개`}>
                  목록 {hits}
                </span>
              )}
            </button>
          );
        })}
        {data.themes.length > FOLDED && (
          <button className="themes__more" aria-expanded={expanded} onClick={toggle}>
            {expanded ? "접기" : `전체 ${data.themes.length}개`}
          </button>
        )}
      </div>
      {selected && (
        <div className="themes__detail">
          <div className="themes__mentions">
            <span className="themes__label">기사에 나온 종목</span>
            {selected.mentions.length === 0 && <span className="themes__muted">없음</span>}
            {selected.mentions.map((m) => (
              <span
                key={m.instrument_id}
                className={
                  ids?.has(m.instrument_id) ? "themes__mention themes__mention--list" : "themes__mention"
                }
              >
                {m.name}
                {ids?.has(m.instrument_id) ? " (목록)" : ""}{" "}
                <span className="themes__muted">{m.articles}건</span>
              </span>
            ))}
          </div>
          <ul className="themes__headlines">
            {selected.headlines.map((h) => (
              <li key={h.url}>
                {safe(h.url) ? (
                  <a href={h.url} target="_blank" rel="noreferrer">
                    {h.title}
                  </a>
                ) : (
                  h.title
                )}
                <span className="themes__muted">
                  {" "}
                  · {h.host ?? ""} {time(h.published_at)}
                </span>
              </li>
            ))}
          </ul>
          <p className="themes__muted">
            종목 이름이 테마 기사에 나왔다는 뜻이지, 그 테마의 수혜주라는 뜻은 아닙니다.
          </p>
        </div>
      )}
    </section>
  );
}
