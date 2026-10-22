import type { Candle } from "../api/client";
import type { ChartColors } from "./chartTheme";

/** 技術指標計算，跟後端 backend/app/strategies/indicators.py 的定義一致（EMA 以 SMA 起算、RSI 用 Wilder 平滑）。 */

type Series = (number | null)[];

export function sma(values: number[], n: number): Series {
  const out: Series = new Array(values.length).fill(null);
  let s = 0;
  for (let i = 0; i < values.length; i++) {
    s += values[i];
    if (i >= n) s -= values[i - n];
    if (i >= n - 1) out[i] = s / n;
  }
  return out;
}

export function ema(values: Series, n: number): Series {
  const out: Series = new Array(values.length).fill(null);
  const start = values.findIndex((v) => v !== null); // MACD 訊號線的輸入前段是 null
  if (start < 0 || values.length - start < n) return out;
  const k = 2 / (n + 1);
  let prev = 0;
  for (let i = start; i < start + n; i++) prev += values[i] as number;
  prev /= n;
  out[start + n - 1] = prev;
  for (let i = start + n; i < values.length; i++) {
    prev = (values[i] as number) * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

function rma(values: number[], n: number): Series {
  const out: Series = new Array(values.length).fill(null);
  if (values.length < n) return out;
  let prev = values.slice(0, n).reduce((a, b) => a + b, 0) / n;
  out[n - 1] = prev;
  for (let i = n; i < values.length; i++) {
    prev = (prev * (n - 1) + values[i]) / n;
    out[i] = prev;
  }
  return out;
}

export function rsi(closes: number[], n = 14): Series {
  const out: Series = new Array(closes.length).fill(null);
  if (closes.length <= n) return out;
  const gains: number[] = [];
  const losses: number[] = [];
  for (let i = 1; i < closes.length; i++) {
    gains.push(Math.max(closes[i] - closes[i - 1], 0));
    losses.push(Math.max(closes[i - 1] - closes[i], 0));
  }
  const g = rma(gains, n);
  const l = rma(losses, n);
  for (let i = 0; i < g.length; i++) {
    if (g[i] === null || l[i] === null) continue;
    out[i + 1] = l[i] === 0 ? 100 : 100 - 100 / (1 + (g[i] as number) / (l[i] as number));
  }
  return out;
}

function stdev(values: number[], mean: Series, n: number): Series {
  return mean.map((m, i) => {
    if (m === null) return null;
    let s = 0;
    for (let j = i - n + 1; j <= i; j++) s += (values[j] - m) ** 2;
    return Math.sqrt(s / n);
  });
}

/** 把 hex 色碼加上透明度（畫布不吃 color-mix）。 */
const alpha = (hex: string, a: number) => (/^#[0-9a-f]{6}$/i.test(hex) ? hex + Math.round(a * 255).toString(16).padStart(2, "0") : hex);

export interface Point { time: number; value: number; color?: string }

export interface Output {
  name: string;
  type: "line" | "histogram";
  color: string;
  data: Point[];
  dashed?: boolean;
}

export interface IndicatorDef {
  id: string;
  label: string;
  /** main：疊在 K 線上；sub：K 線下方獨立一格 */
  pane: "main" | "sub";
  build: (candles: Candle[], c: ChartColors) => Output[];
  /** 副圖的水平參考線，例如 RSI 70/30 */
  levels?: number[];
  /** 成交量用 volume 格式（1.2K、3.4M） */
  volume?: boolean;
}

const points = (candles: Candle[], values: Series): Point[] => {
  const out: Point[] = [];
  values.forEach((v, i) => v !== null && out.push({ time: candles[i].time, value: v }));
  return out;
};

const line = (name: string, color: string, candles: Candle[], values: Series, dashed = false): Output =>
  ({ name, type: "line", color, data: points(candles, values), dashed });

export const INDICATORS: IndicatorDef[] = [
  {
    id: "ma", label: "MA", pane: "main",
    build: (k, c) => {
      const cl = k.map((x) => x.close);
      return [line("MA7", c.series2, k, sma(cl, 7)), line("MA25", c.series3, k, sma(cl, 25)), line("MA99", c.series4, k, sma(cl, 99))];
    },
  },
  {
    id: "ema", label: "EMA", pane: "main",
    build: (k, c) => {
      const cl = k.map((x) => x.close);
      return [line("EMA12", c.series2, k, ema(cl, 12), true), line("EMA26", c.series3, k, ema(cl, 26), true)];
    },
  },
  {
    id: "boll", label: "BOLL", pane: "main",
    build: (k, c) => {
      const cl = k.map((x) => x.close);
      const mid = sma(cl, 20);
      const sd = stdev(cl, mid, 20);
      const band = (sign: number) => mid.map((m, i) => (m === null ? null : m + sign * 2 * (sd[i] as number)));
      return [line("上軌", c.series1, k, band(1)), line("中軌", c.text2, k, mid, true), line("下軌", c.series1, k, band(-1))];
    },
  },
  {
    id: "vol", label: "成交量", pane: "sub", volume: true,
    build: (k, c) => [{
      name: "VOL", type: "histogram", color: c.text2,
      data: k.map((x) => ({ time: x.time, value: x.volume ?? 0, color: alpha(x.close >= x.open ? c.up : c.down, 0.5) })),
    }],
  },
  {
    id: "rsi", label: "RSI", pane: "sub", levels: [70, 30],
    build: (k, c) => [line("RSI14", c.series3, k, rsi(k.map((x) => x.close), 14))],
  },
  {
    id: "macd", label: "MACD", pane: "sub",
    build: (k, c) => {
      const cl = k.map((x) => x.close);
      const fast = ema(cl, 12);
      const slow = ema(cl, 26);
      const dif = fast.map((f, i) => (f === null || slow[i] === null ? null : f - (slow[i] as number)));
      const dea = ema(dif, 9);
      const hist = dif.map((d, i) => (d === null || dea[i] === null ? null : d - (dea[i] as number)));
      return [
        {
          name: "MACD", type: "histogram", color: c.text2,
          data: points(k, hist).map((p) => ({ ...p, color: alpha(p.value >= 0 ? c.up : c.down, 0.6) })),
        },
        line("DIF", c.series1, k, dif),
        line("DEA", c.series2, k, dea),
      ];
    },
  },
];
