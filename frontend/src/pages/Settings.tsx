import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type BotSettings } from "../api/client";

type Form = Omit<BotSettings, "execution_mode" | "testnet" | "symbols"> & { symbols: string };

export function Settings() {
  const qc = useQueryClient();
  const bot = useQuery({ queryKey: ["bot"], queryFn: () => api<BotSettings>("/api/bot") });
  const [form, setForm] = useState<Form | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (bot.data && !form) setForm({ ...bot.data, symbols: bot.data.symbols.join(", ") });
  }, [bot.data, form]);

  const save = useMutation({
    mutationFn: (f: Form) =>
      api<BotSettings>("/api/bot", {
        method: "PUT",
        body: JSON.stringify({
          ...f,
          symbols: f.symbols.split(/[,\s]+/).map((s) => s.trim().toUpperCase()).filter(Boolean),
        }),
      }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["bot"] }); setSaved(true); setTimeout(() => setSaved(false), 2000); },
  });

  if (!form || !bot.data) return null;
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm({ ...form, [k]: v });
  const num = (k: keyof Form) => (e: React.ChangeEvent<HTMLInputElement>) => set(k, Number(e.target.value) as never);

  return (
    <>
      <h1>設定</h1>
      <div className="banner">
        執行模式：<strong>{bot.data.execution_mode === "paper" ? "Paper 本地模擬" : bot.data.testnet ? "Binance Demo Trading" : "⚠ Binance 正式帳戶"}</strong>
        （在後端 .env 設定 EXECUTION_MODE / TESTNET）
      </div>
      <form className="card" onSubmit={(e) => { e.preventDefault(); save.mutate(form); }}>
        <div className="form-grid">
          <label className="field">允許交易的 Ticker<input value={form.symbols} onChange={(e) => set("symbols", e.target.value)} placeholder="BTCUSDT, ETHUSDT" /><small>留空 = 全部允許</small></label>
          <label className="field">下單方式
            <select value={form.size_mode} onChange={(e) => set("size_mode", e.target.value as Form["size_mode"])}>
              <option value="fixed">每筆固定 USDT</option><option value="percent">可用餘額的 %</option>
            </select>
          </label>
          <label className="field">{form.size_mode === "fixed" ? "每筆保證金 (USDT)" : "每筆使用可用餘額 (%)"}<input type="number" step="any" value={form.size_value} onChange={num("size_value")} /><small>合約實際部位 = 保證金 × 槓桿</small></label>
          <label className="field">合約槓桿<input type="number" min={1} max={125} value={form.leverage} onChange={num("leverage")} /></label>
          <label className="field">保證金模式
            <select value={form.margin_mode} onChange={(e) => set("margin_mode", e.target.value as Form["margin_mode"])}>
              <option value="isolated">逐倉 isolated</option><option value="cross">全倉 cross</option>
            </select>
          </label>
          <label className="field">最大同時持倉數<input type="number" min={1} value={form.max_open_positions} onChange={num("max_open_positions")} /></label>
          <label className="field">每日最大虧損 (USDT)<input type="number" step="any" value={form.max_daily_loss_usdt} onChange={num("max_daily_loss_usdt")} /><small>達到後自動停用 Bot</small></label>
          <label className="field">停損 (%)<input type="number" step="any" min={0} value={form.stop_loss_pct} onChange={num("stop_loss_pct")} /><small>0 = 不設</small></label>
          <label className="field">停利 (%)<input type="number" step="any" min={0} value={form.take_profit_pct} onChange={num("take_profit_pct")} /><small>0 = 不設</small></label>
        </div>
        <div className="toolbar" style={{ marginTop: 16, marginBottom: 0 }}>
          <button className="btn primary" type="submit" disabled={save.isPending}>儲存</button>
          {saved && <span className="up">已儲存 ✓</span>}
          {save.isError && <span className="error-text">{(save.error as Error).message}</span>}
        </div>
      </form>
    </>
  );
}
