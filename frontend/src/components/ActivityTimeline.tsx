import { useQuery } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { api, type ActivityLog, type SignalTrace } from "../api/client";
import { ACTION_ZH, fmtPrice, fmtQty, fmtSigned, fmtTime, INTENT_ZH, SIDE_ZH, SOURCE_ZH, STATUS_ZH, TARGET_ZH } from "../format";

const ICON = { info: "●", warn: "▲", error: "✕" } as const;
const CATEGORY_ZH: Record<string, string> = {
  signal: "訊號", order: "下單", trade: "成交", risk: "風控", system: "系統",
};

function Trace({ id }: { id: number }) {
  const { data, isLoading } = useQuery({
    queryKey: ["signals", id],
    queryFn: () => api<SignalTrace>(`/api/signals/${id}`),
  });
  if (isLoading || !data) return <div className="trace muted">載入中…</div>;
  const { signal: s, orders, trades } = data;
  return (
    <div className="trace">
      <div>
        <strong>訊號 #{s.id}</strong> <span className={`badge ${s.status}`}>{STATUS_ZH[s.status] ?? s.status}</span>{" "}
        {SOURCE_ZH[s.source] ?? s.source} · {s.strategy || "—"} · {s.ticker}（{s.market === "futures" ? "合約" : "現貨"}）·{" "}
        {ACTION_ZH[s.action] ?? s.action} → 目標 {TARGET_ZH[s.target] ?? s.target}
        {s.price !== null && <> · 訊號價 {fmtPrice(s.price)}</>}
        {s.reject_reason && <div className="error-text">原因：{s.reject_reason}</div>}
      </div>
      {orders.map((o) => (
        <div key={o.id}>
          ↳ 訂單 #{o.id} {INTENT_ZH[o.intent]} {SIDE_ZH[o.side] ?? o.side} {fmtQty(o.qty)} {o.symbol}{" "}
          <span className={`badge ${o.status}`}>{STATUS_ZH[o.status] ?? o.status}</span>
          {o.error && <span className="error-text"> {o.error}</span>}
        </div>
      ))}
      {trades.map((t) => (
        <div key={t.id}>
          ↳ 成交 {fmtQty(t.qty)} @ {fmtPrice(t.price)} · 手續費 {fmtSigned(-t.fee_usdt, 4)}
          {t.intent.startsWith("close") && (
            <> · 已實現 <span className={t.realized_pnl >= 0 ? "up" : "down"}>{fmtSigned(t.realized_pnl, 4)}</span></>
          )}
        </div>
      ))}
    </div>
  );
}

/** 操作軌跡：有 signal_id 的事件可點開，看該訊號 → 訂單 → 成交的完整鏈路。 */
export function ActivityTimeline({ logs }: { logs: ActivityLog[] }) {
  const [open, setOpen] = useState<number | null>(null);
  if (logs.length === 0) return <div className="empty">還沒有任何事件</div>;
  return (
    <div className="timeline">
      {logs.map((l) => {
        const clickable = l.signal_id !== null;
        const isOpen = clickable && open === l.id;
        return (
          <Fragment key={l.id}>
            <div
              className={`tl-item ${clickable ? "clickable" : ""}`}
              onClick={() => clickable && setOpen(isOpen ? null : l.id)}
              title={clickable ? "點擊查看完整軌跡" : undefined}
            >
              <span className={`tl-icon ${l.level}`} aria-label={l.level}>{ICON[l.level]}</span>
              <div className="tl-body">
                <span className="tl-meta">
                  <span className="when">{fmtTime(l.ts)}</span>
                  <span className="badge">{CATEGORY_ZH[l.category] ?? l.category}</span>
                </span>
                <span className="tl-msg">{l.message}</span>
              </div>
            </div>
            {isOpen && <Trace id={l.signal_id!} />}
          </Fragment>
        );
      })}
    </div>
  );
}
