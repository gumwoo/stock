import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { LIST_REASON_LABEL } from "../api/format";
import type {
  LabCondition,
  LabDay,
  LabGridCell,
  LabHypothesis,
  LabListHypothesis,
  LabPickRow,
  LabPickSummary,
  LabPickTrack,
  LabRuleStat,
  LabView,
} from "../api/types";
import "./Research.css";

/**
 * 연구 탭 = 전략 실험실: 매일 쌓이는 아침 목록 기록으로 손절·익절 규칙과 종목 조건을 비교하고, 미리 고정한 가설을 앞으로 잰다.
 *
 * 숫자는 모두 `/api/lab`(backend/app/services/lab_service.py)이 준다. 수익률은 9시 시가 매수, 기존 연구와 같은 체결 모델
 * (entry_rules.r3), 비용 0.30% 뒤다. 비교표는 탐색이라 판정하지 않고, 판정은 고정 가설에만 한다. 매매 권유가 아니다.
 */

const MARKETS = ["전체", "KOSPI", "KOSDAQ"] as const;
const MARKET_LABEL: Record<string, string> = { 전체: "전체", KOSPI: "코스피", KOSDAQ: "코스닥" };

function pct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

function share(v: number | null | undefined): string {
  return v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`;
}

function num(v: number | null | undefined, digits = 1): string {
  return v === null || v === undefined ? "–" : v.toFixed(digits);
}

function ruleLabel(take: number, stop: number | null): string {
  const t = `+${+(take * 100).toFixed(1)}% 익절`;
  return stop === null ? `${t} · 손절 없음(10시 매도)` : `${t} · -${+(stop * 100).toFixed(1)}% 손절`;
}

function stopLabel(stop: number | null): string {
  return stop === null ? "손절 없음" : `-${+(stop * 100).toFixed(1)}%`;
}

function sign(v: number | null | undefined): string {
  if (v === null || v === undefined) return "";
  return v > 0 ? "lab__pos" : v < 0 ? "lab__neg" : "";
}

function statTitle(s: LabRuleStat): string {
  return `종목일 ${s.n} · 날짜 ${s.days}일 · t ${num(s.t, 2)} · 앞 절반 ${pct(s.first)} / 뒤 절반 ${pct(s.second)} · ${s.flag}`;
}

function Flag({ text }: { text: string }) {
  const cls =
    text === "앞뒤 같은 방향" ? "lab__flag lab__flag--same" : text === "앞뒤 갈림" ? "lab__flag lab__flag--split" : "lab__flag";
  return <span className={cls}>{text}</span>;
}

function Section({ title, badge, children }: { title: string; badge?: string; children: React.ReactNode }) {
  return (
    <section className="lab__section">
      <h2 className="lab__h2">
        {title}
        {badge && <span className="lab__badge">{badge}</span>}
      </h2>
      {children}
    </section>
  );
}

// --- 1. 매일 성적표 --------------------------------------------------------------------------------------

function excludedText(d: LabDay): string {
  const parts = Object.entries(d.excluded).map(([k, v]) => `${k} ${v}`);
  return parts.length ? parts.join(", ") : "없음";
}

function DaysTable({ days, overall }: { days: LabDay[]; overall: LabRuleStat[] }) {
  const headline = days[0]?.rules ?? [];
  return (
    <div className="lab__tablewrap">
      <table className="lab__table">
        <thead>
          <tr>
            <th>날짜</th>
            <th className="lab__num">측정/목록</th>
            <th className="lab__num">코스피·코스닥</th>
            <th className="lab__num">+2.5% 10분 안</th>
            <th className="lab__num">+2.5% 1시간 안</th>
            <th className="lab__num">+5% 1시간 안</th>
            {headline.map((r) => (
              <th key={`${r.take}-${r.stop}`} className="lab__num">
                {ruleLabel(r.take, r.stop)}
              </th>
            ))}
            <th className="lab__num">10시(비용 전)</th>
            <th className="lab__num">목록에서 뺀 종목({headline[0] ? ruleLabel(headline[0].take, headline[0].stop) : ""})</th>
            <th>잴 수 없던 종목</th>
          </tr>
        </thead>
        <tbody>
          {[...days].reverse().map((d) => (
            <tr key={d.day}>
              <td>
                {d.day.slice(5).replace("-", "/")}
                {d.pending && <span className="lab__pending">수집 중</span>}
              </td>
              <td className="lab__num">
                {d.measured}/{d.members}
              </td>
              <td className="lab__num">
                {d.kospi}·{d.kosdaq}
              </td>
              <td className="lab__num">{share(d.hit25_10)}</td>
              <td className="lab__num">{share(d.hit25_60)}</td>
              <td className="lab__num">{share(d.hit5_60)}</td>
              {d.rules.map((r) => (
                <td key={`${r.take}-${r.stop}`} className={`lab__num ${sign(r.mean)}`}>
                  {pct(r.mean)}
                </td>
              ))}
              <td className={`lab__num ${sign(d.at_ten)}`}>{pct(d.at_ten)}</td>
              <td className={`lab__num ${sign(d.excluded_rules?.[0]?.mean)}`}>
                {d.excluded_names ? `${d.excluded_names}개 ${pct(d.excluded_rules[0]?.mean)}` : "–"}
              </td>
              <td className="lab__small">{excludedText(d)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td colSpan={6}>누적(날짜별 평균의 평균)</td>
            {headline.map((r) => {
              const cell = overall.find((c) => c.take === r.take && c.stop === r.stop);
              return (
                <td key={`${r.take}-${r.stop}`} className={`lab__num ${sign(cell?.mean)}`} title={cell ? statTitle(cell) : ""}>
                  {pct(cell?.mean)}
                </td>
              );
            })}
            <td colSpan={3} />
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

// --- 2. 규칙 비교표 --------------------------------------------------------------------------------------

function Grid({ cells, market, caption }: { cells: LabGridCell[]; market: string; caption: string }) {
  const pick = cells.filter((c) => c.market === market);
  const takes = [...new Set(pick.map((c) => c.take))];
  const stops = [...new Set(pick.map((c) => c.stop))];
  const days = pick[0]?.days ?? 0;
  return (
    <div className="lab__grid lab__tablewrap">
      <p className="lab__caption">
        {caption} · {days}일
      </p>
      <table className="lab__table">
        <thead>
          <tr>
            <th>익절 \ 손절</th>
            {stops.map((s) => (
              <th key={String(s)} className="lab__num">
                {stopLabel(s)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {takes.map((t) => (
            <tr key={t}>
              <th scope="row">+{+(t * 100).toFixed(1)}%</th>
              {stops.map((s) => {
                const c = pick.find((x) => x.take === t && x.stop === s);
                return (
                  <td key={String(s)} className={`lab__num ${sign(c?.mean)}`} title={c ? statTitle(c) : ""}>
                    {pct(c?.mean)}
                    {c && c.flag !== "앞뒤 같은 방향" && (
                      <>
                        <span className="lab__mark" aria-hidden="true">
                          {c.flag === "표본 부족" ? "·" : "↕"}
                        </span>
                        <span className="lab__sr"> ({c.flag})</span>
                      </>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --- 3. 종목 조건 비교 -----------------------------------------------------------------------------------

function conditionLabel(feature: string, label: string): string {
  if (feature === "목록 이유") return LIST_REASON_LABEL[label] ?? label;
  if (feature === "시장") return MARKET_LABEL[label] ?? label;
  return label.replace("KOSPI", "코스피").replace("KOSDAQ", "코스닥");
}

function Conditions({ rows }: { rows: LabCondition[] }) {
  // 기록(우리 목록/기준표)을 바꾸면 key로 다시 마운트되어 첫 조건부터 보인다.
  const features = [...new Set(rows.map((r) => r.feature))];
  const [feature, setFeature] = useState(features[0] ?? "");
  const pick = rows.filter((r) => r.feature === feature);
  const rules = pick[0]?.rules ?? [];
  return (
    <>
      <div className="lab__chips" role="group" aria-label="조건">
        {features.map((f) => (
          <button
            key={f}
            aria-pressed={f === feature}
            className={f === feature ? "lab__chip lab__chip--on" : "lab__chip"}
            onClick={() => setFeature(f)}
          >
            {f}
          </button>
        ))}
      </div>
      <div className="lab__tablewrap">
        <table className="lab__table">
          <thead>
            <tr>
              <th>{feature}</th>
              <th className="lab__num">종목일</th>
              <th className="lab__num">+2.5% 1시간 안</th>
              {rules.map((r) => (
                <th key={`${r.take}-${r.stop}`} className="lab__num">
                  {ruleLabel(r.take, r.stop)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {pick.map((row) => (
              <tr key={row.label}>
                <td>{conditionLabel(row.feature, row.label)}</td>
                <td className="lab__num">{row.n}</td>
                <td className="lab__num">{share(row.hit25)}</td>
                {row.rules.map((r) => (
                  <td key={`${r.take}-${r.stop}`} className={`lab__num ${sign(r.mean)}`} title={statTitle(r)}>
                    {pct(r.mean)} <Flag text={r.flag} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

// --- 4. 가설 추적 ----------------------------------------------------------------------------------------

function HypothesisCard({ h, frozenAt }: { h: LabHypothesis; frozenAt: string }) {
  const a = h.after;
  return (
    <article className="lab__hyp">
      <header className="lab__hyphead">
        <span className="lab__key">{h.key}</span>
        <span className={a.state === "성립" ? "lab__state lab__state--yes" : "lab__state"}>{a.state}</span>
      </header>
      <p className="lab__hyptext">{h.text}</p>
      <p className="lab__small">
        규칙 {ruleLabel(h.take, h.stop)} · 예측 {h.sign > 0 ? "0보다 크다" : "0보다 작다"} · 고정{" "}
        {new Date(frozenAt).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" })}
      </p>
      <dl className="lab__hypnums">
        <div>
          <dt>고정 뒤 우리 목록</dt>
          <dd>
            {a.days}/60일 · 종목일 {a.n} · 평균 <span className={sign(a.mean)}>{pct(a.mean)}</span> · t {num(a.t, 2)}
          </dd>
        </div>
        <div>
          <dt>고정 전 근거: 3개월 기준표</dt>
          <dd>
            {h.reference ? (
              <>
                {h.reference.days}일 · 종목일 {h.reference.n} · 평균{" "}
                <span className={sign(h.reference.mean)}>{pct(h.reference.mean)}</span> · t {num(h.reference.t, 2)} · 앞{" "}
                {pct(h.reference.first)} / 뒤 {pct(h.reference.second)}
              </>
            ) : (
              "없음"
            )}
          </dd>
        </div>
        <div>
          <dt>고정 전 우리 목록(참고)</dt>
          <dd>
            {h.before.days}일 · 종목일 {h.before.n} · 평균 <span className={sign(h.before.mean)}>{pct(h.before.mean)}</span>
          </dd>
        </div>
      </dl>
      <p className="lab__basis">{h.basis}</p>
    </article>
  );
}

// --- S3: 3개 겹침 + 기술 상위 2 추적 ----------------------------------------------------------------------

const PHASE_LABEL: Record<LabPickRow["phase"], string> = {
  after: "판정에 셈",
  before: "고정 전(참고)",
  late: "늦게 만든 목록(셈 안 함)",
};

function hm(label: string | null | undefined): string {
  return label ? `${label.slice(0, 2)}:${label.slice(2)}` : "–";
}

function LevelsTable({ s, title }: { s: LabPickSummary; title: string }) {
  return (
    <div className="lab__tablewrap">
      <table className="lab__table">
        <caption className="lab__small">
          {title}: {s.days}일 · {s.measured}종목 · 10시 전 최대 중앙값 {pct(s.median_max_ten)} · 장중 최대 중앙값{" "}
          {pct(s.median_max_day)} · 최대 전에 먼저 빠진 깊이 중앙값 {pct(s.median_dip)}
        </caption>
        <thead>
          <tr>
            <th>시가 대비</th>
            {s.levels.map((l) => (
              <th key={l.level} className="lab__num">
                +{+(l.level * 100).toFixed(1)}%
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>10시 전 체결</td>
            {s.levels.map((l) => (
              <td key={l.level} className="lab__num">
                {share(l.ten)}
              </td>
            ))}
          </tr>
          <tr>
            <td>장 마감까지 체결</td>
            {s.levels.map((l) => (
              <td key={l.level} className="lab__num">
                {share(l.day)}
              </td>
            ))}
          </tr>
          <tr>
            <td>10시 전 최대가 나온 시각</td>
            <td colSpan={s.levels.length} className="lab__small">
              {Object.entries(s.peak_ten_at)
                .map(([k, v]) => `${k} ${v}`)
                .join(" · ")}
            </td>
          </tr>
          <tr>
            <td>장중 최대가 나온 시각</td>
            <td colSpan={s.levels.length} className="lab__small">
              {Object.entries(s.peak_day_at)
                .map(([k, v]) => `${k} ${v}`)
                .join(" · ")}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

function PickTrack({ t }: { t: LabPickTrack }) {
  const a = t.after;
  const j = a.judged;
  const rows = [...t.rows].reverse();
  return (
    <>
      <article className="lab__hyp">
        <header className="lab__hyphead">
          <span className="lab__key">{t.key}</span>
          <span className={j.state === "성립" ? "lab__state lab__state--yes" : "lab__state"}>{j.state}</span>
        </header>
        <p className="lab__hyptext">{t.text}</p>
        <p className="lab__small">
          예측: 날마다 고른 종목 중 +{+(t.take * 100).toFixed(1)}% 체결 비율이 {Math.round(t.base * 100)}%보다 높다 · 고정{" "}
          {new Date(t.frozen_at).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" })}
        </p>
        <dl className="lab__hypnums">
          <div>
            <dt>고정 뒤 우리 목록</dt>
            <dd>
              {j.days}/60일 · 종목 {j.n} · 날짜 평균 도달률{" "}
              {j.mean === null ? "–" : `${Math.round((j.mean + t.base) * 100)}%`} · t {num(j.t, 2)} · 앞{" "}
              {j.first === null ? "–" : `${Math.round((j.first + t.base) * 100)}%`} / 뒤{" "}
              {j.second === null ? "–" : `${Math.round((j.second + t.base) * 100)}%`}
            </dd>
          </div>
          <div>
            <dt>참고(판정 아님)</dt>
            <dd>
              50% 넘은 날 {a.wins} · 못 넘은 날 {a.losses} · 부호 검정 p {a.sign_p === null ? "–" : a.sign_p.toFixed(3)} · +
              {+(t.take * 100).toFixed(1)}% 지정가(10시 매도, 비용 뒤) 평균 {pct(a.ret_take_mean)} · 잴 수 없던 종목 {a.unmeasured}
            </dd>
          </div>
        </dl>
        <p className="lab__basis">{t.basis}</p>
      </article>
      <LevelsTable s={a} title="고정 뒤" />
      {t.before.measured > 0 && <LevelsTable s={t.before} title="고정 전(참고, 결과를 보고 만든 조건)" />}
      <div className="lab__tablewrap">
        <table className="lab__table">
          <thead>
            <tr>
              <th>날짜</th>
              <th>종목</th>
              <th className="lab__num">순위</th>
              <th className="lab__num">기술</th>
              <th>겹친 이유</th>
              <th className="lab__num">10시 전 최대</th>
              <th className="lab__num">장중 최대</th>
              <th className="lab__num">최대 전 최저</th>
              <th className="lab__num">마감</th>
              <th>+2% 체결</th>
              <th>구분</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={11}>아직 고른 종목이 없습니다.</td>
              </tr>
            )}
            {rows.map((r) => {
              const p = r.peak;
              const hit = r.levels[String(t.take)];
              return (
                <tr key={`${r.day}-${r.instrument_id}`} className={r.phase === "after" ? "" : "lab__muted"}>
                  <td>{r.day.slice(5).replace("-", "/")}</td>
                  <td>{r.name}</td>
                  <td className="lab__num">{r.rank}</td>
                  <td className="lab__num">{num(r.technical, 0)}</td>
                  <td>{r.reasons.map((x) => LIST_REASON_LABEL[x] ?? x).join(" · ")}</td>
                  {p ? (
                    <>
                      <td className="lab__num">
                        <span className={sign(p.max_ten)}>{pct(p.max_ten, 1)}</span> {hm(p.max_ten_at)}
                      </td>
                      <td className="lab__num">
                        <span className={sign(p.max_day)}>{pct(p.max_day, 1)}</span> {hm(p.max_day_at)}
                        {!p.after_ten && <span className="lab__pending">10시 뒤 거래 없음</span>}
                      </td>
                      <td className="lab__num">{pct(p.dip_before_peak, 1)}</td>
                      <td className="lab__num">
                        <span className={sign(p.last)}>{pct(p.last, 1)}</span> {hm(p.last_at)}
                      </td>
                      <td>{hit?.ten ? `10시 전 ${hm(hit.ten)}` : hit?.day ? `10시 뒤 ${hm(hit.day)}` : "안 닿음"}</td>
                    </>
                  ) : (
                    <td colSpan={5}>{r.unmeasured}</td>
                  )}
                  <td>{PHASE_LABEL[r.phase]}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="lab__caption">
        고르기: 목록에 남은 종목 중 좋은 뉴스·공시·뉴스 급증·검색 급증 가운데 3개 이상이 겹친 종목을 기술 점수 순(같으면 목록
        순위)으로 2개. 1분봉이 있든 없든 먼저 고르고, 잴 수 없으면 이유만 남깁니다. 최대·최저·마감은 9시 시가 대비(비용 전)이고
        최대는 09:00 봉 고가까지 포함한 원시 고가입니다. "체결"은 성적표와 같은 판정(09:01부터, 목표가 호가 올림, 고가가 목표가를
        넘거나 봉 시가가 목표가 이상이어야)이라 최대 %가 +2%를 넘어도 체결로 안 칠 수 있습니다. 1분봉은 장 마감 뒤 16:20에 받습니다.
      </p>
    </>
  );
}

const LIST_HYP_TEXT: Record<string, string> = {
  H1: "좋은 뉴스 종목이 그날 목록 평균보다 낫다(시가→종가)",
  H2: "나쁜 뉴스 종목이 그날 목록 평균보다 못하다(시가→종가)",
  H3: "검색 급증 종목이 첫 1시간에 나머지보다 낫다",
  H4: "1~10위가 11~40위보다 낫다(시가→종가)",
  H5: "목록 전체가 자기 시장 지수보다 낫다",
};
const LIST_STATE: Record<string, string> = {
  "not enough days": "기록 중(20일 전)",
  reading: "읽는 중(판정 아님)",
  established: "성립",
  "not established": "성립 안 함",
};

function ListHypotheses({ rows }: { rows: LabListHypothesis[] }) {
  if (!rows.length) return null;
  return (
    <div className="lab__tablewrap">
      <p className="lab__caption">
        아침 목록 가설 H1~H7(2026-09-26 고정, 첫 봉 시가 기준·비용 전). 같은 문턱(20일 읽기, 처음 60일로 한 번 판정)으로 잽니다.
      </p>
      <table className="lab__table">
        <thead>
          <tr>
            <th />
            <th>질문</th>
            <th className="lab__num">날짜</th>
            <th className="lab__num">평균</th>
            <th className="lab__num">t</th>
            <th className="lab__num">앞 / 뒤 절반</th>
            <th>상태</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              <td className="lab__key">{r.key}</td>
              <td>{LIST_HYP_TEXT[r.key] ?? r.text}</td>
              <td className="lab__num">{r.days}</td>
              <td className={`lab__num ${sign(r.mean)}`}>{pct(r.mean)}</td>
              <td className="lab__num">{num(r.t, 2)}</td>
              <td className="lab__num">
                {pct(r.first)} / {pct(r.second)}
              </td>
              <td>{LIST_STATE[r.state] ?? r.state}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --- 화면 ----------------------------------------------------------------------------------------------------

function cellName(c: LabGridCell): string {
  return `${MARKET_LABEL[c.market] ?? c.market} ${ruleLabel(c.take, c.stop)}`;
}

function bestCell(cells: LabGridCell[]): LabGridCell | null {
  return cells.reduce<LabGridCell | null>(
    (b, c) => (c.mean !== null && (b === null || (b.mean ?? -1e9) < c.mean) ? c : b),
    null,
  );
}

function GridLine({ cells, label }: { cells: LabGridCell[]; label: string }) {
  const b = bestCell(cells);
  const positive = cells.filter((c) => (c.mean ?? 0) > 0).length;
  return (
    <>
      {label}의 규칙 {cells.length}칸(시장 전체·코스피·코스닥 × 익절 3 × 손절 5) 중 비용 뒤 평균이 0보다 큰 칸은 {positive}개
      {b && (
        <>
          , 가장 높은 칸은 {cellName(b)} {pct(b.mean)}
        </>
      )}
      입니다.
    </>
  );
}

function Summary({ view }: { view: LabView }) {
  return (
    <div className="lab__summary">
      <p>
        <strong>지금까지:</strong>{" "}
        <GridLine cells={view.grid.reference} label={`3개월 기준표(${view.reference_meta.days ?? "–"}일)`} />{" "}
        <GridLine cells={view.grid.ours} label={`우리 목록(${view.days.length}일)`} /> 손절·익절 숫자만 바꿔서 비용 뒤에 남는다는
        근거는 아직 없습니다. 칸이 많아 우연히 0보다 큰 칸이 나올 수 있어서 판정은 아래 고정 가설로만 합니다.
      </p>
    </div>
  );
}

export function Research() {
  const [view, setView] = useState<LabView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [market, setMarket] = useState<(typeof MARKETS)[number]>("전체");
  const [source, setSource] = useState<"ours" | "reference">("ours");

  useEffect(() => {
    let alive = true;
    api
      .lab()
      .then((v) => alive && setView(v))
      .catch((e: Error) => alive && setError(e.message));
    return () => {
      alive = false;
    };
  }, []);

  const conditionRows = useMemo(() => (view ? view.conditions[source] : []), [view, source]);

  if (error) return <p className="lab__error">불러오지 못했습니다: {error}</p>;
  if (!view) return <p className="lab__loading">불러오는 중…</p>;

  return (
    <div className="lab">
      <header className="lab__header">
        <h1 className="lab__h1">전략 실험실</h1>
        <p className="lab__lead">
          매일 쌓이는 아침 목록 기록으로 손절·익절 규칙과 종목 조건을 비교하고, 미리 고정한 가설을 앞으로 잽니다. 숫자는 모두
          지난 기록이고 매매 권유가 아닙니다.
        </p>
        <p className="lab__rule">
          9시 시가(09:00 봉 시가)에 샀다고 보고, 익절·손절은 09:01부터 기존 연구와 같은 체결 모델(목표가는 호가 올림·목표가 체결,
          손절은 1호가 아래 체결, 봉 시가가 이미 넘었으면 그 시가)로 팝니다. 둘 다 안 닿으면 10시 전 마지막 종가. 비용{" "}
          {view.cost_pct.toFixed(2)}%를 뺐습니다. 평균은 그날 평균 하나를 관측 하나로 센 날짜 기준입니다.
        </p>
      </header>

      <Summary view={view} />

      <Section title="매일 성적표" badge="우리 목록">
        <p className="lab__caption">
          화면·카톡에 나간 목록 종목 중 그날 1분봉으로 잴 수 있는 종목만 셉니다(1분봉은 장 마감 뒤 16:20에 받습니다). 도달률은
          손절 없이 그 시간 안에 목표가에 닿아 팔린 비율입니다. 2026-10-06 목록부터는 판단 점수 40 미만·전일 +15% 이상 종목을
          목록에서 빼고(원래 순위 유지), 뺀 종목의 결과는 따로 한 칸에 둡니다 — 빼는 규칙이 계속 맞는지 보려고.
        </p>
        <DaysTable days={view.days} overall={view.kept_rules} />
      </Section>

      <Section title="규칙 비교표" badge="탐색 — 판정 아님">
        <div className="lab__chips" role="group" aria-label="시장">
          {MARKETS.map((m) => (
            <button
              key={m}
                aria-pressed={m === market}
              className={m === market ? "lab__chip lab__chip--on" : "lab__chip"}
              onClick={() => setMarket(m)}
            >
              {MARKET_LABEL[m]}
            </button>
          ))}
        </div>
        <div className="lab__grids">
          <Grid cells={view.grid.ours} market={market} caption="우리 목록" />
          <Grid cells={view.grid.reference} market={market} caption="3개월 공시 기준표" />
        </div>
        <p className="lab__caption">
          칸의 숫자는 비용 뒤 평균입니다. ↕는 앞·뒤 절반의 방향이 갈린 칸, ·는 날짜가 20일보다 적은 칸입니다. 칸에 마우스를 올리면
          종목일·날짜·t·앞뒤 절반이 보입니다. 칸이 많아서 우연히 좋아 보이는 칸이 섞입니다.
        </p>
      </Section>

      <Section title="종목 조건 비교" badge="탐색 — 판정 아님">
        <div className="lab__chips" role="group" aria-label="기록">
          <button
            aria-pressed={source === "ours"}
            className={source === "ours" ? "lab__chip lab__chip--on" : "lab__chip"}
            onClick={() => setSource("ours")}
          >
            우리 목록
          </button>
          <button
            aria-pressed={source === "reference"}
            className={source === "reference" ? "lab__chip lab__chip--on" : "lab__chip"}
            onClick={() => setSource("reference")}
          >
            3개월 공시 기준표
          </button>
        </div>
        <Conditions key={source} rows={conditionRows} />
        <p className="lab__caption">
          한 종목이 여러 이유로 목록에 들면 여러 줄에 함께 들어갑니다. 지수 대형주도 포함합니다(list-review는 따로 뺍니다).
        </p>
      </Section>

      <Section title="3개 겹침 + 기술 상위 2 추적" badge="고정 뒤 기록으로만 판정">
        {view.pick_track ? (
          <PickTrack t={view.pick_track} />
        ) : (
          <p className="lab__error">이 섹션을 계산하지 못했습니다(나머지 화면은 그대로입니다).</p>
        )}
      </Section>

      <Section title="가설 추적" badge="고정 뒤 기록으로만 판정">
        <p className="lab__caption">
          가설은 결과를 보기 전에 고정했고, 고정 뒤에 얼린 목록 날만 셉니다. 20일까지는 기록만, 처음 60일로 한 번만 판정합니다
          (평균이 예측 방향, |t| ≥ 2, 앞·뒤 절반 모두 예측 방향). 매일 다시 묻지 않습니다.
        </p>
        <div className="lab__hyps">
          {view.hypotheses.map((h) => (
            <HypothesisCard key={h.key} h={h} frozenAt={view.frozen_at} />
          ))}
        </div>
        <ListHypotheses rows={view.list_hypotheses} />
      </Section>

      <footer className="lab__limits">
        <h2 className="lab__h3">한계</h2>
        <ul>
          <li>3개월 기준표는 한 장세(2026-06-30~09-21)의 공시가 있던 종목이고, 지금 상장된 종목만 들어 있습니다.</li>
          <li>칸과 조건이 많아 몇 칸은 우연히 좋아 보입니다. 그래서 비교표는 판정하지 않습니다.</li>
          <li>
            시가 갭은 실제로는 9시 체결 순간에야 확정됩니다(그 전에는 예상체결가로 짐작). 손절은 09:01부터 봅니다 — 09:00 봉
            안에서 이미 손절선 밑으로 갔다 온 경우는 손절로 치지 않습니다.
          </li>
          <li>09:00 봉이 없는 종목(첫 체결이 늦음)과 시초 잠김 종목은 뺍니다. 그래서 큰 갭 종목이 덜 잡힐 수 있습니다.</li>
        </ul>
      </footer>
    </div>
  );
}
