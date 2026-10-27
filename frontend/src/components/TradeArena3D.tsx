import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import * as THREE from "three";
import { RoundedBoxGeometry } from "three/examples/jsm/geometries/RoundedBoxGeometry.js";
import type { Market, Position } from "../api/client";
import { fmtPct, fmtPrice, fmtSigned } from "../format";

/** 工作站：有持倉（已用即時價格算好損益），或是啟用中的策略在等進場（待機） */
export type ArenaItem =
  | { kind: "position"; key: string; position: Position; mark: number | null; pnl: number }
  | { kind: "idle"; key: string; symbol: string; market: Market; strategies: { name: string; interval: string; shadow: boolean }[] };

/**
 * 「生產線」監控畫面：每個持倉是輸送帶上的一個工作站，即時報價像貨物一樣送進工作站。
 * 鏡頭固定不動，所有數字都放在畫面上方固定位置的 2D 卡片，3D 只表現狀態（燈號顏色、報價流動）。
 */

const UP = new THREE.Color("#2bff88");
const DOWN = new THREE.Color("#ff3d6e");
const FLAT = new THREE.Color("#5fd4ff");
const IDLE = new THREE.Color("#3d5a7a");
const SPACING = 6; // 工作站間距（世界座標）
const PITCH = THREE.MathUtils.degToRad(32); // 鏡頭俯角
const FOV = 18; // 視角窄一點，透視變形小，左右兩端的工作站不會歪太多
const ROW_PX = 310; // 每條生產線佔的高度（px）
const CARD_PX = 180; // 卡片高度（含停損停利那幾列）：第一排工作站上方要留這麼多空間
const MIN_COL_PX = 210;
const BELT_TOP = 0.16;
const ROBOT_SCALE = 1.5;
const STATION_TOP = BELT_TOP + 1.8 * ROBOT_SCALE; // 機器人天線頂端的高度，卡片貼在它上方
const MOUTH_Y = 0.76; // 嘴巴在機器人身上的高度（機器人自己的座標）
const FACE_Z = 0.575;

const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const statusColor = (item: ArenaItem) =>
  item.kind === "idle" ? IDLE : item.pnl > 0 ? UP : item.pnl < 0 ? DOWN : FLAT;
const chartLink = (symbol: string, market: Market) => `/chart?symbol=${encodeURIComponent(symbol)}&market=${market}`;

type Mood = "happy" | "sad" | "flat" | "sleep" | "bye";
/** 平倉的工作站會多留一下：先揮手 BYE_WAVE 秒，再花 BYE_EXIT 秒跳起來消失，然後才重排生產線 */
const BYE_WAVE = 1.6;
const BYE_EXIT = 0.6;
const FAREWELL_MS = 2400;
type Shown = ArenaItem & { leaving?: boolean };
const moodOf = (item: ArenaItem): Mood =>
  item.kind === "idle" ? "sleep" : item.pnl > 0 ? "happy" : item.pnl < 0 ? "sad" : "flat";
const SMILE: Record<Mood, number> = { happy: 1, flat: 0.2, sad: -0.85, sleep: 0.25, bye: 0.8 };

/** 每個工作站是一隻小機器人：表情只表現狀態（賺錢開心、虧損冒汗、待機睡覺），數字都在 2D 卡片上 */
interface Robot {
  root: THREE.Group; // 固定在線上的位置
  body: THREE.Group; // 會跳、會壓扁的部分
  shadow: THREE.Mesh;
  eyes: THREE.Mesh[];
  mouth: THREE.Mesh;
  cheeks: THREE.Mesh[];
  arms: THREE.Group[];
  antenna: THREE.Group;
  bulb: THREE.MeshStandardMaterial;
  belly: THREE.MeshStandardMaterial;
  drop: THREE.Mesh;
  zzz: THREE.Sprite[];
  x: number; z: number; colW: number; seed: number;
  mood: Mood;
  smile: number; // 嘴巴弧度，往 SMILE[mood] 慢慢靠過去
  blush: number;
  pop: number; // 出場動畫進度 0→1
  chomp: number; // 吃到金幣的壓扁量，會自己消退
  cheer: number; // 轉虧為盈的大跳剩餘秒數
  bye: number; // 平倉告別開始後經過的秒數，-1 = 沒有要走
  byeConfetti: boolean; // 賺錢收工：跳走的時候放彩帶
  blinkAt: number;
}
interface Packet { obj: THREE.Group; robot: Robot; from: number; t: number }
interface Particle { mesh: THREE.Mesh; v: THREE.Vector3; spin: THREE.Vector3; life: number }

/** 輸送帶表面的斜紋貼圖（捲動 offset 就是帶子在動） */
function beltTexture() {
  const c = document.createElement("canvas");
  c.width = 128;
  c.height = 32;
  const ctx = c.getContext("2d")!;
  ctx.fillStyle = "#0b1a30";
  ctx.fillRect(0, 0, 128, 32);
  ctx.strokeStyle = "#1c4a78";
  ctx.lineWidth = 6;
  for (let x = -32; x < 160; x += 32) {
    ctx.beginPath();
    ctx.moveTo(x, 32);
    ctx.lineTo(x + 16, 0);
    ctx.stroke();
  }
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = THREE.RepeatWrapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/** 睡覺冒出來的「Z」 */
function zTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const ctx = c.getContext("2d")!;
  ctx.font = "bold 52px ui-rounded, system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = "#a9c8ff";
  ctx.fillText("Z", 32, 34);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

function disposeTree(obj: THREE.Object3D) {
  obj.traverse((o) => {
    const m = o as THREE.Mesh;
    m.geometry?.dispose();
    const mats = Array.isArray(m.material) ? m.material : m.material ? [m.material] : [];
    mats.forEach((mat) => mat.dispose());
  });
}

function buildRobot(x: number, z: number, colW: number, zTex: THREE.Texture): Robot {
  const root = new THREE.Group();
  root.position.set(x, BELT_TOP, z);
  root.scale.setScalar(ROBOT_SCALE);
  const shadow = new THREE.Mesh(
    new THREE.CircleGeometry(0.7, 32),
    new THREE.MeshBasicMaterial({ color: "#000000", transparent: true, opacity: 0.4, depthWrite: false }),
  );
  shadow.rotation.x = -Math.PI / 2;
  shadow.position.y = 0.01;
  root.add(shadow);
  const body = new THREE.Group();
  root.add(body);

  const shell = new THREE.MeshStandardMaterial({ color: "#e6eeff", roughness: 0.35, metalness: 0.05 });
  const dark = new THREE.MeshStandardMaterial({ color: "#2a3b5c", roughness: 0.5 });
  const glow = new THREE.MeshBasicMaterial({ color: "#8ff7ff" });
  for (const s of [-1, 1]) {
    const foot = new THREE.Mesh(new THREE.SphereGeometry(0.2, 16, 12), dark);
    foot.scale.set(1, 0.55, 1.25);
    foot.position.set(s * 0.32, 0.1, 0.05);
    body.add(foot);
  }
  const torso = new THREE.Mesh(new RoundedBoxGeometry(1.3, 1.15, 1.0, 4, 0.32), shell);
  torso.position.y = 0.775;
  body.add(torso);
  const face = new THREE.Mesh(
    new RoundedBoxGeometry(0.88, 0.56, 0.1, 3, 0.045),
    new THREE.MeshStandardMaterial({ color: "#0a1830", roughness: 0.15, metalness: 0.3 }),
  );
  face.position.set(0, 0.86, 0.52);
  body.add(face);
  const eyes = [-1, 1].map((s) => {
    const eye = new THREE.Mesh(new THREE.SphereGeometry(0.085, 16, 12), glow);
    eye.position.set(s * 0.2, 0.93, FACE_Z);
    body.add(eye);
    return eye;
  });
  // 半圈 torus：scale.y 正的是微笑、負的是嘴角下垂，中間平滑過渡
  const mouth = new THREE.Mesh(new THREE.TorusGeometry(0.1, 0.026, 8, 20, Math.PI), glow);
  mouth.rotation.z = Math.PI;
  mouth.position.set(0, MOUTH_Y, FACE_Z);
  body.add(mouth);
  const cheekMat = new THREE.MeshBasicMaterial({ color: "#ff7eb6", transparent: true, opacity: 0.85 });
  const cheeks = [-1, 1].map((s) => {
    const cheek = new THREE.Mesh(new THREE.CircleGeometry(0.06, 16), cheekMat);
    cheek.position.set(s * 0.34, 0.8, FACE_Z + 0.005);
    body.add(cheek);
    return cheek;
  });
  const belly = new THREE.MeshStandardMaterial({ color: FLAT, emissive: FLAT, emissiveIntensity: 1 });
  const strip = new THREE.Mesh(new RoundedBoxGeometry(0.46, 0.08, 0.06, 2, 0.025), belly);
  strip.position.set(0, 0.45, 0.5);
  body.add(strip);
  const arms = [-1, 1].map((s) => {
    const pivot = new THREE.Group();
    pivot.position.set(s * 0.66, 0.88, 0);
    const arm = new THREE.Mesh(new THREE.CapsuleGeometry(0.085, 0.26, 4, 10), shell);
    arm.position.y = -0.2;
    pivot.add(arm);
    body.add(pivot);
    return pivot;
  });
  const antenna = new THREE.Group();
  antenna.position.y = 1.35;
  const stem = new THREE.Mesh(new THREE.CylinderGeometry(0.025, 0.025, 0.28, 8), dark);
  stem.position.y = 0.14;
  antenna.add(stem);
  const bulb = new THREE.MeshStandardMaterial({ color: FLAT, emissive: FLAT, emissiveIntensity: 1.4 });
  const ball = new THREE.Mesh(new THREE.SphereGeometry(0.1, 16, 12), bulb);
  ball.position.y = 0.33;
  antenna.add(ball);
  body.add(antenna);
  const drop = new THREE.Mesh(
    new THREE.SphereGeometry(0.055, 12, 10),
    new THREE.MeshBasicMaterial({ color: "#9ad8ff", transparent: true, opacity: 0 }),
  );
  drop.scale.set(1, 1.45, 1);
  body.add(drop);
  const zzz = [0, 1, 2].map(() => {
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: zTex, transparent: true, opacity: 0, depthWrite: false }));
    body.add(sp);
    return sp;
  });
  return {
    root, body, shadow, eyes, mouth, cheeks, arms, antenna, bulb, belly, drop, zzz,
    x, z, colW, seed: Math.random() * 10,
    mood: "flat", smile: SMILE.flat, blush: 0, pop: 1, chomp: 0, cheer: 0, bye: -1, byeConfetti: false, blinkAt: 1 + Math.random() * 3,
  };
}

/** 回彈一點點的 ease：出場時「啵」一下 */
const easeOutBack = (k: number) => 1 + 2.7 * Math.pow(k - 1, 3) + 1.7 * Math.pow(k - 1, 2);

function animateRobot(r: Robot, t: number, dt: number, still: boolean) {
  const follow = still ? 1 : Math.min(1, dt * 6);
  r.smile += (SMILE[r.mood] - r.smile) * follow;
  r.blush += ((r.mood === "happy" || r.mood === "bye" ? 1 : 0) - r.blush) * follow;
  r.pop = still ? 1 : Math.min(1, r.pop + dt / 0.6);
  r.chomp = Math.max(0, r.chomp - dt * 4);
  r.cheer = Math.max(0, r.cheer - dt);
  const ph = t + r.seed;
  const m = still ? "still" : r.mood;

  let hop = 0, tiltX = 0, tiltZ = 0, breathe = 0, armR = 0.2, armL = 0.2, antX = 0, antZ = 0;
  if (m === "happy") {
    hop = Math.abs(Math.sin(ph * 4)) * 0.07;
    armR = 0.5 + (0.5 + 0.5 * Math.sin(ph * 9)) * 1.4; // 揮手
    armL = 0.3 + Math.sin(ph * 4) * 0.1;
    antZ = Math.sin(ph * 6) * 0.2;
  } else if (m === "sad") {
    tiltX = 0.14;
    tiltZ = Math.sin(ph * 1.4) * 0.04;
    armR = armL = 0.04;
    antX = 0.55; // 天線垂下來
  } else if (m === "sleep") {
    breathe = Math.sin(ph * 1.6) * 0.03;
    tiltZ = Math.sin(ph * 0.8) * 0.05;
    armR = armL = 0.1;
    antX = 0.3;
  } else if (m === "bye") {
    armR = 1.3 + Math.sin(ph * 11) * 0.7; // 用力揮手掰掰
    armL = 0.15;
    tiltZ = Math.sin(ph * 5.5) * 0.08; // 身體跟著揮手左右晃
    antZ = Math.sin(ph * 5.5) * 0.25;
  } else if (m === "flat") {
    tiltZ = Math.sin(ph * 1.2) * 0.03;
    antZ = Math.sin(ph * 2) * 0.08;
  }
  if (r.cheer > 0) {
    hop += Math.sin((1 - r.cheer / 0.8) * Math.PI) * 0.32;
    armR = armL = 2.4;
  }
  // 揮完手：轉一圈跳起來，越跳越小直到消失
  let leave = 0;
  if (r.bye >= 0 && !still) {
    r.bye += dt;
    leave = THREE.MathUtils.clamp((r.bye - BYE_WAVE) / BYE_EXIT, 0, 1);
    hop += Math.sin(leave * Math.PI * 0.5) * 1.4;
    if (leave > 0) armR = armL = 2.6;
  }
  const s = Math.max(0.001, easeOutBack(THREE.MathUtils.clamp(r.pop, 0, 1)) * (1 - leave * leave));
  const sx = 1 + 0.1 * r.chomp, sy = 1 - 0.14 * r.chomp + breathe;
  r.body.position.y = hop;
  r.body.rotation.set(tiltX, leave * Math.PI * 2, tiltZ);
  r.body.scale.set(s * sx, s * sy, s * sx);
  r.shadow.scale.setScalar(Math.max(0.01, s * (1 - hop * 1.2)));
  r.arms[1].rotation.z = armR;
  r.arms[0].rotation.z = -armL;
  r.antenna.rotation.set(antX, 0, antZ);

  // 眨眼
  let eyeY = r.mood === "happy" || r.mood === "bye" ? 0.7 : r.mood === "sad" ? 0.85 : 1;
  if (!still && t > r.blinkAt) {
    eyeY = 0.1;
    if (t > r.blinkAt + 0.12) r.blinkAt = t + 2 + Math.random() * 3.5;
  }
  if (r.mood === "sleep") eyeY = 0.12;
  r.eyes.forEach((e) => e.scale.set(1, eyeY, 0.5));
  r.mouth.scale.set(1 + r.chomp * 0.5, Math.sign(r.smile || 1) * Math.max(0.22, Math.abs(r.smile)) + r.chomp, 1);
  r.cheeks.forEach((c) => c.scale.setScalar(Math.max(0.001, r.blush)));

  // 虧損時一滴汗從頭側滑下來
  const dm = r.drop.material as THREE.MeshBasicMaterial;
  if (r.mood === "sad" && !still) {
    const k = (ph * 0.5) % 1;
    r.drop.position.set(0.46, 1.22 - k * 0.35, 0.42);
    dm.opacity = k < 0.15 ? k / 0.15 : 1 - (k - 0.15) / 0.85;
  } else dm.opacity = 0;

  // 待機時冒 Zzz
  r.zzz.forEach((sp, i) => {
    const mat = sp.material as THREE.SpriteMaterial;
    if (r.mood !== "sleep" || still) { mat.opacity = 0; return; }
    const k = (ph * 0.35 + i / 3) % 1;
    sp.position.set(0.45 + k * 0.45 + Math.sin(k * 6 + i) * 0.06, 1.35 + k * 0.5, 0.2);
    sp.scale.setScalar(0.14 + k * 0.16);
    mat.opacity = Math.sin(k * Math.PI) * 0.9;
  });
  r.bulb.emissiveIntensity = still ? 1.2 : 1.1 + Math.sin(ph * (r.mood === "sleep" ? 1 : 2.4)) * 0.35;
}

/** 平倉了：開始揮手告別（燈號維持平倉前的顏色） */
function startBye(r: Robot, item: ArenaItem) {
  if (r.bye >= 0) return;
  r.bye = 0;
  r.mood = "bye";
  r.cheer = 0;
  r.byeConfetti = item.kind === "position" && item.pnl > 0;
}

/** 換心情、換燈號顏色；轉成獲利時回傳 true（讓外面放彩帶） */
function applyStatus(r: Robot, item: ArenaItem, instant: boolean) {
  const color = statusColor(item);
  [r.bulb, r.belly].forEach((m) => { m.color.copy(color); m.emissive.copy(color); });
  const mood = moodOf(item);
  const cheered = !instant && mood === "happy" && r.mood !== "happy";
  r.mood = mood;
  if (instant) { r.smile = SMILE[mood]; r.blush = mood === "happy" ? 1 : 0; }
  if (cheered) r.cheer = 0.8;
  return cheered;
}

const CONFETTI = ["#ffd84d", "#ff7eb6", "#5fd4ff", "#2bff88", "#b18cff"];

/** 卡片上的數字：值變動時短暫閃綠／閃紅（位置不動），跟一般看盤軟體一樣 */
function Flash({ value, children }: { value: number | null; children: ReactNode }) {
  const prev = useRef(value);
  const [dir, setDir] = useState<"" | "up" | "down">("");
  useEffect(() => {
    const old = prev.current;
    prev.current = value;
    if (old === null || value === null || value === old) return;
    setDir(value > old ? "up" : "down");
    const t = setTimeout(() => setDir(""), 700);
    return () => clearTimeout(t);
  }, [value]);
  return <span className={dir ? `flash-${dir}` : undefined}>{children}</span>;
}

function IdleCard({ item, width }: { item: Extract<ArenaItem, { kind: "idle" }>; width: number }) {
  const [first, ...rest] = item.strategies;
  return (
    <Link to={chartLink(item.symbol, item.market)} className="station-card idle" style={{ width }} title={`查看 ${item.symbol} 的 K 線`}>
      <div className="sc-head">
        <b>{item.symbol}</b>
        <span className="badge">待機</span>
      </div>
      <div className="sc-wait">等待進場訊號</div>
      <div className="sc-row"><span className="muted">策略</span>
        <span className="sc-name" title={item.strategies.map((x) => x.name).join("\n")}>{first.name}{first.shadow ? "（影子）" : ""}</span>
      </div>
      <div className="sc-row"><span className="muted">週期</span><span>{first.interval}{rest.length > 0 && ` · 另 ${rest.length} 個策略`}</span></div>
    </Link>
  );
}

/** 停損／停利價，加上離現價還有多遠（例如 −2.1%） */
function ExitRow({ label, price, mark, cls }: { label: string; price: number | null; mark: number | null; cls: string }) {
  const dist = price !== null && mark ? (price - mark) / mark : null;
  return (
    <div className="sc-row">
      <span className="muted">{label}</span>
      <span className={price === null ? "muted" : cls}>
        {price === null ? "未設定" : fmtPrice(price)}
        {dist !== null && <small className="sc-dist">{dist > 0 ? "+" : dist < 0 ? "−" : ""}{fmtPct(Math.abs(dist))}</small>}
      </span>
    </div>
  );
}

function StationCard({ item, width }: { item: Shown; width: number }) {
  if (item.kind === "idle") return <IdleCard item={item} width={width} />;
  const { position: p, mark, pnl } = item;
  const margin = (p.qty * p.entry_price) / Math.max(1, p.leverage);
  const roe = margin > 0 ? pnl / margin : null;
  const cls = pnl > 0 ? "up" : pnl < 0 ? "down" : "";
  return (
    <Link to={chartLink(p.symbol, p.market)} className={`station-card ${cls}${item.leaving ? " leaving" : ""}`} style={{ width }}
      title={`查看 ${p.symbol} 的 K 線與買賣點`}>
      <div className="sc-head">
        <b>{p.symbol}</b>
        {item.leaving
          ? <span className="badge">👋 已平倉</span>
          : <span className={`badge ${p.side}`}>{p.side === "long" ? "▲ 多" : "▼ 空"} {p.leverage}x</span>}
      </div>
      <div className="sc-price"><Flash value={mark}>{fmtPrice(mark)}</Flash></div>
      <div className="sc-row"><span className="muted">進場</span><span>{fmtPrice(p.entry_price)}</span></div>
      <ExitRow label="停損" price={p.stop_price} mark={mark} cls="down" />
      <ExitRow label="停利" price={p.take_profit_price} mark={mark} cls="up" />
      <div className={`sc-row sc-pnl ${cls}`}>
        <span>{fmtSigned(pnl)} USDT</span>
        <span>{roe === null ? "" : `${roe > 0 ? "+" : ""}${fmtPct(roe)}`}</span>
      </div>
      {(p.stop_price !== null || p.take_profit_price !== null) && (
        <div className="sc-src">{p.native_protection ? "停損停利掛在交易所" : "停損停利由軟體監控"}</div>
      )}
    </Link>
  );
}

export function TradeArena3D({ items }: { items: ArenaItem[] }) {
  // 剛平倉的持倉先留在原位 FAREWELL_MS 揮手告別，生產線等它走了才重排
  // （在 render 裡就比對，不能等 effect：不然會先重排一次、機器人被拆掉又重新蹦出來）
  const [prevItems, setPrevItems] = useState(items);
  const [departed, setDeparted] = useState<{ item: ArenaItem; index: number; until: number }[]>([]);
  if (prevItems !== items) {
    const now = new Set(items.map((i) => i.key));
    const gone = prevItems.flatMap((item, index) =>
      item.kind === "position" && !now.has(item.key) ? [{ item, index, until: Date.now() + FAREWELL_MS }] : []);
    setPrevItems(items);
    if (gone.length) setDeparted((d) => [...d, ...gone]);
  }
  useEffect(() => {
    if (!departed.length) return;
    const next = Math.min(...departed.map((d) => d.until));
    const t = setTimeout(() => setDeparted((d) => d.filter((x) => x.until > Date.now())), next - Date.now());
    return () => clearTimeout(t);
  }, [departed]);
  const positions = useMemo(() => {
    const list: Shown[] = [...items];
    [...departed].sort((a, b) => a.index - b.index)
      .forEach((d) => list.splice(Math.min(d.index, list.length), 0, { ...d.item, leaving: true }));
    return list;
  }, [items, departed]);

  const hostRef = useRef<HTMLDivElement>(null);
  const ctx = useRef<{
    renderer: THREE.WebGLRenderer;
    scene: THREE.Scene;
    camera: THREE.PerspectiveCamera;
    line: THREE.Group;
    belt: THREE.CanvasTexture;
    zTex: THREE.CanvasTexture;
    robots: Map<string, Robot>;
    packets: Packet[];
    particles: Particle[];
  } | null>(null);
  const [width, setWidth] = useState(0);
  const [anchors, setAnchors] = useState<Record<string, { x: number; y: number }>>({});
  const [failed, setFailed] = useState(false);
  const lastMark = useRef<Record<string, number | null>>({});
  const seen = useRef<Set<string> | null>(null); // 看過的工作站：新開的倉才「啵」出場、放彩帶

  const n = positions.length;
  const held = items.filter((i) => i.kind === "position").length;
  const idle = items.length - held;
  // 一排最多放幾站看寬度；鏡頭遠近也只看寬度，站少的時候不會被放大，而是置中排列
  const capacity = Math.max(1, Math.min(6, Math.floor(width / MIN_COL_PX)));
  const cols = Math.min(capacity, Math.max(1, n));
  const rows = Math.max(1, Math.ceil(n / cols));
  const colPx = width / capacity;
  const height = rows * ROW_PX;
  const keys = positions.map((i) => i.key).join("|");

  /** 從機器人頭上噴出一把彩帶 */
  const burst = (r: Robot) => {
    const c = ctx.current;
    if (!c || reducedMotion() || c.particles.length > 200) return;
    for (let i = 0; i < 26; i++) {
      const color = CONFETTI[i % CONFETTI.length];
      const mesh = new THREE.Mesh(
        new THREE.PlaneGeometry(0.1, 0.06),
        new THREE.MeshBasicMaterial({ color, side: THREE.DoubleSide, transparent: true }),
      );
      mesh.position.set(r.x, BELT_TOP + 1.6 * ROBOT_SCALE, r.z);
      const a = Math.random() * Math.PI * 2;
      const sp = 1 + Math.random() * 1.6;
      c.line.add(mesh);
      c.particles.push({
        mesh,
        v: new THREE.Vector3(Math.cos(a) * sp, 2.6 + Math.random() * 2, Math.sin(a) * sp * 0.6),
        spin: new THREE.Vector3(Math.random() * 10, Math.random() * 10, Math.random() * 10),
        life: 1.4 + Math.random() * 0.5,
      });
    }
  };

  // renderer / 燈光 / 地板只建一次
  useEffect(() => {
    const host = hostRef.current!;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    } catch {
      setFailed(true);
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.domElement.style.width = "100%";
    renderer.domElement.style.height = "100%";
    host.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(FOV, 1, 0.1, 500);
    scene.add(new THREE.HemisphereLight("#bcd8ff", "#0b1a30", 1.1));
    const key = new THREE.DirectionalLight("#ffffff", 1.8);
    key.position.set(6, 12, 10);
    scene.add(key);
    const rim = new THREE.DirectionalLight("#5fd4ff", 0.8); // 背後打一點藍光，機器人輪廓比較立體
    rim.position.set(-6, 5, -8);
    scene.add(rim);
    const grid = new THREE.GridHelper(200, 100, "#123a63", "#0c2240");
    scene.add(grid);
    const line = new THREE.Group();
    scene.add(line);
    ctx.current = {
      renderer, scene, camera, line, belt: beltTexture(), zTex: zTexture(),
      robots: new Map(), packets: [], particles: [],
    };

    const ro = new ResizeObserver(([e]) => setWidth(Math.round(e.contentRect.width)));
    ro.observe(host);

    const clock = new THREE.Clock();
    let frame = 0;
    const animate = () => {
      frame = requestAnimationFrame(animate);
      const c = ctx.current!;
      const dt = Math.min(clock.getDelta(), 0.1);
      const t = clock.elapsedTime;
      const still = reducedMotion();
      if (!still) c.belt.offset.x -= dt * 0.6;
      c.robots.forEach((r) => {
        animateRobot(r, t, dt, still);
        if (r.byeConfetti && r.bye > BYE_WAVE) { r.byeConfetti = false; burst(r); }
      });
      // 報價金幣：沿輸送帶滾過去，最後一跳跳進機器人嘴裡
      c.packets = c.packets.filter((pk) => {
        pk.t += dt / 1.1;
        const r = pk.robot;
        const k = Math.min(1, pk.t);
        const edge = r.x - 0.95 * ROBOT_SCALE;
        if (k < 0.65) {
          const u = k / 0.65;
          pk.obj.position.set(pk.from + (edge - pk.from) * u, BELT_TOP + 0.2 + Math.abs(Math.sin(u * Math.PI * 3)) * 0.12, r.z);
        } else {
          const u = (k - 0.65) / 0.35;
          pk.obj.position.set(
            edge + (r.x - edge) * u,
            BELT_TOP + 0.2 + (MOUTH_Y * ROBOT_SCALE - 0.2) * u + Math.sin(u * Math.PI) * 0.8,
            r.z + FACE_Z * ROBOT_SCALE * u,
          );
          pk.obj.scale.setScalar(1 - u * 0.7);
        }
        pk.obj.rotation.y += dt * 8;
        if (pk.t < 1) return true;
        r.chomp = 1;
        line.remove(pk.obj);
        disposeTree(pk.obj);
        return false;
      });
      // 彩帶：往上噴、重力往下掉、邊轉邊淡出
      c.particles = c.particles.filter((p) => {
        p.life -= dt;
        p.v.y -= 6 * dt;
        p.v.multiplyScalar(1 - dt * 0.8);
        p.mesh.position.addScaledVector(p.v, dt);
        p.mesh.rotation.x += p.spin.x * dt;
        p.mesh.rotation.y += p.spin.y * dt;
        p.mesh.rotation.z += p.spin.z * dt;
        (p.mesh.material as THREE.MeshBasicMaterial).opacity = Math.min(1, p.life / 0.5);
        if (p.life > 0 && p.mesh.position.y > 0) return true;
        line.remove(p.mesh);
        disposeTree(p.mesh);
        return false;
      });
      renderer.render(scene, camera);
    };
    animate();

    return () => {
      cancelAnimationFrame(frame);
      ro.disconnect();
      ctx.current?.belt.dispose();
      ctx.current?.zTex.dispose();
      disposeTree(scene);
      renderer.dispose();
      renderer.domElement.remove();
      ctx.current = null;
    };
  }, []);

  // 持倉組合或寬度改變：重排生產線、重新算卡片位置（只在這時候，不是每一幀）
  useLayoutEffect(() => {
    const c = ctx.current;
    if (!c || width === 0) return;
    c.renderer.setSize(width, height, false);
    const cam = c.camera;
    cam.aspect = width / height;
    // 固定鏡頭：距離算到剛好看得到整排工作站
    const dist = (capacity * SPACING) / (2 * Math.tan(THREE.MathUtils.degToRad(FOV / 2)) * cam.aspect);
    cam.position.set(0, 0.9 + Math.sin(PITCH) * dist, Math.cos(PITCH) * dist);
    cam.lookAt(0, 0.9, 0);
    cam.updateProjectionMatrix();
    cam.updateMatrixWorld();
    const toPx = (v: THREE.Vector3) => {
      const p = v.clone().project(cam);
      return { x: ((p.x + 1) / 2) * width, y: ((1 - p.y) / 2) * height };
    };
    // 每一排放在哪個 z：用二分搜尋讓工作站頂端剛好落在第 r 排的位置（卡片在它上方）
    const rowZ = Array.from({ length: rows }, (_, r) => {
      const want = CARD_PX + 14 + r * ROW_PX;
      let lo = -400, hi = Math.cos(PITCH) * dist * 0.8; // lo 越遠、畫面上越高
      for (let i = 0; i < 40; i++) {
        const mid = (lo + hi) / 2;
        if (toPx(new THREE.Vector3(0, STATION_TOP, mid)).y < want) lo = mid; else hi = mid;
      }
      return (lo + hi) / 2;
    });

    c.line.children.slice().forEach((o) => { c.line.remove(o); disposeTree(o); });
    c.robots.clear();
    c.packets = [];
    c.particles = [];

    // 每一排的「螢幕 px ↔ 世界 x」換算（鏡頭沒有左右轉，所以是線性的）：欄位依螢幕寬度均分，卡片才不會被切掉
    const rowScale = rowZ.map((z) => {
      const x0 = toPx(new THREE.Vector3(0, STATION_TOP, z)).x;
      const perUnit = toPx(new THREE.Vector3(1, STATION_TOP, z)).x - x0;
      return { toWorld: (px: number) => (px - x0) / perUnit, colW: colPx / perUnit };
    });
    rowZ.forEach((z, r) => {
      const { toWorld } = rowScale[r];
      const left = toWorld(0) - 1;
      const len = toWorld(width) + 1 - left;
      const belt = new THREE.Mesh(
        new THREE.BoxGeometry(len, BELT_TOP, 1.1),
        new THREE.MeshStandardMaterial({ map: c.belt, metalness: 0.3, roughness: 0.6 }),
      );
      belt.position.set(left + len / 2, BELT_TOP / 2, z);
      c.line.add(belt);
      const rail = new THREE.Mesh(
        new THREE.BoxGeometry(len, 0.06, 0.06),
        new THREE.MeshBasicMaterial({ color: "#1fb6ff" }),
      );
      rail.position.set(left + len / 2, 0.18, z + 0.58);
      c.line.add(rail);
    });
    const firstLayout = seen.current === null;
    const known = seen.current ?? new Set<string>();
    const next: Record<string, { x: number; y: number }> = {};
    positions.forEach((item, i) => {
      const r = Math.floor(i / cols);
      const z = rowZ[r];
      const inRow = Math.min(cols, n - r * cols); // 最後一排可能不滿，一樣置中
      const x = rowScale[r].toWorld(width / 2 + ((i % cols) - (inRow - 1) / 2) * colPx);
      const robot = buildRobot(x, z, rowScale[r].colW, c.zTex);
      applyStatus(robot, item, true);
      if (item.leaving) startBye(robot, item);
      if (!known.has(item.key)) {
        robot.pop = firstLayout ? -i * 0.15 : 0; // 第一次進場一隻接一隻跳出來
        if (!firstLayout && item.kind === "position") burst(robot); // 新開倉，放彩帶
      }
      c.line.add(robot.root);
      c.robots.set(item.key, robot);
      next[item.key] = toPx(new THREE.Vector3(x, STATION_TOP, z));
    });
    seen.current = new Set(positions.map((i) => i.key));
    setAnchors(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keys, width, capacity, cols, rows, height]);

  // 損益變號 → 換表情跟燈號；報價變動 → 送一枚金幣給機器人吃
  useEffect(() => {
    const c = ctx.current;
    if (!c) return;
    const still = reducedMotion();
    positions.forEach((item) => {
      const r = c.robots.get(item.key);
      if (!r) return;
      if (item.leaving) { startBye(r, item); return; }
      if (applyStatus(r, item, false)) burst(r);
      if (item.kind === "idle") return; // 待機的沒有即時報價
      const prev = lastMark.current[item.key];
      lastMark.current[item.key] = item.mark;
      if (still || prev === undefined || prev === null || item.mark === null || item.mark === prev || c.packets.length > 60) return;
      const tick = item.mark > prev ? UP : DOWN;
      const coin = new THREE.Mesh(
        new THREE.CylinderGeometry(0.16, 0.16, 0.06, 20),
        new THREE.MeshStandardMaterial({ color: tick, emissive: tick, emissiveIntensity: 0.7, metalness: 0.4, roughness: 0.3 }),
      );
      coin.rotation.x = Math.PI / 2; // 硬幣正面朝鏡頭，外層 group 負責自轉
      const obj = new THREE.Group();
      obj.add(coin);
      const from = r.x - r.colW / 2 + 0.2;
      obj.position.set(from, BELT_TOP + 0.2, r.z);
      c.line.add(obj);
      c.packets.push({ obj, robot: r, from, t: 0 });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [positions]);

  const cardWidth = Math.max(150, Math.min(200, colPx - 16));

  return (
    <div className="arena">
      <div className="arena-hud tl">
        <span className="hud-tag">PRODUCTION LINE</span>
        <span>{held} 個持倉 · {idle} 個待機</span>
      </div>
      <div className="arena-stage" style={{ height }}>
        <div className="arena-canvas" ref={hostRef} role="img"
          aria-label={`持倉生產線：${held} 個持倉、${idle} 個待機的機器人，天線燈綠色代表獲利、紅色代表虧損、灰色代表待機`} />
        {positions.map((item) => {
          const a = anchors[item.key];
          return a ? (
            <div key={item.key} className="station-anchor" style={{ left: a.x, top: a.y }}>
              <StationCard item={item} width={cardWidth} />
            </div>
          ) : null;
        })}
        {failed && <div className="arena-empty">這個瀏覽器不支援 WebGL，無法顯示 3D 場景</div>}
        {!failed && n === 0 && (
          <div className="arena-empty">
            <span>生產線停機中 · 沒有持倉，也沒有啟用中的策略 <Link to="/strategies">前往策略 →</Link></span>
          </div>
        )}
      </div>
      <div className="arena-legend">
        <span><i style={{ background: "#2bff88" }} />獲利（開心揮手）</span>
        <span><i style={{ background: "#ff3d6e" }} />虧損（垂頭冒汗）</span>
        <span><i style={{ background: "#3d5a7a" }} />待機（睡覺等進場訊號）</span>
        <span>輸送帶上的金幣 = 每一筆報價跳動（綠漲紅跌） · 點卡片看 K 線</span>
      </div>
    </div>
  );
}
