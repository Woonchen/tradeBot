export const fmtNum = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined || Number.isNaN(v)
    ? "—"
    : v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });

/** 價格依大小自動決定小數位數 */
export const fmtPrice = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : fmtNum(v, v >= 1000 ? 2 : v >= 1 ? 4 : 6);

/** 數量：去掉浮點誤差（0.0061138799999999995 → 0.00611388），最多 8 位小數 */
export const fmtQty = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : Number(v.toPrecision(10)).toLocaleString("en-US", { maximumFractionDigits: 8 });

export const fmtSigned = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${fmtNum(Math.abs(v), digits)}`;

export const fmtPct = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined ? "—" : `${fmtNum(v * 100, digits)}%`;

export const fmtTime = (iso: string) =>
  new Date(iso).toLocaleString("zh-TW", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });

export const INTERVAL_SEC: Record<string, number> = {
  "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400,
  "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400,
};

/** 距離某個時間點還有多久（「約 3 小時後」）；已經過了就回「即將」 */
export const fmtUntil = (ms: number) => {
  const s = Math.round((ms - Date.now()) / 1000);
  if (s <= 30) return "即將";
  if (s < 3600) return `約 ${Math.ceil(s / 60)} 分鐘後`;
  if (s < 86400) return `約 ${Math.round(s / 3600)} 小時後`;
  return `約 ${Math.round(s / 86400)} 天後`;
};

export const pnlClass = (v: number | null | undefined) => (v ? (v > 0 ? "up" : "down") : "");

export const INTENT_ZH: Record<string, string> = {
  open_long: "開多", close_long: "平多", open_short: "開空", close_short: "平空",
};
export const SIDE_ZH: Record<string, string> = { buy: "買入", sell: "賣出" };
export const TARGET_ZH: Record<string, string> = { long: "多單", short: "空單", flat: "空手" };
export const SOURCE_ZH: Record<string, string> = {
  tradingview: "TradingView", manual: "手動", protection: "停損停利", strategy: "策略",
};
export const ACTION_ZH: Record<string, string> = {
  buy: "買", sell: "賣", long: "做多", short: "做空", flat: "空手", close: "平倉", exit: "平倉",
};
export const STATUS_ZH: Record<string, string> = {
  accepted: "排隊中", executed: "已執行", rejected: "已拒絕", failed: "失敗",
  new: "新建", open: "掛單中", filled: "已成交", canceled: "已取消", unknown: "狀態不明",
};

/** 用即時價格重算未實現損益（沒有即時價格就用後端的數值） */
export const livePnl = (
  p: { side: "long" | "short"; qty: number; entry_price: number; mark_price: number | null; unrealized_pnl: number },
  price: number | undefined,
) =>
  price === undefined
    ? { mark: p.mark_price, pnl: p.unrealized_pnl }
    : { mark: price, pnl: (price - p.entry_price) * p.qty * (p.side === "long" ? 1 : -1) };
