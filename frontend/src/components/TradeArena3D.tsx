import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import * as THREE from "three";
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
const STATION_TOP = 1.9;

const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const statusColor = (item: ArenaItem) =>
  item.kind === "idle" ? IDLE : item.pnl > 0 ? UP : item.pnl < 0 ? DOWN : FLAT;
const chartLink = (symbol: string, market: Market) => `/chart?symbol=${encodeURIComponent(symbol)}&market=${market}`;

interface Station { group: THREE.Group; light: THREE.MeshStandardMaterial; panel: THREE.MeshStandardMaterial; edges: THREE.LineBasicMaterial; x: number; z: number; colW: number }
interface Packet { mesh: THREE.Mesh; from: number; to: number; t: number }

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

function disposeTree(obj: THREE.Object3D) {
  obj.traverse((o) => {
    const m = o as THREE.Mesh;
    m.geometry?.dispose();
    const mats = Array.isArray(m.material) ? m.material : m.material ? [m.material] : [];
    mats.forEach((mat) => mat.dispose());
  });
}

function buildStation(x: number, z: number, colW: number): Station {
  const group = new THREE.Group();
  group.position.set(x, 0, z);
  const body = new THREE.Mesh(
    new THREE.BoxGeometry(1.8, 1.4, 1.6),
    new THREE.MeshStandardMaterial({ color: "#16263f", metalness: 0.6, roughness: 0.45 }),
  );
  body.position.y = 0.9;
  group.add(body);
  const edges = new THREE.LineBasicMaterial({ color: FLAT });
  const edgeLines = new THREE.LineSegments(new THREE.EdgesGeometry(body.geometry), edges);
  edgeLines.position.copy(body.position);
  group.add(edgeLines);
  // 正面的狀態燈條
  const panel = new THREE.MeshStandardMaterial({ color: FLAT, emissive: FLAT, emissiveIntensity: 1 });
  const strip = new THREE.Mesh(new THREE.BoxGeometry(1.3, 0.16, 0.05), panel);
  strip.position.set(0, 1.1, 0.82);
  group.add(strip);
  // 頂部信號燈
  const light = new THREE.MeshStandardMaterial({ color: FLAT, emissive: FLAT, emissiveIntensity: 1.4 });
  const beacon = new THREE.Mesh(new THREE.CylinderGeometry(0.22, 0.22, 0.3, 24), light);
  beacon.position.y = 1.75;
  group.add(beacon);
  return { group, light, panel, edges, x, z, colW };
}

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
      <div className="sc-row"><span className="muted">策略</span><span className="sc-name">{first.name}{first.shadow ? "（影子）" : ""}</span></div>
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

function StationCard({ item, width }: { item: ArenaItem; width: number }) {
  if (item.kind === "idle") return <IdleCard item={item} width={width} />;
  const { position: p, mark, pnl } = item;
  const margin = (p.qty * p.entry_price) / Math.max(1, p.leverage);
  const roe = margin > 0 ? pnl / margin : null;
  const cls = pnl > 0 ? "up" : pnl < 0 ? "down" : "";
  return (
    <Link to={chartLink(p.symbol, p.market)} className={`station-card ${cls}`} style={{ width }}
      title={`查看 ${p.symbol} 的 K 線與買賣點`}>
      <div className="sc-head">
        <b>{p.symbol}</b>
        <span className={`badge ${p.side}`}>{p.side === "long" ? "▲ 多" : "▼ 空"} {p.leverage}x</span>
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

export function TradeArena3D({ items: positions }: { items: ArenaItem[] }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const ctx = useRef<{
    renderer: THREE.WebGLRenderer;
    scene: THREE.Scene;
    camera: THREE.PerspectiveCamera;
    line: THREE.Group;
    belt: THREE.CanvasTexture;
    stations: Map<string, Station>;
    packets: Packet[];
  } | null>(null);
  const [width, setWidth] = useState(0);
  const [anchors, setAnchors] = useState<Record<string, { x: number; y: number }>>({});
  const [failed, setFailed] = useState(false);
  const lastMark = useRef<Record<string, number | null>>({});

  const n = positions.length;
  const held = positions.filter((i) => i.kind === "position").length;
  // 一排最多放幾站看寬度；鏡頭遠近也只看寬度，站少的時候不會被放大，而是置中排列
  const capacity = Math.max(1, Math.min(6, Math.floor(width / MIN_COL_PX)));
  const cols = Math.min(capacity, Math.max(1, n));
  const rows = Math.max(1, Math.ceil(n / cols));
  const colPx = width / capacity;
  const height = rows * ROW_PX;
  const keys = positions.map((i) => i.key).join("|");

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
    scene.add(new THREE.AmbientLight("#7fb0ff", 0.7));
    const key = new THREE.DirectionalLight("#ffffff", 1.6);
    key.position.set(6, 12, 10);
    scene.add(key);
    const grid = new THREE.GridHelper(200, 100, "#123a63", "#0c2240");
    scene.add(grid);
    const line = new THREE.Group();
    scene.add(line);
    ctx.current = { renderer, scene, camera, line, belt: beltTexture(), stations: new Map(), packets: [] };

    const ro = new ResizeObserver(([e]) => setWidth(Math.round(e.contentRect.width)));
    ro.observe(host);

    const clock = new THREE.Clock();
    let frame = 0;
    const animate = () => {
      frame = requestAnimationFrame(animate);
      const c = ctx.current!;
      const dt = Math.min(clock.getDelta(), 0.1);
      const t = clock.elapsedTime;
      if (!reducedMotion()) {
        c.belt.offset.x -= dt * 0.6;
        c.stations.forEach((s) => { s.light.emissiveIntensity = 1.1 + Math.sin(t * 2.4 + s.x) * 0.35; });
      }
      // 報價貨物：沿輸送帶滑進工作站，到了就消失
      c.packets = c.packets.filter((pk) => {
        pk.t += dt / 0.9;
        pk.mesh.position.x = pk.from + (pk.to - pk.from) * Math.min(1, pk.t);
        if (pk.t < 1) return true;
        line.remove(pk.mesh);
        disposeTree(pk.mesh);
        return false;
      });
      renderer.render(scene, camera);
    };
    animate();

    return () => {
      cancelAnimationFrame(frame);
      ro.disconnect();
      ctx.current?.belt.dispose();
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

    c.line.children.slice().forEach((o) => { c.line.remove(o); if (o.userData.disposable) disposeTree(o); });
    c.stations.forEach((s) => disposeTree(s.group));
    c.stations.clear();
    c.packets = [];

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
      const mat = new THREE.MeshStandardMaterial({ map: c.belt, metalness: 0.3, roughness: 0.6 });
      const belt = new THREE.Mesh(new THREE.BoxGeometry(len, 0.16, 1.1), mat);
      belt.position.set(left + len / 2, 0.08, z);
      belt.userData.disposable = true;
      c.line.add(belt);
      const rail = new THREE.Mesh(
        new THREE.BoxGeometry(len, 0.06, 0.06),
        new THREE.MeshBasicMaterial({ color: "#1fb6ff" }),
      );
      rail.position.set(left + len / 2, 0.18, z + 0.58);
      rail.userData.disposable = true;
      c.line.add(rail);
    });
    const next: Record<string, { x: number; y: number }> = {};
    positions.forEach((item, i) => {
      const r = Math.floor(i / cols);
      const z = rowZ[r];
      const inRow = Math.min(cols, n - r * cols); // 最後一排可能不滿，一樣置中
      const x = rowScale[r].toWorld(width / 2 + ((i % cols) - (inRow - 1) / 2) * colPx);
      const st = buildStation(x, z, rowScale[r].colW);
      c.line.add(st.group);
      c.stations.set(item.key, st);
      next[item.key] = toPx(new THREE.Vector3(x, STATION_TOP, z));
    });
    setAnchors(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keys, width, capacity, cols, rows, height]);

  // 損益變號 → 換燈號顏色；報價變動 → 送一個貨物進工作站
  useEffect(() => {
    const c = ctx.current;
    if (!c) return;
    const still = reducedMotion();
    positions.forEach((item) => {
      const st = c.stations.get(item.key);
      if (!st) return;
      const color = statusColor(item);
      [st.light, st.panel].forEach((m) => { m.color.copy(color); m.emissive.copy(color); });
      st.edges.color.copy(color).lerp(new THREE.Color("#ffffff"), 0.2);
      if (item.kind === "idle") return; // 待機的沒有即時報價
      const prev = lastMark.current[item.key];
      lastMark.current[item.key] = item.mark;
      if (still || prev === undefined || prev === null || item.mark === null || item.mark === prev || c.packets.length > 60) return;
      const tick = item.mark > prev ? UP : DOWN;
      const mesh = new THREE.Mesh(
        new THREE.BoxGeometry(0.34, 0.34, 0.34),
        new THREE.MeshStandardMaterial({ color: tick, emissive: tick, emissiveIntensity: 0.8 }),
      );
      const from = st.x - st.colW / 2 + 0.2;
      mesh.position.set(from, 0.36, st.z);
      c.line.add(mesh);
      c.packets.push({ mesh, from, to: st.x - 0.9, t: 0 });
    });
  }, [positions]);

  const cardWidth = Math.max(150, Math.min(200, colPx - 16));

  return (
    <div className="arena">
      <div className="arena-hud tl">
        <span className="hud-tag">PRODUCTION LINE</span>
        <span>{held} 個持倉 · {n - held} 個待機</span>
      </div>
      <div className="arena-stage" style={{ height }}>
        <div className="arena-canvas" ref={hostRef} role="img"
          aria-label={`持倉生產線：${held} 個持倉、${n - held} 個待機的工作站，燈號綠色代表獲利、紅色代表虧損、灰色代表待機`} />
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
        <span><i style={{ background: "#2bff88" }} />獲利</span>
        <span><i style={{ background: "#ff3d6e" }} />虧損</span>
        <span><i style={{ background: "#3d5a7a" }} />待機（策略等待進場）</span>
        <span>輸送帶上的方塊 = 每一筆報價跳動（綠漲紅跌） · 點卡片看 K 線</span>
      </div>
    </div>
  );
}
