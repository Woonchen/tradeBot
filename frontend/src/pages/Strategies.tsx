import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  api, type BacktestResult, type BacktestStats, type BotSettings, type LivePerf, type Market, type ParamSpec, type PerformanceReport,
  type StrategyConfig, type StrategyInfo, type SweepResult,
} from "../api/client";
import { BacktestChart } from "../components/BacktestChart";
import { RuleBuilder, rulesForMarket, type Rules } from "../components/RuleBuilder";
import { EquityChart } from "../components/EquityChart";
import { METRICS, SweepHeatmap, type Metric } from "../components/SweepHeatmap";
import { fmtNum, fmtPct, fmtPrice, fmtSigned, fmtTime, INTERVAL_SEC, pnlClass } from "../format";

const INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];
const BARS = [1000, 5000, 10000, 20000];
const approxDays = (bars: number, interval: string) => {
  const days = (bars * INTERVAL_SEC[interval]) / 86400;
  return days < 1 ? `約 ${Math.round(days * 24)} 小時` : `約 ${Math.round(days)} 天`;
};
const fmtDate = (t: number | null) => (t === null ? "—" : new Date(t * 1000).toLocaleDateString("zh-TW"));

type Sizing = {
  size_mode: "fixed" | "percent"; size_value: number; leverage: number; start_equity: number;
  stop_loss_pct: number; take_profit_pct: number;
};
const EXIT_ZH: Record<string, string> = { signal: "策略訊號", stop: "停損", take_profit: "停利", end: "回測結束" };
const protectionText = (sl: number, tp: number) =>
  sl || tp ? `；停損 ${sl ? `${sl}%` : "不設"}、停利 ${tp ? `${tp}%` : "不設"}` : "；不設停損停利";
const TARGET_ZH: Record<string, string> = { long: "▲ 多", short: "▼ 空", flat: "空手" };

type Draft = {
  id?: number; name: string; strategy: string; symbol: string; market: Market; interval: string;
  params: Record<string, any>; enabled: boolean; enter_on_enable: boolean; mode: "live" | "shadow";
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

/** 最近期間 vs 更早期間：兩段都不賠，才比較能相信策略不是剛好配合了某段行情。 */
function PeriodCompare({ recent, earlier }: { recent: BacktestStats; earlier: BacktestStats }) {
  const rows: [string, BacktestStats][] = [["更早期間", earlier], ["最近期間", recent]];
  const pf = (x: BacktestStats) => (x.profit_factor === null ? "—" : fmtNum(x.profit_factor));
  const verdict = earlier.total_return >= 0 && recent.total_return >= 0
    ? <span className="up">兩段都沒有虧損 ✓</span>
    : <span className="down">至少一段虧損：策略可能只適合特定行情，建議不要直接用真錢</span>;
  return (
    <div className="card">
      <h2>期間驗證</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>期間</th><th>日期</th><th className="num">策略報酬</th><th className="num">獲利因子</th><th className="num">勝率</th><th className="num">交易</th><th className="num">最大回撤</th><th className="num">同期幣價</th></tr></thead>
          <tbody>{rows.map(([label, x]) => (
            <tr key={label}>
              <td>{label}</td>
              <td className="muted">{fmtDate(x.from)} ～ {fmtDate(x.to)}</td>
              <td className={`num ${pnlClass(x.total_return)}`}>{fmtSigned(x.total_return * 100)}%</td>
              <td className="num">{pf(x)}</td>
              <td className="num">{fmtPct(x.win_rate)}</td>
              <td className="num">{x.trades}</td>
              <td className="num">{fmtPct(x.max_drawdown)}</td>
              <td className={`num ${pnlClass(x.buy_hold_return)}`}>{fmtSigned(x.buy_hold_return * 100)}%</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      <p style={{ margin: "10px 0 0", fontSize: 13 }}>{verdict}</p>
    </div>
  );
}

function BacktestView({ result, compareRequested }: { result: BacktestResult; compareRequested: boolean }) {
  const s = result.stats;
  const z = result.sizing;
  const tile = (label: string, value: string, cls = "", sub?: string) => (
    <div className="card tile"><div className="label">{label}</div><div className={`value ${cls}`}>{value}</div>{sub && <div className="sub">{sub}</div>}</div>
  );
  const equity = result.equity.map((p) => ({ ts: new Date(p.time * 1000).toISOString(), total_usdt: p.value, spot_usdt: 0, futures_usdt: 0 }));
  return (
    <div className="grid" style={{ marginTop: 16 }}>
      <p className="muted" style={{ margin: 0, fontSize: 12 }}>
        {fmtDate(s.from)} ～ {fmtDate(s.to)}，{s.bars} 根 K 棒；
        {z.size_mode === "fixed" ? `每筆 ${fmtNum(z.size_value)} USDT 保證金` : `每筆用可用資金 ${z.size_value}%`}
        {z.market === "futures" && ` × ${z.leverage} 倍槓桿`}{protectionText(z.stop_loss_pct, z.take_profit_pct)}，起始 {fmtNum(z.start_equity)} USDT。
        {result.warmup > 0 && ` 另外多抓前面 ${result.warmup} 根讓指標穩定，不計入結果；起點當下若已在部位內，要等判斷改變才進場（跟實際啟用一樣）。`}
      </p>
      {result.earlier && <PeriodCompare recent={s} earlier={result.earlier} />}
      {compareRequested && !result.earlier && <p className="muted" style={{ margin: 0 }}>歷史資料不夠長，無法驗證更早的同長度期間。</p>}
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
              <thead><tr><th>方向</th><th>進場</th><th className="num">進場價</th><th>出場</th><th className="num">出場價</th><th className="num">損益</th><th className="num">手續費</th><th className="num">淨損益</th><th>出場原因</th></tr></thead>
              <tbody>{[...result.trades].reverse().map((t, i) => (
                <tr key={i}>
                  <td><span className={`badge ${t.side}`}>{TARGET_ZH[t.side]}</span></td>
                  <td className="muted">{fmtTime(new Date(t.entry_time * 1000).toISOString())}</td><td className="num">{fmtPrice(t.entry)}</td>
                  <td className="muted">{fmtTime(new Date(t.exit_time * 1000).toISOString())}</td><td className="num">{fmtPrice(t.exit)}</td>
                  <td className={`num ${pnlClass(t.pnl)}`}>{fmtSigned(t.pnl)}</td><td className="num">{fmtNum(t.fees)}</td>
                  <td className={`num ${pnlClass(t.net)}`}>{fmtSigned(t.net)}</td>
                  <td className={t.exit_reason === "stop" ? "down" : t.exit_reason === "take_profit" ? "up" : "muted"}>{EXIT_ZH[t.exit_reason] ?? "—"}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

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
function SweepPanel({ info, d, bars, sizing }: { info: StrategyInfo; d: Draft; bars: number; sizing: Sizing | null }) {
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
        ...(sizing ?? {}), x: { param: x!.param, values: xs }, ...(y ? { y: { param: y.param, values: ys } } : {}),
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

function Editor({ catalog, initial, onDone }: { catalog: StrategyInfo[]; initial: Draft; onDone: () => void }) {
  const qc = useQueryClient();
  const bot = useQuery({ queryKey: ["bot"], queryFn: () => api<BotSettings>("/api/bot") });
  const [d, setD] = useState<Draft>(initial);
  const [bars, setBars] = useState(5000);
  const [compare, setCompare] = useState(true);
  const [sizing, setSizing] = useState<Sizing | null>(null); // null = 照「設定」頁
  const info = catalog.find((c) => c.key === d.strategy)!;
  const setSz = <K extends keyof Sizing>(k: K, v: Sizing[K]) => sizing && setSizing({ ...sizing, [k]: v });

  const backtest = useMutation({
    mutationFn: (req: { compare: boolean }) => api<BacktestResult>("/api/strategies/backtest", {
      method: "POST",
      body: JSON.stringify({
        strategy: d.strategy, symbol: d.symbol, market: d.market, interval: d.interval,
        params: cleanParams(info, d.params, d.market), bars, compare_earlier: req.compare, enter_on_enable: d.enter_on_enable,
        ...(sizing ?? {}),
      }),
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
      <label className="field" style={{ maxWidth: 420, marginTop: 14 }}>執行方式
        <select value={d.mode} onChange={(e) => set("mode", e.target.value as Draft["mode"])}>
          <option value="live">實際下單</option>
          <option value="shadow">影子模式（只記錄、不下單）</option>
        </select>
        <small>
          {d.mode === "shadow"
            ? "照常判斷，但只記錄虛擬交易：不送單、不佔用部位，同一個交易對可以同時跑好幾個影子策略。結果在下方「實際績效」"
            : "照判斷真的下單；同一個交易對同時只能有一個實際下單的策略。改成影子模式不會平掉既有部位"}
        </small>
      </label>
      <label className="check" style={{ marginTop: 14 }}>
        <input type="checkbox" checked={d.enter_on_enable} onChange={(e) => set("enter_on_enable", e.target.checked)} />
        啟用時立即依目前判斷進場
        <span className="muted" style={{ fontSize: 12 }}>
          （關閉：啟用後只記下判斷，等判斷改變才下單。打開：第一次判斷是多單或空單就直接進場；已在啟用中的策略改這個設定並儲存，也會重新判斷並進場）
        </span>
      </label>
      <div className="bt-panel">
        <h2 style={{ margin: 0 }}>回測</h2>
        <div className="form-grid">
          <label className="field">期間
            <select value={bars} onChange={(e) => setBars(Number(e.target.value))}>
              {BARS.map((b) => <option key={b} value={b}>最近 {b.toLocaleString()} 根（{approxDays(b, d.interval)}）</option>)}
            </select>
            <small>越長越可靠；20,000 根約需 10–30 秒</small>
          </label>
          <label className="field">下單金額
            <select value={sizing ? "custom" : "bot"} onChange={(e) => setSizing(e.target.value === "bot" || !bot.data ? null : {
              size_mode: bot.data.size_mode, size_value: bot.data.size_value, leverage: bot.data.leverage, start_equity: 10000,
              stop_loss_pct: bot.data.stop_loss_pct, take_profit_pct: bot.data.take_profit_pct,
            })}>
              <option value="bot">照「設定」頁</option>
              <option value="custom">自訂</option>
            </select>
            {!sizing && bot.data && (
              <small>
                {bot.data.size_mode === "fixed" ? `每筆 ${bot.data.size_value} USDT 保證金` : `可用資金的 ${bot.data.size_value}%`}
                {d.market === "futures" && ` × ${bot.data.leverage} 倍槓桿`}{protectionText(bot.data.stop_loss_pct, bot.data.take_profit_pct)}，起始 10,000 USDT
              </small>
            )}
          </label>
          {sizing && (
            <>
              <label className="field">計算方式
                <select value={sizing.size_mode} onChange={(e) => setSz("size_mode", e.target.value as Sizing["size_mode"])}>
                  <option value="fixed">每筆固定 USDT</option><option value="percent">可用資金 %</option>
                </select>
              </label>
              <label className="field">{sizing.size_mode === "fixed" ? "每筆保證金（USDT）" : "每筆使用可用資金（%）"}
                <input type="number" min={0} step="any" value={sizing.size_value} onChange={(e) => setSz("size_value", Number(e.target.value))} />
              </label>
              {d.market === "futures" && (
                <label className="field">槓桿（倍）
                  <input type="number" min={1} max={125} step={1} value={sizing.leverage} onChange={(e) => setSz("leverage", Number(e.target.value))} />
                </label>
              )}
              <label className="field">起始資金（USDT）
                <input type="number" min={1} step="any" value={sizing.start_equity} onChange={(e) => setSz("start_equity", Number(e.target.value))} />
              </label>
              <label className="field">停損（價格反向 %）
                <input type="number" min={0} step="any" value={sizing.stop_loss_pct} onChange={(e) => setSz("stop_loss_pct", Number(e.target.value))} />
                <small>0 = 不設</small>
              </label>
              <label className="field">停利（價格順向 %）
                <input type="number" min={0} step="any" value={sizing.take_profit_pct} onChange={(e) => setSz("take_profit_pct", Number(e.target.value))} />
                <small>0 = 不設</small>
              </label>
            </>
          )}
        </div>
        <label className="check">
          <input type="checkbox" checked={compare} onChange={(e) => setCompare(e.target.checked)} />
          同時回測更早、一樣長的期間（驗證策略不是只適合最近的行情）
        </label>
        <div className="toolbar" style={{ margin: 0 }}>
          <button className="btn" disabled={backtest.isPending} onClick={() => backtest.mutate({ compare })}>
            {backtest.isPending ? "回測中…" : "執行回測"}
          </button>
          <span className="muted" style={{ fontSize: 12 }}>指標會自動多抓前面的歷史暖機，不計入結果</span>
        </div>
      </div>
      <div className="toolbar" style={{ marginTop: 16, marginBottom: 0 }}>
        <span className="muted" style={{ fontSize: 12 }}>
          {d.mode === "shadow" ? "影子模式的虛擬交易同樣使用「設定」頁的下單規則（虛擬帳戶起始 10,000 USDT）。" : "實際下單使用「設定」頁的下單規則。"}
          {d.enter_on_enable ? "啟用後第一次判斷就會照目前判斷進場。" : "啟用後會等策略判斷「改變」時才進場，不會立刻進場。"}
        </span>
        <span style={{ flex: 1 }} />
        <button className="btn" onClick={onDone}>取消</button>
        <button className="btn" disabled={save.isPending} onClick={() => save.mutate(false)}>儲存（停用）</button>
        <button className="btn primary" disabled={save.isPending} onClick={() => save.mutate(true)}>儲存並啟用</button>
      </div>
      {backtest.isError && <p className="error-text">{(backtest.error as Error).message}</p>}
      {save.isError && <p className="error-text">{(save.error as Error).message}</p>}
      {backtest.data && <BacktestView result={backtest.data} compareRequested={backtest.variables?.compare ?? false} />}
      <SweepPanel key={d.strategy} info={info} d={d} bars={bars} sizing={sizing} />
    </div>
  );
}

/** 策略最近一次啟用以來的 K 棒數，用來回測同一段期間。 */
const since = (c: StrategyConfig) => c.enabled_at ?? c.created_at;
const barsSince = (c: StrategyConfig) => Math.floor((Date.now() - Date.parse(since(c))) / 1000 / INTERVAL_SEC[c.interval]);

function PerfCells({ p }: { p: LivePerf }) {
  return (
    <>
      <td className="num">{p.trades}</td>
      <td className="num">{fmtPct(p.win_rate)}</td>
      <td className="num">{p.profit_factor === null ? "—" : fmtNum(p.profit_factor)}</td>
      <td className={`num ${pnlClass(p.net)}`}>{fmtSigned(p.net)}</td>
      <td className="num">{fmtNum(p.fees)}</td>
      <td>{p.open.length === 0 ? <span className="muted">—</span> : p.open.map((o) => (
        <div key={o.symbol}><span className={`badge ${o.side}`}>{TARGET_ZH[o.side]}</span> <span className={pnlClass(o.unrealized)}>{fmtSigned(o.unrealized)}</span></div>
      ))}</td>
    </>
  );
}

/** 每個策略的實際成交績效，並可用同一段期間回測對照。 */
function Performance({ labelOf }: { labelOf: (key: string) => string }) {
  const perf = useQuery({ queryKey: ["strategies", "performance"], queryFn: () => api<PerformanceReport>("/api/strategies/performance"), refetchInterval: 30_000 });
  const cmp = useMutation({
    mutationFn: (c: StrategyConfig) => api<BacktestResult>("/api/strategies/backtest", {
      method: "POST",
      body: JSON.stringify({
        strategy: c.strategy, symbol: c.symbol, market: c.market, interval: c.interval, params: c.params,
        bars: Math.min(barsSince(c), 20000), enter_on_enable: c.enter_on_enable,
      }),
    }),
  });
  if (!perf.data) return null;
  const rows = perf.data.strategies.filter((x) => x.config.enabled || x.trades > 0 || x.open.length > 0);
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h2>實際績效</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>策略</th><th>啟用時間</th><th className="num">完整交易</th><th className="num">勝率</th><th className="num">獲利因子</th><th className="num">淨損益</th><th className="num">手續費</th><th>持倉中（未實現）</th><th /></tr></thead>
          <tbody>
            {rows.map((x) => {
              const c = x.config;
              const bars = barsSince(c);
              const shown = cmp.variables?.id === c.id;
              const bt = shown ? cmp.data?.stats : undefined;
              return [
                <tr key={c.id}>
                  <td>{c.name}{c.mode === "shadow" && <span className="badge" style={{ marginLeft: 6 }}>影子（虛擬）</span>}<div className="muted" style={{ fontSize: 12 }}>{labelOf(c.strategy)} · {c.symbol} {c.interval}{c.enabled ? "" : " · 已停用"}</div></td>
                  <td className="muted">{fmtTime(since(c))}</td>
                  <PerfCells p={x} />
                  <td>
                    <button className="btn small" disabled={bars < 50 || cmp.isPending} title={bars < 50 ? "啟用未滿 50 根 K 棒，還不能比較" : ""}
                      onClick={() => cmp.mutate(c)}>{cmp.isPending && shown ? "回測中…" : "回測同期間"}</button>
                  </td>
                </tr>,
                shown && (bt || cmp.isError) ? (
                  <tr key={`${c.id}-bt`}>
                    <td colSpan={9} style={{ background: "var(--surface-2)" }}>
                      {cmp.isError ? <span className="error-text">{(cmp.error as Error).message}</span> : bt && (
                        <span style={{ fontSize: 13 }}>
                          同期間回測（{fmtDate(bt.from)} ～ {fmtDate(bt.to)}）：完整交易 <b>{bt.trades}</b>、勝率 <b>{fmtPct(bt.win_rate)}</b>、
                          獲利因子 <b>{bt.profit_factor === null ? "—" : fmtNum(bt.profit_factor)}</b>、報酬 <b className={pnlClass(bt.total_return)}>{fmtSigned(bt.total_return * 100)}%</b>
                          <span className="muted">　交易數或勝率差很多時，可能是全域停損、下單失敗或手動操作造成的；金額依回測條件計算，跟實際金額不能直接比</span>
                        </span>
                      )}
                    </td>
                  </tr>
                ) : null,
              ];
            })}
            {perf.data.others.map((o) => (
              <tr key={o.label}>
                <td>{o.label}</td><td className="muted">—</td><PerfCells p={o} /><td />
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
        部位由哪個策略開倉，整筆交易（含被停損停利、手動或在交易所平倉）就算給它；開倉到全部平掉算一筆完整交易，淨損益已扣開平倉手續費。
        只列出啟用中或有成交紀錄的策略。
      </p>
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
      body: JSON.stringify({
        name: c.name, strategy: c.strategy, symbol: c.symbol, market: c.market, interval: c.interval, params: c.params,
        enabled: !c.enabled, enter_on_enable: c.enter_on_enable, mode: c.mode,
      }),
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
    return { name: autoName("BTCUSDT", first.label, "1h"), strategy: first.key, symbol: "BTCUSDT", market: "futures", interval: "1h", params: defaults(first), enabled: false, enter_on_enable: false, mode: "live" };
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
                  <td>{c.name}{c.mode === "shadow" && <span className="badge" style={{ marginLeft: 6 }}>影子</span>}</td>
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
                    <button className="btn small" onClick={() => setEditing({ id: c.id, name: c.name, strategy: c.strategy, symbol: c.symbol, market: c.market, interval: c.interval, params: c.params, enabled: c.enabled, enter_on_enable: c.enter_on_enable, mode: c.mode })}>編輯／回測</button>
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
        {toggle.isError && <p className="error-text">{(toggle.error as Error).message}</p>}
      </div>
      <Performance labelOf={labelOf} />
    </>
  );
}
