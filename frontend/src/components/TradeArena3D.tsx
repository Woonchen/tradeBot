import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import type { EquityPoint, Position } from "../api/client";
import { fmtNum, fmtPrice, fmtSigned } from "../format";

/** 場景裡每座塔對應的持倉（已用即時價格算好損益） */
export interface ArenaPosition { key: string; position: Position; mark: number | null; pnl: number }

interface Props {
  positions: ArenaPosition[];
  equity: EquityPoint[];
  /** 今日權益變化：決定中央能量核心的顏色 */
  todayChange: number | null | undefined;
}

const UP = new THREE.Color("#2bff88");
const DOWN = new THREE.Color("#ff3d6e");
const FLAT = new THREE.Color("#5fd4ff");
const SPACING = 3.2;
const MAX_HEIGHT = 7;

const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** 塔頂的文字看板（canvas 畫字當貼圖） */
function makeLabel(title: string, value: string, color: THREE.Color) {
  const canvas = document.createElement("canvas");
  canvas.width = 512;
  canvas.height = 192;
  const ctx = canvas.getContext("2d")!;
  const hex = `#${color.getHexString()}`;
  ctx.fillStyle = "rgba(4, 10, 24, 0.78)";
  ctx.strokeStyle = hex;
  ctx.lineWidth = 4;
  ctx.beginPath();
  ctx.roundRect(6, 6, 500, 180, 18);
  ctx.fill();
  ctx.stroke();
  ctx.textAlign = "center";
  ctx.fillStyle = "#e8f6ff";
  ctx.font = "600 52px ui-monospace, Menlo, monospace";
  ctx.fillText(title, 256, 78);
  ctx.fillStyle = hex;
  ctx.font = "700 60px ui-monospace, Menlo, monospace";
  ctx.fillText(value, 256, 152);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false }));
  sprite.scale.set(3.2, 1.2, 1);
  return sprite;
}

function disposeTree(obj: THREE.Object3D) {
  obj.traverse((o) => {
    const m = o as THREE.Mesh;
    m.geometry?.dispose();
    const mats = Array.isArray(m.material) ? m.material : m.material ? [m.material] : [];
    mats.forEach((mat) => {
      (mat as THREE.SpriteMaterial).map?.dispose();
      mat.dispose();
    });
  });
}

/** 依持倉建塔：高度 = 損益佔最大損益的比例，底面積 = 名目價值，綠漲紅跌 */
function buildTowers(items: ArenaPosition[]) {
  const group = new THREE.Group();
  const maxPnl = Math.max(1e-9, ...items.map((i) => Math.abs(i.pnl)));
  const maxNotional = Math.max(1e-9, ...items.map((i) => Math.abs(i.position.qty * i.position.entry_price)));
  const cols = Math.ceil(Math.sqrt(items.length));
  items.forEach((item, idx) => {
    const { position: p, pnl } = item;
    const color = pnl > 0 ? UP : pnl < 0 ? DOWN : FLAT;
    const height = 0.6 + (Math.abs(pnl) / maxPnl) * (MAX_HEIGHT - 0.6);
    const width = 0.7 + Math.sqrt(Math.abs(p.qty * p.entry_price) / maxNotional) * 1.1;
    const row = Math.floor(idx / cols);
    const col = idx % cols;
    const x = (col - (cols - 1) / 2) * SPACING;
    const z = (row - (Math.ceil(items.length / cols) - 1) / 2) * SPACING;

    const tower = new THREE.Group();
    tower.position.set(x, 0, z);
    tower.userData = { key: item.key, height, baseY: 0 };

    const body = new THREE.Mesh(
      new THREE.BoxGeometry(width, height, width),
      new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: 0.35, metalness: 0.4, roughness: 0.25, transparent: true, opacity: 0.82 }),
    );
    body.position.y = height / 2;
    body.userData.key = item.key;
    tower.add(body);

    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(body.geometry),
      new THREE.LineBasicMaterial({ color: color.clone().lerp(new THREE.Color("#ffffff"), 0.4) }),
    );
    edges.position.copy(body.position);
    tower.add(edges);

    // 地面光環：多單實心環、空單虛線感的雙環，不只靠顏色區分方向
    const ring = new THREE.Mesh(
      new THREE.RingGeometry(width * 0.8, width * 0.95, 48),
      new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.55, side: THREE.DoubleSide }),
    );
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.02;
    tower.add(ring);
    if (p.side === "short") {
      const ring2 = new THREE.Mesh(
        new THREE.RingGeometry(width * 1.05, width * 1.12, 48),
        new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.4, side: THREE.DoubleSide }),
      );
      ring2.rotation.x = -Math.PI / 2;
      ring2.position.y = 0.02;
      tower.add(ring2);
    }

    const label = makeLabel(`${p.symbol} ${p.side === "long" ? "▲多" : "▼空"}`, fmtSigned(pnl), color);
    label.position.y = height + 1;
    label.userData.float = true;
    tower.add(label);

    group.add(tower);
  });
  return group;
}

/** 近 7 天權益曲線：場景後方一條發光的 3D 光帶 */
function buildEquityRibbon(points: EquityPoint[], span: number) {
  const group = new THREE.Group();
  const vals = points.map((p) => p.total_usdt).filter((v) => Number.isFinite(v));
  if (vals.length < 2) return group;
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const range = max - min || 1;
  const width = Math.max(14, span + 8);
  const step = Math.max(1, Math.floor(vals.length / 160));
  const pts: THREE.Vector3[] = [];
  for (let i = 0; i < vals.length; i += step) {
    const t = i / (vals.length - 1);
    pts.push(new THREE.Vector3(-width / 2 + t * width, 1.5 + ((vals[i] - min) / range) * 5, -span / 2 - 6));
  }
  const curve = new THREE.CatmullRomCurve3(pts);
  const rising = vals[vals.length - 1] >= vals[0];
  const color = rising ? UP : DOWN;
  group.add(new THREE.Mesh(
    new THREE.TubeGeometry(curve, Math.min(400, pts.length * 3), 0.09, 8, false),
    new THREE.MeshBasicMaterial({ color }),
  ));
  // 光帶下方的半透明「牆」，讓曲線有體積感
  const wallPos: number[] = [];
  const samples = curve.getPoints(200);
  samples.forEach((v, i) => {
    if (i === 0) return;
    const a = samples[i - 1];
    wallPos.push(a.x, 0, a.z, v.x, 0, v.z, v.x, v.y, v.z, a.x, 0, a.z, v.x, v.y, v.z, a.x, a.y, a.z);
  });
  const wallGeo = new THREE.BufferGeometry();
  wallGeo.setAttribute("position", new THREE.Float32BufferAttribute(wallPos, 3));
  group.add(new THREE.Mesh(wallGeo, new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.08, side: THREE.DoubleSide, depthWrite: false })));
  return group;
}

export function TradeArena3D({ positions, equity, todayChange }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<{
    scene: THREE.Scene;
    towers: THREE.Group;
    ribbon: THREE.Group;
    core: THREE.Mesh;
    pickables: THREE.Object3D[];
  } | null>(null);
  const [hover, setHover] = useState<{ key: string; x: number; y: number } | null>(null);
  const [failed, setFailed] = useState(false);
  const itemsRef = useRef(positions);
  itemsRef.current = positions;

  // 建立 renderer / 相機 / 燈光，只做一次
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
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    host.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2("#050a18", 0.028);
    const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 200);
    camera.position.set(11, 9, 13);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.maxPolarAngle = Math.PI * 0.47;
    controls.minDistance = 6;
    controls.maxDistance = 40;
    controls.target.set(0, 2, 0);
    controls.autoRotate = !reducedMotion();
    controls.autoRotateSpeed = 0.6;
    // 使用者拖曳過就停止自動旋轉，雙擊恢復
    controls.addEventListener("start", () => { controls.autoRotate = false; });
    const onDbl = () => { controls.autoRotate = !reducedMotion(); };
    renderer.domElement.addEventListener("dblclick", onDbl);

    scene.add(new THREE.AmbientLight("#6fa8ff", 0.5));
    const key = new THREE.DirectionalLight("#ffffff", 1.4);
    key.position.set(8, 14, 6);
    scene.add(key);
    const rim = new THREE.PointLight("#b45cff", 40, 40);
    rim.position.set(-10, 6, -8);
    scene.add(rim);

    // 地面網格 + 外圈光環
    const grid = new THREE.GridHelper(80, 80, "#1fb6ff", "#0f2a4a");
    (grid.material as THREE.Material).transparent = true;
    (grid.material as THREE.Material).opacity = 0.55;
    scene.add(grid);
    const floorRing = new THREE.Mesh(
      new THREE.RingGeometry(15, 15.15, 128),
      new THREE.MeshBasicMaterial({ color: "#1fb6ff", transparent: true, opacity: 0.5, side: THREE.DoubleSide }),
    );
    floorRing.rotation.x = -Math.PI / 2;
    floorRing.position.y = 0.01;
    scene.add(floorRing);

    // 背景星塵
    const starGeo = new THREE.BufferGeometry();
    const starPos = new Float32Array(900 * 3);
    for (let i = 0; i < 900; i++) {
      const r = 40 + Math.random() * 50;
      const th = Math.random() * Math.PI * 2;
      const ph = Math.random() * Math.PI * 0.5;
      starPos.set([r * Math.cos(th) * Math.sin(ph), r * Math.cos(ph) * 0.6 + 4, r * Math.sin(th) * Math.sin(ph)], i * 3);
    }
    starGeo.setAttribute("position", new THREE.BufferAttribute(starPos, 3));
    const stars = new THREE.Points(starGeo, new THREE.PointsMaterial({ color: "#9fdcff", size: 0.25, transparent: true, opacity: 0.8, fog: false }));
    scene.add(stars);

    // 中央能量核心：今日賺錢綠、賠錢紅，持倉圍繞它排列
    const core = new THREE.Mesh(
      new THREE.IcosahedronGeometry(0.5, 1),
      new THREE.MeshStandardMaterial({ color: FLAT, emissive: FLAT, emissiveIntensity: 1.2, wireframe: true }),
    );
    core.position.y = 0.6;
    scene.add(core);

    const towers = new THREE.Group();
    const ribbon = new THREE.Group();
    scene.add(towers, ribbon);
    sceneRef.current = { scene, towers, ribbon, core, pickables: [] };

    const resize = () => {
      const w = host.clientWidth;
      const h = host.clientHeight;
      renderer.setSize(w, h, false);
      renderer.domElement.style.width = "100%";
      renderer.domElement.style.height = "100%";
      camera.aspect = w / Math.max(1, h);
      camera.updateProjectionMatrix();
    };
    const ro = new ResizeObserver(resize);
    ro.observe(host);
    resize();

    // 滑鼠移到塔上顯示詳細資料
    const raycaster = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    const onMove = (e: PointerEvent) => {
      const rect = renderer.domElement.getBoundingClientRect();
      ndc.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
      raycaster.setFromCamera(ndc, camera);
      const hit = raycaster.intersectObjects(sceneRef.current?.pickables ?? [], false)[0];
      const k = hit?.object.userData.key as string | undefined;
      setHover(k ? { key: k, x: e.clientX - rect.left, y: e.clientY - rect.top } : null);
      renderer.domElement.style.cursor = k ? "pointer" : "grab";
    };
    const onLeave = () => setHover(null);
    renderer.domElement.addEventListener("pointermove", onMove);
    renderer.domElement.addEventListener("pointerleave", onLeave);

    const clock = new THREE.Clock();
    let frame = 0;
    const animate = () => {
      frame = requestAnimationFrame(animate);
      const t = clock.getElapsedTime();
      const still = reducedMotion();
      if (!still) {
        core.rotation.y = t * 0.8;
        core.rotation.x = t * 0.3;
        core.scale.setScalar(1 + Math.sin(t * 2) * 0.08);
        stars.rotation.y = t * 0.01;
        towers.children.forEach((tower, i) => {
          tower.children.forEach((c) => {
            if (c.userData.float) c.position.y = tower.userData.height + 1 + Math.sin(t * 1.5 + i) * 0.12;
          });
        });
      }
      controls.update();
      renderer.render(scene, camera);
    };
    animate();

    return () => {
      cancelAnimationFrame(frame);
      ro.disconnect();
      controls.dispose();
      renderer.domElement.removeEventListener("pointermove", onMove);
      renderer.domElement.removeEventListener("pointerleave", onLeave);
      renderer.domElement.removeEventListener("dblclick", onDbl);
      disposeTree(scene);
      renderer.dispose();
      renderer.domElement.remove();
      sceneRef.current = null;
    };
  }, []);

  // 持倉 / 損益變動：重建塔群
  useEffect(() => {
    const s = sceneRef.current;
    if (!s) return;
    s.towers.children.slice().forEach((c) => { s.towers.remove(c); disposeTree(c); });
    const built = buildTowers(positions);
    built.children.slice().forEach((c) => s.towers.add(c));
    s.pickables = [];
    s.towers.traverse((o) => { if (o instanceof THREE.Mesh && o.userData.key) s.pickables.push(o); });
  }, [positions]);

  useEffect(() => {
    const s = sceneRef.current;
    if (!s) return;
    s.ribbon.children.slice().forEach((c) => { s.ribbon.remove(c); disposeTree(c); });
    const cols = Math.ceil(Math.sqrt(Math.max(1, positions.length)));
    buildEquityRibbon(equity, cols * SPACING).children.slice().forEach((c) => s.ribbon.add(c));
  }, [equity, positions.length]);

  useEffect(() => {
    const s = sceneRef.current;
    if (!s) return;
    const c = todayChange ? (todayChange > 0 ? UP : DOWN) : FLAT;
    const mat = s.core.material as THREE.MeshStandardMaterial;
    mat.color.copy(c);
    mat.emissive.copy(c);
  }, [todayChange]);

  const hovered = hover ? itemsRef.current.find((i) => i.key === hover.key) : undefined;

  return (
    <div className="arena">
      <div className="arena-canvas" ref={hostRef} role="img"
        aria-label={`3D 持倉場景：${positions.length} 個持倉，塔越高代表損益越大，綠色獲利、紅色虧損；後方光帶是近 7 天總權益`} />
      <div className="arena-hud tl">
        <span className="hud-tag">TRADE ARENA</span>
        <span>{positions.length} 個持倉</span>
      </div>
      <div className="arena-hud br">拖曳旋轉 · 滾輪縮放 · 雙擊恢復自動旋轉</div>
      <div className="arena-legend">
        <span><i style={{ background: "#2bff88" }} />獲利</span>
        <span><i style={{ background: "#ff3d6e" }} />虧損</span>
        <span>塔高 = 損益大小 · 粗細 = 倉位價值 · 雙光環 = 空單</span>
      </div>
      {failed && <div className="arena-empty">這個瀏覽器不支援 WebGL，無法顯示 3D 場景</div>}
      {!failed && positions.length === 0 && <div className="arena-empty">目前沒有持倉 · 等待策略進場…</div>}
      {hovered && hover && (
        <div className="arena-tip" style={{ left: hover.x, top: hover.y }}>
          <b>{hovered.position.symbol}</b> <span className="muted">{hovered.position.market} · {hovered.position.side === "long" ? "多單" : "空單"} {hovered.position.leverage}x</span>
          <div>進場 {fmtPrice(hovered.position.entry_price)} → 現價 {fmtPrice(hovered.mark)}</div>
          <div>數量 {fmtNum(hovered.position.qty, 4)}</div>
          <div className={hovered.pnl > 0 ? "up" : hovered.pnl < 0 ? "down" : ""}>未實現 {fmtSigned(hovered.pnl)} USDT</div>
          {hovered.position.owner && <div className="muted">來源：{hovered.position.owner}</div>}
        </div>
      )}
    </div>
  );
}
