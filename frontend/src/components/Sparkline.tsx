import { memo } from "react";

/**
 * 목록 행의 작은 추세선: 오늘 1분봉 종가(서버가 최대 60점으로 솎은 것).
 *
 * 보조 표시다. 등락률 글자가 옆에 있으므로 스크린리더에는 숨긴다. 색은 옆의 등락률(전일 대비)과 같은 방향으로 맞춘다.
 * 선의 모양은 오늘 흐름이고 색은 전일 대비라, 갭 상승 뒤 밀린 종목은 빨간 선이 내려가는 모양이 된다.
 */
// 체결마다 목록이 다시 그려지므로, 값이 바뀐 행만 다시 계산한다(추세선은 15초마다 바뀐다).
export const Sparkline = memo(function Sparkline({
  values,
  change,
  width = 64,
  height = 18,
}: {
  values: number[] | undefined;
  change: number | undefined;
  width?: number;
  height?: number;
}) {
  if (!values || values.length < 2) return <span className="spark spark--empty" aria-hidden="true" />;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const step = width / (values.length - 1);
  const points = values
    .map((v, i) => `${(i * step).toFixed(1)},${(height - 1 - ((v - lo) / span) * (height - 2)).toFixed(1)}`)
    .join(" ");
  const color =
    change == null || change === 0 ? "var(--c-flat)" : change > 0 ? "var(--c-up)" : "var(--c-down)";
  return (
    <svg className="spark" width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
      <polyline points={points} fill="none" stroke={color} strokeWidth="1.25" strokeLinejoin="round" />
    </svg>
  );
});
