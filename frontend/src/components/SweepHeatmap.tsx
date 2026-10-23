import { useState } from "react";
import type { SweepCell, SweepResult, SweepStats } from "../api/client";
import { fmtNum, fmtPct, fmtSigned } from "../format";

export type Metric = "total_return" | "profit_factor" | "max_drawdown";

/** diverging：藍 = 好、紅 = 差、灰 = 中性；sequential：單一色相，越深越大。 */
export const METRICS: Record<Metric, { label: string; kind: "diverging" | "sequential"; value: (s: SweepStats) => number | null; fmt: (s: SweepStats) => string; note: string }> = {
  total_return: {
    label: "策略報酬", kind: "diverging", value: (s) => s.total_return,
    fmt: (s) => `${fmtSigned(s.total_return * 100, 1)}%`, note: "藍 = 賺、紅 = 賠",
  },
  profit_factor: {
    // 用 log2 讓 0.5 與 2 離中心一樣遠（1 = 打平）
    label: "獲利因子", kind: "diverging", value: (s) => (s.profit_factor === null ? null : Math.log2(Math.max(s.profit_factor, 1e-6))),
    fmt: (s) => (s.profit_factor === null ? "—" : fmtNum(s.profit_factor)), note: "藍 = 大於 1（有賺）、紅 = 小於 1",
  },
  max_drawdown: {
    label: "最大回撤", kind: "sequential", value: (s) => s.max_drawdown,
    fmt: (s) => fmtPct(s.max_drawdown, 0), note: "越紅回撤越大",
  },
};

const STEPS = 5; // 每一邊幾階：分成固定幾階比連續漸層好讀

function shade(v: number | null, max: number, kind: "diverging" | "sequential") {
  if (v === null || max <= 0) return undefined;
  const level = Math.min(STEPS, Math.ceil((Math.abs(v) / max) * STEPS));
  const pole = kind === "sequential" || v < 0 ? "var(--div-neg)" : "var(--div-pos)";
  // 每階比例由 CSS 的 --heat-step 決定（淺色 16%、深色 12%），讓最深的格子文字對比仍 ≥ 4.5:1
  return `color-mix(in oklab, ${pole} calc(var(--heat-step) * ${level}), var(--div-mid))`;
}

const bothProfitable = (c: SweepCell) => !!c.recent && !!c.earlier && c.recent.total_return > 0 && c.earlier.total_return > 0;

function Grid({ data, period, metric, max, onPick, picked }: {
  data: SweepResult; period: "recent" | "earlier"; metric: Metric; max: number;
  onPick: (yi: number, xi: number) => void; picked: [number, number] | null;
}) {
  const m = METRICS[metric];
  const ys = data.y?.values ?? [null];
  return (
    <div className="table-wrap">
      <table className="heat">
        <thead>
          <tr>
            <th className="y" aria-label={data.y ? `縱軸 ${data.y.label}、橫軸 ${data.x.label}` : data.x.label} />
            {data.x.values.map((v) => <th key={v}>{v}</th>)}
          </tr>
        </thead>
        <tbody>{ys.map((yv, yi) => (
          <tr key={yi}>
            <th className="y">{yv ?? ""}</th>
            {data.x.values.map((xv, xi) => {
              const c = data.cells[yi][xi];
              const st = c[period];
              if (!st) {
                return <td key={xi}><div className="cell na" title={c.error ?? "沒有資料"}>{c.error ? "✕" : "—"}</div></td>;
              }
              const both = bothProfitable(c);
              const sel = picked?.[0] === yi && picked?.[1] === xi;
              return (
                <td key={xi}>
                  <div
                    className={`cell${both ? " both" : ""}`} tabIndex={0}
                    style={{ background: shade(m.value(st), max, m.kind), boxShadow: sel ? "0 0 0 2px var(--accent)" : undefined }}
                    onMouseEnter={() => onPick(yi, xi)} onFocus={() => onPick(yi, xi)}
                    aria-label={`${data.x.label} ${xv}${data.y ? `、${data.y.label} ${yv}` : ""}：${m.label} ${m.fmt(st)}${both ? "，兩段都賺" : ""}`}
                  >
                    {both && "✓ "}{m.fmt(st)}
                  </div>
                </td>
              );
            })}
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function Legend({ metric, max }: { metric: Metric; max: number }) {
  const m = METRICS[metric];
  const levels = m.kind === "diverging"
    ? [...Array.from({ length: STEPS }, (_, i) => -(STEPS - i)), 0, ...Array.from({ length: STEPS }, (_, i) => i + 1)]
    : Array.from({ length: STEPS + 1 }, (_, i) => i);
  const end = (sign: number) =>
    metric === "total_return" ? `${fmtSigned(sign * max * 100, 0)}%`
      : metric === "profit_factor" ? fmtNum(2 ** (sign * max)) : fmtPct(max, 0);
  return (
    <div className="heat-legend">
      <span>{m.label}：{m.kind === "diverging" ? end(-1) : "0%"}</span>
      <span className="ramp">{levels.map((l) => <span key={l} style={{ background: l === 0 ? "var(--div-mid)" : shade(l, STEPS, m.kind) }} />)}</span>
      <span>{end(1)}</span>
      <span className="muted">{m.note}；「✓」加框 = 兩段期間報酬都 &gt; 0</span>
    </div>
  );
}

function Detail({ data, at }: { data: SweepResult; at: [number, number] }) {
  const [yi, xi] = at;
  const c = data.cells[yi][xi];
  const label = `${data.x.label} ${data.x.values[xi]}${data.y ? `、${data.y.label} ${data.y.values[yi]}` : ""}`;
  if (c.error) return <p className="muted" style={{ margin: 0 }}>{label}：{c.error}</p>;
  const row = (name: string, s: SweepStats | null | undefined) => s && (
    <tr key={name}>
      <td>{name}</td>
      <td className={`num ${s.total_return > 0 ? "up" : s.total_return < 0 ? "down" : ""}`}>{fmtSigned(s.total_return * 100, 2)}%</td>
      <td className="num">{s.profit_factor === null ? "—" : fmtNum(s.profit_factor)}</td>
      <td className="num">{fmtPct(s.win_rate)}</td>
      <td className="num">{s.trades}</td>
      <td className="num">{fmtPct(s.max_drawdown)}</td>
      <td className="num">{s.funding_included ? fmtSigned(s.funding) : "—"}</td>
      <td className="num">{fmtSigned(s.buy_hold_return * 100, 1)}%</td>
    </tr>
  );
  return (
    <div>
      <strong style={{ fontSize: 13 }}>{label}</strong>
      <div className="table-wrap">
        <table>
          <thead><tr><th>期間</th><th className="num">報酬</th><th className="num">獲利因子</th><th className="num">勝率</th><th className="num">交易</th><th className="num">最大回撤</th><th className="num" title="正 = 付出、負 = 收到">資金費</th><th className="num">同期幣價</th></tr></thead>
          <tbody>{row("更早期間", c.earlier)}{row("最近期間", c.recent)}</tbody>
        </table>
      </div>
    </div>
  );
}

/** 參數穩健性：每個參數組合在兩段期間的回測結果，畫成兩張共用色階的熱圖。 */
export function SweepHeatmap({ data, metric }: { data: SweepResult; metric: Metric }) {
  const [picked, setPicked] = useState<[number, number] | null>(null);
  const m = METRICS[metric];
  const vals = data.cells.flat().flatMap((c) => [c.recent, c.earlier]).filter((s): s is SweepStats => !!s).map(m.value)
    .filter((v): v is number => v !== null);
  const max = Math.max(...vals.map(Math.abs), 1e-9); // 兩張圖共用同一個色階才能比較
  const cells = data.cells.flat().filter((c) => c.recent);
  const both = cells.filter(bothProfitable).length;
  const periods: ["earlier" | "recent", string][] = data.earlier_available ? [["earlier", "更早期間"], ["recent", "最近期間"]] : [["recent", "最近期間"]];
  return (
    <div className="grid" style={{ gap: 12 }}>
      <Legend metric={metric} max={max} />
      <div className="muted" style={{ fontSize: 12 }}>
        橫軸：<b>{data.x.label}</b>{data.y && <>　縱軸：<b>{data.y.label}</b></>}
      </div>
      <div className="heat-wrap">
        {periods.map(([p, title]) => (
          <div key={p}>
            <h2 style={{ fontSize: 13, margin: "0 0 6px" }}>{title}</h2>
            <Grid data={data} period={p} metric={metric} max={max} onPick={(yi, xi) => setPicked([yi, xi])} picked={picked} />
          </div>
        ))}
      </div>
      {data.earlier_available
        ? <p style={{ margin: 0, fontSize: 13 }}>{cells.length} 個組合中，<b>{both}</b> 個在兩段期間都賺錢。好的參數應該是一整片相連的「✓」，只有零星幾格通常是運氣。</p>
        : <p className="muted" style={{ margin: 0 }}>歷史資料不夠長，只有最近期間。</p>}
      {picked ? <Detail data={data} at={picked} /> : <p className="muted" style={{ margin: 0, fontSize: 12 }}>把滑鼠移到格子上（或用 Tab 鍵選取）看兩段期間的完整數字。</p>}
    </div>
  );
}
