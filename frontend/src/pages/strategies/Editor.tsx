import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type BacktestResult, type BotSettings, type Market, type StrategyInfo } from "../../api/client";
import { RuleBuilder } from "../../components/RuleBuilder";
import { SymbolSelect } from "../../components/SymbolSelect";
import { fmtNum, SIZE_MODES, sizeLabel, sizeText } from "../../format";
import { INTERVALS, BARS, approxDays, type Sizing, protectionText, NO_ORDER, orderBody, hasOrder, NO_PYRAMID, pyramidBody, pyramidText, type Draft, autoName, defaults, cleanParams, ParamField } from "./shared";
import { BacktestView } from "./BacktestView";
import { SweepPanel } from "./SweepPanel";

export function Editor({ catalog, initial, onDone }: { catalog: StrategyInfo[]; initial: Draft; onDone: () => void }) {
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
        ...(sizing ?? orderBody(d)), // 沒有另外自訂時，回測用這個策略的下單規則
        ...pyramidBody(d),
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
  // 改到既有策略的市場、交易對或週期時先確認：回測和儲存共用同一份表單，切去試回測後按儲存很容易誤改
  const keyChanges = d.id ? ([
    ["市場", initial.market, d.market], ["交易對", initial.symbol, d.symbol], ["K 線週期", initial.interval, d.interval],
  ] as const).filter(([, a, b]) => a !== b) : [];
  const trySave = (enabled: boolean) => {
    if (keyChanges.length) {
      const zh = (v: string) => (v === "futures" ? "合約" : v === "spot" ? "現貨" : v);
      const lines = keyChanges.map(([label, a, b]) => `${label}：${zh(a)} → ${zh(b)}`).join("\n");
      if (!confirm(`確定要修改策略「${initial.name}」的\n\n${lines}\n\n儲存後策略會重新開始觀察。如果只是想試回測，請按取消，回測不需要儲存。`)) return;
    }
    save.mutate(enabled);
  };
  // 實際會用的下單規則：策略有設定的用策略的，沒填的用設定頁
  const eff = bot.data && {
    size_mode: d.size_mode ?? bot.data.size_mode, size_value: d.size_value ?? bot.data.size_value,
    leverage: d.leverage ?? bot.data.leverage, stop_loss_pct: d.stop_loss_pct ?? bot.data.stop_loss_pct,
    take_profit_pct: d.take_profit_pct ?? bot.data.take_profit_pct,
  };
  const custom = hasOrder(d);

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
        <label className="field">交易對<SymbolSelect value={d.symbol} onChange={(v) => set("symbol", v)} width={200} /></label>
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
      <div className="bt-panel">
        <h2 style={{ margin: 0 }}>下單設定</h2>
        <div className="form-grid">
          <label className="field">金額、槓桿、停損停利
            <select value={custom ? "custom" : "bot"} onChange={(e) => setD(e.target.value === "bot" || !eff ? { ...d, ...NO_ORDER } : { ...d, ...eff })}>
              <option value="bot">照「設定」頁</option>
              <option value="custom">這個策略自訂</option>
            </select>
            <small>保證金模式、最多同時持倉、每日虧損上限一律照「設定」頁</small>
          </label>
          {custom && eff && (
            <>
              <label className="field">計算方式
                <select value={eff.size_mode} onChange={(e) => set("size_mode", e.target.value as Draft["size_mode"])}>
                  {SIZE_MODES.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
                </select>
              </label>
              <label className="field">{sizeLabel(eff.size_mode)}
                <input type="number" min={0} step="any" value={eff.size_value} onChange={(e) => set("size_value", Number(e.target.value))} />
              </label>
              {d.market === "futures" && (
                <label className="field">槓桿（倍）
                  <input type="number" min={1} max={125} step={1} value={eff.leverage} onChange={(e) => set("leverage", Number(e.target.value))} />
                </label>
              )}
              <label className="field">停損（價格反向 %）
                <input type="number" min={0} step="any" value={eff.stop_loss_pct} onChange={(e) => set("stop_loss_pct", Number(e.target.value))} />
                <small>0 = 不設</small>
              </label>
              <label className="field">停利（價格順向 %）
                <input type="number" min={0} step="any" value={eff.take_profit_pct} onChange={(e) => set("take_profit_pct", Number(e.target.value))} />
                <small>0 = 不設</small>
              </label>
            </>
          )}
        </div>
        <div className="form-grid">
          <label className="field">加碼
            <select value={d.pyramid_atr === null ? "off" : "on"} onChange={(e) => setD(e.target.value === "off"
              ? { ...d, ...NO_PYRAMID } : { ...d, pyramid_atr: 4, pyramid_max: 3, pyramid_frac: 0.5 })}>
              <option value="off">不加碼</option>
              <option value="on">賺錢時加碼</option>
            </select>
            <small>持倉往有利方向走夠遠就加一份，出場時全部一起平倉</small>
          </label>
          {d.pyramid_atr !== null && (
            <>
              <label className="field">間距（ATR）
                <input type="number" min={0.1} step="any" value={d.pyramid_atr} onChange={(e) => set("pyramid_atr", Number(e.target.value))} />
                <small>收盤比上次成交多走幾個 ATR(14) 就加碼</small>
              </label>
              <label className="field">每次份量
                <input type="number" min={0.05} step="any" value={d.pyramid_frac ?? 0.5} onChange={(e) => set("pyramid_frac", Number(e.target.value))} />
                <small>首筆金額的幾倍，0.5 = 半份</small>
              </label>
              <label className="field">最多次數
                <input type="number" min={1} max={10} step={1} value={d.pyramid_max ?? 3} onChange={(e) => set("pyramid_max", Number(e.target.value))} />
                <small>部位最多到首筆的 {fmtNum(1 + (d.pyramid_max ?? 0) * (d.pyramid_frac ?? 0), 2)} 倍</small>
              </label>
            </>
          )}
        </div>
      </div>
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
            <select value={sizing ? "custom" : "bot"} onChange={(e) => setSizing(e.target.value === "bot" || !eff ? null : {
              ...eff, start_equity: 10000,
            })}>
              <option value="bot">照這個策略的下單設定</option>
              <option value="custom">自訂（只用在這次回測）</option>
            </select>
            {!sizing && eff && (
              <small>
                {sizeText(eff.size_mode, eff.size_value)}
                {d.market === "futures" && ` × ${eff.leverage} 倍槓桿`}{protectionText(eff.stop_loss_pct, eff.take_profit_pct)}{pyramidText(d)}，起始 10,000 USDT
              </small>
            )}
          </label>
          {sizing && (
            <>
              <label className="field">計算方式
                <select value={sizing.size_mode} onChange={(e) => setSz("size_mode", e.target.value as Sizing["size_mode"])}>
                  {SIZE_MODES.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
                </select>
              </label>
              <label className="field">{sizeLabel(sizing.size_mode)}
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
        <button className="btn" disabled={save.isPending} onClick={() => trySave(false)}>儲存（停用）</button>
        <button className="btn primary" disabled={save.isPending} onClick={() => trySave(true)}>儲存並啟用</button>
      </div>
      {backtest.isError && <p className="error-text">{(backtest.error as Error).message}</p>}
      {save.isError && <p className="error-text">{(save.error as Error).message}</p>}
      {backtest.data && <BacktestView result={backtest.data} compareRequested={backtest.variables?.compare ?? false} />}
      <SweepPanel key={d.strategy} info={info} d={d} bars={bars} sizing={sizing} />
    </div>
  );
}
