import axe from "axe-core";
import { expect } from "vitest";

/**
 * Fails when axe-core finds any violation in `container`. jsdom has no layout engine, so the
 * colour-contrast rule cannot run here; contrast is covered by tokens.test.ts instead.
 */
export async function expectNoAxeViolations(container: Element = document.body): Promise<void> {
  const results = await axe.run(container, {
    rules: { "color-contrast": { enabled: false } },
    resultTypes: ["violations"],
  });
  const summary = results.violations.map((violation) => ({
    rule: violation.id,
    impact: violation.impact,
    help: violation.help,
    nodes: violation.nodes.map((node) => node.html).slice(0, 3),
  }));
  expect(summary, JSON.stringify(summary, null, 2)).toEqual([]);
}
