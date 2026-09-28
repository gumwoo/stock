import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { MarketWeightFields, OvernightSemis as OvernightData } from "../api/types";
import "./MarketWeight.css";

/**
 * 지수 대형주 표시와 밤사이 미국 반도체 한 줄. 둘 다 참고용이고 목록 선정·채점에는 쓰지 않는다.
 *
 * 대형주는 목록 날 이전 시가총액 순위표에서 그 시장 비중이 5% 이상인 종목이다(서버가 판정해 보낸다).
 */

export function HeavyweightBadge({ row }: { row: MarketWeightFields }) {
  if (!row.heavyweight) return null;
  return (
    <span className="mw__badge" title={heavyweightNote(row) ?? undefined}>
      지수 대형주
    </span>
  );
}

/** "KOSPI 비중 25.7% · 개별 뉴스보다 반도체 업황·시장 흐름의 영향이 큽니다". 대형주가 아니면 null. */
export function heavyweightNote(row: MarketWeightFields): string | null {
  if (!row.heavyweight || row.market_weight_pct == null) return null;
  const drivers = row.sector === "Semiconductors" ? "반도체 업황·시장 흐름" : "업황·시장 흐름";
  return `${row.market_listing ?? "시장"} 비중 ${row.market_weight_pct.toFixed(1)}% · 개별 뉴스보다 ${drivers}의 영향이 큽니다`;
}

function shortDay(iso: string): string {
  const [, m, d] = iso.split("-");
  return `${Number(m)}/${Number(d)}`;
}

function sessionsLabel(sessions: string[]): string {
  if (sessions.length === 0) return "";
  const first = shortDay(sessions[0]);
  const last = shortDay(sessions[sessions.length - 1]);
  return sessions.length === 1 ? `${first} 미국 장` : `${first}~${last} 미국 장 누적`;
}

/** 그 한국 거래일 개장 전에 끝난 미국 반도체 등락. 자료가 없으면 아무것도 그리지 않는다. */
export function OvernightSemis({ day }: { day: string | null }) {
  const [data, setData] = useState<OvernightData | null>(null);

  useEffect(() => {
    if (day === null) return;
    let alive = true;
    setData(null);
    api
      .overnightSemis(day)
      .then((d) => alive && setData(d))
      .catch(() => alive && setData(null)); // 참고용 한 줄이라 실패하면 조용히 뺀다
    return () => {
      alive = false;
    };
  }, [day]);

  const shown = data?.refs.filter((r) => r.change_pct !== null) ?? [];
  if (!data || shown.length === 0) return null;
  const label = sessionsLabel(shown[0].us_sessions);

  return (
    <p className="mw__overnight">
      <span className="mw__overnightHead">밤사이 미국 반도체{label ? ` (${label})` : ""}</span>
      {shown.map((r, i) => (
        <span key={r.code}>
          {i > 0 && <span className="mw__sep"> · </span>}
          {r.label}{" "}
          <span className={r.change_pct! > 0 ? "mw__up" : r.change_pct! < 0 ? "mw__down" : undefined}>
            {r.change_pct! > 0 ? "+" : ""}
            {r.change_pct!.toFixed(2)}%
          </span>
        </span>
      ))}
    </p>
  );
}
