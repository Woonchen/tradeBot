import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { live, useLiveConnected } from "./liveSocket";

// 後端事件 → 要重新抓的 query
const INVALIDATE: Record<string, string[][]> = {
  signal: [["signals"], ["activity"]],
  order_update: [["orders"], ["activity"]],
  trade: [["trades"], ["positions"], ["summary"], ["activity"]],
  position_update: [["positions"], ["summary"]],
  balance_update: [["equity"], ["balances"], ["summary"]],
  bot: [["bot"], ["activity"]],
  strategy: [["strategies"], ["activity"]],
  log: [["activity"]],
};

/** 登入後建立即時連線，收到事件就讓對應的資料自動更新；斷線會自動重連。 */
export function useLiveEvents(): boolean {
  const qc = useQueryClient();
  useEffect(() => {
    live.start();
    const off = live.onMessage((msg) => {
      const keys = INVALIDATE[msg.type] ?? (msg.log ? [["activity"]] : []);
      keys.forEach((queryKey) => qc.invalidateQueries({ queryKey }));
    });
    return () => {
      off();
      live.stop();
    };
  }, [qc]);
  return useLiveConnected();
}
