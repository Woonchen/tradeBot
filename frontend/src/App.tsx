import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { getToken, setToken } from "./api/client";
import { useLiveEvents } from "./api/useLiveEvents";
import { Activity } from "./pages/Activity";
import { ChartPage } from "./pages/ChartPage";
import { Dashboard } from "./pages/Dashboard";
import { Login } from "./pages/Login";
import { PositionsPage } from "./pages/Positions";
import { Settings } from "./pages/Settings";
import { Wallet } from "./pages/Wallet";

const NAV = [
  ["/", "總覽"], ["/chart", "K 線圖"], ["/positions", "倉位"], ["/wallet", "錢包"], ["/activity", "操作軌跡"], ["/settings", "設定"],
];

function Shell() {
  const connected = useLiveEvents();
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">📈 TradeBot</div>
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
