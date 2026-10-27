import type {
  ISeriesPrimitive, ISeriesPrimitiveAxisView, ISeriesPrimitivePaneRenderer, ISeriesPrimitivePaneView,
  SeriesAttachedParameter, Time,
} from "lightweight-charts";
import type { CanvasRenderingTarget2D } from "fancy-canvas";
import type { Candle } from "../api/client";

/** 成交量分佈圖（Volume Profile）：沿價格軸統計成交量，算出 POC 與 Value Area（VAH / VAL）。 */

export interface VolumeProfile {
  /** 價格桶邊界，長度 rowSize + 1，由低到高 */
  bins: number[];
  volumes: number[];
  pocIdx: number;
  poc: number;
  /** Value Area 涵蓋的桶 [vaLow, vaHigh]（含兩端） */
  vaLow: number;
  vaHigh: number;
  vah: number;
  val: number;
}

/**
 * 每根 K 棒的成交量按 [低, 高] 跟各價格桶重疊的長度比例分配；高低相同（沒有波動）的 K 棒整根算進所在的桶。
 * Value Area 從 POC 往上下擴張，每次加入相鄰兩桶中量較大的一邊（平手取上方），直到累積量達 vaPct。
 */
export function calcVolumeProfile(candles: Candle[], rowSize = 60, vaPct = 0.7): VolumeProfile | null {
  if (candles.length === 0 || rowSize < 1) return null;
  let lo = Infinity, hi = -Infinity;
  for (const c of candles) {
    lo = Math.min(lo, c.low);
    hi = Math.max(hi, c.high);
  }
  if (!(hi > lo)) return null;
  const step = (hi - lo) / rowSize;
  const bins = Array.from({ length: rowSize + 1 }, (_, i) => lo + i * step);
  const volumes = new Array<number>(rowSize).fill(0);
  const binOf = (p: number) => Math.min(rowSize - 1, Math.max(0, Math.floor((p - lo) / step)));

  for (const c of candles) {
    const v = c.volume ?? 0;
    if (v <= 0) continue;
    const range = c.high - c.low;
    if (range <= 0) {
      volumes[binOf(c.close)] += v;
      continue;
    }
    for (let i = binOf(c.low); i <= binOf(c.high); i++) {
      const overlap = Math.min(c.high, bins[i + 1]) - Math.max(c.low, bins[i]);
      if (overlap > 0) volumes[i] += (v * overlap) / range;
    }
  }

  let pocIdx = 0;
  for (let i = 1; i < rowSize; i++) if (volumes[i] > volumes[pocIdx]) pocIdx = i;
  const total = volumes.reduce((a, b) => a + b, 0);
  if (total <= 0) return null;

  const target = total * vaPct;
  let acc = volumes[pocIdx];
  let down = pocIdx, up = pocIdx;
  while (acc < target && (down > 0 || up < rowSize - 1)) {
    const upVol = up < rowSize - 1 ? volumes[up + 1] : -1;
    const downVol = down > 0 ? volumes[down - 1] : -1;
    if (upVol >= downVol) acc += volumes[++up];
    else acc += volumes[--down];
  }

  return {
    bins, volumes, pocIdx,
    poc: (bins[pocIdx] + bins[pocIdx + 1]) / 2,
    vaLow: down, vaHigh: up,
    val: bins[down], vah: bins[up + 1],
  };
}

export interface VolumeProfileStyle {
  /** Value Area 內的桶 */
  inside: string;
  /** Value Area 外的桶 */
  outside: string;
  poc: string;
  va: string;
  /** 直方圖最長那條佔圖寬的比例 */
  widthRatio: number;
  showVahVal: boolean;
}

class Renderer implements ISeriesPrimitivePaneRenderer {
  constructor(private src: VolumeProfilePrimitive) {}

  draw(target: CanvasRenderingTarget2D) {
    const { profile: p, series, style: s } = this.src;
    if (!p || !series) return;
    target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      const y = (price: number) => {
        const c = series.priceToCoordinate(price);
        return c === null ? null : c * vr;
      };
      const max = Math.max(...p.volumes);
      const full = bitmapSize.width * s.widthRatio;
      const right = bitmapSize.width;
      for (let i = 0; i < p.volumes.length; i++) {
        const top = y(p.bins[i + 1]);
        const bottom = y(p.bins[i]);
        if (top === null || bottom === null || p.volumes[i] <= 0) continue;
        const gap = Math.max(1, Math.round(vr)); // 桶之間留 1px 縫
        const h = Math.max(1, bottom - top - gap);
        const w = (p.volumes[i] / max) * full;
        ctx.fillStyle = i >= p.vaLow && i <= p.vaHigh ? s.inside : s.outside;
        ctx.fillRect(right - w, top, w, h);
      }
      const hline = (price: number, color: string, dash: number[], width: number) => {
        const yy = y(price);
        if (yy === null) return;
        ctx.save();
        ctx.strokeStyle = color;
        ctx.lineWidth = Math.max(1, Math.round(width * vr));
        ctx.setLineDash(dash.map((d) => d * hr));
        ctx.beginPath();
        ctx.moveTo(0, Math.round(yy) + 0.5);
        ctx.lineTo(bitmapSize.width, Math.round(yy) + 0.5);
        ctx.stroke();
        ctx.restore();
      };
      if (s.showVahVal) {
        hline(p.vah, s.va, [4, 4], 1);
        hline(p.val, s.va, [4, 4], 1);
      }
      hline(p.poc, s.poc, [], 1.5);
    });
  }
}

class PaneView implements ISeriesPrimitivePaneView {
  private r: Renderer;
  constructor(src: VolumeProfilePrimitive) {
    this.r = new Renderer(src);
  }
  zOrder() {
    return "bottom" as const; // 畫在 K 棒後面
  }
  renderer() {
    return this.r;
  }
}

class AxisLabel implements ISeriesPrimitiveAxisView {
  constructor(private src: VolumeProfilePrimitive, private which: "poc" | "vah" | "val") {}
  private price() {
    return this.src.profile?.[this.which] ?? null;
  }
  visible() {
    return this.price() !== null && (this.which === "poc" || this.src.style.showVahVal);
  }
  coordinate() {
    const p = this.price();
    return p === null ? -100 : (this.src.series?.priceToCoordinate(p) ?? -100);
  }
  text() {
    const p = this.price();
    return p === null ? "" : `${this.which.toUpperCase()} ${this.src.formatPrice(p)}`;
  }
  textColor() {
    return "#ffffff";
  }
  backColor() {
    return this.which === "poc" ? this.src.style.poc : this.src.style.va;
  }
}

/** 掛在 K 線 series 上的直方圖：從價格軸往左長，跟著平移縮放重畫。 */
export class VolumeProfilePrimitive implements ISeriesPrimitive<Time> {
  profile: VolumeProfile | null = null;
  series: SeriesAttachedParameter<Time>["series"] | null = null;
  private requestUpdate: (() => void) | null = null;
  private views = [new PaneView(this)];
  private labels = [new AxisLabel(this, "poc"), new AxisLabel(this, "vah"), new AxisLabel(this, "val")];

  constructor(public style: VolumeProfileStyle, public formatPrice: (p: number) => string) {}

  attached(param: SeriesAttachedParameter<Time>) {
    this.series = param.series;
    this.requestUpdate = param.requestUpdate;
  }
  detached() {
    this.series = null;
    this.requestUpdate = null;
  }
  setProfile(p: VolumeProfile | null) {
    this.profile = p;
    this.requestUpdate?.();
  }
  paneViews() {
    return this.views;
  }
  priceAxisViews() {
    return this.labels;
  }
}
