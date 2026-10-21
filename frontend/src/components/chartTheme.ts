import { ColorType, type DeepPartial, type ChartOptions } from "lightweight-charts";
import { useEffect, useState } from "react";

export interface ChartColors {
  surface: string; text: string; text2: string; grid: string; border: string;
  up: string; down: string; warn: string; serious: string; series1: string; accent: string;
}

export function readColors(): ChartColors {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(name).trim();
  return {
    surface: v("--surface"), text: v("--text"), text2: v("--text-2"), grid: v("--grid"), border: v("--border"),
    up: v("--up"), down: v("--down"), warn: v("--warn"), serious: v("--serious"), series1: v("--series-1"),
    accent: v("--accent"),
  };
}

/** 系統切換深淺色時重新讀取顏色。 */
export function useChartColors(): ChartColors {
  const [colors, setColors] = useState(readColors);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const update = () => setColors(readColors());
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  return colors;
}

export function baseOptions(c: ChartColors): DeepPartial<ChartOptions> {
  return {
    autoSize: true,
    layout: { background: { type: ColorType.Solid, color: c.surface }, textColor: c.text2, fontSize: 12 },
    grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
    rightPriceScale: { borderColor: c.border },
    timeScale: { borderColor: c.border, timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 },
    localization: { locale: "zh-TW" },
  };
}
