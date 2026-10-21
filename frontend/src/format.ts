export const fmtNum = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined || Number.isNaN(v)
    ? "—"
    : v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });

/** 價格依大小自動決定小數位數 */
export const fmtPrice = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : fmtNum(v, v >= 1000 ? 2 : v >= 1 ? 4 : 6);

export const fmtSigned = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${fmtNum(Math.abs(v), digits)}`;

export const fmtPct = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined ? "—" : `${fmtNum(v * 100, digits)}%`;

export const fmtTime = (iso: string) =>
  new Date(iso).toLocaleString("zh-TW", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });

export const pnlClass = (v: number | null | undefined) => (v ? (v > 0 ? "up" : "down") : "");

export const INTENT_ZH: Record<string, string> = {
  open_long: "開多", close_long: "平多", open_short: "開空", close_short: "平空",
};
export const STATUS_ZH: Record<string, string> = {
  accepted: "排隊中", executed: "已執行", rejected: "已拒絕", failed: "失敗",
  new: "新建", open: "掛單中", filled: "已成交", canceled: "已取消", unknown: "狀態不明",
};
