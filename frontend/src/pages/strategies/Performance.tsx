import { useMutation, useQuery } from "@tanstack/react-query";
import { api, type BacktestResult, type LivePerf, type PerformanceReport, type StrategyConfig } from "../../api/client";
import { fmtNum, fmtPct, fmtSigned, fmtTime, INTERVAL_SEC, pnlClass } from "../../format";
import { fmtDate, TARGET_ZH, orderBody, pyramidBody } from "./shared";

/** 策略最近一次啟用以來的 K 棒數，用來回測同一段期間。 */
const since = (c: StrategyConfig) => c.enabled_at ?? c.created_at;
const barsSince = (c: StrategyConfig) => Math.floor((Date.now() - Date.parse(since(c))) / 1000 / INTERVAL_SEC[c.interval]);

function PerfCells({ p }: { p: LivePerf }) {
  return (
    <>
      <td className="num">{p.trades}</td>
      <td className="num">{fmtPct(p.win_rate)}</td>
      <td className="num">{p.profit_factor === null ? "—" : fmtNum(p.profit_factor)}</td>
      <td className={`num ${pnlClass(p.net)}`}>{fmtSigned(p.net)}</td>
      <td className="num">{fmtNum(p.fees)}</td>
      <td>{p.open.length === 0 ? <span className="muted">—</span> : p.open.map((o) => (
        <div key={o.symbol}><span className={`badge ${o.side}`}>{TARGET_ZH[o.side]}</span> <span className={pnlClass(o.unrealized)}>{fmtSigned(o.unrealized)}</span></div>
      ))}</td>
    </>
  );
}

/** 每個策略的實際成交績效，並可用同一段期間回測對照。 */
export function Performance({ labelOf }: { labelOf: (key: string) => string }) {
  const perf = useQuery({ queryKey: ["strategies", "performance"], queryFn: () => api<PerformanceReport>("/api/strategies/performance"), refetchInterval: 30_000 });
  const cmp = useMutation({
    mutationFn: (c: StrategyConfig) => api<BacktestResult>("/api/strategies/backtest", {
      method: "POST",
      body: JSON.stringify({
        strategy: c.strategy, symbol: c.symbol, market: c.market, interval: c.interval, params: c.params,
        bars: Math.min(barsSince(c), 20000), enter_on_enable: c.enter_on_enable, ...orderBody(c), ...pyramidBody(c),
      }),
    }),
  });
  if (!perf.data) return null;
  const rows = perf.data.strategies.filter((x) => x.config.enabled || x.trades > 0 || x.open.length > 0);
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h2>實際績效</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>策略</th><th>啟用時間</th><th className="num">完整交易</th><th className="num">勝率</th><th className="num">獲利因子</th><th className="num">淨損益</th><th className="num">手續費</th><th>持倉中（未實現）</th><th /></tr></thead>
          <tbody>
            {rows.map((x) => {
              const c = x.config;
              const bars = barsSince(c);
              const shown = cmp.variables?.id === c.id;
              const bt = shown ? cmp.data?.stats : undefined;
              return [
                <tr key={c.id}>
                  <td>{c.name}{c.mode === "shadow" && <span className="badge" style={{ marginLeft: 6 }}>影子（虛擬）</span>}<div className="muted" style={{ fontSize: 12 }}>{labelOf(c.strategy)} · {c.symbol} {c.interval}{c.enabled ? "" : " · 已停用"}</div></td>
                  <td className="muted">{fmtTime(since(c))}</td>
                  <PerfCells p={x} />
                  <td>
                    <button className="btn small" disabled={bars < 50 || cmp.isPending} title={bars < 50 ? "啟用未滿 50 根 K 棒，還不能比較" : ""}
                      onClick={() => cmp.mutate(c)}>{cmp.isPending && shown ? "回測中…" : "回測同期間"}</button>
                  </td>
                </tr>,
                shown && (bt || cmp.isError) ? (
                  <tr key={`${c.id}-bt`}>
                    <td colSpan={9} style={{ background: "var(--surface-2)" }}>
                      {cmp.isError ? <span className="error-text">{(cmp.error as Error).message}</span> : bt && (
                        <span style={{ fontSize: 13 }}>
                          同期間回測（{fmtDate(bt.from)} ～ {fmtDate(bt.to)}）：完整交易 <b>{bt.trades}</b>、勝率 <b>{fmtPct(bt.win_rate)}</b>、
                          獲利因子 <b>{bt.profit_factor === null ? "—" : fmtNum(bt.profit_factor)}</b>、報酬 <b className={pnlClass(bt.total_return)}>{fmtSigned(bt.total_return * 100)}%</b>
                          <span className="muted">　交易數或勝率差很多時，可能是全域停損、下單失敗或手動操作造成的；金額依回測條件計算，跟實際金額不能直接比</span>
                        </span>
                      )}
                    </td>
                  </tr>
                ) : null,
              ];
            })}
            {perf.data.others.map((o) => (
              <tr key={o.label}>
                <td>{o.label}</td><td className="muted">—</td><PerfCells p={o} /><td />
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
        部位由哪個策略開倉，整筆交易（含被停損停利、手動或在交易所平倉）就算給它；開倉到全部平掉算一筆完整交易，淨損益已扣開平倉手續費。
        只列出啟用中或有成交紀錄的策略。
      </p>
    </div>
  );
}
