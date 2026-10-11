import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

/*
 * axe-core cannot evaluate colour contrast in jsdom (no layout), so the design tokens are checked here
 * against WCAG 2.2 AA directly from src/index.css: 4.5:1 for text, 3:1 for UI boundaries and focus rings.
 */
const css = readFileSync(resolve(__dirname, "..", "index.css"), "utf8");

type Mode = "light" | "dark";

function token(name: string, mode: Mode): string {
  const pattern = new RegExp(`--color-${name}:\\s*([^;]+);`);
  const raw = pattern.exec(css)?.[1]?.trim();
  if (!raw) throw new Error(`Missing token --color-${name}`);
  const pair = /^light-dark\(\s*(.+?)\s*,\s*(.+?)\s*\)$/.exec(raw);
  return pair ? (mode === "light" ? pair[1] : pair[2]) ?? raw : raw;
}

function channels(color: string): [number, number, number] {
  const hex = /^#([0-9a-f]{6})$/i.exec(color)?.[1];
  if (!hex) throw new Error(`Only 6-digit hex tokens are supported here: ${color}`);
  return [0, 2, 4].map((at) => parseInt(hex.slice(at, at + 2), 16) / 255) as [number, number, number];
}

function luminance(color: string): number {
  const [r, g, b] = channels(color).map((value) =>
    value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4,
  ) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

const TEXT_PAIRS: ReadonlyArray<[string, string]> = [
  ["ink", "canvas"],
  ["ink", "surface"],
  ["ink", "surface-sunken"],
  ["ink-muted", "canvas"],
  ["ink-muted", "surface"],
  ["ink-muted", "surface-sunken"],
  ["ink-faint", "surface"],
  ["ink-faint", "canvas"],
  ["primary-ink", "primary"],
  ["primary-ink", "primary-hover"],
  ["primary", "surface"],
  ["primary", "canvas"],
  ["primary", "primary-soft"],
  ["primary-hover", "surface"],
  ["chrome-ink", "chrome"],
  ["chrome-ink", "chrome-raised"],
  ["chrome-muted", "chrome"],
  ["chrome-muted", "chrome-raised"],
  ["danger", "surface"],
  ["danger", "danger-soft"],
  ["warning", "surface"],
  ["warning", "warning-soft"],
  ["success", "surface"],
  ["success", "success-soft"],
  ["info", "surface"],
  ["info", "info-soft"],
  ["scope-implemented", "scope-implemented-soft"],
  ["scope-sandbox", "scope-sandbox-soft"],
  ["scope-simulated", "scope-simulated-soft"],
  ["scope-planned", "scope-planned-soft"],
  ["scope-implemented", "surface"],
  ["scope-sandbox", "surface"],
  ["scope-simulated", "surface"],
  ["scope-planned", "surface"],
  ["chrome-brass", "chrome-raised"],
  ["chrome-brass", "chrome"],
];

const UI_PAIRS: ReadonlyArray<[string, string]> = [
  ["ring", "canvas"],
  ["ring", "surface"],
  ["control", "surface"],
  ["control", "canvas"],
  ["brass", "surface"],
];

describe.each<Mode>(["light", "dark"])("design tokens (%s)", (mode) => {
  it.each(TEXT_PAIRS)("%s on %s meets 4.5:1", (foreground, background) => {
    expect(contrast(token(foreground, mode), token(background, mode))).toBeGreaterThanOrEqual(4.5);
  });

  it.each(UI_PAIRS)("%s against %s meets 3:1", (foreground, background) => {
    expect(contrast(token(foreground, mode), token(background, mode))).toBeGreaterThanOrEqual(3);
  });
});
