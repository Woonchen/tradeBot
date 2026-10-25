import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type BotSettings, type StrategyConfig, type StrategyInfo } from "../api/client";
import { PortfolioBacktest } from "../components/PortfolioBacktest";
import { fmtTime, INTERVAL_SEC } from "../format";
import { TARGET_ZH, NO_ORDER, pickOrder, NO_PYRAMID, pickPyramid, orderSummary, type Draft, autoName, defaults } from "./strategies/shared";
import { Editor } from "./strategies/Editor";
import { Performance } from "./strategies/Performance";

export function Strategies() {
  const qc = useQueryClient();
  const catalog = useQuery({ queryKey: ["strategies", "catalog"], queryFn: () => api<StrategyInfo[]>("/api/strategies/catalog") });
  const configs = useQuery({ queryKey: ["strategies", "configs"], queryFn: () => api<StrategyConfig[]>("/api/strategies"), refetchInterval: 30_000 });
  const [editing, setEditing] = useState<Draft | null>(null);
  const bot = useQuery({ queryKey: ["bot"], queryFn: () => api<BotSettings>("/api/bot") });

  const toggle = useMutation({
    mutationFn: (c: StrategyConfig) => api(`/api/strategies/${c.id}`, {
      method: "PUT",
      body: JSON.stringify({
        name: c.name, strategy: c.strategy, symbol: c.symbol, market: c.market, interval: c.interval, params: c.params,
        enabled: !c.enabled, enter_on_enable: c.enter_on_enable, mode: c.mode, ...pickOrder(c), ...pickPyramid(c),
      }),
    }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["strategies"] }),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api(`/api/strategies/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["strategies"] }),
  });

  if (!catalog.data) return null;
  const labelOf = (key: string) => catalog.data.find((c) => c.key === key)?.label ?? key;
  const newDraft = (): Draft => {
    const first = catalog.data[0];
    return { name: autoName("BTCUSDT", first.label, "1h"), strategy: first.key, symbol: "BTCUSDT", market: "futures", interval: "1h", params: defaults(first), enabled: false, enter_on_enable: false, mode: "live", ...NO_ORDER, ...NO_PYRAMID };
  };

  return (
    <>
      <div className="toolbar" style={{ justifyContent: "space-between" }}>
        <h1 style={{ margin: 0 }}>策略</h1>
        {!editing && <button className="btn primary" onClick={() => setEditing(newDraft())}>＋ 新增策略</button>}
      </div>
      {editing && <Editor key={editing.id ?? "new"} catalog={catalog.data} initial={editing} onDone={() => setEditing(null)} />}
      <div className="card">
        {(configs.data ?? []).length === 0 ? (
          <div className="empty">還沒有策略。按「新增策略」，先回測，再啟用。</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>名稱</th><th>策略</th><th>交易對</th><th>週期</th><th>狀態</th><th>目前判斷</th><th>最後判斷</th><th /></tr></thead>
              <tbody>{configs.data!.map((c) => (
                <tr key={c.id}>
                  <td>
                    {c.name}{c.mode === "shadow" && <span className="badge" style={{ marginLeft: 6 }}>影子</span>}
                    <div className="muted" style={{ fontSize: 12 }}>{orderSummary(c, bot.data)}</div>
                  </td>
                  <td className="muted">{labelOf(c.strategy)}</td>
                  <td>{c.symbol} <span className="muted">{c.market === "futures" ? "合約" : "現貨"}</span></td>
                  <td>{c.interval}</td>
                  <td>
                    <span className="badge"><span className="dot" style={{ background: c.enabled ? "var(--up)" : "var(--text-3)" }} />{c.enabled ? "運行中" : "停用"}</span>
                    {c.last_error && <div className="error-text" title={c.last_error}>⚠ {c.last_error.slice(0, 40)}</div>}
                  </td>
                  <td>{c.last_target ? <span className={`badge ${c.last_target}`}>{TARGET_ZH[c.last_target]}</span> : <span className="muted">—</span>}</td>
                  {/* last_bar_time 是 K 棒的開盤時間；判斷發生在這根收盤之後，所以顯示收盤時間 */}
                  <td className="muted" title={c.last_bar_time ? `依 ${fmtTime(new Date(c.last_bar_time * 1000).toISOString())} 開盤的 ${c.interval} K 棒，收盤後判斷` : undefined}>
                    {c.last_bar_time ? `${fmtTime(new Date((c.last_bar_time + INTERVAL_SEC[c.interval]) * 1000).toISOString())} 收盤` : "—"}
                  </td>
                  <td style={{ display: "flex", gap: 6 }}>
                    <button className="btn small" disabled={toggle.isPending} onClick={() => toggle.mutate(c)}>{c.enabled ? "停用" : "啟用"}</button>
                    <button className="btn small" onClick={() => setEditing({ id: c.id, name: c.name, strategy: c.strategy, symbol: c.symbol, market: c.market, interval: c.interval, params: c.params, enabled: c.enabled, enter_on_enable: c.enter_on_enable, mode: c.mode, ...pickOrder(c), ...pickPyramid(c) })}>編輯／回測</button>
                    <button className="btn small danger" disabled={remove.isPending} onClick={() => confirm(`刪除策略「${c.name}」？既有持倉不會自動平倉。`) && remove.mutate(c.id)}>刪除</button>
                  </td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
        <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
          「目前判斷」是最後一根收盤 K 棒的計算結果，判斷改變時才會下單。策略下單同樣受 Bot 啟用開關、每日虧損上限、停損停利等設定控制。
          自己寫新策略：在 backend/app/strategies/ 新增 Python 檔（參考 ema_cross.py），重新部署後會自動出現在這裡。
        </p>
        {toggle.isError && <p className="error-text">{(toggle.error as Error).message}</p>}
      </div>
      <PortfolioBacktest configs={configs.data ?? []} />
      <Performance labelOf={labelOf} />
    </>
  );
}
