import { useCallback, useEffect, useRef } from "react";
import {
  ColorType,
  CrosshairMode,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from "lightweight-charts";
import type { LiveBar } from "../api/types";

/**
 * A candlestick chart that is fed one bar at a time.
 *
 * Unlike `useCandleChart`, which is given a finished series, this one keeps
 * its viewport while bars arrive: `reset` replaces the series (a new name, a
 * new interval), `push` updates the last bar or appends the next — which is
 * what lightweight-charts' `update` does when the time is the last bar's or
 * later. Colours come from the theme's custom properties, as elsewhere.
 */

function cssVar(el: HTMLElement, name: string, fallback: string): string {
  const value = getComputedStyle(el).getPropertyValue(name).trim();
  return value || fallback;
}

export function useLiveChart(height = 420) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const price = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volume = useRef<ISeriesApi<"Histogram"> | null>(null);
  const colours = useRef({ upSoft: "#fdf3f3", downSoft: "#f1f6fe" });

  useEffect(() => {
    const el = container.current;
    if (!el) return;
    const text = cssVar(el, "--c-text-secondary", "#5b5b66");
    const border = cssVar(el, "--c-border", "#e8e8ea");
    const up = cssVar(el, "--c-up", "#e5484d");
    const down = cssVar(el, "--c-down", "#1d6ef0");
    colours.current = {
      upSoft: cssVar(el, "--c-up-soft", "#fdf3f3"),
      downSoft: cssVar(el, "--c-down-soft", "#f1f6fe"),
    };

    const instance = createChart(el, {
      height,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: text,
        fontFamily: cssVar(el, "--font", "sans-serif"),
        fontSize: 11,
      },
      grid: { vertLines: { color: border }, horzLines: { color: border } },
      rightPriceScale: { borderColor: border },
      // Seoul time on the axis: the browser's own zone may be anything.
      localization: {
        timeFormatter: (t: number) =>
          new Date(t * 1000).toLocaleTimeString("ko-KR", {
            timeZone: "Asia/Seoul",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            hour12: false,
          }),
      },
      timeScale: {
        borderColor: border,
        timeVisible: true,
        secondsVisible: true,
        tickMarkFormatter: (t: number) =>
          new Date(t * 1000).toLocaleTimeString("ko-KR", {
            timeZone: "Asia/Seoul",
            hour: "2-digit",
            minute: "2-digit",
            hour12: false,
          }),
      },
      crosshair: { mode: CrosshairMode.Normal },
    });
    price.current = instance.addCandlestickSeries({
      upColor: up,
      downColor: down,
      borderUpColor: up,
      borderDownColor: down,
      wickUpColor: up,
      wickDownColor: down,
    });
    volume.current = instance.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
      color: border,
    });
    instance.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    chart.current = instance;

    const observer = new ResizeObserver(([entry]) => {
      instance.applyOptions({ width: entry.contentRect.width });
    });
    observer.observe(el);
    return () => {
      observer.disconnect();
      instance.remove();
      chart.current = null;
      price.current = null;
      volume.current = null;
    };
  }, [height]);

  const toVolume = useCallback(
    (b: LiveBar) => ({
      time: b.time as Time,
      value: b.volume,
      color: b.close >= b.open ? colours.current.upSoft : colours.current.downSoft,
    }),
    [],
  );

  const reset = useCallback(
    (bars: LiveBar[]) => {
      price.current?.setData(bars.map((b) => ({ ...b, time: b.time as Time })));
      volume.current?.setData(bars.map(toVolume));
      chart.current?.timeScale().fitContent();
    },
    [toVolume],
  );

  const push = useCallback(
    (bar: LiveBar) => {
      price.current?.update({ ...bar, time: bar.time as Time });
      volume.current?.update(toVolume(bar));
    },
    [toVolume],
  );

  return { container, reset, push };
}
