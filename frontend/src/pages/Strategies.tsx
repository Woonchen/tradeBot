import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  api, type BacktestResult, type BotSettings, type Market, type ParamSpec, type StrategyConfig, type StrategyInfo,
} from "../api/client";
import { BacktestChart } from "../components/BacktestChart";
import { RuleBuilder, rulesForMarket, type Rules } from "../components/RuleBuilder";
import { EquityChart } from "../components/EquityChart";
import { fmtNum, fmtPct, fmtPrice, fmtSigned, fmtTime, pnlClass } from "../format";

const INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];
const BARS = [500, 1000, 2000, 5000];
const TARGET_ZH: Record<string, string> = { long: "▲ 多", short: "▼ 空", flat: "空手" };

type Draft = {
  id?: number; name: string; strategy: string; symbol: string; market: Market; interval: string;
  params: Record<string, any>; enabled: boolean;
};

const autoName = (symbol: string, label: string, interval: string) => `${symbol.replace(/USDT$/, "")} ${label.split(/[（ ]/)[0]} ${interval}`;

const defaults = (info: StrategyInfo) => Object.fromEntries(info.params.map((p) => [p.name, structuredClone(p.default)]));

/** 送出前的整理：條件組合器在現貨時清掉做空條件。 */
const cleanParams = (info: StrategyInfo, params: Record<string, any>, market: Market) =>
  Object.fromEntries(Object.entries(params).map(([k, v]) =>
    [k, info.params.find((p) => p.name === k)?.type === "rules" ? rulesForMarket(v as Rules, market) : v]));

function ParamField({ spec, value, onChange }: { spec: ParamSpec; value: number | boolean; onChange: (v: number | boolean) => void }) {
  if (spec.type === "bool") {
    return (
      <label className="field">
        {spec.label}
        <select value={value ? "1" : "0"} onChange={(e) => onChange(e.target.value === "1")}>
          <option value="1">是</option><option value="0">否</option>
        </select>
        {spec.help && <small>{spec.help}</small>}
      </label>
    );
  }
  return (
    <label className="field">
      {spec.label}
      <input
        type="number" value={String(value)} min={spec.min ?? undefined} max={spec.max ?? undefined}
        step={spec.step ?? (spec.type === "int" ? 1 : "any")}
        onChange={(e) => onChange(e.target.value === "" ? 0 : Number(e.target.value))}
      />
      <small>{spec.help || (spec.min !== null || spec.max !== null ? `範圍 ${spec.min ?? "—"} ~ ${spec.max ?? "—"}` : "")}</small>
    </label>
  );
}

function BacktestView({ result }: { result: BacktestResult }) {
  const s = result.stats;
  const tile = (label: string, value: string, cls = "", sub?: string) => (
    <div className="card tile"><div className="label">{label}</div><div className={`value ${cls}`}>{value}</div>{sub && <div className="sub">{sub}</div>}</div>
  );
  const equity = result.equity.map((p) => ({ ts: new Date(p.time * 1000).toISOString(), total_usdt: p.value, spot_usdt: 0, futures_usdt: 0 }));
  return (
    <div className="grid" style={{ marginTop: 16 }}>
      <div className="tiles" style={{ marginBottom: 0 }}>
        {tile("策略報酬", `${fmtSigned(s.total_return * 100)}%`, pnlClass(s.total_return), `${fmtNum(s.start_equity)} → ${fmtNum(s.final_equity)} USDT`)}
        {tile("最大回撤", fmtPct(s.max_drawdown))}
        {tile("交易次數", String(s.trades), "", `勝率 ${fmtPct(s.win_rate)}`)}
        {tile("獲利因子", s.profit_factor === null ? "—" : fmtNum(s.profit_factor), "", "總獲利 ÷ 總虧損，>1 才有賺")}
        {tile("手續費", fmtNum(s.fees), "", `同期幣價 ${fmtSigned(s.buy_hold_return * 100)}%`)}
      </div>
      <div className="card">
        <h2>{result.symbol} 回測（{s.bars} 根 K 棒）</h2>
        <BacktestChart candles={result.candles} trades={result.trades} />
        <div className="legend-row">
          <span><span className="up">▲</span> 開多／平空</span><span><span className="down">▼</span> 平多／開空，平倉標註淨損益</span>
          <span className="muted">訊號在 K 棒收盤產生，下一根開盤成交</span>
        </div>
      </div>
      <div className="card"><h2>回測權益曲線（USDT）</h2><EquityChart points={equity} short /></div>
      <div className="card">
        <h2>交易明細</h2>
        {result.trades.length === 0 ? <div className="empty">這段期間沒有交易</div> : (
          <div className="table-wrap" style={{ maxHeight: 360, overflowY: "auto" }}>
            <table>
              <thead><tr><th>方向</th><th>進場</th><th className="num">進場價</th><th>出場</th><th className="num">出場價</th><th className="num">損益</th><th className="num">手續費</th><th className="num">淨損益</th></tr></thead>
              <tbody>{[...result.trades].reverse().map((t, i) => (
                <tr key={i}>
                  <td><span className={`badge ${t.side}`}>{TARGET_ZH[t.side]}</span></td>
                  <td className="muted">{fmtTime(new Date(t.entry_time * 1000).toISOString())}</td><td className="num">{fmtPrice(t.entry)}</td>
                  <td className="muted">{fmtTime(new Date(t.exit_time * 1000).toISOString())}</td><td className="num">{fmtPrice(t.exit)}</td>
                  <td className={`num ${pnlClass(t.pnl)}`}>{fmtSigned(t.pnl)}</td><td className="num">{fmtNum(t.fees)}</td>
                  <td className={`num ${pnlClass(t.net)}`}>{fmtSigned(t.net)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function Editor({ catalog, initial, onDone }: { catalog: StrategyInfo[]; initial: Draft; onDone: () => void }) {
  const qc = useQueryClient();
  const bot = useQuery({ queryKey: ["bot"], queryFn: () => api<BotSettings>("/api/bot") });
  const [d, setD] = useState<Draft>(initial);
  const [bars, setBars] = useState(1000);
  const info = catalog.find((c) => c.key === d.strategy)!;

  const backtest = useMutation({
    mutationFn: () => api<BacktestResult>("/api/strategies/backtest", {
      method: "POST",
      body: JSON.stringify({ strategy: d.strategy, symbol: d.symbol, market: d.market, interval: d.interval, params: cleanParams(info, d.params, d.market), bars }),
    }),
  });
  const save = useMutation({
    mutationFn: (enabled: boolean) => api(d.id ? `/api/strategies/${d.id}` : "/api/strategies", {
      method: d.id ? "PUT" : "POST",
      body: JSON.stringify({ ...d, params: cleanParams(info, d.params, d.market), enabled }),
    }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["strategies"] }); onDone(); },
  });
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setD({ ...d, [k]: v });

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h2>{d.id ? `編輯策略：${initial.name}` : "新增策略"}</h2>
      <div className="form-grid">
        <label className="field">名稱<input value={d.name} onChange={(e) => set("name", e.target.value)} /></label>
        <label className="field">策略
          <select value={d.strategy} onChange={(e) => {
            const next = catalog.find((c) => c.key === e.target.value)!;
            // 名稱還是自動產生的就跟著換，使用者改過就不動
            const name = d.name === autoName(d.symbol, info.label, d.interval) ? autoName(d.symbol, next.label, d.interval) : d.name;
            setD({ ...d, name, strategy: next.key, params: defaults(next) });
          }}>
            {catalog.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
          </select>
        </label>
        <label className="field">交易對<input value={d.symbol} onChange={(e) => set("symbol", e.target.value.toUpperCase())} placeholder="BTCUSDT" /></label>
        <label className="field">市場
          <select value={d.market} onChange={(e) => set("market", e.target.value as Market)}>
            <option value="futures">合約</option><option value="spot">現貨（不做空）</option>
          </select>
        </label>
        <label className="field">K 線週期
          <select value={d.interval} onChange={(e) => set("interval", e.target.value)}>
            {INTERVALS.map((i) => <option key={i}>{i}</option>)}
          </select>
          <small>每根 K 棒收盤後判斷一次</small>
        </label>
      </div>
      <p className="muted" style={{ margin: "14px 0 6px" }}>{info.description}</p>
      <div className="form-grid">
        {info.params.filter((p) => p.type !== "rules").map((p) => (
          <ParamField key={p.name} spec={p} value={d.params[p.name] ?? p.default}
            onChange={(v) => set("params", { ...d.params, [p.name]: v })} />
        ))}
      </div>
      {info.params.filter((p) => p.type === "rules").map((p) => (
        <RuleBuilder key={p.name} market={d.market} value={d.params[p.name] ?? p.default}
          onChange={(v) => set("params", { ...d.params, [p.name]: v })} />
      ))}
      <div className="toolbar" style={{ marginTop: 16, marginBottom: 0 }}>
        <label className="field" style={{ display: "flex", alignItems: "center", gap: 6 }}>回測
          <select value={bars} onChange={(e) => setBars(Number(e.target.value))}>
            {BARS.map((b) => <option key={b} value={b}>最近 {b} 根</option>)}
          </select>
        </label>
        <button className="btn" disabled={backtest.isPending} onClick={() => backtest.mutate()}>
          {backtest.isPending ? "回測中…" : "執行回測"}
        </button>
        <span style={{ flex: 1 }} />
        <button className="btn" onClick={onDone}>取消</button>
        <button className="btn" disabled={save.isPending} onClick={() => save.mutate(false)}>儲存（停用）</button>
        <button className="btn primary" disabled={save.isPending} onClick={() => save.mutate(true)}>儲存並啟用</button>
      </div>
      {bot.data && (
        <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
          回測與實際下單都使用「設定」頁的下單規則：{bot.data.size_mode === "fixed" ? `每筆 ${bot.data.size_value} USDT 保證金` : `可用資金的 ${bot.data.size_value}%`}
          {d.market === "futures" && ` × ${bot.data.leverage} 倍槓桿`}。啟用後會等策略判斷「改變」時才下單，不會立刻進場。
        </p>
      )}
      {backtest.isError && <p className="error-text">{(backtest.error as Error).message}</p>}
      {save.isError && <p className="error-text">{(save.error as Error).message}</p>}
      {backtest.data && <BacktestView result={backtest.data} />}
    </div>
  );
}

export function Strategies() {
  const qc = useQueryClient();
  const catalog = useQuery({ queryKey: ["strategies", "catalog"], queryFn: () => api<StrategyInfo[]>("/api/strategies/catalog") });
  const configs = useQuery({ queryKey: ["strategies", "configs"], queryFn: () => api<StrategyConfig[]>("/api/strategies"), refetchInterval: 30_000 });
  const [editing, setEditing] = useState<Draft | null>(null);

  const toggle = useMutation({
    mutationFn: (c: StrategyConfig) => api(`/api/strategies/${c.id}`, {
      method: "PUT",
      body: JSON.stringify({ name: c.name, strategy: c.strategy, symbol: c.symbol, market: c.market, interval: c.interval, params: c.params, enabled: !c.enabled }),
    }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["strategies"] }),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api(`/api/strategies/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["strategies"] }),
  });

  if (!catalog.data) return null;
  const labelOf = (key: string) => catalog.data.find((c) => c.key === key)?.label ?? key;
  const newDraft = (): Draft => {
    const first = catalog.data[0];
    return { name: autoName("BTCUSDT", first.label, "1h"), strategy: first.key, symbol: "BTCUSDT", market: "futures", interval: "1h", params: defaults(first), enabled: false };
  };

  return (
    <>
      <div className="toolbar" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>策略</h1>
        {!editing && <button className="btn primary" onClick={() => setEditing(newDraft())}>＋ 新增策略</button>}
      </div>
      {editing && <Editor key={editing.id ?? "new"} catalog={catalog.data} initial={editing} onDone={() => setEditing(null)} />}
      <div className="card">
        {(configs.data ?? []).length === 0 ? (
          <div className="empty">還沒有策略。按「新增策略」，先回測，再啟用。</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>名稱</th><th>策略</th><th>交易對</th><th>週期</th><th>狀態</th><th>目前判斷</th><th>最後判斷時間</th><th /></tr></thead>
              <tbody>{configs.data!.map((c) => (
                <tr key={c.id}>
                  <td>{c.name}</td>
                  <td className="muted">{labelOf(c.strategy)}</td>
                  <td>{c.symbol} <span className="muted">{c.market === "futures" ? "合約" : "現貨"}</span></td>
                  <td>{c.interval}</td>
                  <td>
                    <span className="badge"><span className="dot" style={{ background: c.enabled ? "var(--up)" : "var(--text-3)" }} />{c.enabled ? "運行中" : "停用"}</span>
                    {c.last_error && <div className="error-text" title={c.last_error}>⚠ {c.last_error.slice(0, 40)}</div>}
                  </td>
                  <td>{c.last_target ? <span className={`badge ${c.last_target}`}>{TARGET_ZH[c.last_target]}</span> : <span className="muted">—</span>}</td>
                  <td className="muted">{c.last_bar_time ? fmtTime(new Date(c.last_bar_time * 1000).toISOString()) : "—"}</td>
                  <td style={{ display: "flex", gap: 6 }}>
                    <button className="btn small" disabled={toggle.isPending} onClick={() => toggle.mutate(c)}>{c.enabled ? "停用" : "啟用"}</button>
                    <button className="btn small" onClick={() => setEditing({ id: c.id, name: c.name, strategy: c.strategy, symbol: c.symbol, market: c.market, interval: c.interval, params: c.params, enabled: c.enabled })}>編輯／回測</button>
                    <button className="btn small danger" disabled={remove.isPending} onClick={() => confirm(`刪除策略「${c.name}」？既有持倉不會自動平倉。`) && remove.mutate(c.id)}>刪除</button>
                  </td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
        <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
          「目前判斷」是最後一根收盤 K 棒的計算結果，判斷改變時才會下單。策略下單同樣受 Bot 啟用開關、每日虧損上限、停損停利等設定控制。
          自己寫新策略：在 backend/app/strategies/ 新增 Python 檔（參考 ema_cross.py），重新部署後會自動出現在這裡。
        </p>
      </div>
    </>
  );
}
