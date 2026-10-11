/** The API sends ISO 8601 UTC timestamps; the console shows them in the operator's locale and zone. */

export interface DateTimeOptions {
  locale?: string;
  /** IANA zone; defaults to the operator's browser zone. */
  timeZone?: string;
}

function defaultLocale(): string {
  return typeof navigator !== "undefined" && navigator.language ? navigator.language : "en-US";
}

export function operatorTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

export function parseIsoInstant(iso: string): Date {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) throw new RangeError(`Invalid ISO timestamp: ${JSON.stringify(iso)}`);
  return date;
}

export interface FormattedDateTime {
  /** Date and time, e.g. "Oct 10, 2026, 4:30:00 PM". */
  text: string;
  /** Short zone label, e.g. "GMT-3" or "UTC". */
  zone: string;
}

export function formatDateTime(iso: string, options: DateTimeOptions = {}): FormattedDateTime {
  const date = parseIsoInstant(iso);
  const formatter = new Intl.DateTimeFormat(options.locale ?? defaultLocale(), {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    timeZone: options.timeZone ?? operatorTimeZone(),
    timeZoneName: "short",
  });
  const parts = formatter.formatToParts(date);
  const zone = parts.find((part) => part.type === "timeZoneName")?.value ?? "";
  const text = parts
    .filter((part) => part.type !== "timeZoneName")
    .map((part) => part.value)
    .join("")
    .trim();
  return { text, zone };
}

const UNITS: ReadonlyArray<{ unit: Intl.RelativeTimeFormatUnit; seconds: number }> = [
  { unit: "year", seconds: 31_536_000 },
  { unit: "month", seconds: 2_592_000 },
  { unit: "week", seconds: 604_800 },
  { unit: "day", seconds: 86_400 },
  { unit: "hour", seconds: 3_600 },
  { unit: "minute", seconds: 60 },
];

/** "3 minutes ago", "in 2 days", "just now" (under 45 seconds either way). */
export function formatRelative(iso: string, now: Date = new Date(), locale: string = defaultLocale()): string {
  const deltaSeconds = Math.round((parseIsoInstant(iso).getTime() - now.getTime()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  const magnitude = Math.abs(deltaSeconds);
  if (magnitude < 45) return formatter.format(0, "second");
  for (const { unit, seconds } of UNITS) {
    if (magnitude >= seconds) return formatter.format(Math.trunc(deltaSeconds / seconds), unit);
  }
  return formatter.format(Math.trunc(deltaSeconds / 60), "minute");
}
