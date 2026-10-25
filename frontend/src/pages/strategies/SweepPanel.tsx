import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { api, type ParamSpec, type StrategyInfo, type SweepResult } from "../../api/client";
import { METRICS, SweepHeatmap, type Metric } from "../../components/SweepHeatmap";
import { type Sizing, orderBody, pyramidBody, type Draft, cleanParams } from "./shared";

type AxisDraft = { param: string; from: number; to: number; step: number };
const MAX_CELLS = 100;

/** 從參數規格推一個預設範圍：預設值的一半到兩倍、大約 8 個值。 */
function defaultAxis(spec: ParamSpec): AxisDraft {
  const def = Number(spec.default);
  const lo = Math.max(spec.min ?? -Infinity, spec.type === "int" ? Math.round(def / 2) : def / 2);
  const hi = Math.min(spec.max ?? Infinity, def * 2);
  const raw = (hi - lo) / 7;
  const step = spec.type === "int" ? Math.max(1, Math.round(raw)) : Math.max(0.1, Number(raw.toFixed(1)));
  return { param: spec.name, from: lo, to: hi, step };
}

function axisValues(a: AxisDraft, spec: ParamSpec | undefined): number[] {
  if (!spec || !(a.step > 0) || a.to < a.from) return [];
  const out: number[] = [];
  for (let v = a.from; v <= a.to + 1e-9 && out.length <= 25; v += a.step) {
    out.push(spec.type === "int" ? Math.round(v) : Number(v.toFixed(4)));
  }
  return [...new Set(out)];
}

/** 參數穩健性掃描：一個或兩個數字參數的每個組合，在最近與更早兩段期間各回測一次，畫成熱圖。 */
export function SweepPanel({ info, d, bars, sizing }: { info: StrategyInfo; d: Draft; bars: number; sizing: Sizing | null }) {
  const numeric = info.params.filter((p) => p.type === "int" || p.type === "float");
  const [x, setX] = useState<AxisDraft | null>(numeric[0] ? defaultAxis(numeric[0]) : null);
  const [y, setY] = useState<AxisDraft | null>(null);
  const [metric, setMetric] = useState<Metric>("total_return");
  const spec = (name?: string) => numeric.find((p) => p.name === name);
  const xs = x ? axisValues(x, spec(x.param)) : [];
  const ys = y ? axisValues(y, spec(y.param)) : [];
  const cells = xs.length * (y ? ys.length : 1);
  const tooMany = xs.length > 25 || ys.length > 25 || cells > MAX_CELLS;

  const run = useMutation({
    mutationFn: () => api<SweepResult>("/api/strategies/sweep", {
      method: "POST",
      body: JSON.stringify({
        strategy: d.strategy, symbol: d.symbol, market: d.market, interval: d.interval,
        params: cleanParams(info, d.params, d.market), bars, compare_earlier: true, enter_on_enable: d.enter_on_enable,
        ...(sizing ?? orderBody(d)), ...pyramidBody(d), x: { param: x!.param, values: xs }, ...(y ? { y: { param: y.param, values: ys } } : {}),
      }),
    }),
  });

  if (numeric.length === 0) {
    return (
      <div className="bt-panel">
        <h2 style={{ margin: 0 }}>參數穩健性掃描</h2>
        <p className="muted" style={{ margin: 0 }}>這個策略沒有可以掃描的數字參數。條件組合器的參數在規則裡，可以改用「均線趨勢」等內建策略來掃描。</p>
      </div>
    );
  }

  const axisEditor = (a: AxisDraft, setA: (v: AxisDraft) => void, others: string[]) => {
    const sp = spec(a.param);
    return (
      <>
        <label className="field">參數
          <select value={a.param} onChange={(e) => setA(defaultAxis(spec(e.target.value)!))}>
            {numeric.filter((p) => p.name === a.param || !others.includes(p.name)).map((p) => <option key={p.name} value={p.name}>{p.label}</option>)}
          </select>
          {sp && <small>範圍 {sp.min ?? "—"} ~ {sp.max ?? "—"}，目前設定 {String(d.params[a.param] ?? sp.default)}</small>}
        </label>
        <label className="field">從<input type="number" step="any" value={a.from} onChange={(e) => setA({ ...a, from: Number(e.target.value) })} /></label>
        <label className="field">到<input type="number" step="any" value={a.to} onChange={(e) => setA({ ...a, to: Number(e.target.value) })} /></label>
        <label className="field">間距<input type="number" step="any" min={0} value={a.step} onChange={(e) => setA({ ...a, step: Number(e.target.value) })} /></label>
      </>
    );
  };

  return (
    <div className="bt-panel">
      <h2 style={{ margin: 0 }}>參數穩健性掃描</h2>
      <p className="muted" style={{ margin: 0, fontSize: 12 }}>
        每個參數組合都在「最近」與「更早」兩段期間各回測一次（期間長度、下單金額沿用上方回測設定）。
        好的參數應該是一整片相連都不錯的區域；只有某個數值特別好、旁邊都差，多半是運氣。
      </p>
      <h2 style={{ fontSize: 13, margin: 0 }}>橫軸</h2>
      <div className="form-grid">{x && axisEditor(x, setX, y ? [y.param] : [])}</div>
      <label className="check">
        <input type="checkbox" checked={!!y} disabled={numeric.length < 2} onChange={(e) => {
          const other = numeric.find((p) => p.name !== x?.param);
          setY(e.target.checked && other ? defaultAxis(other) : null);
        }} />
        加上第二個參數（縱軸）{numeric.length < 2 && "：這個策略只有一個數字參數"}
      </label>
      {y && <div className="form-grid">{axisEditor(y, setY, x ? [x.param] : [])}</div>}
      <div className="toolbar" style={{ margin: 0 }}>
        <label className="field" style={{ display: "flex", alignItems: "center", gap: 6 }}>顏色代表
          <select value={metric} onChange={(e) => setMetric(e.target.value as Metric)}>
            {Object.entries(METRICS).map(([k, m]) => <option key={k} value={k}>{m.label}</option>)}
          </select>
        </label>
        <button className="btn" disabled={run.isPending || tooMany || xs.length === 0} onClick={() => run.mutate()}>
          {run.isPending ? "掃描中…" : `掃描 ${cells} 個組合`}
        </button>
        <span className={tooMany ? "error-text" : "muted"} style={{ fontSize: 12 }}>
          {tooMany ? `最多 ${MAX_CELLS} 個組合（每軸最多 25 個值），請縮小範圍或加大間距` : `每個組合跑兩段回測；組合多、K 棒多時可能要一兩分鐘`}
        </span>
      </div>
      {run.isError && <p className="error-text" style={{ margin: 0 }}>{(run.error as Error).message}</p>}
      {run.data && <SweepHeatmap data={run.data} metric={metric} />}
    </div>
  );
}
