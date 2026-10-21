import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type BotSettings } from "../api/client";

export function BotControls() {
  const qc = useQueryClient();
  const bot = useQuery({ queryKey: ["bot"], queryFn: () => api<BotSettings>("/api/bot") });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api("/api/bot", { method: "PUT", body: JSON.stringify({ enabled }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["bot"] }),
  });
  const closeAll = useMutation({
    mutationFn: () => api("/api/bot/close-all", { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["bot"] }),
  });
  if (!bot.data) return null;
  const on = bot.data.enabled;
  return (
    <div className="toolbar" style={{ margin: 0 }}>
      <span className="badge">
        <span className="dot" style={{ background: on ? "var(--up)" : "var(--text-3)" }} />
        {on ? "運行中" : "已停用"}
      </span>
      <button className="btn" disabled={toggle.isPending} onClick={() => toggle.mutate(!on)}>
        {on ? "停用 Bot" : "啟用 Bot"}
      </button>
      <button
        className="btn danger"
        disabled={closeAll.isPending}
        onClick={() => confirm("確定要市價平掉所有持倉並停用 Bot 嗎？") && closeAll.mutate()}
      >
        緊急全部平倉
      </button>
    </div>
  );
}
