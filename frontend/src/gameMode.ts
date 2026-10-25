import { useSyncExternalStore } from "react";

/**
 * 遊戲模式：換成霓虹 HUD 外觀，總覽頁多一個 3D 持倉場景。
 * 只是外觀偏好，存在瀏覽器裡；開關時在 <html> 加上 data-game="on"，CSS 依此換主題。
 */
const KEY = "tradebot.game";
const listeners = new Set<() => void>();

const read = () => {
  try { return localStorage.getItem(KEY) === "on"; } catch { return false; }
};

let on = read();
const apply = () => {
  if (on) document.documentElement.dataset.game = "on";
  else delete document.documentElement.dataset.game;
};
apply();

export function setGameMode(next: boolean) {
  on = next;
  try { localStorage.setItem(KEY, next ? "on" : "off"); } catch { /* 無痕模式等：只在這次有效 */ }
  apply();
  listeners.forEach((l) => l());
}

export const useGameMode = () =>
  useSyncExternalStore((l) => { listeners.add(l); return () => listeners.delete(l); }, () => on);
