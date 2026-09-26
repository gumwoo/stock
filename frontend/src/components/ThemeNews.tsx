import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { ThemeNewsDay } from "../api/types";
import "./ThemeNews.css";

/**
 * 테마어 뉴스: 전날 장 마감 뒤 테마어("AI 관련주", "원전" 등)로 찾은 기사 수와, 그 기사에 이름이 나온 종목.
 *
 * 표시 전용이다. 목록의 선정·순위·점수에는 들어가지 않는다. 밤사이 업종 연구에서 이런 움직임은 9시 시가에
 * 이미 반영돼 있었으므로, 테마 기사가 많다는 것은 "살 이유"가 아니라 "볼 곳"이다.
 */
export function ThemeNews() {
  const [data, setData] = useState<ThemeNewsDay | null>(null);
  const [open, setOpen] = useState<string | null>(null);
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
  const time = (iso: string) =>
    new Date(iso).toLocaleString("ko-KR", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });

  return (
    <section className="themes" aria-label="테마 뉴스">
      <p className="themes__note">
        테마 뉴스 · {time(first.since)} 장 마감 뒤 ~ {time(first.asked_at)} · 표시만 하고 목록 선정·점수에는
        쓰지 않습니다
      </p>
      <div className="themes__chips">
        {data.themes.map((t) => (
          <button
            key={t.theme}
            className={t.theme === open ? "themes__chip themes__chip--on" : "themes__chip"}
            onClick={() => setOpen(t.theme === open ? null : t.theme)}
            title={`검색어: ${t.query}`}
          >
            <span className="themes__name">{t.theme}</span>
            <span className="themes__count">
              {t.articles.toLocaleString("ko-KR")}
              {t.capped ? "+" : ""}
            </span>
          </button>
        ))}
      </div>
      {selected && (
        <div className="themes__detail">
          <div className="themes__mentions">
            <span className="themes__label">기사에 나온 종목</span>
            {selected.mentions.length === 0 && <span className="themes__muted">없음</span>}
            {selected.mentions.map((m) => (
              <span key={m.instrument_id} className="themes__mention">
                {m.name} <span className="themes__muted">{m.articles}건</span>
              </span>
            ))}
          </div>
          <ul className="themes__headlines">
            {selected.headlines.map((h) => (
              <li key={h.url}>
                <a href={h.url} target="_blank" rel="noreferrer">
                  {h.title}
                </a>
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
