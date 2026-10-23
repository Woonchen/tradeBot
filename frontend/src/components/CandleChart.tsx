import { useQuery } from "@tanstack/react-query";
import {
  createChart, LineStyle, type IChartApi, type IPriceLine, type ISeriesApi, type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, qs, type Candle, type Market, type Position, type Trade } from "../api/client";
import { live, useLiveConnected } from "../api/liveSocket";
import { fmtPrice, fmtSigned, INTENT_ZH } from "../format";
import { baseOptions, useChartColors } from "./chartTheme";
import { INDICATORS, type IndicatorDef } from "./indicators";

export const INTERVAL_SECONDS: Record<string, number> = {
  "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400,
};

interface Props {
  symbol: string;
  market: Market;
  interval: string;
  onSelectBar?: (trades: Trade[]) => void;
}

const barTime = (iso: string, sec: number) => (Math.floor(Date.parse(iso) / 1000 / sec) * sec) as UTCTimestamp;

const IND_KEY = "chart.indicators";
const loadEnabled = (): string[] => {
  try {
    const v = JSON.parse(localStorage.getItem(IND_KEY) ?? "null");
    return Array.isArray(v) ? v : ["ma", "vol"];
  } catch {
    return ["ma", "vol"];
  }
};

/** 副圖每格佔整張圖的高度比例（圖表高度會跟著副圖數量加高，見 styles.css 的 .panes-N） */
const PANE_H = [0, 0.17, 0.15, 0.135, 0.12];

const fmtVal = (v: number) => `${v < 0 ? "−" : ""}${fmtPrice(Math.abs(v))}`;
const fmtVol = (v: number) => new Intl.NumberFormat("zh-TW", { notation: "compact", maximumFractionDigits: 2 }).format(v);

interface LiveIndicator {
  def: IndicatorDef;
  series: ISeriesApi<"Line" | "Histogram">[];
}
interface LegendItem { key: string; name: string; color: string; volume?: boolean }

/** K 線圖 + 機器人買賣點 + 持倉的開倉價/停損/停利/強平價線。 */
export function CandleChart({ symbol, market, interval, onSelectBar }: Props) {
  const colors = useChartColors();
  const box = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const linesRef = useRef<IPriceLine[]>([]);
  const [hover, setHover] = useState<Candle | null>(null);
  const sec = INTERVAL_SECONDS[interval];

  // 技術指標
  const [enabled, setEnabled] = useState<string[]>(loadEnabled);
  const toggle = (id: string) =>
    setEnabled((cur) => {
      const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
      try {
        localStorage.setItem(IND_KEY, JSON.stringify(next));
      } catch {
        /* 無痕模式等情況存不了，就只在這次有效 */
      }
      return next;
    });
  const colorsRef = useRef(colors);
  colorsRef.current = colors;
  const candlesRef = useRef<Candle[]>([]);
  const indRef = useRef<LiveIndicator[]>([]);
  const [legend, setLegend] = useState<LegendItem[]>([]);
  const [indLast, setIndLast] = useState<Record<string, number>>({});
  const [indHover, setIndHover] = useState<Record<string, number> | null>(null);
  const subCount = INDICATORS.filter((d) => d.pane === "sub" && enabled.includes(d.id)).length;

  const klines = useQuery({
    queryKey: ["klines", symbol, market, interval],
    queryFn: () => api<{ symbol: string; candles: Candle[] }>(`/api/klines?${qs({ symbol, market, interval, limit: 500 })}`),
    retry: false,
  });
  const resolved = klines.data?.symbol;

  // 即時更新：透過後端轉發 Binance WebSocket 的 K 線；即時連線中斷時改成每 5 秒輪詢
  const connected = useLiveConnected();
  const poll = useQuery({
    queryKey: ["klines-live", symbol, market, interval],
    queryFn: () => api<{ candles: Candle[] }>(`/api/klines?${qs({ symbol, market, interval, limit: 2 })}`),
    refetchInterval: 5000,
    enabled: !!resolved && !connected,
  });
  const trades = useQuery({
    queryKey: ["trades", resolved, market],
    queryFn: () => api<Trade[]>(`/api/trades?${qs({ symbol: resolved, market, limit: 2000 })}`),
    enabled: !!resolved,
  });
  const positions = useQuery({ queryKey: ["positions", "open"], queryFn: () => api<Position[]>("/api/positions") });
  const position = positions.data?.find((p) => p.symbol === resolved && p.market === market);

  // 建立圖表（顏色改變時重建）
  useEffect(() => {
    if (!box.current) return;
    const opts = baseOptions(colors);
    const chart = createChart(box.current, { ...opts, timeScale: { ...opts.timeScale, rightOffset: 6 } });
    const series = chart.addCandlestickSeries({
      upColor: colors.up, downColor: colors.down, borderVisible: false,
      wickUpColor: colors.up, wickDownColor: colors.down,
    });
    chartRef.current = chart;
    seriesRef.current = series;
    chart.subscribeCrosshairMove((p) => {
      const d = p.seriesData.get(series) as Candle | undefined;
      setHover(d ? { ...d, time: p.time as number } : null);
      if (!d) return setIndHover(null);
      const vals: Record<string, number> = {};
      for (const ind of indRef.current) {
        ind.series.forEach((s, i) => {
          const v = p.seriesData.get(s) as { value?: number } | undefined;
          if (v?.value !== undefined) vals[`${ind.def.id}.${i}`] = v.value;
        });
      }
      setIndHover(vals);
    });
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      linesRef.current = [];
      indRef.current = [];
    };
  }, [colors]);

  // 點擊 K 棒 → 列出該根 K 棒內的成交
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !onSelectBar) return;
    const handler = (p: { time?: Time }) => {
      if (p.time === undefined) return;
      const t = p.time as number;
      onSelectBar((trades.data ?? []).filter((x) => barTime(x.executed_at, sec) === t));
    };
    chart.subscribeClick(handler);
    return () => chart.unsubscribeClick(handler);
  }, [colors, trades.data, sec, onSelectBar]);

  useEffect(() => {
    if (!klines.data || !seriesRef.current) return;
    candlesRef.current = klines.data.candles;
    seriesRef.current.setData(klines.data.candles.map((c) => ({ ...c, time: c.time as UTCTimestamp })));
    chartRef.current?.timeScale().scrollToRealTime();
  }, [klines.data, colors]);

  // 指標：開關或資料變動時重建 series，並依副圖數量分配上下高度
  useEffect(() => {
    const chart = chartRef.current;
    const main = seriesRef.current;
    if (!chart || !main) return;
    indRef.current.forEach((ind) => ind.series.forEach((s) => chart.removeSeries(s)));
    indRef.current = [];

    const defs = INDICATORS.filter((d) => enabled.includes(d.id));
    const subs = defs.filter((d) => d.pane === "sub");
    const h = PANE_H[subs.length];
    main.priceScale().applyOptions({ scaleMargins: { top: 0.12, bottom: subs.length ? subs.length * h + 0.03 : 0.08 } });

    const candles = candlesRef.current;
    const items: LegendItem[] = [];
    const last: Record<string, number> = {};
    for (const def of defs) {
      const sub = def.pane === "sub";
      const scaleId = sub ? def.id : "right";
      const outputs = def.build(candles, colors);
      const series = outputs.map((o, i) => {
        const common = {
          priceScaleId: scaleId, lastValueVisible: false, priceLineVisible: false,
          priceFormat: def.volume ? { type: "volume" as const } : { type: "custom" as const, formatter: fmtVal, minMove: 1e-8 },
        };
        const s = o.type === "histogram"
          ? chart.addHistogramSeries({ ...common, color: o.color })
          : chart.addLineSeries({
            ...common, color: o.color, lineWidth: 1, crosshairMarkerVisible: false,
            lineStyle: o.dashed ? LineStyle.Dashed : LineStyle.Solid,
          });
        s.setData(o.data.map((p) => ({ ...p, time: p.time as UTCTimestamp })));
        items.push({ key: `${def.id}.${i}`, name: o.name, color: o.color, volume: def.volume });
        const v = o.data.at(-1)?.value;
        if (v !== undefined) last[`${def.id}.${i}`] = v;
        return s;
      });
      if (sub) {
        const i = subs.indexOf(def);
        const bottom = (subs.length - 1 - i) * h;
        chart.priceScale(scaleId).applyOptions({ scaleMargins: { top: 1 - bottom - h + 0.02, bottom } });
        for (const price of def.levels ?? []) {
          series[0].createPriceLine({ price, color: colors.text2, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "" });
        }
      }
      indRef.current.push({ def, series });
    }
    setLegend(items);
    setIndLast(last);
  }, [enabled, colors, klines.data]);

  // 即時 K 棒進來時，重算指標並只更新最後一點
  const refreshIndicators = () => {
    const candles = candlesRef.current;
    const lastTime = candles.at(-1)?.time;
    const last: Record<string, number> = {};
    for (const ind of indRef.current) {
      ind.def.build(candles, colorsRef.current).forEach((o, i) => {
        const p = o.data.at(-1);
        if (!p || p.time !== lastTime) return;
        ind.series[i].update({ ...p, time: p.time as UTCTimestamp });
        last[`${ind.def.id}.${i}`] = p.value;
      });
    }
    setIndLast(last);
  };

  // 最後一根 K 棒的時間；只能更新同一根或更新的 K 棒，舊的資料會讓 lightweight-charts 丟錯
  const lastTime = useRef(0);
  const [tick, setTick] = useState<Candle | null>(null);
  useEffect(() => {
    lastTime.current = klines.data?.candles.at(-1)?.time ?? 0;
  }, [klines.data]);
  const pushCandle = (c: Candle) => {
    if (!seriesRef.current || c.time < lastTime.current) return;
    seriesRef.current.update({ ...c, time: c.time as UTCTimestamp });
    lastTime.current = c.time;
    const k = candlesRef.current;
    candlesRef.current = k.at(-1)?.time === c.time ? [...k.slice(0, -1), c] : [...k, c];
    refreshIndicators();
    setTick(c);
  };

  useEffect(() => {
    if (!resolved) return;
    setTick(null);
    live.setChart({ symbol: resolved, market, interval });
    const off = live.onMessage((msg) => {
      const d = msg.data;
      if (msg.type === "kline" && d.symbol === resolved && d.market === market && d.interval === interval) pushCandle(d.candle);
    });
    return () => {
      off();
      live.setChart(null);
    };
  }, [resolved, market, interval]);

  useEffect(() => {
    for (const c of poll.data?.candles ?? []) pushCandle(c);
  }, [poll.data]);

  // 買賣點
  const markers = useMemo<SeriesMarker<Time>[]>(() => {
    const first = klines.data?.candles[0]?.time ?? 0;
    return (trades.data ?? [])
      .map((t) => ({ t, time: barTime(t.executed_at, sec) }))
      .filter(({ time }) => time >= first)
      .sort((a, b) => a.time - b.time)
      .map(({ t, time }) => {
        const isBuy = t.side === "buy";
        const isClose = t.intent.startsWith("close");
        return {
          time,
          id: String(t.id),
          position: isBuy ? "belowBar" : "aboveBar",
          shape: isBuy ? "arrowUp" : "arrowDown",
          color: isBuy ? colors.up : colors.down,
          text: `${INTENT_ZH[t.intent] ?? t.side}${isClose ? ` ${fmtSigned(t.realized_pnl)}` : ""}`,
          size: 1.2,
        } as SeriesMarker<Time>;
      });
  }, [trades.data, klines.data, sec, colors]);

  useEffect(() => {
    seriesRef.current?.setMarkers(markers);
  }, [markers]);

  // 持倉價格線
  useEffect(() => {
    const s = seriesRef.current;
    if (!s) return;
    linesRef.current.forEach((l) => s.removePriceLine(l));
    linesRef.current = [];
    if (!position) return;
    const add = (price: number | null, color: string, title: string, style = LineStyle.Dashed) => {
      if (price) linesRef.current.push(s.createPriceLine({ price, color, lineWidth: 2, lineStyle: style, axisLabelVisible: true, title }));
    };
    add(position.entry_price, colors.series1, `開倉 ${position.side === "long" ? "多" : "空"}`, LineStyle.Solid);
    add(position.stop_price, colors.down, "停損");
    add(position.take_profit_price, colors.up, "停利");
    add(position.liquidation_price, colors.warn, "強平", LineStyle.Dotted);
  }, [position, colors, klines.data]);

  const last = hover ?? tick ?? klines.data?.candles.at(-1) ?? null;
  const change = last ? (last.close - last.open) / last.open : 0;
  const indVals = indHover ?? indLast;

  return (
    <div>
      <div className="toolbar">
        <span className="muted" style={{ fontSize: 12 }}>指標</span>
        <div className="seg">
          {INDICATORS.filter((d) => d.pane === "main").map((d) => (
            <button key={d.id} className={enabled.includes(d.id) ? "on" : ""} onClick={() => toggle(d.id)}>{d.label}</button>
          ))}
        </div>
        <div className="seg">
          {INDICATORS.filter((d) => d.pane === "sub").map((d) => (
            <button key={d.id} className={enabled.includes(d.id) ? "on" : ""} onClick={() => toggle(d.id)}>{d.label}</button>
          ))}
        </div>
      </div>
      <div className={`chart-box${subCount ? ` panes-${subCount}` : ""}`} ref={box}>
        <div className="chart-legend">
          <strong style={{ color: "var(--text)" }}>{resolved ?? symbol}</strong>
          <span>{interval}</span>
          {last && (
            <>
              <span>開 {fmtPrice(last.open)}</span>
              <span>高 {fmtPrice(last.high)}</span>
              <span>低 {fmtPrice(last.low)}</span>
              <span>收 {fmtPrice(last.close)}</span>
              <span className={change >= 0 ? "up" : "down"}>{fmtSigned(change * 100)}%</span>
            </>
          )}
          {legend.length > 0 && (
            <span className="ind">
              {legend.map((it) => (
                <span key={it.key} style={{ color: it.color }}>
                  {it.name} {indVals[it.key] === undefined ? "—" : it.volume ? fmtVol(indVals[it.key]) : fmtVal(indVals[it.key])}
                </span>
              ))}
            </span>
          )}
        </div>
      </div>
      {klines.isError && <p className="error-text">K 線載入失敗：{(klines.error as Error).message}</p>}
      <div className="legend-row">
        <span><span className="up">▲</span> 買入（開多／平空）</span>
        <span><span className="down">▼</span> 賣出（平多／開空），平倉標註已實現損益</span>
        {position && (
          <>
            <span style={{ color: colors.series1 }}><span className="swatch-line" style={{ borderTopStyle: "solid" }} /> 開倉價</span>
            {position.stop_price && <span style={{ color: colors.down }}><span className="swatch-line" /> 停損</span>}
            {position.take_profit_price && <span style={{ color: colors.up }}><span className="swatch-line" /> 停利</span>}
            {position.liquidation_price && <span style={{ color: colors.warn }}><span className="swatch-line" style={{ borderTopStyle: "dotted" }} /> 強平</span>}
          </>
        )}
        <span className="muted">{connected ? "● 即時推送" : "○ 連線中斷，每 5 秒更新"} · 點擊 K 棒查看該時段成交明細</span>
      </div>
    </div>
  );
}
