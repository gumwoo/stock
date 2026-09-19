import { useEffect, useRef } from "react";
import {
  ColorType,
  CrosshairMode,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from "lightweight-charts";
import type { Candle } from "../api/types";

/**
 * TradingView's lightweight-charts, wrapped so the library never appears in
 * page code.
 *
 * Two reasons for the isolation. Swapping charting libraries should touch one
 * file, not every screen. And the imperative lifecycle — create, subscribe,
 * resize, dispose — does not mix well with React rendering, so it is better
 * contained behind a ref than scattered through a component.
 *
 * Colours come from CSS custom properties rather than literals, so the chart
 * follows the theme and the up/down convention toggle along with everything
 * else. The library needs resolved values, so they are read at mount.
 */

interface Options {
  candles: Candle[];
  ma20?: (number | null)[];
  ma60?: (number | null)[];
  height?: number;
}

function cssVar(el: HTMLElement, name: string, fallback: string): string {
  const value = getComputedStyle(el).getPropertyValue(name).trim();
  return value || fallback;
}

/** lightweight-charts wants seconds since epoch, not an ISO string. */
function toTime(iso: string): Time {
  return Math.floor(new Date(iso).getTime() / 1000) as Time;
}

export function useCandleChart({ candles, ma20, ma60, height = 420 }: Options) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const priceSeries = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeries = useRef<ISeriesApi<"Histogram"> | null>(null);
  const ma20Series = useRef<ISeriesApi<"Line"> | null>(null);
  const ma60Series = useRef<ISeriesApi<"Line"> | null>(null);

  // Create once. Re-creating on every data change would lose the user's zoom.
  useEffect(() => {
    const el = container.current;
    if (!el) return;

    const text = cssVar(el, "--c-text-secondary", "#5b5b66");
    const border = cssVar(el, "--c-border", "#e8e8ea");
    const up = cssVar(el, "--c-up", "#e5484d");
    const down = cssVar(el, "--c-down", "#1d6ef0");

    const instance = createChart(el, {
      height,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: text,
        fontFamily: cssVar(el, "--font", "sans-serif"),
        fontSize: 11,
      },
      grid: {
        vertLines: { color: border },
        horzLines: { color: border },
      },
      rightPriceScale: { borderColor: border },
      timeScale: { borderColor: border, timeVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
      handleScale: { axisPressedMouseMove: { price: false } },
    });

    priceSeries.current = instance.addCandlestickSeries({
      upColor: up,
      downColor: down,
      borderUpColor: up,
      borderDownColor: down,
      wickUpColor: up,
      wickDownColor: down,
    });

    volumeSeries.current = instance.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
      color: border,
    });
    instance.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });

    ma20Series.current = instance.addLineSeries({
      color: cssVar(el, "--c-text", "#17171c"),
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    ma60Series.current = instance.addLineSeries({
      color: cssVar(el, "--c-text-tertiary", "#8e8e99"),
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
    });

    chart.current = instance;

    const observer = new ResizeObserver(([entry]) => {
      instance.applyOptions({ width: entry.contentRect.width });
    });
    observer.observe(el);

    return () => {
      observer.disconnect();
      instance.remove();
      chart.current = null;
      priceSeries.current = null;
      volumeSeries.current = null;
      ma20Series.current = null;
      ma60Series.current = null;
    };
  }, [height]);

  // Feed data separately, so new bars do not reset the viewport.
  useEffect(() => {
    if (!priceSeries.current || !volumeSeries.current || candles.length === 0) return;

    const el = container.current;
    const upSoft = el ? cssVar(el, "--c-up-soft", "#fdf3f3") : "#fdf3f3";
    const downSoft = el ? cssVar(el, "--c-down-soft", "#f1f6fe") : "#f1f6fe";

    priceSeries.current.setData(
      candles.map((c) => ({
        time: toTime(c.ts),
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      })),
    );

    volumeSeries.current.setData(
      candles.map((c) => ({
        time: toTime(c.ts),
        value: c.volume,
        color: c.close >= c.open ? upSoft : downSoft,
      })),
    );

    if (ma20 && ma20Series.current) {
      ma20Series.current.setData(
        candles
          .map((c, i) => ({ time: toTime(c.ts), value: ma20[i] }))
          .filter((p): p is { time: Time; value: number } => p.value != null),
      );
    }
    if (ma60 && ma60Series.current) {
      ma60Series.current.setData(
        candles
          .map((c, i) => ({ time: toTime(c.ts), value: ma60[i] }))
          .filter((p): p is { time: Time; value: number } => p.value != null),
      );
    }

    chart.current?.timeScale().fitContent();
  }, [candles, ma20, ma60]);

  return container;
}

/** Trailing simple moving average, aligned to the input series. */
export function movingAverage(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}
