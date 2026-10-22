import { useSyncExternalStore } from "react";
import { getToken, setToken } from "./client";

export interface LiveMessage { type: string; data: any; log: unknown }
export interface ChartSub { symbol: string; market: string; interval: string }

/**
 * 跟後端 /ws 的單一連線（全站共用）。
 * - 系統事件（成交、訂單…）→ onMessage 的訂閱者
 * - {"type":"prices"} 持倉即時價格 → 價格 store（usePrice）
 * - 圖表訂閱：setChart() 會記住目前的訂閱，斷線重連後自動重送
 */
class LiveSocket {
  private ws: WebSocket | null = null;
  private retry?: ReturnType<typeof setTimeout>;
  private ping?: ReturnType<typeof setInterval>;
  private running = false;
  private chart: ChartSub | null = null;
  private handlers = new Set<(m: LiveMessage) => void>();
  private statusListeners = new Set<() => void>();
  private priceListeners = new Set<() => void>();
  connected = false;
  prices: Record<string, number> = {};

  start() {
    if (this.running) return;
    this.running = true;
    this.connect();
  }

  stop() {
    this.running = false;
    clearTimeout(this.retry);
    clearInterval(this.ping);
    this.ws?.close();
    this.ws = null;
  }

  private connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    // token 用第一則訊息送，不放在網址：網址會被反向代理與伺服器的存取 log 記下來
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws = ws;
    ws.onopen = () => {
      ws.send(JSON.stringify({ op: "auth", token: getToken() ?? "" }));
      this.setConnected(true);
      this.ping = setInterval(() => ws.send("ping"), 25_000);
      if (this.chart) this.send({ op: "chart", ...this.chart });
    };
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data) as LiveMessage;
      if (msg.type === "prices") {
        this.prices = { ...this.prices, ...msg.data };
        this.priceListeners.forEach((l) => l());
      }
      this.handlers.forEach((h) => h(msg));
    };
    ws.onclose = (e) => {
      clearInterval(this.ping);
      this.setConnected(false);
      if (e.code === 4401) {
        // token 過期或密碼已更換：回到登入頁，不要一直重連
        this.running = false;
        setToken(null);
        return;
      }
      if (this.running && this.ws === ws) this.retry = setTimeout(() => this.connect(), 3000);
    };
  }

  private setConnected(v: boolean) {
    this.connected = v;
    this.statusListeners.forEach((l) => l());
  }

  private send(obj: unknown) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj));
  }

  setChart(sub: ChartSub | null) {
    this.chart = sub;
    this.send({ op: "chart", ...(sub ?? { symbol: null }) });
  }

  onMessage(h: (m: LiveMessage) => void) {
    this.handlers.add(h);
    return () => void this.handlers.delete(h);
  }

  subscribeStatus = (l: () => void) => {
    this.statusListeners.add(l);
    return () => void this.statusListeners.delete(l);
  };

  subscribePrices = (l: () => void) => {
    this.priceListeners.add(l);
    return () => void this.priceListeners.delete(l);
  };
}

export const live = new LiveSocket();

export function useLiveConnected(): boolean {
  return useSyncExternalStore(live.subscribeStatus, () => live.connected);
}

/** 持倉的即時價格（合約為標記價格）；沒有即時資料時回傳 undefined。 */
export function useLivePrices(): Record<string, number> {
  return useSyncExternalStore(live.subscribePrices, () => live.prices);
}
