import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { applyTheme, initTheme, isThemePreference, readStoredTheme, setTheme, THEME_STORAGE_KEY } from "./theme";

describe("theme", () => {
  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
  });
  afterEach(() => vi.restoreAllMocks());

  it("persists an explicit choice and applies it to <html>", () => {
    setTheme("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");

    setTheme("light");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("'system' clears the override and the stored value", () => {
    setTheme("dark");
    setTheme("system");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("restores the stored choice on init", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "dark");
    initTheme();
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("ignores unknown stored values", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "solarized");
    expect(readStoredTheme()).toBe("system");
    expect(isThemePreference("solarized")).toBe(false);
  });

  it("still applies the choice when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    expect(readStoredTheme()).toBe("system");
    expect(() => setTheme("dark")).not.toThrow();
    expect(document.documentElement.dataset.theme).toBe("dark");
    applyTheme("light");
    expect(document.documentElement.dataset.theme).toBe("light");
  });
});
