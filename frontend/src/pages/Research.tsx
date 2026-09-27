import { useState } from "react";
import studiesData from "../research/studies.json";
import { Backtest } from "./Backtest";
import "./Research.css";

/**
 * 연구: 질문과 판정 규칙을 결과보다 먼저 고정하고 잰 것들.
 *
 * 연구 결과는 DB에 없다. `research/studies.json`의 표 행은 `docs/studies/*.md`의 판정 표를 그대로 옮긴 것이고,
 * `backend/tests/unit/test_research_screen.py`가 칸 단위로 같은지 확인한다. 새 연구가 끝나면 그 파일에 항목을 더한다.
 * 기존 일봉 백테스트 화면은 맨 아래 카드 안에서 펼칠 때만 불러온다.
 */

interface StudyRow {
  key: string;
  question: string;
  value: string;
  t: string;
  halves: string;
  holdout: string;
  verdict: string;
}

interface Study {
  id: string;
  title: string;
  period: string;
  question: string;
  conclusion: string;
  badge: string;
  note: string;
  columns: string[];
  doc: string;
  section: string;
  limits: string;
  commit: string;
  cli: string;
  rows: StudyRow[];
}

const STUDIES = studiesData as Study[];

function Card({
  title,
  badge,
  note,
  period,
  summary,
  initiallyOpen,
  children,
}: {
  title: string;
  badge: string;
  note?: string;
  period?: string;
  summary: string;
  initiallyOpen: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(initiallyOpen);
  // 제목은 h2 안의 버튼(이름은 제목·배지·보조 표기), 기간·요약은 버튼 밖에 둔다. 접혀 있어도 기간이 보여야
  // 결론이 일반론으로 읽히지 않는다.
  return (
    <section className="rs__card">
      <h2 className="rs__h2">
        <button className="rs__head" aria-expanded={open} onClick={() => setOpen(!open)}>
          <span className="rs__title">{title}</span>
          <span className="rs__badge">{badge}</span>
          {note && <span className="rs__note">{note}</span>}
          <span className="rs__toggle" aria-hidden="true">
            {open ? "접기" : "자세히"}
          </span>
        </button>
      </h2>
      {period && <p className="rs__period">{period}</p>}
      <p className="rs__summary">{summary}</p>
      {open && <div className="rs__body">{children}</div>}
    </section>
  );
}

function StudyBody({ study }: { study: Study }) {
  return (
    <>
      <p className="rs__q">
        <span className="rs__label">질문</span> {study.question}
      </p>
      <div className="rs__tablewrap">
        <table className="rs__table">
          <thead>
            <tr>
              <th />
              <th>질문</th>
              <th className="rs__num">{study.columns[0]}</th>
              <th className="rs__num">{study.columns[1]}</th>
              <th className="rs__num">{study.columns[2]}</th>
              <th className="rs__num">{study.columns[3]}</th>
              <th>판정</th>
            </tr>
          </thead>
          <tbody>
            {study.rows.map((r) => (
              <tr key={r.key}>
                <td className="rs__key">{r.key}</td>
                <td>{r.question}</td>
                <td className="rs__num">{r.value}</td>
                <td className="rs__num">{r.t}</td>
                <td className="rs__num">{r.halves}</td>
                <td className="rs__num">{r.holdout}</td>
                <td className={r.verdict === "성립" ? "rs__verdict rs__verdict--yes" : "rs__verdict"}>
                  {r.verdict}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="rs__limits">
        <span className="rs__label">한계</span> {study.limits}
      </p>
      <p className="rs__source">
        자세한 내용: {study.doc} · 질문을 고정한 커밋 {study.commit} · <code>{study.cli}</code>
      </p>
    </>
  );
}

export function Research() {
  const [showRuns, setShowRuns] = useState(false);
  return (
    <div className="rs">
      <header className="rs__header">
        <h1 className="rs__h1">연구</h1>
        <p className="rs__lead">
          아래 여섯 연구는 질문과 판정 규칙을 결과보다 먼저 고정하고 잰 것들입니다. 매매 권유가 아닙니다.
        </p>
        <p className="rs__overview">
          지금까지 잰 연구에서 8시~10시 전에 사고파는 매매가 비용 뒤에 남는다는 근거는 찾지 못했습니다. 비용 0.30%를
          넣어 잰 질문 7개(진입·청산 E1~E3, 밤사이 업종 O3, NXT N2·N3, 트럼프 T2)가 모두 성립하지 않았습니다. 공시
          v2는 비용 전으로 쟀고, 비용 전에도 9시에 사서 10시 전에 파는 평균이 0보다 크다는 근거(F1)를 찾지
          못했습니다. 성립한 질문(공시 v1의 D3·D4, v2의 F3)은 갭의 방향이나 움직임의 크기에 관한 것이지 사서
          남는지에 관한 것이 아닙니다. 모두 미리 고정한 규칙으로 판정했고 기간은 3개월~2년입니다. 다만 표본
          규칙 일부(예: 공시 ±30% 제외, 밤사이 9/22·휴장일 처리, 트럼프 NaN 처리)는 결과를 본 뒤 정했고, 공시 v2·v3와 NXT는 이미 본 표본을 다른
          방식으로 다시 본 것이라 독립된 확인이 아닙니다(각 문서에 적었습니다).
        </p>
      </header>

      {STUDIES.map((s, i) => (
        <Card
          key={s.id}
          title={s.title}
          badge={s.badge}
          note={s.note}
          period={s.period}
          summary={s.conclusion}
          initiallyOpen={i === 0}
        >
          <StudyBody study={s} />
        </Card>
      ))}

      <Card
        title="아직 모르는 것"
        badge="판정 전"
        summary="아침 관찰 목록이 맞는지는 첫 목록(2026-09-28)부터 기록을 쌓아 판정하고, 신호와 뉴스 오버레이가 실제 수익으로 이어지는지는 포워드 기록을 쌓는 중입니다."
        initiallyOpen={false}
      >
        <ul className="rs__list">
          <li>
            <strong>아침 관찰 목록이 맞는가.</strong> 첫 V2 목록은 2026-09-28입니다. 미리 고정한 질문 일곱 개(좋은
            뉴스 종목이 그날 목록 평균보다 나은가 등)는 목록 20일째에 읽기 시작하고, 60일째에 처음 60일로 한 번만
            판정합니다. 그중 두 질문(H6·H7: 공시 이유 종목, 그리고 목록 전체를 9시 시가에 사서 10시 전에 파는 평균)은 비용 전으로 재므로 성립해도
            비용 뒤 근거는 아닙니다.
          </li>
          <li>
            <strong>신호와 뉴스 오버레이가 실제 수익으로 이어지는가.</strong> 포워드 기록을 쌓는 중이고, 판단은
            2026년 12월 말 이후입니다.
          </li>
          <li>
            <strong>뉴스로 종목을 고르면 선택 편향이 들어온다.</strong> 발굴 결과는 볼 만한 후보이지 전략 성과가
            아닙니다.
          </li>
        </ul>
        <p className="rs__source">자세한 내용: README.md "무엇을 쟀고, 아직 모르는가", docs/design/intraday.md</p>
      </Card>

      <Card
        title="일봉 채점 백테스트 (이전 규칙 v0.2)"
        badge="판정 아님"
        note="15종목 10년"
        summary="규칙은 대부분 단순 보유보다 뒤처졌고, 많이 오른 종목일수록 격차가 컸습니다(순위 상관 −0.921). 신호 탭의 일봉 점수에 관한 것이고 아침 매매와는 다른 질문입니다."
        initiallyOpen={false}
      >
        <p className="rs__q">
          "주가가 오르면 재무 점수가 떨어져 매수를 멈춘다"는 가설은 반증됐습니다(상승폭과 재무 점수 변화의 순위 상관
          +0.050). 이 숫자를 보고 가중치를 바꾸지는 않았습니다. 홀드아웃을 떼어 둔 바로 그 종목들이기 때문입니다.
        </p>
        <p className="rs__source">자세한 내용: docs/studies/backtest.md</p>
        <button className="rs__runs" aria-expanded={showRuns} onClick={() => setShowRuns(!showRuns)}>
          {showRuns ? "저장된 실행 접기" : "저장된 실행 보기"}
        </button>
        {showRuns && (
          <>
            <p className="rs__meta">
              아래 실행은 DB에 저장된 런이라 위 요약(v0.2)과 규칙 버전이 다를 수 있습니다.
            </p>
            <div className="rs__backtest">
              <Backtest />
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
