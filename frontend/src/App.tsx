import { useEffect, useState } from "react";
import { Dashboard } from "./pages/Dashboard";
import { InstrumentDetail } from "./pages/InstrumentDetail";
import { Backtest } from "./pages/Backtest";
import { api } from "./api/client";
import type { Diagnostics } from "./api/types";
import "./App.css";

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
  const [tab, setTab] = useState<"signals" | "backtest">("signals");

  useEffect(() => {
    api.config().then(setConfig).catch(() => setConfig(null));
  }, []);

  return (
    <div className="shell">
      <header className="shell__bar">
        <span className="shell__brand">stock</span>
        <nav className="shell__tabs">
          <button
            className={tab === "signals" ? "shell__tab shell__tab--on" : "shell__tab"}
            onClick={() => {
              setTab("signals");
              setDetailId(null);
            }}
          >
            Signals
          </button>
          <button
            className={tab === "backtest" ? "shell__tab shell__tab--on" : "shell__tab"}
            onClick={() => {
              setTab("backtest");
              setDetailId(null);
            }}
          >
            Backtest
          </button>
        </nav>
        <span className="shell__disclaimer">
          Rule-based analysis. No orders are placed.
        </span>
      </header>

      <main className="shell__main">
        {tab === "backtest" ? (
          <Backtest />
        ) : detailId === null ? (
          <Dashboard onOpenDetail={setDetailId} />
        ) : (
          <InstrumentDetail instrumentId={detailId} onBack={() => setDetailId(null)} />
        )}
      </main>

      {tab === "signals" && detailId === null && config && config.disabled.length > 0 && (
        <footer className="setup">
          <p className="setup__summary">{config.summary}</p>
          <details>
            <summary>What each missing key would enable</summary>
            <ul>
              {config.disabled.map((d) => (
                <li key={d.name}>
                  <code>{d.set_to_enable.join(", ")}</code>
                  <span>{d.effect}</span>
                </li>
              ))}
            </ul>
          </details>
        </footer>
      )}
    </div>
  );
}
