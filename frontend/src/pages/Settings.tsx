import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { api, type BotSettings } from "../api/client";
import { fmtNum } from "../format";

type Form = Omit<BotSettings, "execution_mode" | "testnet" | "symbols" | "drawdown"> & { symbols: string };

/** 一列設定：左邊欄位名稱 + 說明，右邊輸入框。 */
function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="set-row">
      <span className="set-label">{label}</span>
      <span className="set-control">
        {children}
        {hint && <small>{hint}</small>}
      </span>
    </label>
  );
}

/** 一組設定，例如「下單金額」。 */
function Section({ title, desc, children }: { title: string; desc: string; children: ReactNode }) {
  return (
    <section className="set-section">
      <div className="set-head">
        <h2>{title}</h2>
        <p>{desc}</p>
      </div>
      <div className="set-fields">{children}</div>
    </section>
  );
}

/** 數字輸入框，右側顯示單位。 */
function NumInput({ value, unit, onChange, ...rest }: {
  value: number; unit: string; onChange: (v: number) => void; min?: number; max?: number; step?: number | "any";
}) {
  return (
    <span className="input-unit">
      <input type="number" step="any" {...rest} value={value} onChange={(e) => onChange(Number(e.target.value))} />
      <span>{unit}</span>
    </span>
  );
}

function Seg<T extends string>({ value, options, onChange }: { value: T; options: [T, string][]; onChange: (v: T) => void }) {
  return (
    <span className="seg">
      {options.map(([v, label]) => (
        <button key={v} type="button" className={value === v ? "on" : ""} onClick={() => onChange(v)}>{label}</button>
      ))}
    </span>
  );
}

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
          ...f, drawdown: undefined,
          symbols: f.symbols.split(/[,\s]+/).map((s) => s.trim().toUpperCase()).filter(Boolean),
        }),
      }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["bot"] }); setSaved(true); setTimeout(() => setSaved(false), 2000); },
  });

  if (!form || !bot.data) return null;
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm({ ...form, [k]: v });
  const fixed = form.size_mode === "fixed";
  const lev = Math.max(form.leverage, 1);
  // 價格反向 x% 時，合約保證金大約虧掉 x × 槓桿 %
  const onMargin = (pct: number) => (pct > 0 ? `合約 ${lev} 倍槓桿下，約等於保證金的 ${fmtNum(pct * lev, 1)}%` : "0 = 不設");

  return (
    <>
      <h1>設定</h1>
      <div className="banner">
        執行模式：<strong>{bot.data.execution_mode === "paper" ? "Paper 本地模擬" : bot.data.testnet ? "Binance Demo Trading" : "⚠ Binance 正式帳戶"}</strong>
        （在後端 .env 設定 EXECUTION_MODE / TESTNET）
      </div>
      <form className="card settings" onSubmit={(e) => { e.preventDefault(); save.mutate(form); }}>
        <Section title="交易範圍" desc="限制 TradingView 訊號與自動策略可以交易哪些幣種；手動操作不受限制。">
          <Field label="允許交易的交易對" hint="用逗號或空白分隔，例如 BTCUSDT, ETHUSDT。留空 = 全部允許">
            <input value={form.symbols} onChange={(e) => set("symbols", e.target.value)} placeholder="BTCUSDT, ETHUSDT" />
          </Field>
        </Section>

        <Section title="每筆下單金額" desc="每次開倉投入多少保證金。現貨不使用槓桿，投入金額即買入金額。">
          <Field label="計算方式">
            <Seg value={form.size_mode} onChange={(v) => set("size_mode", v)}
              options={[["fixed", "固定金額"], ["percent", "可用餘額 %"], ["equity", "總權益 %"]]} />
          </Field>
          <Field
            label={fixed ? "每筆保證金" : form.size_mode === "equity" ? "每筆使用總權益" : "每筆使用可用餘額"}
            hint={fixed
              ? `合約部位價值 = ${fmtNum(form.size_value, 2)} × ${lev} 倍 = ${fmtNum(form.size_value * lev, 2)} USDT`
              : form.size_mode === "equity"
                ? `開倉當下帳戶總權益（含其他部位的保證金與未實現損益）的 ${fmtNum(form.size_value, 2)}%，再乘以 ${lev} 倍槓桿。多個策略同時持倉時每個分到一樣多；可用餘額不夠時用剩下的進場`
                : `開倉當下可用 USDT 的 ${fmtNum(form.size_value, 2)}%，合約部位價值再乘以 ${lev} 倍槓桿`}
          >
            <NumInput value={form.size_value} unit={fixed ? "USDT" : "%"} min={0} onChange={(v) => set("size_value", v)} />
          </Field>
        </Section>

        <Section title="合約" desc="只套用在合約（futures）市場，開倉時設定到交易所。">
          <Field label="槓桿倍數" hint="1–125 倍">
            <NumInput value={form.leverage} unit="倍" min={1} max={125} step={1} onChange={(v) => set("leverage", v)} />
          </Field>
          <Field label="保證金模式" hint={form.margin_mode === "isolated" ? "每個部位各自計算保證金，爆倉只損失該部位" : "所有部位共用帳戶餘額當保證金"}>
            <Seg value={form.margin_mode} onChange={(v) => set("margin_mode", v)}
              options={[["isolated", "逐倉 Isolated"], ["cross", "全倉 Cross"]]} />
          </Field>
        </Section>

        <Section title="風險控管" desc="開新倉前檢查，超過限制的訊號會被拒絕；平倉不受影響。">
          <Field label="最多同時持倉" hint="現貨與合約合計；反手（先平再開）不會被擋">
            <NumInput value={form.max_open_positions} unit="個" min={1} step={1} onChange={(v) => set("max_open_positions", v)} />
          </Field>
          <Field label="每日虧損上限" hint="當日（UTC 0 點起算）已實現損益扣手續費後虧損達此金額，自動停用 Bot">
            <NumInput value={form.max_daily_loss_usdt} unit="USDT" min={0} onChange={(v) => set("max_daily_loss_usdt", v)} />
          </Field>
          <Field label="回撤熔斷" hint={<>
            權益從最高點回撤超過此比例，<strong>平掉所有持倉</strong>並停用 Bot。現貨、合約分開計算；連續兩次（約 2 分鐘）都超過才觸發。0 = 不啟用。
            重新啟用 Bot 時，最高點從當下重新記錄。轉帳、出金也會被算成回撤。
            {bot.data.drawdown.map((x) => (
              <span key={x.market} style={{ display: "block", marginTop: 4 }}>
                {x.market === "futures" ? "合約" : "現貨"}：最高點 {fmtNum(x.peak, 2)} USDT，目前 {x.equity === null ? "—" : fmtNum(x.equity, 2)}
                {x.drawdown_pct !== null && `，回撤 ${fmtNum(Math.max(x.drawdown_pct, 0), 1)}%`}
              </span>
            ))}
          </>}>
            <NumInput value={form.max_drawdown_pct} unit="%" min={0} max={99} onChange={(v) => set("max_drawdown_pct", v)} />
          </Field>
          <Field label="熔斷後降槓桿" hint={form.breaker_fallback_leverage > 0
            ? `熔斷全部平倉後，槓桿高於 ${form.breaker_fallback_leverage} 倍的合約策略降到 ${form.breaker_fallback_leverage} 倍，Bot 繼續運作；已經是這個倍數再觸發，就停用 Bot（只自動降一次）。策略要等判斷改變才會重新進場`
            : "0 = 熔斷就停用 Bot，要手動重新啟用"}>
            <NumInput value={form.breaker_fallback_leverage} unit="倍" min={0} max={125} step={1} onChange={(v) => set("breaker_fallback_leverage", v)} />
          </Field>
        </Section>

        <Section title="停損與停利" desc="以開倉價為基準的價格變動幅度，每個部位開倉時自動算出觸發價。">
          <Field label="停損：價格反向" hint={onMargin(form.stop_loss_pct)}>
            <NumInput value={form.stop_loss_pct} unit="%" min={0} onChange={(v) => set("stop_loss_pct", v)} />
          </Field>
          <Field label="停利：價格順向" hint={onMargin(form.take_profit_pct)}>
            <NumInput value={form.take_profit_pct} unit="%" min={0} onChange={(v) => set("take_profit_pct", v)} />
          </Field>
        </Section>

        <div className="set-actions">
          <button className="btn primary" type="submit" disabled={save.isPending}>儲存設定</button>
          {saved && <span className="up">已儲存 ✓</span>}
          {save.isError && <span className="error-text">{(save.error as Error).message}</span>}
        </div>
      </form>
    </>
  );
}
