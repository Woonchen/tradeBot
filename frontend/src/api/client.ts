export type Market = "spot" | "futures";

export interface Signal {
  id: number; received_at: string; source: string; strategy: string; ticker: string; symbol: string | null;
  market: Market; action: string; target: string; price: number | null; status: string; reject_reason: string | null;
}
export interface Order {
  id: number; signal_id: number | null; symbol: string; market: Market; side: string; qty: number; filled_qty: number;
  avg_price: number | null; status: string; intent: string; reduce_only: boolean; error: string | null; created_at: string;
}
export interface Trade {
  id: number; order_id: number | null; signal_id: number | null; symbol: string; market: Market; side: "buy" | "sell";
  intent: string; price: number; qty: number; fee_usdt: number; realized_pnl: number; executed_at: string;
}
export interface Position {
  id: number; symbol: string; market: Market; side: "long" | "short"; qty: number; entry_price: number;
  mark_price: number | null; unrealized_pnl: number; realized_pnl: number; leverage: number;
  liquidation_price: number | null; stop_price: number | null; take_profit_price: number | null;
  native_protection: boolean; opened_at: string; closed_at: string | null;
  /** 開倉＋平倉手續費（USDT） */
  fees: number;
  /** 已實現損益扣掉手續費 */
  net: number;
  /** 誰開的倉：策略名稱、TradingView、手動…；找不到成交紀錄時是 null */
  owner: string | null;
}
export interface ActivityLog {
  id: number; ts: string; level: "info" | "warn" | "error"; category: string; message: string;
  signal_id: number | null; order_id: number | null;
}
export interface EquityPoint { ts: string; spot_usdt: number; futures_usdt: number; total_usdt: number }
export interface BalanceRow { market: Market; asset: string; free: number; locked: number; usdt_value: number }
export interface Candle { time: number; open: number; high: number; low: number; close: number; volume?: number }
export interface Summary {
  equity: number | null; equity_change_today: number; realized_today: number; realized_total: number;
  unrealized: number; open_positions: number; closed_trades: number; win_rate: number | null;
  max_drawdown: number; start_equity: number | null;
}
export interface BotSettings {
  enabled: boolean; symbols: string[]; size_mode: "fixed" | "percent"; size_value: number; leverage: number;
  margin_mode: "isolated" | "cross"; max_open_positions: number; max_daily_loss_usdt: number;
  stop_loss_pct: number; take_profit_pct: number; execution_mode: "paper" | "binance"; testnet: boolean;
}
export interface SignalTrace { signal: Signal; orders: Order[]; trades: Trade[]; logs: ActivityLog[] }

const TOKEN_KEY = "tradebot.token";

/** 目前連的是哪種帳戶（側邊欄、危險操作的確認都會用到） */
export const modeLabel = (b: Pick<BotSettings, "execution_mode" | "testnet">) =>
  b.execution_mode === "paper" ? "Paper 本地模擬" : b.testnet ? "Binance Demo 帳戶" : "Binance 正式帳戶（真錢）";
export const isLive = (b: Pick<BotSettings, "execution_mode" | "testnet">) => b.execution_mode === "binance" && !b.testnet;

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 無痕模式等情況下 storage 不可用，只是下次要重新登入 */
  }
  window.dispatchEvent(new Event("tradebot-auth"));
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers });
  if (res.status === 401 && token) setToken(null);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, typeof body.detail === "string" ? body.detail : res.statusText);
  }
  return res.json();
}

export const qs = (params: Record<string, string | number | undefined | null>) =>
  new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "") as [string, string][],
  ).toString();

export interface ParamSpec {
  name: string; label: string; type: "int" | "float" | "bool" | "rules"; default: any;
  min: number | null; max: number | null; step: number | null; help: string;
}
export interface StrategyInfo { key: string; label: string; description: string; params: ParamSpec[] }
export interface StrategyConfig {
  id: number; name: string; strategy: string; symbol: string; market: Market; interval: string;
  params: Record<string, any>; enabled: boolean; enter_on_enable: boolean; last_target: string | null;
  /** live：真的下單；shadow：影子模式，只記錄虛擬交易 */
  mode: "live" | "shadow";
  last_bar_time: number | null; last_run_at: string | null; last_error: string | null; created_at: string;
  /** 最近一次啟用（或啟用中改了設定）的時間 */
  enabled_at: string | null;
}
export interface OpenTrip { symbol: string; market: Market; side: "long" | "short"; qty: number; opened_at: string; realized: number; fees: number; unrealized: number | null }
export interface LivePerf {
  trades: number; wins: number; win_rate: number | null; net: number; fees: number; profit_factor: number | null;
  first_trade: string | null; last_trade: string | null; open: OpenTrip[];
}
export interface PerformanceReport {
  strategies: ({ config: StrategyConfig } & LivePerf)[];
  others: ({ label: string } & LivePerf)[];
}
export interface BacktestTrade {
  side: "long" | "short"; entry_time: number; exit_time: number; entry: number; exit: number; qty: number;
  pnl: number; fees: number; net: number; exit_reason: "signal" | "stop" | "take_profit" | "end";
  /** 這筆付掉的資金費（正 = 付、負 = 收），淨損益已扣掉 */
  funding: number;
}
export interface BacktestStats {
  start_equity: number; final_equity: number; total_return: number; max_drawdown: number; trades: number;
  win_rate: number | null; profit_factor: number | null; fees: number; buy_hold_return: number; bars: number;
  from: number | null; to: number | null;
  /** 資金費合計（正 = 付出）；funding_included 為 false 時沒有扣（現貨或拿不到資料） */
  funding: number; funding_included: boolean;
}
export interface BacktestSizing {
  market: Market; size_mode: "fixed" | "percent"; size_value: number; leverage: number; start_equity: number;
  stop_loss_pct: number; take_profit_pct: number;
}
export interface MetricPoint { value: number; ts: number; prev: number | null; week_ago: number | null }
export interface SentimentReport {
  fear_greed: { value: number; classification: string; ts: number; prev: number | null; week_ago: number | null } | null;
  symbols: {
    symbol: string;
    /** rate：最新一次（每 8 小時 %）；avg7_annualized：近 7 天平均換算年化 % */
    funding: { rate: number; ts: number; avg7_annualized: number } | null;
    long_short: MetricPoint | null; top_long_short: MetricPoint | null; open_interest: MetricPoint | null; taker_ratio: MetricPoint | null;
  }[];
  /** 系統自己記錄的合約數據累積了幾天 */
  recorded_days: number;
}
export interface SweepStats {
  total_return: number; profit_factor: number | null; max_drawdown: number; trades: number; win_rate: number | null;
  buy_hold_return: number; from: number | null; to: number | null; funding: number; funding_included: boolean;
}
/** 參數掃描的一格：參數衝突時只有 error；沒有更早期間時 earlier 是 null */
export interface SweepCell { recent?: SweepStats; earlier?: SweepStats | null; error?: string }
export interface SweepAxis { param: string; label: string; values: number[] }
export interface SweepResult {
  symbol: string; x: SweepAxis; y: SweepAxis | null;
  /** cells[列 = y][欄 = x] */
  cells: SweepCell[][];
  sizing: BacktestSizing; bars: number; earlier_available: boolean;
}
export interface BacktestResult {
  symbol: string; params: Record<string, any>; candles: (Candle & { volume: number })[];
  trades: BacktestTrade[]; equity: { time: number; value: number }[];
  stats: BacktestStats;
  sizing: BacktestSizing;
  /** 前面多抓、只用來計算指標的 K 棒數（不計入結果） */
  warmup: number;
  /** 要求驗證更早期間時的結果；歷史不夠時是 null */
  earlier: BacktestStats | null;
}
