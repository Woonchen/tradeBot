import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { getToken } from "./client";

// 後端事件 → 要重新抓的 query
const INVALIDATE: Record<string, string[][]> = {
  signal: [["signals"], ["activity"]],
  order_update: [["orders"], ["activity"]],
  trade: [["trades"], ["positions"], ["summary"], ["activity"]],
  position_update: [["positions"], ["summary"]],
  balance_update: [["equity"], ["balances"], ["summary"]],
  bot: [["bot"], ["activity"]],
  log: [["activity"]],
};

/** 連到後端 /ws，收到事件就讓對應的資料自動更新；斷線會自動重連。 */
export function useLiveEvents(): boolean {
  const qc = useQueryClient();
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout>;
    let ping: ReturnType<typeof setInterval>;
    let closed = false;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(getToken() ?? "")}`);
      ws.onopen = () => {
        setConnected(true);
        ping = setInterval(() => ws?.send("ping"), 25_000);
      };
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data) as { type: string; log: unknown };
        const keys = INVALIDATE[msg.type] ?? (msg.log ? [["activity"]] : []);
        keys.forEach((queryKey) => qc.invalidateQueries({ queryKey }));
      };
      ws.onclose = () => {
        setConnected(false);
        clearInterval(ping);
        if (!closed) retry = setTimeout(connect, 3000);
      };
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      clearInterval(ping);
      ws?.close();
    };
  }, [qc]);

  return connected;
}
