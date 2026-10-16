/** The marker the backend writes in place of personal data; it is never shown to people (see audit-activity.ts). */
const REDACTED = "[redacted]";

/** A copy of `value` with every redacted marker replaced by `hidden` (a localized word), so the JSON view never shows the raw marker. */
export function scrubRedacted(value: unknown, hidden: string): unknown {
  if (typeof value === "string") return value.trim().toLowerCase() === REDACTED ? hidden : value;
  if (Array.isArray(value)) return value.map((item) => scrubRedacted(item, hidden));
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, scrubRedacted(item, hidden)]));
  }
  return value;
}

export function metadataJson(value: unknown, hidden: string): string {
  return JSON.stringify(scrubRedacted(value, hidden), null, 2);
}

export function isEmptyObject(value: unknown): boolean {
  return typeof value !== "object" || value === null || Object.keys(value).length === 0;
}
