import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { api, qs, type Market, type Position, type Trade } from "../api/client";
import { CandleChart, INTERVAL_SECONDS } from "../components/CandleChart";
import { SymbolSelect, toTicker } from "../components/SymbolSelect";
import { fmtPrice, fmtQty, fmtSigned, fmtTime, INTENT_ZH, pnlClass } from "../format";

function TradesTable({ trades }: { trades: Trade[] }) {
  if (trades.length === 0) return <div className="empty">沒有成交</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th>時間</th><th>動作</th><th className="num">價格</th><th className="num">數量</th><th className="num">手續費</th><th className="num">已實現損益</th><th>訊號</th></tr>
        </thead>
        <tbody>
          {trades.map((t) => (
            <tr key={t.id}>
              <td className="muted">{fmtTime(t.executed_at)}</td>
              <td className={t.side === "buy" ? "up" : "down"}>{t.side === "buy" ? "▲" : "▼"} {INTENT_ZH[t.intent]}</td>
              <td className="num">{fmtPrice(t.price)}</td>
              <td className="num">{fmtQty(t.qty)}</td>
              <td className="num">{fmtSigned(-t.fee_usdt, 4)}</td>
              <td className={`num ${pnlClass(t.realized_pnl)}`}>{t.intent.startsWith("close") ? fmtSigned(t.realized_pnl, 4) : "—"}</td>
              <td className="muted">{t.signal_id ? `#${t.signal_id}` : "交易所"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ChartPage() {
  const positions = useQuery({ queryKey: ["positions", "open"], queryFn: () => api<Position[]>("/api/positions") });
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [market, setMarket] = useState<Market>("futures");
  const [interval, setInterval] = useState("15m");
  const [picked, setPicked] = useState<Trade[] | null>(null);

  // 預設顯示第一個持倉
  const [initialised, setInitialised] = useState(false);
  useEffect(() => {
    if (initialised || !positions.data) return;
    const p = positions.data[0];
    if (p) {
      setSymbol(toTicker(p.symbol));
      setMarket(p.market);
    }
    setInitialised(true);
  }, [positions.data, initialised]);

  const klines = useQuery({
    queryKey: ["klines", symbol, market, interval],
    queryFn: () => api<{ symbol: string }>(`/api/klines?${qs({ symbol, market, interval, limit: 500 })}`),
    retry: false,
  });
  const resolved = klines.data?.symbol;
  const trades = useQuery({
    queryKey: ["trades", resolved, market],
    queryFn: () => api<Trade[]>(`/api/trades?${qs({ symbol: resolved, market, limit: 2000 })}`),
    enabled: !!resolved,
  });

  const onSelectBar = useCallback((t: Trade[]) => setPicked(t), []);

  return (
    <>
      <h1>K 線與買賣點</h1>
      <div className="toolbar">
        <SymbolSelect value={symbol} onChange={(s) => { setSymbol(s); setPicked(null); }} />
        <div className="seg">
          {(["spot", "futures"] as Market[]).map((m) => (
            <button key={m} className={market === m ? "on" : ""} onClick={() => { setMarket(m); setPicked(null); }}>
              {m === "spot" ? "現貨" : "合約"}
            </button>
          ))}
        </div>
        <div className="seg">
          {Object.keys(INTERVAL_SECONDS).map((i) => (
            <button key={i} className={interval === i ? "on" : ""} onClick={() => { setInterval(i); setPicked(null); }}>{i}</button>
          ))}
        </div>
      </div>
      <div className="card">
        <CandleChart symbol={symbol} market={market} interval={interval} onSelectBar={onSelectBar} />
      </div>
      {picked && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="toolbar" style={{ justifyContent: "space-between" }}>
            <h2 style={{ margin: 0 }}>所選 K 棒的成交</h2>
            <button className="btn small" onClick={() => setPicked(null)}>關閉</button>
          </div>
          <TradesTable trades={picked} />
        </div>
      )}
      <div className="card" style={{ marginTop: 16 }}>
        <h2>{resolved ?? symbol} 成交紀錄</h2>
        <TradesTable trades={trades.data ?? []} />
      </div>
    </>
  );
}
