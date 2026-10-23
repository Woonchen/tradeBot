import { createChart, LineStyle, type SeriesMarker, type Time, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";
import type { BacktestTrade, Candle, Plot } from "../api/client";
import { fmtSigned } from "../format";
import { baseOptions, useChartColors, type ChartColors } from "./chartTheme";

/** 副圖每格佔整張圖的高度比例（圖會跟著副圖數量加高，見 styles.css 的 .panes-N） */
const PANE_H = [0, 0.17, 0.15, 0.135, 0.12];

/**
 * 顏色依出現順序分配（固定順序、不循環），K 線上的線與每一格副圖各自從第一個顏色開始，
 * 同一格裡的線才分得出來；同一格超過四條的用中性灰。
 */
export function plotColors(plots: Plot[], c: ChartColors): string[] {
  const palette = [c.series1, c.series2, c.series3, c.series4];
  const used: Record<string, number> = {};
  return plots.map((p) => {
    const key = p.pane === "main" ? "main" : `sub:${p.group}`;
    const i = used[key] ?? 0;
    used[key] = i + 1;
    return palette[i] ?? c.text2;
  });
}

/** 回測的 K 線 + 進出場點 + 策略用到的指標（疊在 K 線上或畫在副圖）。 */
export function BacktestChart({ candles, trades, plots = [] }: { candles: Candle[]; trades: BacktestTrade[]; plots?: Plot[] }) {
  const colors = useChartColors();
  const box = useRef<HTMLDivElement>(null);
  const groups = [...new Set(plots.filter((p) => p.pane === "sub").map((p) => p.group))].slice(0, PANE_H.length - 1);

  useEffect(() => {
    if (!box.current) return;
    const chart = createChart(box.current, baseOptions(colors));
    const series = chart.addCandlestickSeries({
      upColor: colors.up, downColor: colors.down, borderVisible: false, wickUpColor: colors.up, wickDownColor: colors.down,
    });
    series.setData(candles.map((c) => ({ ...c, time: c.time as UTCTimestamp })));

    const h = PANE_H[groups.length];
    series.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: groups.length ? groups.length * h + 0.03 : 0.08 } });
    const lineColors = plotColors(plots, colors);
    const leveled = new Set<string>();
    plots.forEach((p, idx) => {
      const sub = p.pane === "sub";
      const gi = groups.indexOf(p.group);
      if (sub && gi < 0) return; // 副圖太多時多出來的不畫
      const scaleId = sub ? `sub-${gi}` : "right";
      const line = chart.addLineSeries({
        priceScaleId: scaleId, color: lineColors[idx], lineWidth: 1, priceLineVisible: false,
        lastValueVisible: false, crosshairMarkerVisible: false,
      });
      const data: { time: UTCTimestamp; value: number }[] = [];
      p.values.forEach((v, i) => { if (v !== null && candles[i]) data.push({ time: candles[i].time as UTCTimestamp, value: v }); });
      line.setData(data);
      if (sub) {
        const bottom = (groups.length - 1 - gi) * h;
        chart.priceScale(scaleId).applyOptions({ scaleMargins: { top: 1 - bottom - h + 0.02, bottom } });
        if (!leveled.has(p.group)) { // 同一格的參考線只畫一次
          leveled.add(p.group);
          for (const price of p.levels) {
            line.createPriceLine({ price, color: colors.text2, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "" });
          }
        }
      }
    });

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
  }, [candles, trades, plots, colors]);

  const lineColors = plotColors(plots, colors);
  return (
    <>
      <div className={`chart-box${groups.length ? ` panes-${groups.length}` : ""}`} ref={box} />
      {plots.length > 0 && (
        <div className="legend-row">
          {plots.map((p, i) => (
            <span key={p.name} style={{ color: lineColors[i] }}>
              <span className="swatch-line" style={{ borderTopStyle: "solid" }} />
              <span style={{ color: "var(--text-2)" }}>{p.name}{p.pane === "sub" ? "（副圖）" : ""}</span>
            </span>
          ))}
        </div>
      )}
    </>
  );
}
