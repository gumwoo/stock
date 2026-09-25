import { useEffect, useState } from "react";
import { Dashboard } from "./pages/Dashboard";
import { InstrumentDetail } from "./pages/InstrumentDetail";
import { Backtest } from "./pages/Backtest";
import { Live } from "./pages/Live";
import { api } from "./api/client";
import type { Diagnostics } from "./api/types";
import "./App.css";

/** What each switched-off capability would add, in Korean. The API's own text stays English for its logs. */
const CAPABILITY_EFFECT: Record<string, string> = {
  toss_broker: "실계좌 연동과 토스증권 실시간 시세. 없으면 시세는 yfinance로 대신합니다(토스는 호출 IP 등록도 필요).",
  kis_quotes: "한국투자증권 1분봉과 실시간 차트.",
  sec_fundamentals: "미국 종목 재무. 없으면 미국 종목은 재무 요인 없이 채점합니다.",
  dart_fundamentals: "한국 종목 재무와 공시.",
  naver_news: "한국 뉴스와 검색 추세.",
  threads_social: "Threads 소셜 데이터.",
  reddit_social: "Reddit 소셜 데이터.",
  llm_sentiment: "LLM 뉴스 판정·해석.",
  email_alerts: "이메일 알림.",
  webhook_alerts: "웹훅 알림.",
};

/**
 * Application shell.
 *
 * The capability banner is not an error state. A system with no credentials
 * configured is a supported way to run: collectors without keys sit out and
 * everything else works. The banner says which value would switch each one on,
 * because that is the only actionable thing to show.
 */
export function App() {
  const [config, setConfig] = useState<Diagnostics | null>(null);
  const [detailId, setDetailId] = useState<number | null>(null);
  const [tab, setTab] = useState<"signals" | "live" | "backtest">("signals");

  useEffect(() => {
    api.config().then(setConfig).catch(() => setConfig(null));
  }, []);

  return (
    <div className="shell">
      <header className="shell__bar">
        <span className="shell__brand">주식 분석</span>
        <nav className="shell__tabs">
          <button
            className={tab === "signals" ? "shell__tab shell__tab--on" : "shell__tab"}
            onClick={() => {
              setTab("signals");
              setDetailId(null);
            }}
          >
            신호
          </button>
          <button
            className={tab === "live" ? "shell__tab shell__tab--on" : "shell__tab"}
            onClick={() => {
              setTab("live");
              setDetailId(null);
            }}
          >
            오늘의 관찰
          </button>
          <button
            className={tab === "backtest" ? "shell__tab shell__tab--on" : "shell__tab"}
            onClick={() => {
              setTab("backtest");
              setDetailId(null);
            }}
          >
            백테스트
          </button>
        </nav>
        <span className="shell__disclaimer">
          규칙 기반 분석입니다. 주문은 하지 않습니다.
        </span>
      </header>

      <main className="shell__main">
        {tab === "backtest" ? (
          <Backtest />
        ) : tab === "live" ? (
          <Live />
        ) : detailId === null ? (
          <Dashboard onOpenDetail={setDetailId} />
        ) : (
          <InstrumentDetail instrumentId={detailId} onBack={() => setDetailId(null)} />
        )}
      </main>

      {tab === "signals" && detailId === null && config && config.disabled.length > 0 && (
        <footer className="setup">
          <p className="setup__summary">
            선택 기능 {config.enabled.length}/{config.enabled.length + config.disabled.length}개가
            켜져 있습니다. 키가 없는 기능은 그 기능만 쉬고, 나머지는 그대로 돕니다.
          </p>
          <details>
            <summary>없는 키를 넣으면 켜지는 기능</summary>
            <ul>
              {config.disabled.map((d) => (
                <li key={d.name}>
                  <code>{d.set_to_enable.join(", ")}</code>
                  <span>{CAPABILITY_EFFECT[d.name] ?? d.effect}</span>
                </li>
              ))}
            </ul>
          </details>
        </footer>
      )}
    </div>
  );
}
