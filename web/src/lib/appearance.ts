// The look (Classic, Warm: the old page's two, same storage key) and light/dark, kept in this browser only.
import { useSyncExternalStore } from "react";

export type Look = "classic" | "warm";
export type Mode = "system" | "light" | "dark";

const LOOK_KEY = "himari.appearance.theme"; // shared with the old page (appearance.js)
const MODE_KEY = "himari.appearance.mode";
const dark = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;

function read<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const value = window.localStorage.getItem(key);
    return (allowed as readonly string[]).includes(value ?? "") ? (value as T) : fallback;
  } catch {
    return fallback; // storage may be off: the choice holds for this page only
  }
}

let look: Look = read(LOOK_KEY, ["classic", "warm"], "classic");
let mode: Mode = read(MODE_KEY, ["system", "light", "dark"], "system");
const listeners = new Set<() => void>();

export function applyAppearance() {
  const root = document.documentElement;
  root.dataset.look = look;
  root.dataset.mode = mode === "system" ? (dark() ? "dark" : "light") : mode;
}

function changed() {
  applyAppearance();
  for (const listener of listeners) listener();
}

export function setLook(value: Look) {
  look = value;
  try { window.localStorage.setItem(LOOK_KEY, value); } catch { /* this page only */ }
  changed();
}

export function setMode(value: Mode) {
  mode = value;
  try { window.localStorage.setItem(MODE_KEY, value); } catch { /* this page only */ }
  changed();
}

export function watchAppearance() {
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener("change", () => mode === "system" && changed());
  window.addEventListener("storage", (event) => {
    if (event.key !== LOOK_KEY && event.key !== MODE_KEY && event.key !== null) return;
    look = read(LOOK_KEY, ["classic", "warm"], "classic");
    mode = read(MODE_KEY, ["system", "light", "dark"], "system");
    changed();
  });
}

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};

export const useAppearance = () =>
  useSyncExternalStore(subscribe, () => `${look}:${mode}`).split(":") as [Look, Mode];
