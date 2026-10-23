import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, getToken, isLive, modeLabel, setToken, type BotSettings } from "./api/client";
import { useLiveEvents } from "./api/useLiveEvents";
import { Activity } from "./pages/Activity";
import { ChartPage } from "./pages/ChartPage";
import { Dashboard } from "./pages/Dashboard";
import { Login } from "./pages/Login";
import { PositionsPage } from "./pages/Positions";
import { Settings } from "./pages/Settings";
import { Strategies } from "./pages/Strategies";
import { Wallet } from "./pages/Wallet";

const NAV = [
  ["/", "總覽"], ["/chart", "K 線圖"], ["/strategies", "策略"], ["/positions", "倉位"], ["/wallet", "錢包"],
  ["/activity", "操作軌跡"], ["/settings", "設定"],
];

/** 側邊欄常駐顯示目前的帳戶；正式帳戶用醒目的紅色，避免忘記正在用真錢。 */
function ModeBadge() {
  const bot = useQuery({ queryKey: ["bot"], queryFn: () => api<BotSettings>("/api/bot") });
  if (!bot.data) return null;
  const live = isLive(bot.data);
  return <div className={`mode-badge${live ? " live" : ""}`} title="在後端 .env 設定 EXECUTION_MODE / TESTNET">{live && "⚠ "}{modeLabel(bot.data)}</div>;
}

function Shell() {
  const connected = useLiveEvents();
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">📈 TradeBot</div>
        <ModeBadge />
        <nav className="nav">
          {NAV.map(([to, label]) => <NavLink key={to} to={to} end={to === "/"}>{label}</NavLink>)}
        </nav>
        <div className="sidebar-foot">
          <span><span className="dot" style={{ background: connected ? "var(--up)" : "var(--down)" }} /> {connected ? "即時連線中" : "連線中斷，重連中…"}</span>
          <button className="btn small" onClick={() => setToken(null)}>登出</button>
        </div>
      </aside>
      <main>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/chart" element={<ChartPage />} />
          <Route path="/strategies" element={<Strategies />} />
          <Route path="/positions" element={<PositionsPage />} />
          <Route path="/wallet" element={<Wallet />} />
          <Route path="/activity" element={<Activity />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" />} />
        </Routes>
      </main>
    </div>
  );
}

export function App() {
  const [token, setTok] = useState(getToken);
  useEffect(() => {
    const onAuth = () => setTok(getToken());
    window.addEventListener("tradebot-auth", onAuth);
    return () => window.removeEventListener("tradebot-auth", onAuth);
  }, []);
  return token ? <Shell /> : <Login />;
}
