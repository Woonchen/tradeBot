import { type BotSettings, type Market, type ParamSpec, type SizeMode, type StrategyConfig, type StrategyInfo } from "../../api/client";
import { rulesForMarket, type Rules } from "../../components/RuleBuilder";
import { fmtNum, INTERVAL_SEC } from "../../format";

export const INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];
export const BARS = [1000, 5000, 10000, 20000];
export const approxDays = (bars: number, interval: string) => {
  const days = (bars * INTERVAL_SEC[interval]) / 86400;
  return days < 1 ? `約 ${Math.round(days * 24)} 小時` : `約 ${Math.round(days)} 天`;
};
export const fmtDate = (t: number | null) => (t === null ? "—" : new Date(t * 1000).toLocaleDateString("zh-TW"));

export type Sizing = {
  size_mode: SizeMode; size_value: number; leverage: number; start_equity: number;
  stop_loss_pct: number; take_profit_pct: number;
};
export const EXIT_ZH: Record<string, string> = { signal: "策略訊號", stop: "停損", take_profit: "停利", end: "回測結束" };
export const protectionText = (sl: number, tp: number) =>
  sl || tp ? `；停損 ${sl ? `${sl}%` : "不設"}、停利 ${tp ? `${tp}%` : "不設"}` : "；不設停損停利";
export const TARGET_ZH: Record<string, string> = { long: "▲ 多", short: "▼ 空", flat: "空手" };

/** 策略自己的下單規則（每項 null = 沿用「設定」頁） */
export type OrderRules = Pick<StrategyConfig, "size_mode" | "size_value" | "leverage" | "stop_loss_pct" | "take_profit_pct">;
export const ORDER_KEYS = ["size_mode", "size_value", "leverage", "stop_loss_pct", "take_profit_pct"] as const;
export const NO_ORDER: OrderRules = { size_mode: null, size_value: null, leverage: null, stop_loss_pct: null, take_profit_pct: null };
/** 只取有設定的欄位（送給 API；沒送的由後端用設定頁補） */
export const orderBody = (o: OrderRules) => Object.fromEntries(ORDER_KEYS.filter((k) => o[k] !== null).map((k) => [k, o[k]]));
export const hasOrder = (o: OrderRules) => ORDER_KEYS.some((k) => o[k] !== null);
export const pickOrder = (c: OrderRules): OrderRules => Object.fromEntries(ORDER_KEYS.map((k) => [k, c[k]])) as OrderRules;

/** 加碼設定（三個都 null = 不加碼） */
export type Pyramid = Pick<StrategyConfig, "pyramid_atr" | "pyramid_max" | "pyramid_frac">;
export const PYRAMID_KEYS = ["pyramid_atr", "pyramid_max", "pyramid_frac"] as const;
export const NO_PYRAMID: Pyramid = { pyramid_atr: null, pyramid_max: null, pyramid_frac: null };
export const pickPyramid = (c: Pyramid): Pyramid => Object.fromEntries(PYRAMID_KEYS.map((k) => [k, c[k]])) as Pyramid;
export const pyramidBody = (c: Pyramid) => (c.pyramid_atr === null ? {} : pickPyramid(c));
export const pyramidText = (c: Pyramid) =>
  c.pyramid_atr === null ? "" : `；每漲 ${c.pyramid_atr} 個 ATR 加碼 ${c.pyramid_frac} 份，最多 ${c.pyramid_max} 次`;

/** 列表用的一行摘要：實際會用的下單設定（策略沒填的用設定頁的值，整組都照設定頁時註明） */
export function orderSummary(c: StrategyConfig, bot: BotSettings | undefined): string {
  if (!bot) return "";
  const mode = c.size_mode ?? bot.size_mode, value = c.size_value ?? bot.size_value;
  const lev = c.leverage ?? bot.leverage, sl = c.stop_loss_pct ?? bot.stop_loss_pct, tp = c.take_profit_pct ?? bot.take_profit_pct;
  const size = mode === "fixed" ? `每筆 ${fmtNum(value, 0)} USDT` : `${mode === "equity" ? "總權益" : "可用資金"} ${fmtNum(value, 0)}%`;
  const parts = [`${size}${c.market === "futures" ? ` × ${lev} 倍` : ""}`, sl ? `停損 ${fmtNum(sl, 0)}%` : "不設停損"];
  if (tp) parts.push(`停利 ${fmtNum(tp, 0)}%`);
  if (c.pyramid_atr !== null) parts.push(`加碼 ${c.pyramid_atr} ATR × ${c.pyramid_frac} 份 × ${c.pyramid_max} 次`);
  return (hasOrder(c) ? "" : "照設定頁：") + parts.join("｜");
}

export type Draft = OrderRules & Pyramid & {
  id?: number; name: string; strategy: string; symbol: string; market: Market; interval: string;
  params: Record<string, any>; enabled: boolean; enter_on_enable: boolean; mode: "live" | "shadow";
};

export const autoName = (symbol: string, label: string, interval: string) => `${symbol.replace(/USDT$/, "")} ${label.split(/[（ ]/)[0]} ${interval}`;

export const defaults = (info: StrategyInfo) => Object.fromEntries(info.params.map((p) => [p.name, structuredClone(p.default)]));

/** 送出前的整理：條件組合器在現貨時清掉做空條件。 */
export const cleanParams = (info: StrategyInfo, params: Record<string, any>, market: Market) =>
  Object.fromEntries(Object.entries(params).map(([k, v]) =>
    [k, info.params.find((p) => p.name === k)?.type === "rules" ? rulesForMarket(v as Rules, market) : v]));

export function ParamField({ spec, value, onChange }: { spec: ParamSpec; value: number | boolean; onChange: (v: number | boolean) => void }) {
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
