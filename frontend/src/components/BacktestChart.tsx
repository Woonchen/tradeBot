import { createChart, type SeriesMarker, type Time, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";
import type { BacktestTrade, Candle } from "../api/client";
import { fmtSigned } from "../format";
import { baseOptions, useChartColors } from "./chartTheme";

/** 回測的 K 線 + 進出場點。 */
export function BacktestChart({ candles, trades }: { candles: Candle[]; trades: BacktestTrade[] }) {
  const colors = useChartColors();
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!box.current) return;
    const chart = createChart(box.current, baseOptions(colors));
    const series = chart.addCandlestickSeries({
      upColor: colors.up, downColor: colors.down, borderVisible: false, wickUpColor: colors.up, wickDownColor: colors.down,
    });
    series.setData(candles.map((c) => ({ ...c, time: c.time as UTCTimestamp })));
    const markers: SeriesMarker<Time>[] = [];
    for (const t of trades) {
      const long = t.side === "long";
      markers.push({
        time: t.entry_time as UTCTimestamp, position: long ? "belowBar" : "aboveBar",
        shape: long ? "arrowUp" : "arrowDown", color: long ? colors.up : colors.down,
        text: trades.length > 30 ? "" : long ? "開多" : "開空", // 交易多時只標箭頭，避免文字重疊
      });
      markers.push({
        time: t.exit_time as UTCTimestamp, position: long ? "aboveBar" : "belowBar",
        shape: long ? "arrowDown" : "arrowUp", color: long ? colors.down : colors.up,
        text: `${long ? "平多" : "平空"} ${fmtSigned(t.net)}`,
      });
    }
    series.setMarkers(markers.sort((a, b) => (a.time as number) - (b.time as number)));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [candles, trades, colors]);

  return <div className="chart-box" ref={box} />;
}
