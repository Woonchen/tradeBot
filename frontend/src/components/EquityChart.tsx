import { createChart, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import type { EquityPoint } from "../api/client";
import { fmtNum, fmtTime } from "../format";
import { baseOptions, useChartColors } from "./chartTheme";

/** 錢包總權益曲線（單一數列，標題即說明，不另放圖例）。 */
export function EquityChart({ points, short = false }: { points: EquityPoint[]; short?: boolean }) {
  const colors = useChartColors();
  const box = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<{ time: number; value: number } | null>(null);

  useEffect(() => {
    if (!box.current) return;
    const chart = createChart(box.current, baseOptions(colors));
    const series = chart.addAreaSeries({
      lineColor: colors.series1,
      lineWidth: 2,
      topColor: `${colors.series1}40`,
      bottomColor: `${colors.series1}05`,
      priceFormat: { type: "price", precision: 2, minMove: 0.01 },
    });
    // 同一秒可能有多筆快照，lightweight-charts 要求時間嚴格遞增
    const seen = new Map<number, number>();
    for (const p of points) seen.set(Math.floor(Date.parse(p.ts) / 1000), p.total_usdt);
    series.setData([...seen].map(([time, value]) => ({ time: time as UTCTimestamp, value })));
    chart.timeScale().fitContent();
    chart.subscribeCrosshairMove((p) => {
      const d = p.seriesData.get(series) as { value: number } | undefined;
      setHover(d && p.time ? { time: p.time as number, value: d.value } : null);
    });
    return () => chart.remove();
  }, [points, colors]);

  return (
    <div className={`chart-box ${short ? "short" : ""}`} ref={box}>
      {hover && (
        <div className="chart-legend">
          <span>{fmtTime(new Date(hover.time * 1000).toISOString())}</span>
          <strong style={{ color: "var(--text)" }}>{fmtNum(hover.value)} USDT</strong>
        </div>
      )}
      {points.length === 0 && <div className="empty">尚無餘額快照，系統每分鐘記錄一次</div>}
    </div>
  );
}
