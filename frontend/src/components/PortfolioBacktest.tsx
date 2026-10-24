import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type BotSettings, type PortfolioResult, type StrategyConfig } from "../api/client";
import { fmtNum, fmtPct, fmtSigned, fmtTime, pnlClass } from "../format";
import { EquityChart } from "./EquityChart";

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
  const run = useMutation({
    mutationFn: () => api<PortfolioResult>("/api/strategies/portfolio-backtest", {
      method: "POST", body: JSON.stringify({ ids, bars, start_equity: equity, breaker }),
    }),
  });
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
        {fmtDate(s.from)} ～ {fmtDate(s.to)}，起始 {fmtNum(s.start_equity)} USDT。
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
