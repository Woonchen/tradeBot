import { useQuery } from "@tanstack/react-query";
import { Fragment } from "react";
import { Link } from "react-router-dom";
import { api, type ConditionCheck, type PerformanceReport, type StrategyCheck, type StrategyConfig } from "../api/client";
import { fmtNum, fmtPrice, fmtSigned, fmtTime, fmtUntil, INTERVAL_SEC, pnlClass } from "../format";

const JUDGE: Record<string, [string, string]> = { long: ["▲ 多", "long"], short: ["▼ 空", "short"], flat: ["空手", ""] };

/** 下一次判斷：最後處理的 K 棒再過一根收盤後幾秒（跟後端 strategy_runner 的排程一致）。 */
const nextEval = (c: StrategyConfig) =>
  c.last_bar_time === null ? Date.now() : (c.last_bar_time + 2 * INTERVAL_SEC[c.interval] + 3) * 1000;

const CMP: Record<string, string> = { ">": ">", "<": "<", ">=": "≥", "<=": "≤", crosses_above: "上穿", crosses_below: "下穿" };
const GROUP: Record<string, string> = { long_entry: "做多進場", long_exit: "多單出場", short_entry: "做空進場", short_exit: "空單出場" };

/** 一個條件：✓／✗、兩邊目前的數值；價格類沒成立時寫出還差幾 %。 */
function Cond({ c }: { c: ConditionCheck }) {
  const f = (v: number | null) => (v === null ? "無資料" : c.price_scale ? fmtPrice(v) : fmtNum(v, 2));
  const side = (name: string, v: number | null) => (/^-?[\d.]+$/.test(name) ? name : `${name} ${f(v)}`);
  let gap = "";
  if (!c.ok && c.price_scale && c.left_value && c.right_value !== null && [">", ">=", "<", "<="].includes(c.cmp)) {
    const up = c.cmp.startsWith(">");
    const pct = (c.right_value / c.left_value - 1) * 100 * (up ? 1 : -1);
    if (pct > 0) gap = `（還差 ${pct.toFixed(1)}%）`;
  }
  return (
    <div className={`cond ${c.ok ? "ok" : "no"}`}>
      <span aria-label={c.ok ? "成立" : "不成立"}>{c.ok ? "✓" : "✗"}</span> {side(c.left, c.left_value)} {CMP[c.cmp] ?? c.cmp} {side(c.right, c.right_value)}
      {gap && <span className="muted">{gap}</span>}
    </div>
  );
}

/** 持倉中看出場條件，沒持倉看進場條件。 */
function Checks({ check, holding }: { check?: StrategyCheck; holding: "long" | "short" | null }) {
  if (!check) return <span className="muted" style={{ fontSize: 12 }}>條件：等第一次判斷</span>;
  const keys = holding ? [`${holding}_exit`] : ["long_entry", "short_entry"];
  const groups = keys.map((k) => [k, check.groups[k as keyof StrategyCheck["groups"]]] as const).filter(([, g]) => g);
  if (groups.length === 0) return <span className="muted">—</span>;
  return (
    <div className="cond-groups">{groups.map(([k, g]) => (
      <div key={k} className="cond-group">
        <span className="muted">{GROUP[k]}（{g!.logic === "and" ? "全部成立" : "任一成立"}）：</span>
        {g!.conditions.map((c, i) => <Cond key={i} c={c} />)}
      </div>
    ))}</div>
  );
}

/** 總覽頁：啟用中的策略各自的判斷、持倉、各條件目前是否成立、下次判斷時間。 */
export function StrategyStatus() {
  const perf = useQuery({
    queryKey: ["strategies", "performance"],
    queryFn: () => api<PerformanceReport>("/api/strategies/performance"),
    refetchInterval: 30_000,
  });
  const checks = useQuery({
    queryKey: ["strategies", "checks"],
    queryFn: () => api<Record<string, StrategyCheck>>("/api/strategies/checks"),
    refetchInterval: 60_000,
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
                <Fragment key={c.id}>
                <tr className="has-detail">
                  <td>{c.name}{c.mode === "shadow" && <span className="badge" style={{ marginLeft: 6 }}>影子</span>}<div className="muted" style={{ fontSize: 12 }}>{c.symbol} {c.market === "futures" ? "合約" : "現貨"} · {c.interval}</div></td>
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
                <tr className="detail-row">
                  <td colSpan={5}><Checks check={checks.data?.[String(c.id)]} holding={holding ? (x.open[0].side as "long" | "short") : null} /></td>
                </tr>
                </Fragment>
              );
            })}</tbody>
          </table>
        </div>
      )}
      {perf.isError && <p className="error-text">策略狀態載入失敗</p>}
    </div>
  );
}
