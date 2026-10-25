import { type BacktestResult, type BacktestStats } from "../../api/client";
import { BacktestChart } from "../../components/BacktestChart";
import { EquityChart } from "../../components/EquityChart";
import { fmtNum, fmtPct, fmtPrice, fmtSigned, fmtTime, pnlClass, sizeText } from "../../format";
import { fmtDate, EXIT_ZH, protectionText, TARGET_ZH } from "./shared";

/** 最近期間 vs 更早期間：兩段都不賠，才比較能相信策略不是剛好配合了某段行情。 */
function PeriodCompare({ recent, earlier }: { recent: BacktestStats; earlier: BacktestStats }) {
  const rows: [string, BacktestStats][] = [["更早期間", earlier], ["最近期間", recent]];
  const pf = (x: BacktestStats) => (x.profit_factor === null ? "—" : fmtNum(x.profit_factor));
  const verdict = earlier.total_return >= 0 && recent.total_return >= 0
    ? <span className="up">兩段都沒有虧損 ✓</span>
    : <span className="down">至少一段虧損：策略可能只適合特定行情，建議不要直接用真錢</span>;
  return (
    <div className="card">
      <h2>期間驗證</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>期間</th><th>日期</th><th className="num">策略報酬</th><th className="num">獲利因子</th><th className="num">勝率</th><th className="num">交易</th><th className="num">最大回撤</th><th className="num">同期幣價</th></tr></thead>
          <tbody>{rows.map(([label, x]) => (
            <tr key={label}>
              <td>{label}</td>
              <td className="muted">{fmtDate(x.from)} ～ {fmtDate(x.to)}</td>
              <td className={`num ${pnlClass(x.total_return)}`}>{fmtSigned(x.total_return * 100)}%</td>
              <td className="num">{pf(x)}</td>
              <td className="num">{fmtPct(x.win_rate)}</td>
              <td className="num">{x.trades}</td>
              <td className="num">{fmtPct(x.max_drawdown)}</td>
              <td className={`num ${pnlClass(x.buy_hold_return)}`}>{fmtSigned(x.buy_hold_return * 100)}%</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      <p style={{ margin: "10px 0 0", fontSize: 13 }}>{verdict}</p>
    </div>
  );
}

export function BacktestView({ result, compareRequested }: { result: BacktestResult; compareRequested: boolean }) {
  const s = result.stats;
  const z = result.sizing;
  const tile = (label: string, value: string, cls = "", sub?: string) => (
    <div className="card tile"><div className="label">{label}</div><div className={`value ${cls}`}>{value}</div>{sub && <div className="sub">{sub}</div>}</div>
  );
  const equity = result.equity.map((p) => ({ ts: new Date(p.time * 1000).toISOString(), total_usdt: p.value, spot_usdt: 0, futures_usdt: 0 }));
  const pyramided = result.trades.some((t) => t.adds); // 有加碼才顯示「加碼」欄；進場價是平均成本
  return (
    <div className="grid" style={{ marginTop: 16 }}>
      <p className="muted" style={{ margin: 0, fontSize: 12 }}>
        {fmtDate(s.from)} ～ {fmtDate(s.to)}，{s.bars} 根 K 棒；
        {sizeText(z.size_mode, z.size_value)}
        {z.market === "futures" && ` × ${z.leverage} 倍槓桿`}{protectionText(z.stop_loss_pct, z.take_profit_pct)}，起始 {fmtNum(z.start_equity)} USDT。
        {result.warmup > 0 && ` 另外多抓前面 ${result.warmup} 根讓指標穩定，不計入結果；起點當下若已在部位內，要等判斷改變才進場（跟實際啟用一樣）。`}
        {z.market === "futures" && (s.funding_included
          ? " 已照實際持倉扣收 Binance 歷史資金費率（每 8 小時結算）。"
          : " ⚠ 沒有扣資金費率（拿不到歷史資料），合約長時間持倉的結果會偏樂觀。")}
      </p>
      {result.earlier && <PeriodCompare recent={s} earlier={result.earlier} />}
      {compareRequested && !result.earlier && <p className="muted" style={{ margin: 0 }}>歷史資料不夠長，無法驗證更早的同長度期間。</p>}
      <div className="tiles" style={{ marginBottom: 0 }}>
        {tile("策略報酬", `${fmtSigned(s.total_return * 100)}%`, pnlClass(s.total_return), `${fmtNum(s.start_equity)} → ${fmtNum(s.final_equity)} USDT`)}
        {tile("最大回撤", fmtPct(s.max_drawdown))}
        {tile("交易次數", String(s.trades), "", `勝率 ${fmtPct(s.win_rate)}`)}
        {tile("獲利因子", s.profit_factor === null ? "—" : fmtNum(s.profit_factor), "", "總獲利 ÷ 總虧損，>1 才有賺")}
        {tile("手續費", fmtNum(s.fees), "", s.funding_included
          ? `資金費 ${s.funding >= 0 ? "付出" : "收到"} ${fmtNum(Math.abs(s.funding))} · 同期幣價 ${fmtSigned(s.buy_hold_return * 100)}%`
          : `同期幣價 ${fmtSigned(s.buy_hold_return * 100)}%`)}
      </div>
      <div className="card">
        <h2>{result.symbol} 回測（{s.bars} 根 K 棒）</h2>
        <BacktestChart candles={result.candles} trades={result.trades} plots={result.plots} />
        <div className="legend-row">
          <span><span className="up">▲</span> 開多／平空</span><span><span className="down">▼</span> 平多／開空，平倉標註淨損益</span>
          <span className="muted">訊號在 K 棒收盤產生，下一根開盤成交</span>
        </div>
      </div>
      <div className="card"><h2>回測權益曲線（USDT）</h2><EquityChart points={equity} short /></div>
      <div className="card">
        <h2>交易明細</h2>
        {result.trades.length === 0 ? <div className="empty">這段期間沒有交易</div> : (
          <div className="table-wrap" style={{ maxHeight: 360, overflowY: "auto" }}>
            <table>
              <thead><tr><th>方向</th><th>進場</th><th className="num">{pyramided ? "平均進場價" : "進場價"}</th><th>出場</th><th className="num">出場價</th><th className="num">損益</th><th className="num">手續費</th>{s.funding_included && <th className="num">資金費</th>}<th className="num">淨損益</th>{pyramided && <th className="num">加碼</th>}<th>出場原因</th></tr></thead>
              <tbody>{[...result.trades].reverse().map((t, i) => (
                <tr key={i}>
                  <td><span className={`badge ${t.side}`}>{TARGET_ZH[t.side]}</span></td>
                  <td className="muted">{fmtTime(new Date(t.entry_time * 1000).toISOString())}</td><td className="num">{fmtPrice(t.entry)}</td>
                  <td className="muted">{fmtTime(new Date(t.exit_time * 1000).toISOString())}</td><td className="num">{fmtPrice(t.exit)}</td>
                  <td className={`num ${pnlClass(t.pnl)}`}>{fmtSigned(t.pnl)}</td><td className="num">{fmtNum(t.fees)}</td>
                  {s.funding_included && <td className="num" title="正 = 付出、負 = 收到">{fmtSigned(t.funding)}</td>}
                  <td className={`num ${pnlClass(t.net)}`}>{fmtSigned(t.net)}</td>
                  {pyramided && <td className="num">{t.adds ? `${t.adds} 次` : <span className="muted">—</span>}</td>}
                  <td className={t.exit_reason === "stop" ? "down" : t.exit_reason === "take_profit" ? "up" : "muted"}>{EXIT_ZH[t.exit_reason] ?? "—"}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
