import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";

import { setLocale } from "./lib/locale";

// Screens are lazy chunks: the first import of each is transformed on demand, which outlasts the 1s default.
configure({ asyncUtilTimeout: 5000 });

// vitest runs without globals, so testing-library cannot register its own cleanup.
afterEach(() => cleanup());

// The product default is pt-BR; most suites assert the English copy and pin it here. src/i18n/i18n.test.tsx covers
// the pt-BR default, the switch and persistence.
beforeEach(() => setLocale("en-US"));

// jsdom lacks a few browser APIs that Radix primitives rely on.
if (typeof window !== "undefined") {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  window.ResizeObserver ??= ResizeObserverStub;
  window.HTMLElement.prototype.hasPointerCapture ??= () => false;
  window.HTMLElement.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.releasePointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
  window.matchMedia ??= ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
}
