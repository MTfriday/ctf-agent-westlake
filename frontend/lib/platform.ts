/**
 * 平台 / 快捷键检测。
 *
 * muteki 原版快捷键文案是 macOS 专属（⌘↵、⌘K）。这里统一做平台检测：
 * macOS → ⌘，Windows / Linux → Ctrl，让提示在三种平台都正确。
 */

export const IS_MAC: boolean =
  typeof navigator !== "undefined" &&
  /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent || "");

/** 主修饰键：⌘（macOS）或 Ctrl（Windows / Linux）。 */
export const MOD_KEY: string = IS_MAC ? "⌘" : "Ctrl";

/** 派发快捷键：⌘↵（macOS）或 Ctrl+↵（其他）。 */
export const DISPATCH_SHORTCUT: string = IS_MAC ? "⌘↵" : "Ctrl+↵";

/** 命令面板快捷键：⌘K（macOS）或 Ctrl+K（其他）。 */
export const PALETTE_SHORTCUT: string = IS_MAC ? "⌘K" : "Ctrl+K";
