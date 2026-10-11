import { useCallback, useSyncExternalStore } from "react";

export type ThemePreference = "light" | "dark" | "system";

export const THEME_STORAGE_KEY = "fh.theme";

const PREFERENCES: readonly ThemePreference[] = ["light", "dark", "system"];
const listeners = new Set<() => void>();

export function isThemePreference(value: unknown): value is ThemePreference {
  return PREFERENCES.some((preference) => preference === value);
}

export function readStoredTheme(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isThemePreference(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

/** Applies the preference to <html>: "system" removes the override so prefers-color-scheme wins. */
export function applyTheme(preference: ThemePreference): void {
  const root = document.documentElement;
  if (preference === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", preference);
}

export function setTheme(preference: ThemePreference): void {
  try {
    if (preference === "system") window.localStorage.removeItem(THEME_STORAGE_KEY);
    else window.localStorage.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    // Storage can be blocked (private windows, site-data settings): the choice still applies to this page.
  }
  applyTheme(preference);
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function readApplied(): ThemePreference {
  const attribute = document.documentElement.getAttribute("data-theme");
  return isThemePreference(attribute) ? attribute : "system";
}

export function useTheme(): {
  theme: ThemePreference;
  setTheme: (preference: ThemePreference) => void;
} {
  const theme = useSyncExternalStore(subscribe, readApplied, () => "system" as const);
  const update = useCallback((preference: ThemePreference) => setTheme(preference), []);
  return { theme, setTheme: update };
}

/** Called once before first render (the inline script in index.html does the same to avoid a flash). */
export function initTheme(): void {
  applyTheme(readStoredTheme());
}
