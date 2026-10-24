import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type BotSettings, type PortfolioResult, type SizeMode, type StrategyConfig } from "../api/client";
import { fmtNum, fmtPct, fmtSigned, fmtTime, pnlClass, SIZE_MODES, sizeLabel } from "../format";
import { EquityChart } from "./EquityChart";

type Override = { size_mode: SizeMode; size_value: number; leverage: number };
type Run = { label: string; r: PortfolioResult };

/** 這次回測用的下單規則，寫成一句話（各策略不同時寫「照各策略」） */
function sizingLabel(r: PortfolioResult): string {
  const kinds = new Set(r.legs.map((l) => `${l.size_mode}|${l.size_value}|${l.leverage}`));
  if (kinds.size !== 1) return "照各策略的下單設定";
  const l = r.legs[0];
  const what = l.size_mode === "fixed" ? `每筆 ${fmtNum(l.size_value, 0)} USDT` : `${l.size_mode === "equity" ? "總權益" : "可用資金"} ${l.size_value}%`;
  return `${what} × ${l.leverage} 倍`;
}

const fmtDate = (t: number | null) => (t === null ? "—" : new Date(t * 1000).toLocaleDateString("zh-TW"));

/** 多個策略共用同一個帳戶一起回測：部位大小照各自的下單規則、以當下的共用權益計算。 */
export function PortfolioBacktest({ configs }: { configs: StrategyConfig[] }) {
  const bot = useQuery({ queryKey: ["bot"], queryFn: () => api<BotSettings>("/api/bot") });
  const defaults = configs.filter((c) => c.enabled && c.mode === "live").map((c) => c.id);
  const [picked, setPicked] = useState<number[] | null>(null); // null = 預設（啟用中、實際下單的）
  const ids = picked ?? defaults;
  const [bars, setBars] = useState(3000);
  const [equity, setEquity] = useState(10000);
  const [breaker, setBreaker] = useState(true);
  const [override, setOverride] = useState<Override | null>(null); // null = 照各策略自己的下單設定
  const [runs, setRuns] = useState<Run[]>([]); // 這次開啟頁面後跑過的結果，方便比較
  const run = useMutation({
    mutationFn: () => api<PortfolioResult>("/api/strategies/portfolio-backtest", {
      method: "POST", body: JSON.stringify({ ids, bars, start_equity: equity, breaker, ...(override ?? {}) }),
    }),
    onSuccess: (r) => setRuns((xs) => [{ label: sizingLabel(r), r }, ...xs].slice(0, 8)),
  });
  const setOv = <K extends keyof Override>(k: K, v: Override[K]) => override && setOverride({ ...override, [k]: v });
  const toggle = (id: number) => setPicked(ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]);
  const dd = bot.data?.max_drawdown_pct ?? 0;

  return (
    <div className="card">
      <h2>投資組合回測</h2>
      <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
        勾選的策略共用同一個帳戶一起回測：每筆部位大小照策略自己的下單規則，用當下的共用權益計算；
        可用餘額不夠、持倉數達到上限（設定頁 {bot.data?.max_open_positions ?? "—"} 個）的訊號會被跳過，跟實際下單一樣。
      </p>
      <div className="pf-picks">
        {configs.map((c) => (
          <label key={c.id} className="check">
            <input type="checkbox" checked={ids.includes(c.id)} onChange={() => toggle(c.id)} />
            {c.name}<span className="muted">{c.mode === "shadow" ? "（影子）" : !c.enabled ? "（停用）" : ""}</span>
          </label>
        ))}
      </div>
      <div className="form-grid" style={{ marginTop: 12 }}>
        <label className="field">K 棒數<input type="number" min={100} max={20000} step={100} value={bars} onChange={(e) => setBars(Number(e.target.value))} />
          <small>各策略自己週期的根數（4h 3,000 根 ≈ 500 天）</small></label>
        <label className="field">起始資金（USDT）<input type="number" min={1} step="any" value={equity} onChange={(e) => setEquity(Number(e.target.value))} />
          <small>所有勾選的策略共用這筆資金</small></label>
      </div>
      <div className="form-grid" style={{ marginTop: 4 }}>
        <label className="field">下單設定
          <select value={override ? "custom" : "own"} onChange={(e) => setOverride(e.target.value === "own" ? null : { size_mode: "equity", size_value: 11, leverage: 2 })}>
            <option value="own">照各策略自己的設定</option><option value="custom">這次統一用…（不改策略）</option>
          </select>
        </label>
        {override && (
          <>
            <label className="field">計算方式
              <select value={override.size_mode} onChange={(e) => setOv("size_mode", e.target.value as SizeMode)}>
                {SIZE_MODES.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
              </select>
            </label>
            <label className="field">{sizeLabel(override.size_mode)}
              <input type="number" min={0} step="any" value={override.size_value} onChange={(e) => setOv("size_value", Number(e.target.value))} />
            </label>
            <label className="field">槓桿（倍）
              <input type="number" min={1} max={125} step={1} value={override.leverage} onChange={(e) => setOv("leverage", Number(e.target.value))} />
            </label>
          </>
        )}
      </div>
      <label className="check">
        <input type="checkbox" checked={breaker} onChange={(e) => setBreaker(e.target.checked)} />
        套用回撤熔斷（設定頁 {dd > 0 ? `${dd}%` : "未啟用"}）：權益從最高點回撤超過上限就全部平倉、停止交易
      </label>
      <div className="toolbar" style={{ margin: "8px 0 0" }}>
        <button className="btn primary" disabled={run.isPending || ids.length === 0} onClick={() => run.mutate()}>
          {run.isPending ? "回測中…" : `回測 ${ids.length} 個策略`}
        </button>
        <span className="muted" style={{ fontSize: 12 }}>不模擬強平；資金費在平倉時入帳</span>
      </div>
      {run.isError && <p className="error-text">{(run.error as Error).message}</p>}
      {runs.length > 1 && <RunCompare runs={runs} />}
      {run.data && <PortfolioView r={run.data} />}
    </div>
  );
}

function PortfolioView({ r }: { r: PortfolioResult }) {
  const s = r.stats;
  const tile = (label: string, value: string, cls = "", sub?: string) => (
    <div className="card tile"><div className="label">{label}</div><div className={`value ${cls}`}>{value}</div>{sub && <div className="sub">{sub}</div>}</div>
  );
  const skipped = Object.entries(s.skipped);
  const points = r.equity.map((p) => ({ ts: new Date(p.time * 1000).toISOString(), total_usdt: p.value, spot_usdt: 0, futures_usdt: 0 }));
  return (
    <div className="grid" style={{ marginTop: 16 }}>
      <p className="muted" style={{ margin: 0, fontSize: 12 }}>
        {fmtDate(s.from)} ～ {fmtDate(s.to)}，起始 {fmtNum(s.start_equity)} USDT，{sizingLabel(r)}。
        {skipped.length > 0 && ` 跳過的訊號：${skipped.map(([k, v]) => `${k} ${v} 次`).join("、")}。`}
      </p>
      {s.stopped_at !== null && (
        <p className="error-text" style={{ margin: 0 }}>⚠ {fmtTime(new Date(s.stopped_at * 1000).toISOString())} 觸發回撤熔斷：全部平倉並停止交易，之後的權益不再變動。</p>
      )}
      <div className="tiles" style={{ marginBottom: 0 }}>
        {tile("總報酬", `${fmtSigned(s.total_return * 100)}%`, pnlClass(s.total_return), `${fmtNum(s.start_equity)} → ${fmtNum(s.final_equity)} USDT`)}
        {tile("年化報酬", s.cagr === null ? "—" : `${fmtSigned(s.cagr * 100)}%`, s.cagr === null ? "" : pnlClass(s.cagr))}
        {tile("最大回撤", fmtPct(s.max_drawdown), "", "整個帳戶，含未實現損益")}
        {tile("交易次數", String(s.trades), "", `最多同時 ${s.max_concurrent} 個持倉`)}
        {tile("平均曝險", `${fmtNum(s.avg_exposure, 2)} 倍`, "", "平均部位價值 ÷ 權益")}
      </div>
      <div className="card"><h2>組合權益曲線（USDT）</h2><EquityChart points={points} short /></div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>策略</th><th>交易對</th><th className="num">交易</th><th className="num">勝率</th><th className="num">淨損益（USDT）</th></tr></thead>
          <tbody>{[...r.legs].sort((a, b) => b.net - a.net).map((x) => (
            <tr key={x.id}>
              <td>{x.name}</td><td className="muted">{x.symbol}</td><td className="num">{x.trades}</td>
              <td className="num">{fmtPct(x.win_rate)}</td>
              <td className={`num ${pnlClass(x.net)}`}>{fmtSigned(x.net)}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  );
}

/** 這次開啟頁面後跑過的組合回測並排比較（最新的在上面）。 */
function RunCompare({ runs }: { runs: Run[] }) {
  const d = (t: number | null) => fmtDate(t);
  return (
    <div style={{ marginTop: 16 }}>
      <h3 style={{ margin: "0 0 6px", fontSize: 14 }}>比較（這次開啟頁面後跑過的）</h3>
      <div className="table-wrap">
        <table>
          <thead><tr><th>下單設定</th><th>期間</th><th className="num">策略</th><th className="num">總報酬</th><th className="num">年化</th><th className="num">最大回撤</th><th className="num">年化 ÷ 回撤</th><th className="num">平均曝險</th><th>熔斷</th></tr></thead>
          <tbody>{runs.map(({ label, r }, i) => {
            const s = r.stats;
            return (
              <tr key={i}>
                <td>{label}{i === 0 && <span className="badge" style={{ marginLeft: 6 }}>最新</span>}</td>
                <td className="muted">{d(s.from)} ～ {d(s.to)}</td>
                <td className="num">{r.legs.length}</td>
                <td className={`num ${pnlClass(s.total_return)}`}>{fmtSigned(s.total_return * 100, 1)}%</td>
                <td className={`num ${s.cagr === null ? "" : pnlClass(s.cagr)}`}>{s.cagr === null ? "—" : `${fmtSigned(s.cagr * 100, 1)}%`}</td>
                <td className="num">{fmtPct(s.max_drawdown)}</td>
                <td className="num">{s.cagr === null || s.max_drawdown === 0 ? "—" : fmtNum(s.cagr / s.max_drawdown, 2)}</td>
                <td className="num">{fmtNum(s.avg_exposure, 2)} 倍</td>
                <td>{s.stopped_at !== null ? <span className="error-text">觸發</span> : <span className="muted">—</span>}</td>
              </tr>
            );
          })}</tbody>
        </table>
      </div>
    </div>
  );
}
