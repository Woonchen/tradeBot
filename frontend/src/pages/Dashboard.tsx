import { useQuery } from "@tanstack/react-query";
import { lazy, Suspense, useMemo } from "react";
import { Link } from "react-router-dom";
import { api, type ActivityLog, type EquityPoint, type Position, type StrategyConfig, type Summary } from "../api/client";
import { useLivePrices } from "../api/liveSocket";
import { ActivityTimeline } from "../components/ActivityTimeline";
import { EquityChart } from "../components/EquityChart";
import { SentimentCard } from "../components/SentimentCard";
import { toTicker } from "../components/SymbolSelect";
import type { ArenaItem } from "../components/TradeArena3D";
import { StrategyStatus } from "../components/StrategyStatus";
import { useGameMode } from "../gameMode";
import { fmtNum, fmtPct, fmtSigned, livePnl, pnlClass } from "../format";
import { BotControls } from "./BotControls";
import { PositionsTable } from "./Positions";

// three.js 很大：只有開了遊戲模式才下載
const TradeArena3D = lazy(() => import("../components/TradeArena3D").then((m) => ({ default: m.TradeArena3D })));

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
  const positions = useQuery({ queryKey: ["positions", "open"], queryFn: () => api<Position[]>("/api/positions") });
  const prices = useLivePrices();
  const game = useGameMode();
  // 遊戲模式：剛平倉的部位要知道平倉原因（停損暈倒、停利放煙火）；跟持倉一樣在成交事件時重新抓
  const recentClosed = useQuery({
    queryKey: ["positions", "closed", "recent"], queryFn: () => api<Position[]>("/api/positions?status=closed&limit=10"), enabled: game,
  });
  const exits = useMemo(() => Object.fromEntries((recentClosed.data ?? [])
    .flatMap((p) => p.exit_reason ? [[`${p.market}:${p.symbol}:${p.id}`, p.exit_reason]] : [])), [recentClosed.data]);
  const strategies = useQuery({
    queryKey: ["strategies", "configs"], queryFn: () => api<StrategyConfig[]>("/api/strategies"), enabled: game,
  });
  const arena = useMemo(() => {
    const held: ArenaItem[] = (positions.data ?? []).map((p) => {
      const key = `${p.market}:${p.symbol}`;
      return { kind: "position", key: `${key}:${p.id}`, position: p, ...livePnl(p, prices[key]) };
    });
    // 啟用中的策略、但那個幣目前沒有持倉 → 待機的工作站（同一個幣多個策略合併成一站）
    const busy = new Set((positions.data ?? []).map((p) => `${p.market}:${toTicker(p.symbol)}`));
    const idle = new Map<string, Extract<ArenaItem, { kind: "idle" }>>();
    for (const c of strategies.data ?? []) {
      const key = `${c.market}:${toTicker(c.symbol)}`;
      if (!c.enabled || busy.has(key)) continue;
      const entry = idle.get(key) ?? { kind: "idle", key: `idle:${key}`, symbol: toTicker(c.symbol), market: c.market, strategies: [] };
      entry.strategies.push({ name: c.name, interval: c.interval, shadow: c.mode === "shadow" });
      idle.set(key, entry);
    }
    return [...held, ...idle.values()];
  }, [positions.data, prices, strategies.data]);
  const unrealized = positions.data
    ? positions.data.reduce((sum, p) => sum + livePnl(p, prices[`${p.market}:${p.symbol}`]).pnl, 0)
    : summary.data?.unrealized;
  const s = summary.data;
  const totalReturn = s?.equity && s.start_equity ? s.equity / s.start_equity - 1 : null;

  return (
    <>
      <div className="toolbar" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>總覽</h1>
        <BotControls />
      </div>
      {game && (
        <Suspense fallback={<div className="arena arena-loading">載入 3D 場景中…</div>}>
          <TradeArena3D items={arena} exits={exits} />
        </Suspense>
      )}
      <div className="tiles">
        <Tile label="總權益 (USDT)" value={fmtNum(s?.equity)} sub={`今日 ${fmtSigned(s?.equity_change_today)}`} />
        <Tile label="今日已實現（扣手續費）" value={fmtSigned(s?.realized_today)} cls={pnlClass(s?.realized_today)} />
        <Tile label="未實現損益（即時）" value={fmtSigned(unrealized)} cls={pnlClass(unrealized)} sub={`${positions.data?.length ?? s?.open_positions ?? 0} 個持倉`} />
        <Tile label="累計已實現" value={fmtSigned(s?.realized_total)} cls={pnlClass(s?.realized_total)} sub={`總報酬 ${totalReturn === null ? "—" : fmtSigned(totalReturn * 100) + "%"}`} />
        <Tile label="勝率" value={fmtPct(s?.win_rate)} sub={`${s?.closed_trades ?? 0} 筆完整交易 · 最大回撤 ${fmtPct(s?.max_drawdown)}`} />
      </div>
      <div className="two-col">
        <div className="grid">
          <div className="card">
            <h2>總權益（近 7 天，USDT）</h2>
            <EquityChart points={equity.data ?? []} short />
          </div>
          <StrategyStatus />
          <div className="card">
            <h2>目前持倉</h2>
            <PositionsTable compact />
          </div>
        </div>
        <div className="grid" style={{ alignContent: "start" }}>
        <SentimentCard />
        <div className="card">
          <div className="toolbar" style={{ justifyContent: "space-between" }}>
            <h2 style={{ margin: 0 }}>最近事件</h2>
            <Link to="/activity">全部 →</Link>
          </div>
          <ActivityTimeline logs={logs.data ?? []} />
        </div>
        </div>
      </div>
    </>
  );
}
