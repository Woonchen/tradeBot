import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, qs, type ActivityLog } from "../api/client";
import { ActivityTimeline } from "../components/ActivityTimeline";

const LEVELS = [["", "全部"], ["info", "一般"], ["warn", "警告"], ["error", "錯誤"]];
const CATEGORIES = [["", "全部"], ["signal", "訊號"], ["order", "下單"], ["trade", "成交"], ["risk", "風控"], ["system", "系統"]];

export function Activity() {
  const [level, setLevel] = useState("");
  const [category, setCategory] = useState("");
  const [limit, setLimit] = useState(200);
  const { data = [], isFetching } = useQuery({
    queryKey: ["activity", level, category, limit],
    queryFn: () => api<ActivityLog[]>(`/api/activity?${qs({ level, category, limit })}`),
  });
  return (
    <>
      <h1>操作軌跡</h1>
      <div className="toolbar">
        <div className="seg">{LEVELS.map(([v, l]) => <button key={v} className={level === v ? "on" : ""} onClick={() => setLevel(v)}>{l}</button>)}</div>
        <div className="seg">{CATEGORIES.map(([v, l]) => <button key={v} className={category === v ? "on" : ""} onClick={() => setCategory(v)}>{l}</button>)}</div>
        <span className="muted">點擊帶有訊號的事件，可展開「訊號 → 訂單 → 成交」完整鏈路</span>
      </div>
      <div className="card">
        <ActivityTimeline logs={data} />
        {data.length >= limit && limit < 1000 && (
          <div style={{ textAlign: "center", marginTop: 12 }}>
            <button className="btn" disabled={isFetching} onClick={() => setLimit(Math.min(limit + 200, 1000))}>載入更多</button>
          </div>
        )}
      </div>
    </>
  );
}
