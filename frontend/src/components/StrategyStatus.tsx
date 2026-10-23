import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type PerformanceReport, type StrategyConfig } from "../api/client";
import { fmtSigned, fmtTime, fmtUntil, INTERVAL_SEC, pnlClass } from "../format";

const JUDGE: Record<string, [string, string]> = { long: ["▲ 多", "long"], short: ["▼ 空", "short"], flat: ["空手", ""] };

/** 下一次判斷：最後處理的 K 棒再過一根收盤後幾秒（跟後端 strategy_runner 的排程一致）。 */
const nextEval = (c: StrategyConfig) =>
  c.last_bar_time === null ? Date.now() : (c.last_bar_time + 2 * INTERVAL_SEC[c.interval] + 3) * 1000;

/** 總覽頁：啟用中的策略各自的判斷、持倉、下次判斷時間。 */
export function StrategyStatus() {
  const perf = useQuery({
    queryKey: ["strategies", "performance"],
    queryFn: () => api<PerformanceReport>("/api/strategies/performance"),
    refetchInterval: 30_000,
  });
  const rows = (perf.data?.strategies ?? []).filter((x) => x.config.enabled);
  return (
    <div className="card">
      <div className="toolbar" style={{ justifyContent: "space-between" }}>
        <h2 style={{ margin: 0 }}>策略狀態</h2>
        <Link to="/strategies">管理 →</Link>
      </div>
      {rows.length === 0 ? <div className="empty">沒有啟用中的策略</div> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>策略</th><th>目前判斷</th><th>持倉（未實現）</th><th className="num">已實現淨損益</th><th>下次判斷</th></tr></thead>
            <tbody>{rows.map((x) => {
              const c = x.config;
              const [label, cls] = JUDGE[c.last_target ?? ""] ?? ["—", ""];
              const holding = x.open.length > 0;
              // 判斷是多/空但沒有持倉：啟用時只記下判斷、等判斷改變才進場
              const waiting = !holding && (c.last_target === "long" || c.last_target === "short");
              const next = nextEval(c);
              return (
                <tr key={c.id}>
                  <td>{c.name}<div className="muted" style={{ fontSize: 12 }}>{c.symbol} {c.market === "futures" ? "合約" : "現貨"} · {c.interval}</div></td>
                  <td>
                    {c.last_target ? <span className={`badge ${cls}`}>{label}</span> : <span className="muted">—</span>}
                    {waiting && <div className="muted" style={{ fontSize: 12 }}>等判斷改變才進場</div>}
                  </td>
                  <td>{holding ? x.open.map((o) => (
                    <div key={o.symbol}>
                      <span className={`badge ${o.side}`}>{o.side === "long" ? "▲ 多" : "▼ 空"}</span>{" "}
                      <span className={pnlClass(o.unrealized)}>{fmtSigned(o.unrealized)}</span>
                    </div>
                  )) : <span className="muted">—</span>}</td>
                  <td className={`num ${pnlClass(x.net)}`}>{x.trades ? fmtSigned(x.net) : <span className="muted">—</span>}</td>
                  <td>{fmtUntil(next)}<div className="muted" style={{ fontSize: 12 }}>{fmtTime(new Date(next).toISOString())}</div></td>
                </tr>
              );
            })}</tbody>
          </table>
        </div>
      )}
      {perf.isError && <p className="error-text">策略狀態載入失敗</p>}
    </div>
  );
}
