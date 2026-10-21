import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Position } from "../api/client";
import { useLivePrices } from "../api/liveSocket";
import { fmtPrice, fmtSigned, fmtTime, livePnl, pnlClass } from "../format";

export function PositionsTable({ compact = false, status = "open" }: { compact?: boolean; status?: "open" | "closed" }) {
  const { data = [] } = useQuery({
    queryKey: ["positions", status],
    queryFn: () => api<Position[]>(`/api/positions?status=${status}`),
    refetchInterval: status === "open" ? 15_000 : false,
  });
  const close = useMutation({ mutationFn: (id: number) => api(`/api/positions/${id}/close`, { method: "POST" }) });
  const prices = useLivePrices();
  if (data.length === 0) return <div className="empty">{status === "open" ? "目前沒有持倉" : "沒有歷史部位"}</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>交易對</th><th>方向</th><th className="num">數量</th><th className="num">開倉價</th>
            {status === "open" ? (
              <>
                <th className="num">標記價</th><th className="num">未實現損益</th>
                {!compact && <><th className="num">槓桿</th><th className="num">強平價</th><th className="num">停損 / 停利</th><th>開倉時間</th></>}
                <th />
              </>
            ) : (
              <><th className="num">已實現損益</th><th>開倉</th><th>平倉</th></>
            )}
          </tr>
        </thead>
        <tbody>
          {data.map((p) => {
            const { mark, pnl } = livePnl(p, status === "open" ? prices[`${p.market}:${p.symbol}`] : undefined);
            return (
            <tr key={p.id}>
              <td>{p.symbol} <span className="muted">{p.market === "futures" ? "合約" : "現貨"}</span></td>
              <td><span className={`badge ${p.side}`}>{p.side === "long" ? "▲ 多" : "▼ 空"}</span></td>
              <td className="num">{p.qty}</td>
              <td className="num">{fmtPrice(p.entry_price)}</td>
              {status === "open" ? (
                <>
                  <td className="num">{fmtPrice(mark)}</td>
                  <td className={`num ${pnlClass(pnl)}`}>{fmtSigned(pnl, 4)}</td>
                  {!compact && (
                    <>
                      <td className="num">{p.leverage}x</td>
                      <td className="num">{fmtPrice(p.liquidation_price)}</td>
                      <td className="num">{fmtPrice(p.stop_price)} / {fmtPrice(p.take_profit_price)}{(p.stop_price || p.take_profit_price) && <span className="muted"> {p.native_protection ? "交易所" : "軟體"}</span>}</td>
                      <td className="muted">{fmtTime(p.opened_at)}</td>
                    </>
                  )}
                  <td>
                    <button
                      className="btn small danger"
                      disabled={close.isPending}
                      onClick={() => confirm(`確定市價平倉 ${p.symbol}？`) && close.mutate(p.id)}
                    >平倉</button>
                  </td>
                </>
              ) : (
                <>
                  <td className={`num ${pnlClass(p.realized_pnl)}`}>{fmtSigned(p.realized_pnl, 4)}</td>
                  <td className="muted">{fmtTime(p.opened_at)}</td>
                  <td className="muted">{p.closed_at && fmtTime(p.closed_at)}</td>
                </>
              )}
            </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function PositionsPage() {
  const [tab, setTab] = useState<"open" | "closed">("open");
  return (
    <>
      <h1>倉位</h1>
      <div className="toolbar">
        <div className="seg">
          <button className={tab === "open" ? "on" : ""} onClick={() => setTab("open")}>持倉中</button>
          <button className={tab === "closed" ? "on" : ""} onClick={() => setTab("closed")}>歷史部位</button>
        </div>
      </div>
      <div className="card"><PositionsTable status={tab} /></div>
    </>
  );
}
