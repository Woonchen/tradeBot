import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type BalanceRow, type EquityPoint } from "../api/client";
import { EquityChart } from "../components/EquityChart";
import { fmtNum, fmtPct, fmtSigned, fmtTime, pnlClass } from "../format";

const RANGES = [{ d: 1, label: "1 天" }, { d: 7, label: "7 天" }, { d: 30, label: "30 天" }, { d: 365, label: "1 年" }];

function maxDrawdown(values: number[]) {
  let peak = -Infinity, mdd = 0;
  for (const v of values) { peak = Math.max(peak, v); if (peak > 0) mdd = Math.max(mdd, (peak - v) / peak); }
  return mdd;
}

export function Wallet() {
  const [days, setDays] = useState(7);
  const [showTable, setShowTable] = useState(false);
  const equity = useQuery({ queryKey: ["equity", days], queryFn: () => api<EquityPoint[]>(`/api/equity?days=${days}`) });
  const balances = useQuery({ queryKey: ["balances"], queryFn: () => api<BalanceRow[]>("/api/balances") });
  const pts = equity.data ?? [];
  const first = pts[0]?.total_usdt, last = pts.at(-1)?.total_usdt;
  const change = first && last ? last - first : null;

  return (
    <>
      <h1>錢包</h1>
      <div className="tiles">
        <div className="card tile"><div className="label">總權益 (USDT)</div><div className="value">{fmtNum(last)}</div>
          <div className="sub">現貨 {fmtNum(pts.at(-1)?.spot_usdt)} · 合約 {fmtNum(pts.at(-1)?.futures_usdt)}</div></div>
        <div className="card tile"><div className="label">區間變化</div><div className={`value ${pnlClass(change)}`}>{fmtSigned(change)}</div>
          <div className="sub">{first && change !== null ? fmtSigned((change / first) * 100) + "%" : "—"}</div></div>
        <div className="card tile"><div className="label">區間最大回撤</div><div className="value">{fmtPct(maxDrawdown(pts.map((p) => p.total_usdt)))}</div></div>
      </div>
      <div className="card">
        <div className="toolbar" style={{ justifyContent: "space-between" }}>
          <h2 style={{ margin: 0 }}>總權益曲線（USDT）</h2>
          <div className="toolbar" style={{ margin: 0 }}>
            <div className="seg">
              {RANGES.map((r) => <button key={r.d} className={days === r.d ? "on" : ""} onClick={() => setDays(r.d)}>{r.label}</button>)}
            </div>
            <button className="btn small" onClick={() => setShowTable(!showTable)}>{showTable ? "看圖表" : "看表格"}</button>
          </div>
        </div>
        {showTable ? (
          <div className="table-wrap" style={{ maxHeight: 400, overflowY: "auto" }}>
            <table>
              <thead><tr><th>時間</th><th className="num">現貨</th><th className="num">合約</th><th className="num">總計</th></tr></thead>
              <tbody>{[...pts].reverse().map((p) => (
                <tr key={p.ts}><td className="muted">{fmtTime(p.ts)}</td><td className="num">{fmtNum(p.spot_usdt)}</td><td className="num">{fmtNum(p.futures_usdt)}</td><td className="num">{fmtNum(p.total_usdt)}</td></tr>
              ))}</tbody>
            </table>
          </div>
        ) : <EquityChart points={pts} />}
      </div>
      <div className="card" style={{ marginTop: 16 }}>
        <h2>資產明細</h2>
        {(balances.data ?? []).length === 0 ? <div className="empty">尚無資料</div> : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>帳戶</th><th>資產</th><th className="num">可用</th><th className="num">凍結/保證金</th><th className="num">價值 (USDT)</th></tr></thead>
              <tbody>{balances.data!.map((b) => (
                <tr key={`${b.market}-${b.asset}`}>
                  <td>{b.market === "spot" ? "現貨" : "合約"}</td><td>{b.asset}</td>
                  <td className="num">{fmtNum(b.free, 6)}</td><td className="num">{fmtNum(b.locked, 6)}</td><td className="num">{fmtNum(b.usdt_value)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
