// the companion window's own controls, guarded: outside tauri (the vite
// dev page in a browser tab) every call is a quiet no-op, so the window
// renders and the clock works there too. positions are LOGICAL pixels -
// the stored per-form places must survive a monitor change of scale.

import {
  currentMonitor,
  getCurrentWindow,
  LogicalPosition,
  LogicalSize,
} from "@tauri-apps/api/window";
import type { Point, Rect, Size } from "./companion";

export function inTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/** hide, never quit: the clock is the sidecar's, and the tray brings her
 * back */
export async function hideWindow(): Promise<void> {
  if (!inTauri()) return;
  await getCurrentWindow().hide();
}

export async function setAlwaysOnTop(on: boolean): Promise<void> {
  if (!inTauri()) return;
  await getCurrentWindow().setAlwaysOnTop(on);
}

/** null outside tauri (nothing to ask) */
export async function isAlwaysOnTop(): Promise<boolean | null> {
  if (!inTauri()) return null;
  return getCurrentWindow().isAlwaysOnTop();
}

export async function resizeWindow(size: Size): Promise<void> {
  if (!inTauri()) return;
  await getCurrentWindow().setSize(new LogicalSize(size.width, size.height));
}

export async function moveWindow(point: Point): Promise<void> {
  if (!inTauri()) return;
  await getCurrentWindow().setPosition(new LogicalPosition(point.x, point.y));
}

/** the window's top-left in logical pixels; null outside tauri */
export async function windowPosition(): Promise<Point | null> {
  if (!inTauri()) return null;
  const win = getCurrentWindow();
  const [physical, scale] = await Promise.all([
    win.outerPosition(),
    win.scaleFactor(),
  ]);
  const logical = physical.toLogical(scale);
  return { x: Math.round(logical.x), y: Math.round(logical.y) };
}

/** the work area (screen minus menu bar / dock / taskbar) of the monitor
 * the window is on, in logical pixels; null outside tauri or when the
 * monitor can't be told */
export async function workArea(): Promise<Rect | null> {
  if (!inTauri()) return null;
  const monitor = await currentMonitor();
  if (!monitor) return null;
  const pos = monitor.workArea.position.toLogical(monitor.scaleFactor);
  const size = monitor.workArea.size.toLogical(monitor.scaleFactor);
  return {
    x: Math.round(pos.x),
    y: Math.round(pos.y),
    width: Math.round(size.width),
    height: Math.round(size.height),
  };
}
