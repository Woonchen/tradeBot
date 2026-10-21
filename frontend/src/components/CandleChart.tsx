import { useQuery } from "@tanstack/react-query";
import {
  createChart, LineStyle, type IChartApi, type IPriceLine, type ISeriesApi, type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, qs, type Candle, type Market, type Position, type Trade } from "../api/client";
import { fmtPrice, fmtSigned, INTENT_ZH } from "../format";
import { baseOptions, useChartColors } from "./chartTheme";

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

/** K 線圖 + 機器人買賣點 + 持倉的開倉價/停損/停利/強平價線。 */
export function CandleChart({ symbol, market, interval, onSelectBar }: Props) {
  const colors = useChartColors();
  const box = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const linesRef = useRef<IPriceLine[]>([]);
  const [hover, setHover] = useState<Candle | null>(null);
  const sec = INTERVAL_SECONDS[interval];

  const klines = useQuery({
    queryKey: ["klines", symbol, market, interval],
    queryFn: () => api<{ symbol: string; candles: Candle[] }>(`/api/klines?${qs({ symbol, market, interval, limit: 500 })}`),
    retry: false,
  });
  const resolved = klines.data?.symbol;

  // 即時更新：每 5 秒抓最後兩根 K 棒
  const live = useQuery({
    queryKey: ["klines-live", symbol, market, interval],
    queryFn: () => api<{ candles: Candle[] }>(`/api/klines?${qs({ symbol, market, interval, limit: 2 })}`),
    refetchInterval: 5000,
    enabled: !!resolved,
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
    });
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      linesRef.current = [];
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
    seriesRef.current.setData(klines.data.candles.map((c) => ({ ...c, time: c.time as UTCTimestamp })));
    chartRef.current?.timeScale().scrollToRealTime();
  }, [klines.data, colors]);

  useEffect(() => {
    const s = seriesRef.current;
    if (!s || !live.data || !klines.data) return;
    const last = klines.data.candles.at(-1)?.time ?? 0;
    for (const c of live.data.candles) if (c.time >= last) s.update({ ...c, time: c.time as UTCTimestamp });
  }, [live.data, klines.data]);

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

  const last = hover ?? klines.data?.candles.at(-1) ?? null;
  const change = last ? (last.close - last.open) / last.open : 0;

  return (
    <div>
      <div className="chart-box" ref={box}>
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
        <span className="muted">點擊 K 棒查看該時段成交明細</span>
      </div>
    </div>
  );
}
