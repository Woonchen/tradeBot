import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, type MetricPoint, type SentimentReport } from "../api/client";
import { fmtNum, fmtSigned } from "../format";

const FG_ZH: Record<string, string> = {
  "Extreme Fear": "極度恐懼", Fear: "恐懼", Neutral: "中性", Greed: "貪婪", "Extreme Greed": "極度貪婪",
};

// 經驗上常用的判讀門檻，只是提示，不是交易訊號
function fundingHint(rate: number) {
  if (rate >= 0.03) return ["做多偏擁擠", "warn"];
  if (rate < 0) return ["做空偏擁擠", "warn"];
  return ["正常", ""];
}
function ratioHint(v: number) {
  if (v >= 2) return ["明顯偏多", "warn"];
  if (v <= 0.8) return ["明顯偏空", "warn"];
  return ["", ""];
}
function takerHint(v: number) {
  if (v >= 1.1) return ["買方較積極", ""];
  if (v <= 0.9) return ["賣方較積極", ""];
  return ["", ""];
}
function oiHint(p: MetricPoint) {
  if (p.week_ago === null) return ["", ""];
  const ch = p.value / p.week_ago - 1;
  if (ch >= 0.15) return ["槓桿資金增加", "warn"];
  if (ch <= -0.15) return ["去槓桿", ""];
  return ["", ""];
}

const Hint = ({ h }: { h: string[] }) => (h[0] ? <div className={`badge ${h[1]}`} style={{ marginTop: 2 }}>{h[0]}</div> : null);
const delta = (p: MetricPoint, digits = 2) => (p.prev === null ? "" : `較昨日 ${fmtSigned(p.value - p.prev, digits)}`);
const pctChange = (now: number, then: number | null) => (then ? `${fmtSigned((now / then - 1) * 100, 1)}%` : "—");

type Row = SentimentReport["symbols"][number];
const sub = (t: string) => <div className="muted" style={{ fontSize: 11 }}>{t}</div>;

const ROWS: [string, (x: Row) => ReactNode][] = [
  ["資金費率", (x) => x.funding ? <>{fmtNum(x.funding.rate, 4)}%{sub(`7 日年化 ${fmtNum(x.funding.avg7_annualized, 1)}%`)}<Hint h={fundingHint(x.funding.rate)} /></> : "—"],
  ["散戶多空比", (x) => x.long_short ? <>{fmtNum(x.long_short.value)}{sub(delta(x.long_short))}<Hint h={ratioHint(x.long_short.value)} /></> : "—"],
  ["大戶多空比", (x) => x.top_long_short ? <>{fmtNum(x.top_long_short.value)}{sub(delta(x.top_long_short))}<Hint h={ratioHint(x.top_long_short.value)} /></> : "—"],
  ["未平倉量", (x) => x.open_interest ? <>{fmtNum(x.open_interest.value / 1e9)}B{sub(`7 日 ${pctChange(x.open_interest.value, x.open_interest.week_ago)}`)}<Hint h={oiHint(x.open_interest)} /></> : "—"],
  ["主動買賣比", (x) => x.taker_ratio ? <>{fmtNum(x.taker_ratio.value)}<Hint h={takerHint(x.taker_ratio.value)} /></> : "—"],
];

/** 總覽頁：恐懼貪婪指數與各幣的合約情緒數據。 */
export function SentimentCard() {
  const q = useQuery({ queryKey: ["sentiment"], queryFn: () => api<SentimentReport>("/api/sentiment"), refetchInterval: 300_000 });
  const d = q.data;
  const fg = d?.fear_greed;
  return (
    <div className="card">
      <h2>市場情緒</h2>
      {q.isError && <p className="error-text">市場情緒載入失敗</p>}
      {fg ? (
        <div style={{ display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap", marginBottom: 10 }}>
          <span className="muted" style={{ fontSize: 12 }}>恐懼貪婪指數</span>
          <span style={{ fontSize: 26, fontWeight: 650 }}>{fg.value}</span>
          <span className="badge">{FG_ZH[fg.classification] ?? fg.classification}</span>
          <span className="muted" style={{ fontSize: 12 }}>
            昨天 {fg.prev ?? "—"} · 一週前 {fg.week_ago ?? "—"}
          </span>
        </div>
      ) : d && <p className="muted">恐懼貪婪指數尚未載入（每小時更新）</p>}
      {d && d.symbols.length > 0 && (
        // 列 = 指標、欄 = 幣：幣通常只有幾個，這樣窄的欄位也放得下
        <div className="table-wrap">
          <table>
            <thead><tr><th />{d.symbols.map((x) => <th key={x.symbol} className="num">{x.symbol.replace(/USDT$/, "")}</th>)}</tr></thead>
            <tbody>
              {ROWS.map(([label, render]) => (
                <tr key={label} style={{ verticalAlign: "top" }}>
                  <td>{label}</td>
                  {d.symbols.map((x) => <td key={x.symbol} className="num">{render(x)}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
        多空比 &gt; 1 代表做多的帳戶（大戶為持倉）比較多；主動買賣比 &gt; 1 代表吃單買方較積極。判讀標籤是常見的經驗門檻，僅供參考，
        研究顯示這些指標單獨使用的效果有限。合約數據 Binance 只保留 30 天，系統已自行記錄 {d?.recorded_days ?? 0} 天。
      </p>
    </div>
  );
}
