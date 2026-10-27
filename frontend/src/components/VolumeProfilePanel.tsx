import { useEffect, useState } from "react";

/** 成交量分佈圖的設定：統計範圍模式與各項參數，存在瀏覽器裡。 */

export interface VpSettings {
  /** visible：畫面上看得到的 K 棒；fixed：在圖上選取的固定區間；session：每個交易時段各畫一個 */
  mode: "visible" | "fixed" | "session";
  /** 交易時段從幾點開始：0 = UTC 00:00（跟交易所日 K 一致），8 = 台灣 00:00 */
  sessionTz: 0 | 8;
  sessionLen: "day" | "week";
  /** 固定區間，unix 秒；to 是終點 K 棒結束的時間（不含） */
  range: { from: number; to: number } | null;
  rows: number;
  /** 價值區間涵蓋的成交量比例，0.5–0.95 */
  vaPct: number;
  /** 直方圖最長那條佔寬度的比例 */
  widthPct: number;
  showVahVal: boolean;
  /** null 表示跟著主題色 */
  pocColor: string | null;
}

export const VP_DEFAULTS: VpSettings = {
  mode: "visible", range: null, sessionTz: 0, sessionLen: "day", rows: 60, vaPct: 0.7, widthPct: 0.25, showVahVal: true, pocColor: null,
};

const LIMITS = {
  rows: [10, 300],
  vaPct: [0.5, 0.95],
  widthPct: [0.1, 0.8],
} as const;

const clamp = (v: number, [lo, hi]: readonly [number, number]) => Math.min(hi, Math.max(lo, v));
const KEY = "chart.vp";

export function loadVpSettings(): VpSettings {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) ?? "null");
    if (!v || typeof v !== "object") return VP_DEFAULTS;
    const s = { ...VP_DEFAULTS, ...v } as VpSettings;
    return {
      ...s,
      mode: s.mode === "fixed" || s.mode === "session" ? s.mode : "visible",
      sessionTz: s.sessionTz === 8 ? 8 : 0,
      sessionLen: s.sessionLen === "week" ? "week" : "day",
      rows: Math.round(clamp(Number(s.rows) || VP_DEFAULTS.rows, LIMITS.rows)),
      vaPct: clamp(Number(s.vaPct) || VP_DEFAULTS.vaPct, LIMITS.vaPct),
      widthPct: clamp(Number(s.widthPct) || VP_DEFAULTS.widthPct, LIMITS.widthPct),
      showVahVal: s.showVahVal !== false,
      pocColor: typeof s.pocColor === "string" && /^#[0-9a-f]{6}$/i.test(s.pocColor) ? s.pocColor : null,
      range: s.range && Number.isFinite(s.range.from) && Number.isFinite(s.range.to) ? s.range : null,
    };
  } catch {
    return VP_DEFAULTS;
  }
}

const DAY = 86400;
/** 交易時段的長度（秒） */
export const sessionSeconds = (s: VpSettings) => (s.sessionLen === "week" ? 7 * DAY : DAY);
/** t（unix 秒）所在交易時段的起點；週以週一起算（1970-01-01 是週四，所以位移 4 天） */
export function sessionStart(t: number, s: VpSettings) {
  const len = sessionSeconds(s);
  const shift = s.sessionTz * 3600 - (s.sessionLen === "week" ? 4 * DAY : 0);
  return Math.floor((t + shift) / len) * len - shift;
}

export function saveVpSettings(s: VpSettings) {
  try {
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    /* 無痕模式等情況存不了，就只在這次有效 */
  }
}

/** 數字欄位：打字時不限制，離開欄位或按 Enter 才夾到範圍內並套用 */
function NumField({ value, scale, limits, step, onCommit }: {
  value: number; scale: number; limits: readonly [number, number]; step: number; onCommit: (v: number) => void;
}) {
  const shown = String(Math.round(value * scale));
  const [draft, setDraft] = useState(shown);
  useEffect(() => setDraft(shown), [shown]);
  const commit = () => {
    const v = Number(draft);
    if (draft.trim() === "" || !Number.isFinite(v)) return setDraft(shown);
    const n = clamp(v / scale, limits);
    setDraft(String(Math.round(n * scale)));
    onCommit(n);
  };
  return (
    <input
      type="number" min={limits[0] * scale} max={limits[1] * scale} step={step} value={draft}
      onChange={(e) => setDraft(e.target.value)} onBlur={commit} onKeyDown={(e) => e.key === "Enter" && commit()}
    />
  );
}

interface Props {
  value: VpSettings;
  onChange: (s: VpSettings) => void;
  /** 主題預設的 POC 顏色 */
  defaultPocColor: string;
}

export function VolumeProfilePanel({ value: s, onChange, defaultPocColor }: Props) {
  const set = (patch: Partial<VpSettings>) => onChange({ ...s, ...patch });
  return (
    <div className="vp-panel">
      <div className="form-grid">
        <label className="field">
          價格分桶數（行數）
          <NumField value={s.rows} scale={1} limits={LIMITS.rows} step={1} onCommit={(v) => set({ rows: Math.round(v) })} />
          <small>{LIMITS.rows[0]}–{LIMITS.rows[1]}，越多越細</small>
        </label>
        <label className="field">
          價值區間比例（%）
          <NumField value={s.vaPct} scale={100} limits={LIMITS.vaPct} step={1} onCommit={(v) => set({ vaPct: Math.round(v * 100) / 100 })} />
          <small>VAH 到 VAL 之間涵蓋的成交量，常用 70%</small>
        </label>
        <label className="field">
          直方圖寬度（%）
          <NumField value={s.widthPct} scale={100} limits={LIMITS.widthPct} step={5} onCommit={(v) => set({ widthPct: Math.round(v * 100) / 100 })} />
          <small>最長那條佔{s.mode === "visible" ? "圖表" : s.mode === "fixed" ? "區間" : "時段"}寬度的比例</small>
        </label>
        {s.mode === "session" && (
          <>
            <label className="field">
              時段長度
              <select value={s.sessionLen} onChange={(e) => set({ sessionLen: e.target.value === "week" ? "week" : "day" })}>
                <option value="day">每日</option>
                <option value="week">每週（週一起）</option>
              </select>
            </label>
            <label className="field">
              時段起點
              <select value={s.sessionTz} onChange={(e) => set({ sessionTz: e.target.value === "8" ? 8 : 0 })}>
                <option value={0}>UTC 00:00（台灣 08:00，同交易所日 K）</option>
                <option value={8}>台灣 00:00</option>
              </select>
            </label>
          </>
        )}
        <label className="field">
          POC 線顏色
          <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input type="color" value={s.pocColor ?? defaultPocColor} onChange={(e) => set({ pocColor: e.target.value })} style={{ padding: 2, width: 44, height: 32 }} />
            {s.pocColor && <button className="btn small" onClick={() => set({ pocColor: null })}>恢復預設</button>}
          </span>
        </label>
        <label className="check">
          <input type="checkbox" checked={s.showVahVal} onChange={(e) => set({ showVahVal: e.target.checked })} />
          顯示 VAH／VAL 邊界線
        </label>
      </div>
      <div style={{ marginTop: 10 }}>
        <button className="btn small" onClick={() => onChange({ ...VP_DEFAULTS, mode: s.mode, range: s.range, sessionTz: s.sessionTz, sessionLen: s.sessionLen })}>參數恢復預設</button>
      </div>
    </div>
  );
}
