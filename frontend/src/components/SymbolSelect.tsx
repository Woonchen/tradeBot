import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type BotSettings, type Position, type StrategyConfig } from "../api/client";

/** 常見的主流幣，加在持倉、允許交易清單、策略用到的幣後面。 */
const COMMON = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT"];
const OTHER = "__other__";

/** BTC/USDT:USDT（部位用的 ccxt 格式）→ BTCUSDT */
export const toTicker = (s: string) => s.split(":")[0].replace("/", "").toUpperCase();

/**
 * 幣別下拉選單：持倉、允許交易清單、策略用到的幣優先，再來是常見主流幣；
 * 最後一項「其他（自行輸入）」可以輸入清單以外的交易對（按 Enter 或離開輸入框才套用）。
 */
export function SymbolSelect({ value, onChange, width = 170 }: { value: string; onChange: (s: string) => void; width?: number }) {
  const positions = useQuery({ queryKey: ["positions", "open"], queryFn: () => api<Position[]>("/api/positions") });
  const bot = useQuery({ queryKey: ["bot"], queryFn: () => api<BotSettings>("/api/bot") });
  const strategies = useQuery({ queryKey: ["strategies", "configs"], queryFn: () => api<StrategyConfig[]>("/api/strategies") });
  const mine = [...new Set([
    ...(positions.data ?? []).map((p) => toTicker(p.symbol)),
    ...(bot.data?.symbols ?? []).map(toTicker),
    ...(strategies.data ?? []).map((c) => toTicker(c.symbol)),
  ])];
  const common = COMMON.filter((s) => !mine.includes(s));
  const current = toTicker(value);
  const listed = mine.includes(current) || common.includes(current);
  const [custom, setCustom] = useState(!listed);
  const [draft, setDraft] = useState(current);
  useEffect(() => setDraft(current), [current]);
  useEffect(() => { if (listed) setCustom(false); }, [listed]);

  const commit = () => {
    const v = toTicker(draft.trim());
    if (v && v !== current) onChange(v);
  };
  return (
    <span style={{ display: "inline-flex", gap: 6 }}>
      <select
        value={custom ? OTHER : current}
        style={{ width }}
        onChange={(e) => {
          if (e.target.value === OTHER) {
            setCustom(true);
            setDraft("");
          } else {
            setCustom(false);
            onChange(e.target.value);
          }
        }}
      >
        {mine.length > 0 && <optgroup label="持倉、交易清單、策略">{mine.map((s) => <option key={s} value={s}>{s}</option>)}</optgroup>}
        <optgroup label="常見幣種">{common.map((s) => <option key={s} value={s}>{s}</option>)}</optgroup>
        <option value={OTHER}>其他（自行輸入）…</option>
      </select>
      {custom && (
        <input
          autoFocus value={draft} placeholder="例如 PEPEUSDT" style={{ width: 130 }}
          onChange={(e) => setDraft(e.target.value.toUpperCase())}
          onBlur={commit}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } }}
        />
      )}
    </span>
  );
}
