import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { CompanionsToday } from "../api/types";
import "./Companions.css";

/**
 * 장 초반 동반주: 개장 1~10분 매분 KIS 등락률 순위에서 +7% 이상 오른 "이끄는 종목"과, 그 종목과 함께 묶여 움직이곤 하는 "동반 후보".
 *
 * 표시 전용이다. 목록·점수에는 쓰지 않고 매매 권유가 아니다. 동반 후보의 출처는 둘이다:
 * - 이름 같음: 종목명 앞 두 글자가 같은 종목(예: 금호전기·금호건설). 관계없는 회사가 섞일 수 있다.
 * - AI 추정: 밤에 Claude에게 물은 관련주. 틀릴 수 있다(원형에서 틀린 이유도 있었다).
 * 등락률은 그 분의 순위나 현재가 조회 값이라 화면을 볼 때와 몇십 초 다를 수 있다. 덜 오른 후보가 위에 온다.
 */
export function Companions({ onlyDay }: { onlyDay?: string | null }) {
  const [data, setData] = useState<CompanionsToday | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .companions()
        .then((d) => {
          if (!alive) return;
          setData(d);
          setError(null);
        })
        .catch((e: Error) => alive && setError(e.message));
    load();
    const timer = window.setInterval(load, 30_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  if (error) return <p className="companions__note">장 초반 동반주를 불러오지 못했습니다 · {error}</p>;
  if (!data || !data.day || data.leaders.length === 0) return null;
  if (onlyDay !== undefined && data.day !== onlyDay) return null;

  const time = (iso: string | null) =>
    iso ? new Date(iso).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–";
  const pct = (v: number | null) => (v === null ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`);
  const cls = (v: number | null) => (v === null ? "" : v > 0 ? "companions__up" : v < 0 ? "companions__down" : "");
  const label = (s: string) => (s === "NAME" ? "이름 같음" : s === "AI" ? "AI 추정" : s);

  return (
    <section className="companions" aria-label="장 초반 동반주">
      <div className="companions__head">
        <h2 className="companions__title">장 초반 동반주</h2>
        <p className="companions__note">
          {time(data.at)} 순위 기준 · 오른 종목과 함께 묶이곤 하는 종목(덜 오른 순) · 참고용, 매매 권유 아님
          {data.last_run && data.last_run.status !== "SUCCESS" && (
            <> · 마지막 수집 {time(data.last_run.at)} {data.last_run.status}</>
          )}
        </p>
      </div>
      <ul className="companions__list">
        {data.leaders.map((l) => (
          <li key={l.instrument_id} className="companions__row">
            <span className="companions__leader">
              {l.name} <span className={cls(l.change_pct)}>{pct(l.change_pct)}</span>
            </span>
            <span className="companions__arrow" aria-hidden="true">
              →
            </span>
            <span className="companions__items">
              {l.companions.map((c) => (
                <span
                  key={c.instrument_id}
                  className="companions__item"
                  title={`${c.sources.map(label).join(" · ")}${c.reason ? ` — ${c.reason}` : ""} · 조회 ${time(c.fetched_at)}`}
                >
                  {c.name} <span className={cls(c.change_pct)}>{pct(c.change_pct)}</span>
                  <span className="companions__src">{c.sources.map(label).join("·")}</span>
                </span>
              ))}
            </span>
          </li>
        ))}
      </ul>
      <p className="companions__note">
        "이름 같음"은 이름 앞 두 글자가 같다는 뜻이라 관계없는 회사가 섞일 수 있고, "AI 추정"은 틀릴 수 있습니다. 개장 1~10분에만
        매분 갱신됩니다.
      </p>
    </section>
  );
}
