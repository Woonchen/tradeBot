import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type ActivityLog, type EquityPoint, type Summary } from "../api/client";
import { ActivityTimeline } from "../components/ActivityTimeline";
import { EquityChart } from "../components/EquityChart";
import { fmtNum, fmtPct, fmtSigned, pnlClass } from "../format";
import { BotControls } from "./BotControls";
import { PositionsTable } from "./Positions";

function Tile({ label, value, sub, cls }: { label: string; value: string; sub?: string; cls?: string }) {
  return (
    <div className="card tile">
      <div className="label">{label}</div>
      <div className={`value ${cls ?? ""}`}>{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

export function Dashboard() {
  const summary = useQuery({ queryKey: ["summary"], queryFn: () => api<Summary>("/api/summary"), refetchInterval: 30_000 });
  const equity = useQuery({ queryKey: ["equity", 7], queryFn: () => api<EquityPoint[]>("/api/equity?days=7") });
  const logs = useQuery({ queryKey: ["activity", "recent"], queryFn: () => api<ActivityLog[]>("/api/activity?limit=10") });
  const s = summary.data;
  const totalReturn = s?.equity && s.start_equity ? s.equity / s.start_equity - 1 : null;

  return (
    <>
      <div className="toolbar" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>總覽</h1>
        <BotControls />
      </div>
      <div className="tiles">
        <Tile label="總權益 (USDT)" value={fmtNum(s?.equity)} sub={`今日 ${fmtSigned(s?.equity_change_today)}`} />
        <Tile label="今日已實現（扣手續費）" value={fmtSigned(s?.realized_today)} cls={pnlClass(s?.realized_today)} />
        <Tile label="未實現損益" value={fmtSigned(s?.unrealized)} cls={pnlClass(s?.unrealized)} sub={`${s?.open_positions ?? 0} 個持倉`} />
        <Tile label="累計已實現" value={fmtSigned(s?.realized_total)} cls={pnlClass(s?.realized_total)} sub={`總報酬 ${totalReturn === null ? "—" : fmtSigned(totalReturn * 100) + "%"}`} />
        <Tile label="勝率" value={fmtPct(s?.win_rate)} sub={`${s?.closed_trades ?? 0} 筆平倉 · 最大回撤 ${fmtPct(s?.max_drawdown)}`} />
      </div>
      <div className="two-col">
        <div className="grid">
          <div className="card">
            <h2>總權益（近 7 天，USDT）</h2>
            <EquityChart points={equity.data ?? []} short />
          </div>
          <div className="card">
            <h2>目前持倉</h2>
            <PositionsTable compact />
          </div>
        </div>
        <div className="card">
          <div className="toolbar" style={{ justifyContent: "space-between" }}>
            <h2 style={{ margin: 0 }}>最近事件</h2>
            <Link to="/activity">全部 →</Link>
          </div>
          <ActivityTimeline logs={logs.data ?? []} />
        </div>
      </div>
    </>
  );
}
