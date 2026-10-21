import type { Market } from "../api/client";

// ---------------------------------------------------------------- 型別（對應 backend/app/strategies/rules.py）
type Source = "open" | "high" | "low" | "close" | "volume";
export type Operand =
  | { kind: "value"; value: number }
  | { kind: "price"; field: Source }
  | { kind: "sma" | "ema"; length: number; source: Source }
  | { kind: "rsi" | "atr" | "highest" | "lowest"; length: number }
  | { kind: "bb_upper" | "bb_middle" | "bb_lower"; length: number; mult: number }
  | { kind: "macd" | "macd_signal" | "macd_hist"; fast: number; slow: number; signal: number };
type Kind = Operand["kind"];
type Cmp = ">" | "<" | ">=" | "<=" | "crosses_above" | "crosses_below";
export interface Condition { left: Operand; cmp: Cmp; right: Operand }
export interface Group { logic: "and" | "or"; conditions: Condition[] }
export interface Rules {
  long_entry: Group; long_exit: Group; short_entry: Group; short_exit: Group;
  stop_loss_pct: number; take_profit_pct: number;
}
type GroupKey = "long_entry" | "long_exit" | "short_entry" | "short_exit";

// ---------------------------------------------------------------- 選項
const KINDS: { kind: Kind; label: string; group: string }[] = [
  { kind: "price", label: "價格", group: "價格" },
  { kind: "value", label: "數值", group: "價格" },
  { kind: "sma", label: "SMA 均線", group: "趨勢" },
  { kind: "ema", label: "EMA 均線", group: "趨勢" },
  { kind: "highest", label: "前 N 根最高價", group: "趨勢" },
  { kind: "lowest", label: "前 N 根最低價", group: "趨勢" },
  { kind: "rsi", label: "RSI", group: "動能" },
  { kind: "macd", label: "MACD 線", group: "動能" },
  { kind: "macd_signal", label: "MACD 訊號線", group: "動能" },
  { kind: "macd_hist", label: "MACD 柱狀圖", group: "動能" },
  { kind: "bb_upper", label: "布林上軌", group: "波動" },
  { kind: "bb_middle", label: "布林中軌", group: "波動" },
  { kind: "bb_lower", label: "布林下軌", group: "波動" },
  { kind: "atr", label: "ATR", group: "波動" },
];
const SOURCES: [Source, string][] = [["close", "收盤價"], ["open", "開盤價"], ["high", "最高價"], ["low", "最低價"], ["volume", "成交量"]];
const CMPS: [Cmp, string][] = [
  [">", "大於"], ["<", "小於"], [">=", "大於等於"], ["<=", "小於等於"], ["crosses_above", "向上穿越"], ["crosses_below", "向下穿越"],
];
const GROUPS: { key: GroupKey; label: string; hint: string }[] = [
  { key: "long_entry", label: "做多進場", hint: "成立時開多（持有空單則反手）" },
  { key: "long_exit", label: "做多出場", hint: "持有多單時成立就平倉" },
  { key: "short_entry", label: "做空進場", hint: "成立時開空（持有多單則反手）" },
  { key: "short_exit", label: "做空出場", hint: "持有空單時成立就平倉" },
];

function defaultOperand(kind: Kind): Operand {
  switch (kind) {
    case "value": return { kind, value: 0 };
    case "price": return { kind, field: "close" };
    case "sma": case "ema": return { kind, length: 20, source: "close" };
    case "rsi": case "atr": return { kind, length: 14 };
    case "highest": case "lowest": return { kind, length: 20 };
    case "bb_upper": case "bb_middle": case "bb_lower": return { kind, length: 20, mult: 2 };
    default: return { kind, fast: 12, slow: 26, signal: 9 };
  }
}

// ---------------------------------------------------------------- 文字描述
export function describeOperand(o: Operand): string {
  const label = KINDS.find((k) => k.kind === o.kind)?.label ?? o.kind;
  switch (o.kind) {
    case "value": return String(o.value);
    case "price": return SOURCES.find(([s]) => s === o.field)![1];
    case "sma": case "ema": return `${o.kind.toUpperCase()}(${o.length}${o.source !== "close" ? `, ${SOURCES.find(([s]) => s === o.source)![1]}` : ""})`;
    case "rsi": case "atr": return `${o.kind.toUpperCase()}(${o.length})`;
    case "highest": case "lowest": return `前 ${o.length} 根${o.kind === "highest" ? "最高" : "最低"}價`;
    case "bb_upper": case "bb_middle": case "bb_lower": return `${label}(${o.length}, ${o.mult})`;
    default: return `${label}(${o.fast}, ${o.slow}, ${o.signal})`;
  }
}
export const describeCondition = (c: Condition) =>
  `${describeOperand(c.left)} ${CMPS.find(([v]) => v === c.cmp)![1]} ${describeOperand(c.right)}`;

// ---------------------------------------------------------------- 範本
const cond = (left: Operand, cmp: Cmp, right: Operand): Condition => ({ left, cmp, right });
const g = (logic: "and" | "or", ...conditions: Condition[]): Group => ({ logic, conditions });
const empty = () => g("and");
const CLOSE: Operand = { kind: "price", field: "close" };
const val = (value: number): Operand => ({ kind: "value", value });

export const TEMPLATES: { name: string; description: string; rules: Rules }[] = [
  {
    name: "RSI 超賣 + 趨勢過濾",
    description: "只在收盤價高於 EMA200（多頭趨勢）時，RSI 從超賣區回升才買；RSI 回到 60 出場，停損 3%",
    rules: {
      long_entry: g("and", cond({ kind: "rsi", length: 14 }, "crosses_above", val(30)), cond(CLOSE, ">", { kind: "ema", length: 200, source: "close" })),
      long_exit: g("or", cond({ kind: "rsi", length: 14 }, ">", val(60))),
      short_entry: empty(), short_exit: empty(), stop_loss_pct: 3, take_profit_pct: 0,
    },
  },
  {
    name: "均線交叉（多空反手）",
    description: "EMA12 向上穿越 EMA26 做多、向下穿越做空，永遠在場內",
    rules: {
      long_entry: g("and", cond({ kind: "ema", length: 12, source: "close" }, "crosses_above", { kind: "ema", length: 26, source: "close" })),
      long_exit: empty(),
      short_entry: g("and", cond({ kind: "ema", length: 12, source: "close" }, "crosses_below", { kind: "ema", length: 26, source: "close" })),
      short_exit: empty(), stop_loss_pct: 0, take_profit_pct: 0,
    },
  },
  {
    name: "布林通道突破",
    description: "收盤價向上突破布林上軌做多，跌回中軌出場",
    rules: {
      long_entry: g("and", cond(CLOSE, "crosses_above", { kind: "bb_upper", length: 20, mult: 2 })),
      long_exit: g("or", cond(CLOSE, "crosses_below", { kind: "bb_middle", length: 20, mult: 2 })),
      short_entry: empty(), short_exit: empty(), stop_loss_pct: 0, take_profit_pct: 0,
    },
  },
  {
    name: "通道突破（海龜）",
    description: "收盤價突破前 20 根最高價做多，跌破前 10 根最低價出場",
    rules: {
      long_entry: g("and", cond(CLOSE, ">", { kind: "highest", length: 20 })),
      long_exit: g("or", cond(CLOSE, "<", { kind: "lowest", length: 10 })),
      short_entry: empty(), short_exit: empty(), stop_loss_pct: 0, take_profit_pct: 0,
    },
  },
  {
    name: "MACD 動能",
    description: "MACD 柱狀圖由負轉正做多，由正轉負出場",
    rules: {
      long_entry: g("and", cond({ kind: "macd_hist", fast: 12, slow: 26, signal: 9 }, "crosses_above", val(0))),
      long_exit: g("or", cond({ kind: "macd_hist", fast: 12, slow: 26, signal: 9 }, "crosses_below", val(0))),
      short_entry: empty(), short_exit: empty(), stop_loss_pct: 0, take_profit_pct: 0,
    },
  },
];

// ---------------------------------------------------------------- 元件
function Num({ label, value, onChange, step = 1, width = 64 }: { label: string; value: number; onChange: (v: number) => void; step?: number | "any"; width?: number }) {
  return (
    <label className="rb-num" title={label}>
      <span>{label}</span>
      <input type="number" value={value} step={step} style={{ width }} onChange={(e) => onChange(e.target.value === "" ? 0 : Number(e.target.value))} />
    </label>
  );
}

function OperandEditor({ value, onChange }: { value: Operand; onChange: (o: Operand) => void }) {
  const set = (patch: Partial<Operand>) => onChange({ ...value, ...patch } as Operand);
  const o = value as Record<string, any>;
  return (
    <span className="rb-operand">
      <select value={value.kind} onChange={(e) => onChange(defaultOperand(e.target.value as Kind))}>
        {["價格", "趨勢", "動能", "波動"].map((grp) => (
          <optgroup key={grp} label={grp}>
            {KINDS.filter((k) => k.group === grp).map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}
          </optgroup>
        ))}
      </select>
      {value.kind === "value" && <Num label="" value={value.value} step="any" width={80} onChange={(v) => set({ value: v })} />}
      {(value.kind === "price" || value.kind === "sma" || value.kind === "ema") && (
        <select value={value.kind === "price" ? value.field : value.source}
          onChange={(e) => set(value.kind === "price" ? { field: e.target.value as Source } : { source: e.target.value as Source })}>
          {SOURCES.map(([s, l]) => <option key={s} value={s}>{l}</option>)}
        </select>
      )}
      {"length" in o && <Num label="長度" value={o.length} onChange={(v) => set({ length: v })} />}
      {"mult" in o && <Num label="倍數" value={o.mult} step={0.1} onChange={(v) => set({ mult: v })} />}
      {"fast" in o && (
        <>
          <Num label="快" value={o.fast} width={52} onChange={(v) => set({ fast: v })} />
          <Num label="慢" value={o.slow} width={52} onChange={(v) => set({ slow: v })} />
          <Num label="訊號" value={o.signal} width={52} onChange={(v) => set({ signal: v })} />
        </>
      )}
    </span>
  );
}

function GroupEditor({ label, hint, group, onChange }: { label: string; hint: string; group: Group; onChange: (g: Group) => void }) {
  const update = (i: number, c: Condition) => onChange({ ...group, conditions: group.conditions.map((x, j) => (j === i ? c : x)) });
  const add = () => onChange({
    ...group,
    conditions: [...group.conditions, { left: { kind: "rsi", length: 14 }, cmp: ">", right: { kind: "value", value: 50 } }],
  });
  return (
    <div className="rb-group">
      <div className="rb-group-head">
        <strong>{label}</strong>
        <span className="muted">{hint}</span>
        {group.conditions.length > 1 && (
          <div className="seg" style={{ marginLeft: "auto" }}>
            <button className={group.logic === "and" ? "on" : ""} onClick={() => onChange({ ...group, logic: "and" })}>全部成立</button>
            <button className={group.logic === "or" ? "on" : ""} onClick={() => onChange({ ...group, logic: "or" })}>任一成立</button>
          </div>
        )}
      </div>
      {group.conditions.length === 0 && <div className="muted rb-empty">（未設定，不會觸發）</div>}
      {group.conditions.map((c, i) => (
        <div className="rb-row" key={i}>
          {i > 0 && <span className="rb-join">{group.logic === "and" ? "且" : "或"}</span>}
          <OperandEditor value={c.left} onChange={(left) => update(i, { ...c, left })} />
          <select className="rb-cmp" value={c.cmp} onChange={(e) => update(i, { ...c, cmp: e.target.value as Cmp })}>
            {CMPS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
          <OperandEditor value={c.right} onChange={(right) => update(i, { ...c, right })} />
          <button className="btn small" title="刪除條件" onClick={() => onChange({ ...group, conditions: group.conditions.filter((_, j) => j !== i) })}>✕</button>
        </div>
      ))}
      {group.conditions.length < 10 && <button className="btn small" onClick={add}>＋ 新增條件</button>}
    </div>
  );
}

export function RuleBuilder({ value, onChange, market }: { value: Rules; onChange: (r: Rules) => void; market: Market }) {
  const groups = market === "spot" ? GROUPS.filter((x) => x.key.startsWith("long")) : GROUPS;
  return (
    <div className="rb">
      <div className="toolbar" style={{ marginBottom: 4 }}>
        <span className="muted">套用範本：</span>
        {TEMPLATES.map((t) => (
          <button key={t.name} className="btn small" title={t.description}
            onClick={() => confirm(`套用範本「${t.name}」？目前的條件會被取代。\n\n${t.description}`) && onChange(structuredClone(t.rules))}>
            {t.name}
          </button>
        ))}
      </div>
      {groups.map((x) => (
        <GroupEditor key={x.key} label={x.label} hint={x.hint} group={value[x.key]} onChange={(gr) => onChange({ ...value, [x.key]: gr })} />
      ))}
      {market === "spot" && <p className="muted" style={{ fontSize: 12 }}>現貨不能做空，所以只有做多的條件。</p>}
      <div className="toolbar" style={{ marginTop: 4 }}>
        <Num label="停損 %" value={value.stop_loss_pct} step={0.1} width={72} onChange={(v) => onChange({ ...value, stop_loss_pct: v })} />
        <Num label="停利 %" value={value.take_profit_pct} step={0.1} width={72} onChange={(v) => onChange({ ...value, take_profit_pct: v })} />
        <span className="muted" style={{ fontSize: 12 }}>以 K 棒收盤價相對進場價計算，0 = 不使用。這是策略本身的出場條件，跟「設定」頁的停損停利是兩回事。</span>
      </div>
    </div>
  );
}

/** 現貨不做空：送出前把做空條件清空，避免畫面上看不到的條件影響結果。 */
export const rulesForMarket = (r: Rules, market: Market): Rules =>
  market === "spot" ? { ...r, short_entry: { logic: "and", conditions: [] }, short_exit: { logic: "and", conditions: [] } } : r;
