import { randomBytes } from "node:crypto";

/** A short id unique per call (and per run), so journeys never collide on shared data. */
export function uniqueId(): string {
  return `${Date.now().toString(36)}${randomBytes(3).toString("hex")}`;
}

/** Human-readable unique name, e.g. `E2E key 1k3j9a0f2b`. */
export function uniqueName(label: string): string {
  return `E2E ${label} ${uniqueId()}`;
}

/** A valid project slug (lowercase letters, digits, hyphens) unique per call. */
export function uniqueSlug(label = "e2e"): string {
  return `${label}-${uniqueId()}`;
}
