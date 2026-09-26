import type { PreopenToday } from "../api/types";
import "./MorningStatus.css";

/** 단계 상태를 글자로. 색만으로 구분하지 않는다. */
const STATUS: Record<string, { mark: string; text: string }> = {
  SUCCESS: { mark: "✓", text: "끝남" },
  PARTIAL: { mark: "◐", text: "일부" },
  FAILED: { mark: "✕", text: "실패" },
  SKIPPED: { mark: "–", text: "건너뜀" },
  RUNNING: { mark: "…", text: "진행 중" },
  SLOW: { mark: "…", text: "오래 걸림" },
  MISSING: { mark: "✕", text: "만들지 못함" },
};
const WAITING = { mark: "○", text: "아직" };

/** "2026-09-28" → "9월 28일(월)". 날짜만 있는 값이라 한국 시간 자정으로 읽는다. */
export function dayLabel(day: string): string {
  const [y, m, d] = day.split("-").map(Number);
  const week = "일월화수목금토"[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
  return `${m}월 ${d}일(${week})`;
}

/**
 * 아침 흐름 체크리스트: 07:00 수집부터 08:50 목록까지 각 단계가 어디까지 왔나.
 *
 * 표시만 한다. 단계 이름과 순서는 서버(`/api/preopen/today`)가 실제 체인 순서로 준다.
 */
export function MorningStatus({ data, title }: { data: PreopenToday; title: string }) {
  return (
    <section className="morning" aria-label="아침 흐름 상태">
      <h2 className="morning__title">{title}</h2>
      <p className="morning__sub">
        {dayLabel(data.day)} 아침 ·{" "}
        {data.pool
          ? `후보 풀 ${data.pool.pool_count}종목${data.pool.status === "DEGRADED_FALLBACK" ? " (대체 풀)" : ""}`
          : "아직 후보 풀이 없습니다"}
      </p>
      <ol className="morning__steps">
        {data.stages.map((s) => {
          const st = (s.status && STATUS[s.status]) || WAITING;
          return (
            <li key={s.name} className="morning__step" data-status={s.status ?? "WAITING"}>
              <span className="morning__at">{s.at}</span>
              <span className="morning__label">{s.label}</span>
              <span className="morning__state">
                <span aria-hidden="true">{st.mark}</span> {st.text}
              </span>
              {s.note && <span className="morning__note">{s.note}</span>}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
